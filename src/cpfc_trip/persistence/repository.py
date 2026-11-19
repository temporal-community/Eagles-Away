import hashlib
import hmac
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from cpfc_trip.catalog import load_catalog, validate_brief
from cpfc_trip.config import Settings
from cpfc_trip.domain import CreateSession, Limits, SessionInput
from cpfc_trip.origins import LIVE_ORIGINS, alternate_departure_route, routes_for_origin
from cpfc_trip.persistence.models import SessionRow


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Repository:
    def __init__(self, engine: AsyncEngine, settings: Settings):
        self.sessions = async_sessionmaker(engine, expire_on_commit=False)
        self.settings = settings

    async def create(self, request: CreateSession, token: str) -> SessionInput:
        fingerprint = digest(request.model_dump_json())
        async with self.sessions() as db:
            old = await db.scalar(
                select(SessionRow).where(SessionRow.submission_id == str(request.submission_id))
            )
            if old:
                if not hmac.compare_digest(old.access_hash, digest(token)):
                    raise PermissionError("Submission is not accessible")
                if old.request_hash != fingerprint:
                    raise ValueError("Submission ID was already used with a different brief")
                return SessionInput.model_validate(old.workflow_input)
            fixtures, routes = load_catalog()
            brief = validate_brief(request.brief, fixtures, datetime.now(UTC))
            if self.settings.planner_mode == "live" and brief.origin_city not in LIVE_ORIGINS:
                raise ValueError(
                    "This departure city is not currently supported for live planning. Choose a city shown in the planner."
                )
            routes = tuple(
                route
                for route in routes_for_origin(routes, brief.origin_city)
                if route.fixture_id in brief.fixture_ids
            )
            if brief.departure_city is not None:
                departure_city = brief.departure_city
                routes = tuple(
                    alternate
                    for route in routes
                    if (
                        alternate := alternate_departure_route(
                            route, brief.origin_city, departure_city
                        )
                    )
                )
                available = {
                    route.fixture_id
                    for route in routes
                    if route.enabled or self.settings.planner_mode == "recorded"
                }
                if not set(brief.fixture_ids) <= available:
                    raise ValueError(
                        "Flight search from this nearby departure city is not available"
                    )
            if brief.transport_mode == "rail" and any(
                not any(
                    route.mode == "rail" and route.enabled and route.fixture_id == fixture_id
                    for route in routes
                )
                for fixture_id in brief.fixture_ids
            ):
                raise ValueError(
                    "Train search is not available for this match from this city. Choose flights or another match."
                )
            if self.settings.planner_mode == "live":
                enabled = {r.fixture_id for r in routes if r.enabled}
                if not set(brief.fixture_ids) <= enabled:
                    raise ValueError("Live planning is not yet enabled for this fixture")
                if brief.travellers.child_ages or brief.travellers.rooms != 1:
                    raise ValueError(
                        "Live planning currently supports adults sharing one room. Child and multi-room pricing are still being validated."
                    )
            sid = uuid4()
            data = SessionInput(
                public_session_id=sid,
                brief=brief,
                fixtures=tuple(f for f in fixtures if f.id in brief.fixture_ids),
                routes=routes,
                catalog_version=next(
                    f.catalog_version for f in fixtures if f.id in brief.fixture_ids
                ),
                planner_mode=self.settings.planner_mode,
                limits=Limits(),
            )
            db.add(
                SessionRow(
                    id=str(sid),
                    submission_id=str(request.submission_id),
                    request_hash=fingerprint,
                    access_hash=digest(token),
                    created_at=datetime.now(UTC),
                    workflow_input=json.loads(data.model_dump_json()),
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                return await self.create(request, token)
            return data

    async def submitted(self, submission_id: UUID, token: str) -> bool:
        async with self.sessions() as db:
            access_hash = await db.scalar(
                select(SessionRow.access_hash).where(SessionRow.submission_id == str(submission_id))
            )
        return access_hash is not None and hmac.compare_digest(access_hash, digest(token))

    async def authorize(self, sid: UUID, token: str) -> bool:
        async with self.sessions() as db:
            row = await db.get(SessionRow, str(sid))
            return bool(row and hmac.compare_digest(row.access_hash, digest(token)))
