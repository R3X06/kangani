"""Slash-command handlers and the reply-keyboard button router for
Kangani's navigation layer.

Each view is split into a `build_xxx_view(chat_id) -> (text, keyboard)`
function and a thin handler that sends it. `callbacks.py` reuses the same
`build_xxx_view` functions when editing a message in place (a callback query
needs `edit_message_text`, not `reply_text`), so a button tap and its
slash-command equivalent always render identically -- one query, one
keyboard build, multiple thin entry points.
"""

import calendar
import html
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
import keyboards
import scheduler
import timetable_image


def _container_label(row: dict) -> str:
    """A task's attachment label: its topic name, or 'unfiled' if unattached."""
    return row.get("topic_name") or "unfiled"


def build_tasks_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    tasks = database.query_tasks(chat_id=chat_id)
    if not tasks:
        return "You don't have any tasks yet.", None
    lines = []
    for t in tasks:
        deadline_part = f", due {t['deadline']}" if t["deadline"] else ""
        lines.append(
            f"#{t['id']} [{_container_label(t)}] {t['title']} — "
            f"{t['status']}, {t['progress_pct']}%{deadline_part}"
        )
    return "\n".join(lines), keyboards.task_list_keyboard(tasks)


def build_reminders_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    reminders = database.list_pending_reminders(chat_id)
    if not reminders:
        return "You don't have any upcoming reminders.", None
    tz_name = os.environ.get("TIMEZONE", "UTC")
    lines = []
    for r in reminders:
        local_dt = scheduler.parse_iso_datetime(r["trigger_data"]).astimezone(
            ZoneInfo(tz_name)
        )
        lines.append(
            f"#{r['id']} {local_dt.strftime('%Y-%m-%d %H:%M %z')}: {r['message']}"
        )
    return "\n".join(lines), keyboards.reminder_list_keyboard(reminders)


def build_topics_root_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    topics = database.list_topics(chat_id)
    roots = [t for t in topics if t["parent_topic_id"] is None]
    if not roots:
        return (
            "You don't have any topics yet. Create one by telling me about "
            "something you want to track.",
            None,
        )
    return "Choose a topic to browse:", keyboards.topic_root_keyboard(roots)


