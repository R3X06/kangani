"""Time-based reminder delivery, built on python-telegram-bot's JobQueue.

JobQueue wraps APScheduler's AsyncIOScheduler running on the bot's own
asyncio event loop, so firing a reminder is just a normal awaited call to
context.bot.send_message -- no thread bridging needed.
"""

import logging
import os
from datetime import date, datetime, timedelta, timezone
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


# Number of official teaching weeks in a semester -- the span a real,
# recess-adjusted week number must land in to be meaningful.
_MAX_SEMESTER_WEEK = 13


def _raw_week_number(anchor_date: date, target_date: date) -> int | None:
    """Continuous week count since the anchor: 7-day blocks from the anchor
    (day 0 -> week 1), uncapped and with NO recess adjustment. None if
    target_date is before the anchor.

    This is the raw calendar count recess weeks are themselves stored in terms
    of, so it must never subtract them -- otherwise the stored recess numbers
    and the counting they're compared against would drift apart.
    """
    if target_date < anchor_date:
        return None
    return ((target_date - anchor_date).days // 7) + 1


def compute_week_number(
    anchor_date: date, target_date: date, recess_weeks: frozenset[int] = frozenset()
) -> int | None:
    """Official (recess-adjusted) semester week number of target_date.

    Starts from the raw continuous count, then:
    - returns None if target_date is before the anchor, or its raw week IS a
      recess week (a recess week has no official teaching-week number);
    - otherwise subtracts however many recess weeks fall strictly before it
      (each one shifts the official numbering down by one from there on);
    - returns that only if it lands in 1..13, else None (pre-/post-semester).

    This is what /week N, week_pattern, and everything user-facing means by
    "week N": the school's official numbering with recess weeks skipped.
    """
    raw = _raw_week_number(anchor_date, target_date)
    if raw is None or raw in recess_weeks:
        return None
    official = raw - sum(1 for r in recess_weeks if r < raw)
    return official if 1 <= official <= _MAX_SEMESTER_WEEK else None


def official_to_continuous(
    official_week: int, recess_weeks: frozenset[int] = frozenset()
) -> int:
    """Inverse of compute_week_number: the continuous week that carries a given
    official week number.

    Recess weeks push everything after them one slot later, so the continuous
    week is the official number plus however many recess weeks fall at or
    before it. That's self-referential (adding a recess week can pull another
    recess week into range), so iterate to a fixed point -- recess sets are
    tiny, so this settles in a couple of passes.
    """
    continuous = official_week
    while True:
        shifted = official_week + sum(1 for r in recess_weeks if r <= continuous)
        if shifted == continuous:
            return continuous
        continuous = shifted


WEEKDAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]


def resolve_week_range(
    anchor_date: date | None,
    today: date,
    week_number: int | None,
    recess_weeks: frozenset[int] = frozenset(),
) -> tuple[date, date, int | None]:
    """Resolve a (monday, sunday, week_label) triple for a week view.

    week_number here is an OFFICIAL week number, so it's converted to its
    continuous count via official_to_continuous before deriving the Monday --
    that's what makes /week N land on the right calendar week once recess weeks
    have shifted the numbering. An explicit week only makes sense with an
    anchor set, so anchor_date=None raises AnchorNotSetError. With
    week_number=None it's the Monday..Sunday containing `today`, labelled with
    that week's official (recess-aware) number if an anchor exists (else None).

    Shared by commands.build_week_view and timetable_data.build_weekly_context
    so a /week text view and its rendered image always cover the same range.
    """
    if week_number is not None:
        if anchor_date is None:
            raise AnchorNotSetError(ANCHOR_NOT_SET_MESSAGE)
        continuous = official_to_continuous(week_number, recess_weeks)
        monday = anchor_date + timedelta(days=7 * (continuous - 1))
        wk_label: int | None = week_number
    else:
        monday = today - timedelta(days=today.weekday())  # Monday of this week
        wk_label = (
            compute_week_number(anchor_date, monday, recess_weeks)
            if anchor_date
            else None
        )
    return monday, monday + timedelta(days=6), wk_label

ANCHOR_NOT_SET_MESSAGE = (
    "Set your semester start date first — tell me which date week 1 begins."
)


class AnchorNotSetError(Exception):
    """Raised when a non-'every' schedule block must be resolved to a semester
    week but no anchor is set for the chat yet -- so callers can surface a clear
    prompt instead of silently showing or hiding the block."""


def week_matches(week_pattern: str, week_number: int) -> bool:
    """Whether a resolved semester week number satisfies a block's week_pattern.
    Assumes week_pattern is already valid (validated at create time)."""
    if week_pattern == "every":
        return True
    if week_pattern == "odd":
        return week_number % 2 == 1
    if week_pattern == "even":
        return week_number % 2 == 0
    return week_number in {int(p) for p in week_pattern.split(",")}


def expand_occurrences(
    blocks: list[dict],
    date_from: str,
    date_to: str,
    anchor_date: date | None,
    recess_weeks: frozenset[int] = frozenset(),
) -> list[dict]:
    """Expand recurring/one-off schedule blocks into concrete dated occurrences
    within [date_from, date_to], applying each block's week_pattern.

    The semester anchor and recess-week set are passed in (resolved once by the
    caller) rather than re-queried here. 'every' means "every official teaching
    week", so once an anchor is set, any date whose official week is None --
    pre-semester, post-semester, OR a recess week -- shows nothing regardless of
    pattern. Only when no anchor is set at all do we skip the bounds check (an
    'every' block still shows; an odd/even/explicit-list block is genuinely
    unresolvable, so it raises AnchorNotSetError).
    """
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)

    def _keep(candidate_date: date, block: dict) -> bool:
        pattern = block.get("week_pattern") or "every"
        if anchor_date is None:
            # No anchor yet -- nothing to bound 'every' against, so it shows
            # unconditionally; but odd/even/explicit-list patterns are genuinely
            # unresolvable without an anchor, so those still raise.
            if pattern == "every":
                return True
            raise AnchorNotSetError(ANCHOR_NOT_SET_MESSAGE)
        wk = compute_week_number(anchor_date, candidate_date, recess_weeks)
        if wk is None:  # pre-/post-semester or a recess week -> doesn't run
            return False
        if pattern == "every":
            return True
        return week_matches(pattern, wk)

    occurrences = []
    for block in blocks:
        if block["specific_date"] is not None:
            block_date = date.fromisoformat(block["specific_date"])
            if d_from <= block_date <= d_to and _keep(block_date, block):
                occurrences.append({**block, "occurrence_date": block["specific_date"]})
        else:
            d = d_from
            while d <= d_to:
                if WEEKDAY_CODES[d.weekday()] == block["day_of_week"] and _keep(d, block):
                    occurrences.append({**block, "occurrence_date": d.isoformat()})
                d += timedelta(days=1)
    occurrences.sort(key=lambda o: (o["occurrence_date"], o["start_time"]))
    return occurrences


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
