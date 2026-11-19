import asyncio
import os
from datetime import timedelta
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker
from temporalio.client import WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.common import PinnedVersioningOverride, WorkerDeploymentVersion
from temporalio.service import RPCError, RPCStatusCode

from cpfc_trip.api import create_app
from cpfc_trip.config import Settings
from cpfc_trip.domain import SessionInput
from cpfc_trip.persistence.database import engine
from cpfc_trip.persistence.models import RateLimitHit
from cpfc_trip.persistence.rate_limit import Hit, SharedRateLimiter
from cpfc_trip.persistence.repository import Repository

NOW = 6_000_042.5
TOKEN = "x" * 64


async def submit(
    client: AsyncClient,
    settings: Settings,
    session_input: SessionInput,
    submission_id: str | None = None,
    headers: dict[str, str] | None = None,
):
    return await client.post(
        "/api/sessions",
        json={
            "submission_id": submission_id or str(uuid4()),
            "brief": session_input.brief.model_dump(mode="json"),
        },
        headers={
            "Origin": settings.frontend_origin,
            "X-Submission-Token": TOKEN,
            **(headers or {}),
        },
    )


def shared_app(settings: Settings, repository: Repository, **limits: int):
    configured = settings.model_copy(update=limits)
    limiter = SharedRateLimiter(repository.sessions, clock=lambda: NOW)
    temporal = AsyncMock()
    return configured, temporal, create_app(configured, repository, temporal, limiter)


def client_for(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_per_ip_limit_returns_429_until_the_window_ends(
    settings: Settings, repository: Repository, session_input: SessionInput
) -> None:
    configured, temporal, app = shared_app(settings, repository, session_create_per_minute=2)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await submit(client, configured, session_input)).status_code == 202
        assert (await submit(client, configured, session_input)).status_code == 202
        limited = await submit(client, configured, session_input)
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "18"
    assert temporal.start_workflow.await_count == 2


async def test_global_admission_cap_returns_honest_503(
    settings: Settings, repository: Repository, session_input: SessionInput
) -> None:
    configured, temporal, app = shared_app(settings, repository, session_create_per_10_minutes=1)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await submit(client, configured, session_input)).status_code == 202
        busy = await submit(client, configured, session_input)
    assert busy.status_code == 503
    assert busy.json()["detail"] == (
        "Trip planning is busy right now. Please try again in a few minutes."
    )
    assert busy.headers["retry-after"] == "558"
    temporal.start_workflow.assert_awaited_once()


async def test_retrying_the_same_submission_is_not_charged(
    settings: Settings, repository: Repository, session_input: SessionInput
) -> None:
    configured, temporal, app = shared_app(settings, repository, session_create_per_minute=1)
    submission_id = str(uuid4())
    async with app.router.lifespan_context(app), client_for(app) as client:
        first = await submit(client, configured, session_input, submission_id)
        retry = await submit(client, configured, session_input, submission_id)
        other = await submit(client, configured, session_input)
    assert first.status_code == retry.status_code == 202
    assert first.json() == retry.json()
    assert other.status_code == 429
    async with repository.sessions() as db:
        counts = dict((await db.execute(select(RateLimitHit.key, RateLimitHit.hits))).all())
    assert counts == {"create:127.0.0.1": 2, "create:all": 1}


async def test_client_limit_groups_ipv6_networks_and_mapped_ipv4(
    settings: Settings, repository: Repository, session_input: SessionInput
) -> None:
    configured = settings.model_copy(
        update={
            "app_env": "preview",
            "preview_access_required": False,
            "session_create_per_minute": 1,
        }
    )
    temporal = AsyncMock()
    limiter = SharedRateLimiter(repository.sessions, clock=lambda: NOW)
    app = create_app(configured, repository, temporal, limiter)
    addresses = (
        "2001:db8:10::1",
        "2001:db8:10::2%" + "scope" * 20,
        "2001:db8:11::1",
        "203.0.113.9",
        "::ffff:203.0.113.9",
    )
    async with app.router.lifespan_context(app), client_for(app) as client:
        statuses = [
            (
                await submit(
                    client, configured, session_input, headers={"do-connecting-ip": address}
                )
            ).status_code
            for address in addresses
        ]
    assert statuses == [202, 429, 202, 202, 429]
    assert temporal.start_workflow.await_count == 3


@pytest.mark.parametrize(
    ("app_env", "expected"),
    [
        ("development", "127.0.0.1"),
        ("test", "127.0.0.1"),
        ("preview", "203.0.113.9"),
        ("production", "203.0.113.9"),
    ],
)
async def test_client_ip_comes_from_do_connecting_ip_only_on_app_platform(
    settings: Settings, session_input: SessionInput, app_env: str, expected: str
) -> None:
    configured = settings.model_copy(
        update={
            "app_env": app_env,
            "preview_access_required": False,
            "worker_build_id": "build-sha",
        }
    )
    repository = AsyncMock()
    repository.submitted.return_value = False
    repository.create.return_value = session_input
    limiter = AsyncMock()
    limiter.hit.return_value = Hit(count=1, retry_after=60)
    temporal = AsyncMock()
    app = create_app(configured, repository, temporal, limiter)
    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await submit(
            client,
            configured,
            session_input,
            headers={"do-connecting-ip": "203.0.113.9", "X-Forwarded-For": "198.51.100.1"},
        )
    assert response.status_code == 202
    assert limiter.hit.await_args_list[0].args == (f"create:{expected}", 60)
    options = temporal.start_workflow.await_args.kwargs
    assert options["execution_timeout"] == timedelta(
        seconds=session_input.limits.planning_seconds + 120
    )
    assert options["versioning_override"] == PinnedVersioningOverride(
        WorkerDeploymentVersion(deployment_name="cpfc-trip", build_id="build-sha")
    )