def build_topics_detail_view(
    chat_id: int, topic_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    topics = database.list_topics(chat_id)
    by_id = {t["id"]: t for t in topics}
    topic = by_id.get(topic_id)
    if topic is None:
        return "That topic no longer exists.", None
    subtopics = [t for t in topics if t["parent_topic_id"] == topic_id]
    counts = database.get_topic_counts(chat_id, topic_id)

    lines = [topic["path"]]
    meta = []
    if topic.get("kind"):
        meta.append(topic["kind"])
    if topic.get("full_name"):
        meta.append(topic["full_name"])
    if topic.get("nickname"):
        meta.append(f"aka {topic['nickname']}")
    if meta:
        lines.append(" · ".join(meta))
    text = "\n".join(lines)

    if topic["parent_topic_id"] is not None:
        back_target = f"topic:open:{topic['parent_topic_id']}"
    else:
        back_target = "topic:root"
    return text, keyboards.topic_detail_keyboard(
        topic_id, subtopics, counts, back_target
    )


def build_topics_notes_view(
    chat_id: int, topic_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    topics = database.list_topics(chat_id)
    if not any(t["id"] == topic_id for t in topics):
        return "That topic no longer exists.", None

    notes = database.query_notes(chat_id=chat_id, topic_id=topic_id)
    if not notes:
        text = "No notes on this topic yet."
    else:
        lines = []
        for n in notes:
            tag = "[reference] " if n["is_reference"] else ""
            source_part = f" (source: {n['source']})" if n["source"] else ""
            lines.append(f"{tag}{n['content']}{source_part}")
        text = "\n".join(lines)
    return text, keyboards.topic_notes_keyboard(topic_id)


def build_topic_files_view(
    chat_id: int, topic_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    topics = database.list_topics(chat_id)
    if not any(t["id"] == topic_id for t in topics):
        return "That topic no longer exists.", None
    files = database.list_files(chat_id=chat_id, topic_id=topic_id)
    if not files:
        text = "No files under this topic yet. Send me a document to file here."
    else:
        lines = []
        for f in files:
            name = f.get("nickname") or f.get("file_name") or "file"
            lines.append(f"#{f['id']} {name}")
        text = "Files under this topic (and below):\n" + "\n".join(lines)
    return text, keyboards.topic_files_keyboard(topic_id, files)


def build_topic_delete_confirm_view(
    chat_id: int, topic_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    topic = database.get_topic(chat_id, topic_id)
    if topic is None:
        return "That topic no longer exists.", None
    counts = database.get_topic_counts(chat_id, topic_id)
    bits = [f"{v} {k}" for k, v in counts.items() if v]
    detail = ("This also deletes everything under it: " + ", ".join(bits) + "."
              if bits else "It has nothing else under it.")
    text = (
        f"Delete '{topic['name']}'?\n{detail}\nThis cannot be undone."
    )
    return text, keyboards.topic_delete_confirm_keyboard(topic_id)


def build_notes_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    notes = database.query_notes(chat_id=chat_id, limit=20)
    if not notes:
        return "You don't have any notes yet.", None
    lines = []
    for n in notes:
        tag = "[reference] " if n["is_reference"] else ""
        source_part = f" (source: {n['source']})" if n["source"] else ""
        where = n["topic_name"] or "general"
        lines.append(
            f"#{n['id']} [{where}] {tag}{n['content']}{source_part}"
        )
    return "\n".join(lines), None


def build_events_root_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    events = database.list_event_topics(chat_id, upcoming_only=True)
    if not events:
        return "You don't have any upcoming events yet.", None
    return "Choose an event:", keyboards.event_list_keyboard(events)


def build_events_detail_view(
    chat_id: int, topic_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    # "Events" are topics with kind 'event[:type]' and an event_datetime.
    event = database.get_topic(chat_id, topic_id)
    if event is None or not (event["kind"] or "").casefold().startswith("event"):
        return "That event no longer exists.", None

    tasks = database.query_tasks(chat_id=chat_id, topic_id=topic_id)
    subtopics = [
        t
        for t in database.list_topics(chat_id)
        if t["parent_topic_id"] == topic_id
    ]

    lines = [event["name"]]
    if event["event_datetime"]:
        tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
        local = scheduler.parse_iso_datetime(event["event_datetime"]).astimezone(tz)
        lines.append(local.strftime("%a %d %b %Y, %H:%M"))
    if event["status"]:
        lines.append(f"Status: {event['status']}")
    text = "\n".join(lines)

    return text, keyboards.event_detail_keyboard(topic_id, tasks, subtopics)


# --- /today and /week -----------------------------------------------------
#
# These render a single monospace Telegram message wrapped in <pre>...</pre>
# (parse_mode="HTML"), so EVERY piece of dynamic text (module/class titles,
# locations, task titles, reminder messages) must be HTML-escaped before
# interpolation -- a raw <, >, or & in user content would otherwise break
# Telegram's HTML parser.

_MAX_SEMESTER_WEEK = 13


def _class_row(occ: dict, label_format: str) -> str:
    """One indented class line: 'HH:MM Title - Location' (location dropped when
    absent). Title is the resolved module label plus class_type when set. Fully
    escaped."""
    label = database.resolve_label(occ, label_format)
    title_bits = [b for b in (label, occ.get("class_type")) if b]
    title = " ".join(title_bits) or "Block"
    row = f"{occ['start_time']} {title}"
    if occ.get("location"):
        row += f" - {occ['location']}"
    return html.escape(row)


def _wrap_pre(body: str) -> str:
    return f"<pre>{body}</pre>"


def build_today_view(chat_id: int) -> str:
    tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
    today = datetime.now(tz).date()
    today_iso = today.isoformat()
    label_format = database.get_timetable_label_format(chat_id)

    anchor = database.get_semester_anchor(chat_id)
    anchor_date = date.fromisoformat(anchor) if anchor else None
    recess = frozenset(database.get_recess_weeks(chat_id))
    wk = (
        scheduler.compute_week_number(anchor_date, today, recess)
        if anchor_date
        else None
    )
    header = f"Today — {today.strftime('%A %d %b')}"
    if wk is not None:
        header += f"  ·  Week {wk}"
    lines = [header, ""]

    # Section 1: classes (always shown -- the primary reason to run /today).
    blocks = database.list_schedule_blocks(
        chat_id=chat_id, date_from=today_iso, date_to=today_iso
    )
    lines.append("Classes")
    try:
        occ = scheduler.expand_occurrences(
            blocks, today_iso, today_iso, anchor_date, recess
        )
        if occ:
            lines.extend(f"  {_class_row(o, label_format)}" for o in occ)
        else:
            lines.append("  No classes today")
    except scheduler.AnchorNotSetError:
        lines.append(f"  {html.escape(scheduler.ANCHOR_NOT_SET_MESSAGE)}")

    # Section 2: tasks due today (omitted entirely when empty).
    start_utc = scheduler.format_utc_iso(datetime.combine(today, time.min, tzinfo=tz))
    end_utc = scheduler.format_utc_iso(datetime.combine(today, time.max, tzinfo=tz))
    tasks = database.query_tasks(
        chat_id=chat_id, deadline_from=start_utc, deadline_to=end_utc
    )
    if tasks:
        lines += ["", "Due today"]
        for t in tasks:
            lines.append(html.escape(f"  {t['title']} — {_container_label(t)}"))

    # Section 3: reminders whose trigger time falls today (omitted when empty).
    today_reminders = []
    for r in database.list_pending_reminders(chat_id):
        local_dt = scheduler.parse_iso_datetime(r["trigger_data"]).astimezone(tz)
        if local_dt.date() == today:
            today_reminders.append((local_dt, r))
    if today_reminders:
        lines += ["", "Reminders"]
        for local_dt, r in today_reminders:
            lines.append(html.escape(f"  {local_dt.strftime('%H:%M')} {r['message']}"))

    return _wrap_pre("\n".join(lines))


def build_week_view(chat_id: int, week_number: int | None = None) -> str:
    tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
    today = datetime.now(tz).date()
    label_format = database.get_timetable_label_format(chat_id)
    anchor = database.get_semester_anchor(chat_id)
    anchor_date = date.fromisoformat(anchor) if anchor else None
    recess = frozenset(database.get_recess_weeks(chat_id))

    try:
        monday, sunday, wk_label = scheduler.resolve_week_range(
            anchor_date, today, week_number, recess
        )
    except scheduler.AnchorNotSetError:
        # An explicit week only makes sense relative to the anchor.
        return _wrap_pre(html.escape(scheduler.ANCHOR_NOT_SET_MESSAGE))

    range_str = f"{monday.strftime('%d %b')} – {sunday.strftime('%d %b')}"
    header = f"Week {wk_label}  ({range_str})" if wk_label is not None else range_str

    try:
        occ = scheduler.expand_occurrences(
            database.list_schedule_blocks(
                chat_id=chat_id, date_from=monday.isoformat(), date_to=sunday.isoformat()
            ),
            monday.isoformat(),
            sunday.isoformat(),
            anchor_date,
            recess,
        )
    except scheduler.AnchorNotSetError:
        return _wrap_pre(html.escape(scheduler.ANCHOR_NOT_SET_MESSAGE))

    by_day: dict[str, list[dict]] = {}
    for o in occ:  # already sorted by (occurrence_date, start_time)
        by_day.setdefault(o["occurrence_date"], []).append(o)

    lines = [header, ""]
    for i in range(7):
        d = monday + timedelta(days=i)
        lines.append(d.strftime("%A %d %b"))
        day_occ = by_day.get(d.isoformat())
        if day_occ:
            lines.extend(f"  {_class_row(o, label_format)}" for o in day_occ)
        else:
            lines.append("  — nothing")

    return _wrap_pre("\n".join(lines))


async def today_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = build_today_view(update.effective_chat.id)
    await update.message.reply_text(text, parse_mode="HTML")


async def week_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    week_number = None
    if context.args:
        try:
            week_number = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Usage: /week [week number], e.g. /week 3")
            return
        if not 1 <= week_number <= _MAX_SEMESTER_WEEK:
            await update.message.reply_text(
                f"Week number must be between 1 and {_MAX_SEMESTER_WEEK}."
            )
            return
    text = build_week_view(update.effective_chat.id, week_number)
    await update.message.reply_text(text, parse_mode="HTML")


# --- image variants of /today, /week, plus /monthimage --------------------
#
# These reuse the same context builders (timetable_data) and render them to PNG
# via the shared Playwright browser stashed in bot_data at startup. Only the
# AnchorNotSetError case falls back to text; the templates themselves carry
# graceful empty states ("~ no classes today ~", "~ free day ~", "No modules
# scheduled this month"), so an otherwise-empty period still renders a proper
# image rather than a blank one.

_IMAGE_UNAVAILABLE_MESSAGE = (
    "Image rendering isn't available right now — try the text view instead."
)


def _parse_month_arg(arg: str) -> int | None:
    """A month number (1-12) or a full/abbreviated month name, or None if
    neither. Case-insensitive."""
    arg = arg.strip()
    if arg.isdigit():
        m = int(arg)
        return m if 1 <= m <= 12 else None
    low = arg.lower()
    for i in range(1, 13):
        if low in (calendar.month_name[i].lower(), calendar.month_abbr[i].lower()):
            return i
    return None


async def dayimage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    browser = context.bot_data.get("browser")
    if browser is None:
        await update.message.reply_text(_IMAGE_UNAVAILABLE_MESSAGE)
        return
    tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
    target = datetime.now(tz).date()
    try:
        png = await timetable_image.render_daily_image(browser, chat_id, target)
    except scheduler.AnchorNotSetError:
        await update.message.reply_text(scheduler.ANCHOR_NOT_SET_MESSAGE)
        return
    await context.bot.send_photo(chat_id, photo=png)


async def weekimage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    browser = context.bot_data.get("browser")
    if browser is None:
        await update.message.reply_text(_IMAGE_UNAVAILABLE_MESSAGE)
        return
    week_number = None
    if context.args:
        try:
            week_number = int(context.args[0])
        except ValueError:
            await update.message.reply_text(
                "Usage: /weekimage [week number], e.g. /weekimage 3"
            )
            return
        if not 1 <= week_number <= _MAX_SEMESTER_WEEK:
            await update.message.reply_text(
                f"Week number must be between 1 and {_MAX_SEMESTER_WEEK}."
            )
            return
    try:
        png = await timetable_image.render_weekly_image(browser, chat_id, week_number)
    except scheduler.AnchorNotSetError:
        await update.message.reply_text(scheduler.ANCHOR_NOT_SET_MESSAGE)
        return
    await context.bot.send_photo(chat_id, photo=png)


async def monthimage_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    browser = context.bot_data.get("browser")
    if browser is None:
        await update.message.reply_text(_IMAGE_UNAVAILABLE_MESSAGE)
        return
    tz = ZoneInfo(os.environ.get("TIMEZONE", "UTC"))
    today = datetime.now(tz).date()
    year, month = today.year, today.month
    if context.args:
        parsed = _parse_month_arg(context.args[0])
        if parsed is None:
            await update.message.reply_text(
                "Usage: /monthimage [month], e.g. /monthimage 9 or /monthimage September"
            )
            return
        month = parsed
    try:
        png = await timetable_image.render_monthly_image(browser, chat_id, year, month)
    except scheduler.AnchorNotSetError:
        await update.message.reply_text(scheduler.ANCHOR_NOT_SET_MESSAGE)
        return
    await context.bot.send_photo(chat_id, photo=png)


async def tasks_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_tasks_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb)


async def reminders_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_reminders_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb)


async def topics_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_topics_root_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb)


async def notes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_notes_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb)


async def events_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, kb = build_events_root_view(update.effective_chat.id)
    await update.message.reply_text(text, reply_markup=kb)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Here's your menu:", reply_markup=keyboards.persistent_reply_keyboard()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "I'm KANGĀNi, your personal assistant. Talk to me naturally -- I sort "
        "out where things go.\n\n"
        "CAPTURE\n"
        "\"add a task to finish the report by Friday\"\n"
        "\"remind me to call mom at 6pm\"\n"
        "\"note under Backpropagation: chain rule intuition\"\n"
        "\"SC2001 lecture Mondays 9-11am at LT1\"\n"
        "Or drop in your NTU registration PDF and I'll read the timetable.\n\n"
        "ASK -- combine a topic with what you want to see:\n"
        "\"Y3S1 calendar\" -- everything under Y3S1 (lessons, tasks, reminders, notes)\n"
        "\"Y3S1 lesson calendar\" -- just the lessons under it\n"
        "\"SC2001 tutorials\" -- just SC2001's tutorials\n"
        "\"my labs this week\" -- every lab, this week\n"
        "\"what's due\" -- upcoming deadlines\n"
        "\"general reminders\" -- reminders not tied to anything\n\n"
        "HOW IT'S ORGANISED\n"
        "Everything lives in one tree of topics (year > semester > module, or "
        "any shape you like). Tasks, notes and reminders attach to a topic -- "
        "or to nothing, as a general item. Ask for a topic and I pull "
        "everything nested under it. Tasks and lessons can have categories "
        "(assignment, lab, tutorial...) so you can filter by them. Every item "
        "has a hidden tag -- add \"-tag\" to any listing to see them.\n\n"
        "QUICK NAV\n"
        "/today -- today's lessons, tasks and reminders\n"
        "/week -- this week's timetable (or /week 3 for a specific week)\n"
        "/dayimage /weekimage /monthimage -- the same, as a picture\n"
        f"{keyboards.TASKS_LABEL} / /tasks -- view and update tasks\n"
        f"{keyboards.REMINDERS_LABEL} / /reminders -- view and cancel reminders\n"
        f"{keyboards.TOPICS_LABEL} / /topics -- browse topics and notes\n"
        f"{keyboards.NOTES_LABEL} / /notes -- view recent notes\n"
        f"{keyboards.EVENTS_LABEL} / /events -- browse events (hackathons, talks)\n"
        "/menu -- show the menu again\n"
        "/settings -- view current settings"
    )
    await update.message.reply_text(text)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    tz_name = os.environ.get("TIMEZONE", "UTC")
    anchor = database.get_semester_anchor(chat_id)
    anchor_line = (
        f"Semester week 1 starts: {anchor}"
        if anchor
        else "Semester week 1 starts: not set (tell me which date week 1 begins)"
    )
    fmt = database.get_timetable_label_format(chat_id)
    recess = database.get_recess_weeks(chat_id)
    recess_line = (
        f"Recess weeks: {', '.join(str(w) for w in recess)}" if recess
        else "Recess weeks: none set"
    )
    text = (
        f"Timezone: {tz_name} (server-configured)\n"
        f"{anchor_line}\n"
        f"{recess_line}\n"
        f"Timetable labels: {fmt}\n\n"
        "Change any of these just by telling me (e.g. \"week 1 starts 12 Aug\", "
        "\"use nicknames on the timetable\")."
    )
    await update.message.reply_text(
        text, reply_markup=keyboards.settings_keyboard()
    )


async def nav_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        return
    label = update.message.text
    if label == keyboards.TASKS_LABEL:
        await tasks_command(update, context)
    elif label == keyboards.REMINDERS_LABEL:
        await reminders_command(update, context)
    elif label == keyboards.TOPICS_LABEL:
        await topics_command(update, context)
    elif label == keyboards.NOTES_LABEL:
        await notes_command(update, context)
    elif label == keyboards.EVENTS_LABEL:
        await events_command(update, context)