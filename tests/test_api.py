from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr, ValidationError
from temporalio.client import WorkflowQueryFailedError

from cpfc_trip.api import create_app
from cpfc_trip.catalog import load_catalog
from cpfc_trip.config import Settings
from cpfc_trip.domain import Brief, CreateSession, SessionInput
from cpfc_trip.persistence.rate_limit import Hit
from cpfc_trip.persistence.repository import Repository


def test_preview_requires_access_credentials() -> None:
    """A deployment cannot accidentally expose the preview without its access gate."""
    with pytest.raises(ValidationError, match="Preview requires access credentials"):
        Settings(app_env="preview", preview_username="", preview_password="")


def test_public_preview_does_not_require_access_credentials() -> None:
    """Allow an explicitly public preview without HTTP Basic credentials."""
    settings = Settings(
        app_env="preview",
        preview_access_required=False,
        preview_username="",
        preview_password="",
    )
    assert settings.preview_access_required is False


async def test_session_uses_selected_departure_airports(repository, session_input):
    brief = session_input.brief.model_copy(update={"origin_city": "Manchester"})
    data = await repository.create(
        CreateSession(submission_id=uuid4(), brief=brief),
        "x" * 64,
    )
    assert data.brief.origin_city == "Manchester"
    assert {route.origin for route in data.routes} == {"MAN", "Manchester"}


async def test_api_nearby_departure_preserves_brief_and_uses_only_alternate_flights(
    settings: Settings, repository: Repository, session_input: SessionInput
) -> None:
    live = settings.model_copy(update={"planner_mode": "live"})
    repository.settings = live
    temporal = AsyncMock()
    app = create_app(live, repository, temporal)
    brief = session_input.brief.model_copy(
        update={
            "origin_city": " liverpool ",
            "departure_city": " mANCHESTER ",
            "budget_tier": "comfort",
            "private_room": True,
            "private_bathroom": True,
        }
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        response = await client.post(
            "/api/sessions",
            json={"submission_id": str(uuid4()), "brief": brief.model_dump(mode="json")},
            headers={"Origin": live.frontend_origin, "X-Submission-Token": "x" * 64},
        )
    assert response.status_code == 202
    temporal.start_workflow.assert_awaited_once()
    frozen = temporal.start_workflow.await_args.args[1]
    assert frozen.brief == brief.model_copy(
        update={"origin_city": "Liverpool", "departure_city": "Manchester"}
    )
    assert frozen.fixtures == session_input.fixtures
    assert [(route.mode, route.origin, route.destination) for route in frozen.routes] == [
        ("flight", "MAN", "LYS")
    ]
    (route,) = frozen.routes
    original = next(route for route in session_input.routes if route.mode == "flight")
    assert route.enabled == original.enabled
    assert route.gateway_arrival_hours == original.gateway_arrival_hours
    assert route.gateway_departure_hours == original.gateway_departure_hours
    assert route.minimum_transfer_minutes == original.minimum_transfer_minutes
    assert "Liverpool and Manchester Airport" in route.transfer_note


@pytest.mark.parametrize(
    ("origin", "departure", "mode", "detail"),
    [
        ("Liverpool", "Birmingham", "flight", "supported nearby departure city"),
        ("Belfast", "Manchester", "flight", "supported nearby departure city"),
        ("Liverpool", "Manchester", "rail", "only available for flights"),
    ],
)
async def test_api_rejects_unavailable_nearby_departure(
    settings, repository, session_input, origin, departure, mode, detail
) -> None:
    temporal = AsyncMock()
    app = create_app(settings, repository, temporal)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        response = await client.post(
            "/api/sessions",
            json={
                "submission_id": str(uuid4()),
                "brief": {
                    "fixture_ids": session_input.brief.fixture_ids,
                    "origin_city": origin,
                    "departure_city": departure,
                    "transport_mode": mode,
                },
            },
            headers={"Origin": settings.frontend_origin, "X-Submission-Token": "x" * 64},
        )
    assert response.status_code == 422
    assert detail in response.json()["detail"]
    temporal.start_workflow.assert_not_awaited()


async def test_live_catalog_only_offers_major_airport_cities(settings, repository) -> None:
    """The live brief uses the same fixed airport-city list for every match."""
    app = create_app(settings.model_copy(update={"planner_mode": "live"}), repository, AsyncMock())
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/api/catalog")
    assert response.status_code == 200
    assert response.json()["origin_cities"] == [
        "Belfast",
        "Birmingham",
        "Bristol",
        "Edinburgh",
        "Glasgow",
        "Liverpool",
        "London",
        "Manchester",
        "Newcastle",
    ]
    assert "origin_cities_by_fixture" not in response.json()


async def test_live_session_rejects_removed_departure_city(repository) -> None:
    """Direct API submissions cannot bypass the live form's airport-city list."""
    repository.settings = repository.settings.model_copy(update={"planner_mode": "live"})
    brief = Brief(fixture_ids=("uel-2026-besiktas-away",), origin_city="Bournemouth")
    with pytest.raises(ValueError, match="not currently supported for live planning"):
        await repository.create(
            CreateSession(submission_id=uuid4(), brief=brief),
            "x" * 64,
        )


async def test_unsupported_train_city_and_city_fail_before_search(repository, session_input):
    brief = session_input.brief.model_copy(
        update={"origin_city": "Manchester", "transport_mode": "rail"}
    )
    rail = await repository.create(
        CreateSession(submission_id=uuid4(), brief=brief),
        "x" * 64,
    )
    assert any(route.mode == "rail" for route in rail.routes)
    unsupported_rail = brief.model_copy(update={"origin_city": "Belfast"})
    with pytest.raises(ValueError, match="Train search is not available"):
        await repository.create(
            CreateSession(submission_id=uuid4(), brief=unsupported_rail),
            "x" * 64,
        )
    unsupported = brief.model_copy(update={"origin_city": "Paris", "transport_mode": "flight"})
    with pytest.raises(ValueError, match="supported UK departure city"):
        await repository.create(
            CreateSession(submission_id=uuid4(), brief=unsupported),
            "x" * 64,
        )


@pytest.mark.parametrize("city", ["London", "Manchester"])
@pytest.mark.parametrize("destination", ["Istanbul", "Białystok", "Salzburg"])
async def test_train_mode_requires_a_route_for_the_selected_match(
    repository: Repository, city: str, destination: str
) -> None:
    fixture = next(item for item in load_catalog()[0] if item.city == destination)
    brief = Brief(fixture_ids=(fixture.id,), origin_city=city, transport_mode="rail")
    with pytest.raises(ValueError, match="Train search is not available"):
        await repository.create(
            CreateSession(submission_id=uuid4(), brief=brief),
            "x" * 64,
        )


async def test_api_auth_polling_204_and_csrf(
    settings: Settings, session_input: SessionInput
) -> None:
    repository = AsyncMock()
    repository.authorize.return_value = False
    temporal = AsyncMock()
    handle = Mock()
    handle.query = AsyncMock(return_value=None)
    handle.describe = AsyncMock()
    temporal.get_workflow_handle = Mock(return_value=handle)
    app = create_app(settings, repository, temporal)
    sid = session_input.public_session_id
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client,
    ):
        assert (await client.get(f"/api/sessions/{sid}/snapshot")).status_code == 404
        assert not handle.query.called
        repository.authorize.return_value = True
        client.cookies.set(f"cpfc_{sid}", "x" * 64)
        response = await client.get(f"/api/sessions/{sid}/snapshot?after_revision=3")
        assert response.status_code == 204 and response.headers["cache-control"] == "no-store"
        assert (
            await client.post(
                "/api/sessions",
                json={"submission_id": str(uuid4()), "brief": {"fixture_ids": ["x"]}},
                headers={"Origin": "https://evil.example", "X-Submission-Token": "x" * 64},
            )
        ).status_code == 403
        assert not repository.create.called
        handle.query.assert_awaited_once()
        handle.query.side_effect = WorkflowQueryFailedError("sensitive upstream diagnostics")
        response = await client.get(f"/api/sessions/{sid}/snapshot")
        assert response.status_code == 503
        assert "sensitive" not in response.text


