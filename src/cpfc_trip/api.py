"""JSON-only API. All active session reads Query Temporal directly."""

import base64
import binascii
import ipaddress
import math
import re
import secrets
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timedelta
from typing import Annotated, Any
from uuid import UUID

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from temporalio.client import Client, WorkflowExecutionStatus, WorkflowQueryFailedError
from temporalio.common import (
    PinnedVersioningOverride,
    WorkerDeploymentVersion,
    WorkflowIDConflictPolicy,
    WorkflowIDReusePolicy,
)
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from cpfc_trip.catalog import load_catalog
from cpfc_trip.config import Settings
from cpfc_trip.domain import CreateSession, Snapshot
from cpfc_trip.origins import LIVE_ORIGINS, UK_RAIL
from cpfc_trip.persistence.database import engine
from cpfc_trip.persistence.rate_limit import SharedRateLimiter
from cpfc_trip.persistence.repository import Repository
from cpfc_trip.temporal.client import connect
from cpfc_trip.temporal.workflow import TravelPlanningSessionWorkflow

INTERRUPTED = {
    WorkflowExecutionStatus.FAILED,
    WorkflowExecutionStatus.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT,
    WorkflowExecutionStatus.CANCELED,
}


class LocalLimiter:
    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, limit: int, seconds: int = 60) -> None:
        now = time.monotonic()
        if len(self.hits) > 10000:
            self.hits = defaultdict(
                deque, {k: v for k, v in self.hits.items() if v and v[-1] > now - seconds}
            )
        q = self.hits[key]
        while q and q[0] < now - seconds:
            q.popleft()
        if len(q) >= limit:
            retry_after = max(1, math.ceil(q[0] + seconds - now))
            raise HTTPException(
                429,
                "Too many requests; please try again shortly",
                headers={"Retry-After": str(retry_after)},
            )
        q.append(now)


@contextmanager
def database_available() -> Iterator[None]:
    try:
        yield
    except (DBAPIError, PoolTimeoutError, OSError) as error:
        if isinstance(error, DBAPIError) and not (
            error.connection_invalidated or isinstance(error, OperationalError | InterfaceError)
        ):
            raise
        raise HTTPException(
            503,
            "Trip planning is temporarily unavailable. Please try again shortly.",
            headers={"Retry-After": "5"},
        ) from None


def _ip_bucket(value: str) -> str | None:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.IPv6Network((int(address), 64), strict=False))
    return str(address)


