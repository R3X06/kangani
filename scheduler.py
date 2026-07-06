"""Time-based reminder delivery, built on python-telegram-bot's JobQueue.

JobQueue wraps APScheduler's AsyncIOScheduler running on the bot's own
asyncio event loop, so firing a reminder is just a normal awaited call to
context.bot.send_message -- no thread bridging needed.
"""

import logging
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from telegram.ext import ContextTypes, JobQueue

import database
import keyboards

logger = logging.getLogger(__name__)


def parse_iso_datetime(iso_str: str) -> datetime:
    """Parse an ISO-8601 datetime string into an absolute, tz-aware UTC instant.

    Any explicit offset in the string (e.g. 'Z', '+08:00') is trusted as-is --
    this is what actually determines the absolute moment, so there is no
    "conversion" to get wrong here. Only a genuinely offset-less string falls
    back to a default, and it defaults to the configured local TIMEZONE (not
    UTC): Claude is shown local time as its primary anchor, so a bare
    datetime more likely means "local time with the offset dropped" than
    "UTC".
    """
    s = iso_str.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        tz_name = os.environ.get("TIMEZONE", "UTC")
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(timezone.utc)


def format_utc_iso(dt: datetime) -> str:
    """Serialize a datetime to the canonical UTC 'YYYY-MM-DDTHH:MM:SS.mmmZ'
    string used everywhere in the database, regardless of the input's tzinfo.
    """
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def schedule_reminder(
    job_queue: JobQueue,
    reminder_id: int,
    chat_id: int,
    trigger_dt_utc: datetime,
    message: str,
) -> None:
    job_queue.run_once(
        callback=_fire_reminder,
        when=trigger_dt_utc,
        chat_id=chat_id,
        data={"reminder_id": reminder_id, "message": message},
        name=f"reminder-{reminder_id}",
    )


async def _fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    job = context.job
    reminder_id = job.data["reminder_id"]
    await context.bot.send_message(
        chat_id=job.chat_id,
        text=f"Reminder: {job.data['message']}",
        reply_markup=keyboards.reminder_fired_keyboard(reminder_id),
    )
    database.mark_reminder_fired(reminder_id)


def reschedule_pending_reminders(job_queue: JobQueue) -> None:
    """Call once at bot startup.

    JobQueue's in-memory scheduler does not persist across process restarts,
    but SQLite is the source of truth, so on startup every pending,
    still-future reminder is re-read from the DB and re-registered as a job.
    """
    pending = database.get_pending_future_reminders()
    for reminder in pending:
        schedule_reminder(
            job_queue,
            reminder["id"],
            reminder["chat_id"],
            parse_iso_datetime(reminder["trigger_data"]),
            reminder["message"],
        )
    if pending:
        logger.info("Rescheduled %d pending reminder(s) on startup", len(pending))