async def test_preview_serves_export_and_uses_secure_session_cookie(
    settings: Settings, session_input: SessionInput, tmp_path
) -> None:
    """A hosted preview serves static pages and keeps session access on HTTPS."""
    export = tmp_path / "out"
    (export / "plan").mkdir(parents=True)
    (export / "index.html").write_text("<h1>Eagles Away preview</h1>")
    (export / "plan" / "index.html").write_text("<h1>Your plan</h1>")
    preview = settings.model_copy(
        update={
            "app_env": "preview",
            "frontend_origin": "https://eaglesaway.com",
            "additional_frontend_origins": "https://preview.example",
            "static_export_dir": str(export),
            "preview_username": "previewer",
            "preview_password": SecretStr("private-preview-password"),
        }
    )
    repository = AsyncMock()
    repository.create.return_value = session_input
    repository.submitted.return_value = False
    rate_limiter = AsyncMock()
    rate_limiter.hit.return_value = Hit(count=1, retry_after=60)
    temporal = AsyncMock()
    app = create_app(preview, repository, temporal, rate_limiter)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="https://preview.example") as client,
    ):
        assert (await client.get("/healthz")).status_code == 200
        denied = await client.get("/")
        assert denied.status_code == 401
        assert denied.headers["www-authenticate"] == 'Basic realm="Eagles Away preview"'
        assert (await client.get("/api/catalog")).status_code == 401
        client.auth = ("previewer", "private-preview-password")
        assert "Eagles Away preview" in (await client.get("/")).text
        assert "Your plan" in (await client.get("/plan/")).text
        assert (await client.get("/api/catalog")).status_code == 200
        assert (
            await client.post(
                "/api/sessions", json={}, headers={"Origin": "https://eaglesaway.com"}
            )
        ).status_code == 422
        assert (
            await client.post("/api/sessions", json={}, headers={"Origin": "https://evil.example"})
        ).status_code == 403
        response = await client.post(
            "/api/sessions",
            json={
                "submission_id": str(uuid4()),
                "brief": session_input.brief.model_dump(mode="json"),
            },
            headers={"Origin": "https://preview.example", "X-Submission-Token": "x" * 64},
        )
        assert response.status_code == 202
        assert "Secure" in response.headers["set-cookie"]


async def test_public_preview_keeps_origin_guard(settings: Settings, tmp_path) -> None:
    """Public pages and catalog need no Basic auth; cross-origin writes stay blocked."""
    export = tmp_path / "out"
    export.mkdir()
    (export / "index.html").write_text("<h1>Eagles Away</h1>")
    public_preview = settings.model_copy(
        update={
            "app_env": "preview",
            "frontend_origin": "https://eaglesaway.com",
            "static_export_dir": str(export),
            "preview_access_required": False,
        }
    )
    app = create_app(public_preview, AsyncMock(), AsyncMock())
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="https://eaglesaway.com") as client,
    ):
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/api/catalog")).status_code == 200
        assert (
            await client.post("/api/sessions", json={}, headers={"Origin": "https://evil.example"})
        ).status_code == 403