def create_app(
    settings: Settings | None = None,
    repository: Repository | None = None,
    temporal: Client | None = None,
    rate_limiter: SharedRateLimiter | None = None,
) -> FastAPI:
    config = settings or Settings()
    limiter = LocalLimiter()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        db = engine(config)
        app.state.repository = repository or Repository(db, config)
        app.state.rate_limiter = rate_limiter or SharedRateLimiter(app.state.repository.sessions)
        app.state.temporal = temporal or await connect(config)
        try:
            yield
        finally:
            await db.dispose()

    app = FastAPI(title="Eagles Away", version="0.1.0", lifespan=lifespan)
    allowed_frontend_origins = {config.frontend_origin}
    allowed_frontend_origins.update(
        origin.strip() for origin in config.additional_frontend_origins.split(",") if origin.strip()
    )
    if config.app_env in {"development", "test"}:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=sorted(allowed_frontend_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST"],
            allow_headers=["Content-Type", "X-Submission-Token"],
        )

    @app.middleware("http")
    async def protect(request: Request, call_next: Any) -> Response:
        if (
            config.app_env == "preview"
            and config.preview_access_required
            and request.url.path != "/healthz"
        ):
            encoded = request.headers.get("authorization", "").removeprefix("Basic ")
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                username, separator, password = decoded.partition(":")
            except (binascii.Error, UnicodeDecodeError):
                username, separator, password = "", "", ""
            if (
                not separator
                or not secrets.compare_digest(username.encode(), config.preview_username.encode())
                or not secrets.compare_digest(
                    password.encode(), config.preview_password.get_secret_value().encode()
                )
            ):
                return Response(
                    status_code=401,
                    headers={
                        "WWW-Authenticate": 'Basic realm="Eagles Away preview"',
                        "Cache-Control": "no-store",
                    },
                )
        if (
            request.method == "POST"
            and request.url.path.startswith("/api/")
            and request.headers.get("origin") not in allowed_frontend_origins
        ):
            return JSONResponse({"detail": "Request origin is not allowed"}, status_code=403)
        if int(request.headers.get("content-length", "0")) > 32000:
            return JSONResponse({"detail": "Request is too large"}, status_code=413)
        response: Response = await call_next(request)
        response.headers.update(
            {
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
        )
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        # FastAPI's default errors include input values, potentially echoing private values or tokens.
        return JSONResponse(
            {"detail": [{"loc": x["loc"], "msg": x["msg"]} for x in exc.errors()]}, status_code=422
        )

    def repo(request: Request) -> Repository:
        return request.app.state.repository  # type: ignore[no-any-return]

    def client(request: Request) -> Client:
        return request.app.state.temporal  # type: ignore[no-any-return]

    def ip(request: Request) -> str:
        # App Platform's edge sets do-connecting-ip to the client address; X-Forwarded-For only
        # holds its ingress there and is client-controlled elsewhere, so it is never read.
        if config.app_env in {"preview", "production"}:
            forwarded = _ip_bucket(request.headers.get("do-connecting-ip", ""))
            if forwarded is not None:
                return forwarded
        return (_ip_bucket(request.client.host) if request.client else None) or "unknown"

    async def admit(request: Request) -> None:
        shared: SharedRateLimiter = request.app.state.rate_limiter
        client_hit = await shared.hit(f"create:{ip(request)}", 60)
        if client_hit.count > config.session_create_per_minute:
            raise HTTPException(
                429,
                "Too many requests; please try again shortly",
                headers={"Retry-After": str(client_hit.retry_after)},
            )
        everyone = await shared.hit("create:all", 600)
        if everyone.count > config.session_create_per_10_minutes:
            raise HTTPException(
                503,
                "Trip planning is busy right now. Please try again in a few minutes.",
                headers={"Retry-After": str(everyone.retry_after)},
            )

    async def authorize(request: Request, public_id: UUID) -> None:
        token = request.cookies.get(f"cpfc_{public_id}", "")
        if not token:
            raise HTTPException(404, "Session not found or access expired")
        with database_available():
            allowed = await repo(request).authorize(public_id, token)
        if not allowed:
            raise HTTPException(404, "Session not found or access expired")

    @app.get("/api/catalog")
    async def catalog() -> dict[str, Any]:
        fixtures, routes = load_catalog()
        if config.planner_mode == "live":
            enabled = {r.fixture_id for r in routes if r.enabled}
            fixtures = tuple(f for f in fixtures if f.id in enabled)
        return {
            "fixtures": fixtures,
            "origin": "London",
            "origin_cities": LIVE_ORIGINS,
            "rail_cities": ["London", *(city for city in sorted(UK_RAIL) if city in LIVE_ORIGINS)],
            "development_mode": config.planner_mode == "recorded",
        }

    @app.post("/api/sessions", status_code=202)
    async def create(
        body: CreateSession,
        request: Request,
        response: Response,
        token: Annotated[str, Header(alias="X-Submission-Token")],
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", token):
            raise HTTPException(422, "A strong submission token is required")
        with database_available():
            if not await repo(request).submitted(body.submission_id, token):
                await admit(request)
            try:
                data = await repo(request).create(body, token)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            except PermissionError:
                raise HTTPException(409, "Submission ID is already in use") from None
        try:
            await client(request).start_workflow(
                TravelPlanningSessionWorkflow.run,
                data,
                id=f"travel-session/{data.public_session_id}",
                task_queue=config.temporal_task_queue,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                execution_timeout=timedelta(seconds=data.limits.planning_seconds + 120),
                versioning_override=(
                    PinnedVersioningOverride(
                        WorkerDeploymentVersion(
                            deployment_name="cpfc-trip", build_id=config.worker_build_id
                        )
                    )
                    if config.worker_build_id
                    else None
                ),
                rpc_timeout=timedelta(seconds=5),
            )
        except WorkflowAlreadyStartedError:
            pass
        except RPCError:
            raise HTTPException(
                503, "Planning is temporarily unavailable. Retry this same submission."
            ) from None
        response.set_cookie(
            f"cpfc_{data.public_session_id}",
            token,
            httponly=True,
            secure=config.app_env in {"preview", "production"},
            samesite="lax",
            path=f"/api/sessions/{data.public_session_id}",
        )
        return {"public_session_id": data.public_session_id}

    @app.get("/api/sessions/{public_id}/snapshot", response_model=Snapshot | None)
    async def snapshot(
        public_id: UUID, request: Request, after_revision: Annotated[int | None, Query(ge=0)] = None
    ) -> Snapshot | Response:
        limiter.check(f"poll-ip:{ip(request)}", config.snapshot_polls_per_minute)
        await authorize(request, public_id)
        limiter.check(f"poll:{public_id}", 90)
        handle = client(request).get_workflow_handle(f"travel-session/{public_id}")
        try:
            value = await handle.query(
                TravelPlanningSessionWorkflow.get_snapshot,
                after_revision,
                rpc_timeout=timedelta(seconds=5),
            )
        except (RPCError, WorkflowQueryFailedError):
            # Completed results stay readable after the worker build that ran them retires.
            try:
                description = await handle.describe(rpc_timeout=timedelta(seconds=5))
            except RPCError as error:
                if error.status == RPCStatusCode.NOT_FOUND:
                    raise HTTPException(
                        404, "This search was interrupted. Please start it again."
                    ) from None
                raise HTTPException(503, "Reconnecting to your saved planning session") from None
            if description.status in INTERRUPTED:
                raise HTTPException(
                    404, "This search was interrupted. Please start it again."
                ) from None
            if description.status != WorkflowExecutionStatus.COMPLETED:
                raise HTTPException(503, "Reconnecting to your saved planning session") from None
            try:
                value = Snapshot.model_validate(
                    await handle.result(rpc_timeout=timedelta(seconds=5))
                )
            except (RPCError, WorkflowQueryFailedError):
                raise HTTPException(503, "Reconnecting to your saved planning session") from None
            if value.state_revision == after_revision:
                value = None
        return value if value else Response(status_code=204)

    @app.get("/healthz")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    async def ready(request: Request) -> dict[str, bool]:
        try:
            async with repo(request).sessions() as db:
                await db.execute(text("SELECT 1"))
            await client(request).service_client.check_health(timeout=timedelta(seconds=3))
        except Exception:
            raise HTTPException(503, "Dependencies are not ready") from None
        return {"ok": True}

    if config.static_export_dir:
        app.mount("/", StaticFiles(directory=config.static_export_dir, html=True), name="frontend")

    return app


def main() -> None:
    uvicorn.run(
        "cpfc_trip.api:create_app", factory=True, host="0.0.0.0", port=8000, access_log=False
    )
