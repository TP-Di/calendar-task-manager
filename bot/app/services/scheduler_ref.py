"""Holds a shared reference to the running APScheduler so settings can reschedule jobs."""
import logging
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)

_scheduler: Optional[AsyncIOScheduler] = None


def set_scheduler(scheduler: AsyncIOScheduler) -> None:
    global _scheduler
    _scheduler = scheduler


def reschedule_briefing(briefing_time: str, timezone: str) -> None:
    """Reschedule the morning_briefing job after BRIEFING_TIME or TIMEZONE changes."""
    if _scheduler is None:
        logger.warning("reschedule_briefing: scheduler not set yet")
        return
    try:
        hour, minute = map(int, briefing_time.split(":"))
        _scheduler.reschedule_job(
            "morning_briefing",
            trigger="cron",
            hour=hour,
            minute=minute,
            timezone=timezone,
        )
        logger.info("Briefing rescheduled to %s %s", briefing_time, timezone)
    except Exception as e:
        logger.warning("Failed to reschedule briefing job: %s", e)