async def test_unpinned_start_without_worker_build_id(
    settings: Settings, session_input: SessionInput
) -> None:
    repository = AsyncMock()
    repository.submitted.return_value = True
    repository.create.return_value = session_input
    limiter = AsyncMock()
    temporal = AsyncMock()
    app = create_app(settings, repository, temporal, limiter)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await submit(client, settings, session_input)).status_code == 202
    limiter.hit.assert_not_awaited()
    assert temporal.start_workflow.await_args.kwargs["versioning_override"] is None


async def test_lost_database_returns_retryable_503(
    settings: Settings, session_input: SessionInput
) -> None:
    repository = AsyncMock()
    lost = OperationalError("SELECT 1", {}, ConnectionResetError("connection reset"))
    repository.submitted.side_effect = lost
    repository.authorize.side_effect = lost
    app = create_app(settings, repository, AsyncMock(), AsyncMock())
    sid = session_input.public_session_id
    async with app.router.lifespan_context(app), client_for(app) as client:
        created = await submit(client, settings, session_input)
        client.cookies.set(f"cpfc_{sid}", TOKEN)
        polled = await client.get(f"/api/sessions/{sid}/snapshot")
    for response in (created, polled):
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"
        assert "connection reset" not in response.text


def snapshot_app(settings: Settings, describe: AsyncMock):
    repository = AsyncMock()
    repository.authorize.return_value = True
    handle = Mock()
    handle.query = AsyncMock(side_effect=WorkflowQueryFailedError("no worker"))
    handle.describe = describe
    temporal = AsyncMock()
    temporal.get_workflow_handle = Mock(return_value=handle)
    return create_app(settings, repository, temporal, AsyncMock())


@pytest.mark.parametrize(
    "describe",
    [
        *(
            AsyncMock(return_value=Mock(status=status))
            for status in (
                WorkflowExecutionStatus.FAILED,
                WorkflowExecutionStatus.TERMINATED,
                WorkflowExecutionStatus.TIMED_OUT,
                WorkflowExecutionStatus.CANCELED,
            )
        ),
        AsyncMock(side_effect=RPCError("not found", RPCStatusCode.NOT_FOUND, b"")),
    ],
)
async def test_interrupted_search_snapshot_returns_404(
    settings: Settings, session_input: SessionInput, describe: AsyncMock
) -> None:
    app = snapshot_app(settings, describe)
    sid = session_input.public_session_id
    async with app.router.lifespan_context(app), client_for(app) as client:
        client.cookies.set(f"cpfc_{sid}", TOKEN)
        response = await client.get(f"/api/sessions/{sid}/snapshot")
    assert response.status_code == 404
    assert response.json()["detail"] == "This search was interrupted. Please start it again."


@pytest.mark.parametrize(
    "describe",
    [
        AsyncMock(return_value=Mock(status=WorkflowExecutionStatus.RUNNING)),
        AsyncMock(side_effect=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")),
    ],
)
async def test_running_or_unreachable_search_snapshot_returns_503(
    settings: Settings, session_input: SessionInput, describe: AsyncMock
) -> None:
    app = snapshot_app(settings, describe)
    sid = session_input.public_session_id
    async with app.router.lifespan_context(app), client_for(app) as client:
        client.cookies.set(f"cpfc_{sid}", TOKEN)
        assert (await client.get(f"/api/sessions/{sid}/snapshot")).status_code == 503


async def test_snapshot_polls_are_limited_per_ip_before_session_lookup(
    settings: Settings, session_input: SessionInput
) -> None:
    repository = AsyncMock()
    repository.authorize.return_value = False
    app = create_app(
        settings.model_copy(update={"snapshot_polls_per_minute": 2}),
        repository,
        AsyncMock(),
        AsyncMock(),
    )
    sid = session_input.public_session_id
    async with app.router.lifespan_context(app), client_for(app) as client:
        client.cookies.set(f"cpfc_{sid}", TOKEN)
        responses = [await client.get(f"/api/sessions/{sid}/snapshot") for _ in range(3)]
    assert [response.status_code for response in responses] == [404, 404, 429]
    assert 1 <= int(responses[2].headers["retry-after"]) <= 60
    assert repository.authorize.await_count == 2


def test_pool_sizing_applies_to_postgresql_only(settings: Settings) -> None:
    postgres = engine(
        settings.model_copy(
            update={
                "database_url": "postgresql://user:password@db.example:25060/defaultdb",
                "database_pool_size": 3,
                "database_max_overflow": 1,
            }
        )
    )
    sqlite = engine(settings)
    try:
        assert postgres.pool.size() == 3
        assert postgres.pool._max_overflow == 1
        assert postgres.pool._recycle == 1800
        assert postgres.pool._pre_ping is True
        assert sqlite.pool._recycle == -1
        assert sqlite.pool._pre_ping is True
    finally:
        postgres.sync_engine.dispose(close=False)
        sqlite.sync_engine.dispose(close=False)


async def test_postgres_concurrent_hits_count_once_each(settings: Settings) -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is required for PostgreSQL counter checks")
    db = engine(settings.model_copy(update={"database_url": url}))
    limiter = SharedRateLimiter(async_sessionmaker(db, expire_on_commit=False))
    key = f"test:{uuid4()}"
    try:
        hits = await asyncio.gather(*(limiter.hit(key, 600) for _ in range(6)))
        assert sorted(hit.count for hit in hits) == [1, 2, 3, 4, 5, 6]
    finally:
        async with db.begin() as connection:
            await connection.execute(delete(RateLimitHit).where(RateLimitHit.key == key))
        await db.dispose()
