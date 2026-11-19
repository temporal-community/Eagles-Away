import asyncio
import logging
import signal
from datetime import timedelta

from temporalio.common import VersioningBehavior, WorkerDeploymentVersion
from temporalio.worker import Worker, WorkerDeploymentConfig

from cpfc_trip.config import Settings
from cpfc_trip.persistence.database import engine
from cpfc_trip.persistence.search_cache import SearchCache
from cpfc_trip.temporal.activities import SearchActivities
from cpfc_trip.temporal.client import connect
from cpfc_trip.temporal.workflow import TravelPlanningSessionWorkflow


async def run() -> None:
    settings = Settings()
    db = engine(settings)
    try:
        client = await connect(settings)
        cache = (
            SearchCache(
                db,
                timedelta(minutes=settings.search_cache_ttl_minutes),
                stale_if_error=timedelta(minutes=settings.search_cache_stale_if_error_minutes),
                early_beta=settings.search_cache_early_beta,
            )
            if settings.search_cache_enabled and settings.search_cache_ttl_minutes
            else None
        )
        search = SearchActivities(cache)
        deployment = (
            WorkerDeploymentConfig(
                version=WorkerDeploymentVersion(
                    deployment_name="cpfc-trip", build_id=settings.worker_build_id
                ),
                use_worker_versioning=True,
                default_versioning_behavior=VersioningBehavior.PINNED,
            )
            if settings.worker_build_id
            else None
        )
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        async with Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[TravelPlanningSessionWorkflow],
            activities=[
                search.search_flights,
                search.search_trains,
                search.search_stays,
            ],
            deployment_config=deployment,
            max_concurrent_activities=settings.worker_max_activities,
            max_heartbeat_throttle_interval=timedelta(seconds=5),
            graceful_shutdown_timeout=timedelta(seconds=150),
        ):
            await stop.wait()
    finally:
        await db.dispose()


def main() -> None:
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger("cpfc_trip").setLevel(logging.INFO)
    logging.getLogger("temporalio.workflow").setLevel(logging.INFO)
    asyncio.run(run())
