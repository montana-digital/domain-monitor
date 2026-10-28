import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.clock import utcnow
from app.notices import notices
from app.service import (
    due_monitor_ids,
    maybe_retain,
    queue_ids,
    registration_due_ids,
    stagger_due,
    try_enqueue,
    try_enqueue_registration,
)

logger = logging.getLogger(__name__)
_scheduler: AsyncIOScheduler | None = None


async def tick() -> None:
    try:
        ids = await asyncio.to_thread(due_monitor_ids)
    except Exception:
        logger.exception("Could not list due monitors")
        ids = []
    queue_ids(ids, try_enqueue)
    try:
        lookups = await asyncio.to_thread(registration_due_ids)
    except Exception:
        logger.exception("Could not list registration lookups")
        lookups = []
    queue_ids(lookups, try_enqueue_registration)
    try:
        await asyncio.to_thread(maybe_retain)
    except Exception:
        logger.exception("Retention failed")
    notices.last_tick = utcnow()


def start_scheduler() -> None:
    global _scheduler
    scheduler = AsyncIOScheduler()
    scheduler.add_job(tick, "interval", seconds=15, id="tick", max_instances=1, coalesce=True)
    scheduler.start()
    _scheduler = scheduler
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(tick())


def shutdown_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def prepare_schedule() -> None:
    stagger_due()
