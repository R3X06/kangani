"""Button-driven quick-add flows for tasks, reminders, and notes.

Lets the person create these three highest-frequency write actions without
going through brain.get_response() at all -- no Claude call, no API cost.
State machine lives in context.chat_data["flow"], mirroring the pending-reply
pattern already used by pdf_import.py, file_storage.py, and
callbacks.handle_topic_edit_reply: a callback tap issues a prompt and stashes
what's being waited for; a following plain-text message is intercepted in
bot.py's message_handler (checked there, since the AI tool loop can't reach
context.chat_data) before it would otherwise reach Claude.

Design choice: buttons cover every decision that has a bounded set of
options (topic, deadline, reminder time, category, reference flag). Only the
fields that are genuinely open-ended free text (a task title / note content
/ reminder message, and a "custom" date/time) fall back to a typed reply --
there's no sensible button set for those.
"""

import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.ext import ContextTypes

import database
import keyboards
import scheduler

logger = logging.getLogger(__name__)

# Telegram inline keyboards get unwieldy well before the platform's hard
# 100-button ceiling. Past this many topics, the picker falls back to a
# plain message rather than degrading into an unusably tall keyboard --
# natural language / /topics is the right tool for a very large tree anyway.
MAX_TOPIC_BUTTONS = 40

CUSTOM_DATETIME_HELP = (
    "Reply with a date (and optionally a time), 24-hour format:\n"
    "YYYY-MM-DD or YYYY-MM-DD HH:MM\n"
    "e.g. 2026-08-20 or 2026-08-20 18:00"
)


def _tz_name() -> str:
    return os.environ.get("TIMEZONE", "UTC")


def _now_local() -> datetime:
    return datetime.now(ZoneInfo(_tz_name()))


def _end_of_day_local(d: date) -> datetime:
    return datetime.combine(d, time(23, 59), tzinfo=ZoneInfo(_tz_name()))


def _parse_custom_local_datetime(text: str) -> datetime | None:
    """Accepts YYYY-MM-DD or YYYY-MM-DD HH:MM. A date with no time defaults
    to 23:59 local (end of that day) -- reasonable for both a deadline and a
    reminder, and keeps this one parser shared by both flows."""
    text = text.strip()
    for fmt, has_time in (("%Y-%m-%d %H:%M", True), ("%Y-%m-%d", False)):
        try:
            # Deliberately naive. The user types local
            # wall-clock time; both exits below attach the local zone before
            # this value escapes the function.
            naive = datetime.strptime(text, fmt)  # noqa: DTZ007
        except ValueError:
            continue
        if not has_time:
            return _end_of_day_local(naive.date())
        return naive.replace(tzinfo=ZoneInfo(_tz_name()))
    return None


def _deadline_preset_to_utc(code: str) -> str | None:
    now = _now_local()
    if code == "today":
        dt_local = _end_of_day_local(now.date())
    elif code == "tomorrow":
        dt_local = _end_of_day_local(now.date() + timedelta(days=1))
    elif code == "fri":
        days_ahead = (4 - now.weekday()) % 7  # Friday = weekday 4
        dt_local = _end_of_day_local(now.date() + timedelta(days=days_ahead))
    elif code == "none":
        return None
    else:
        raise ValueError(f"Unknown deadline preset {code!r}")
    return scheduler.format_utc_iso(dt_local)


def _remindtime_preset_to_local(code: str) -> datetime:
    now = _now_local()
    if code == "10m":
        return now + timedelta(minutes=10)
    if code == "1h":
        return now + timedelta(hours=1)
    if code == "3h":
        return now + timedelta(hours=3)
    if code == "tom9":
        return datetime.combine(
            (now + timedelta(days=1)).date(), time(9, 0), tzinfo=ZoneInfo(_tz_name())
        )
    raise ValueError(f"Unknown reminder-time preset {code!r}")


# --- state helpers --------------------------------------------------------

def _get_state(context: ContextTypes.DEFAULT_TYPE) -> dict | None:
    return context.chat_data.get("flow")


def _set_state(context: ContextTypes.DEFAULT_TYPE, kind: str, **data) -> dict:
    state = {"kind": kind, "awaiting": None, "data": data}
    context.chat_data["flow"] = state
    return state


def _clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("flow", None)


# --- entry point -----------------------------------------------------------

async def add_menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_state(context)
    await update.message.reply_text(
        "What would you like to add?", reply_markup=keyboards.flow_new_item_keyboard()
    )


# --- callback dispatch -----------------------------------------------------

async def flow_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        action = parts[1]

        if action == "cancel":
            _clear_state(context)
            await query.edit_message_text("Cancelled.")
            await query.answer()
            return

        if action == "new":
            await _start_flow(query, context, chat_id, parts)
            await query.answer()
            return

        state = _get_state(context)
        if state is None:
            # Stale keyboard from an already-finished/cancelled flow (e.g. the
            # process restarted, or the person tapped an old message).
            await query.answer("That's expired -- start again with ➕ Add.", show_alert=True)
            return

        if action == "topic":
            await _handle_topic_picked(query, context, chat_id, state, parts[2])
        elif action == "deadline":
            await _handle_deadline_picked(query, context, chat_id, state, parts[2])
        elif action == "remindtime":
            await _handle_remindtime_picked(query, context, chat_id, state, parts[2])
        elif action == "category":
            await _handle_category_picked(query, context, chat_id, state, parts[2])
        elif action == "ref":
            await _handle_reference_picked(query, context, chat_id, state, parts[2])
        else:
            await query.answer()
            return

        await query.answer()
    except (IndexError, ValueError, KeyError):
        logger.exception("flow_callback failed on data %r", query.data)
        await query.answer("Something went wrong with that button.", show_alert=True)


async def _start_flow(query, context, chat_id: int, parts: list[str]) -> None:
    kind = parts[2]
    pre_scoped_topic_id = int(parts[3]) if len(parts) >= 4 else None

    if kind == "reminder":
        _set_state(context, "reminder")
        state = _get_state(context)
        state["awaiting"] = "message"
        await query.message.reply_text("What should I remind you about? Reply with the text.")
        return

    _set_state(context, kind)
    if pre_scoped_topic_id is not None:
        _get_state(context)["data"]["topic_id"] = pre_scoped_topic_id
        await _prompt_for_free_text(query, context, kind)
        return

    topics = database.list_topics(chat_id)
    if len(topics) > MAX_TOPIC_BUTTONS:
        # Too many to button -- treat as unfiled rather than degrade into an
        # unusable keyboard; the person can move it afterward via /topics or
        # natural language.
        _get_state(context)["data"]["topic_id"] = None
        await query.edit_message_text(
            f"You have {len(topics)} topics -- too many to list here, so "
            "this will be unfiled. Move it afterward via /topics if needed."
        )
        await _prompt_for_free_text(query, context, kind)
        return

    await query.edit_message_text(
        "Which topic?", reply_markup=keyboards.flow_topic_picker_keyboard(topics)
    )


async def _prompt_for_free_text(query, context, kind: str) -> None:
    state = _get_state(context)
    if kind == "task":
        state["awaiting"] = "title"
        await query.message.reply_text("What's the task? Reply with the title.")
    else:  # note
        state["awaiting"] = "content"
        await query.message.reply_text("What's the note? Reply with the content.")


async def _handle_topic_picked(query, context, chat_id: int, state: dict, raw: str) -> None:
    if raw == "none":
        state["data"]["topic_id"] = None
        label = "unfiled"
    else:
        topic_id = int(raw)
        topic = database.get_topic(chat_id, topic_id)
        state["data"]["topic_id"] = topic_id
        label = topic["name"] if topic else "that topic"
    await query.edit_message_text(f"Topic: {label}")
    await _prompt_for_free_text(query, context, state["kind"])


async def _handle_deadline_picked(query, context, chat_id: int, state: dict, code: str) -> None:
    if code == "custom":
        state["awaiting"] = "custom_deadline"
        await query.message.reply_text(CUSTOM_DATETIME_HELP)
        return
    state["data"]["deadline"] = _deadline_preset_to_utc(code)
    await _prompt_for_category(query, context, chat_id, state)


async def _prompt_for_category(query, context, chat_id: int, state: dict) -> None:
    categories = database.list_task_categories(chat_id)
    state["data"]["_category_options"] = categories
    if not categories:
        # Nothing to pick from yet -- skip straight to creation rather than
        # showing a keyboard with only "No category" on it.
        await _finalize_task(query, context, chat_id, state, category=None)
        return
    await query.edit_message_text(
        "Category?", reply_markup=keyboards.flow_category_keyboard(categories)
    )


async def _handle_category_picked(query, context, chat_id: int, state: dict, raw: str) -> None:
    category = None
    if raw != "none":
        options = state["data"].get("_category_options", [])
        idx = int(raw)
        category = options[idx] if 0 <= idx < len(options) else None
    await _finalize_task(query, context, chat_id, state, category=category)


async def _finalize_task(query, context, chat_id: int, state: dict, category: str | None) -> None:
    data = state["data"]
    task = database.create_task(
        chat_id=chat_id,
        title=data["title"],
        topic_id=data.get("topic_id"),
        category=category,
        deadline=data.get("deadline"),
    )
    _clear_state(context)
    deadline_part = f", due {task['deadline']}" if task["deadline"] else ""
    where = task.get("topic_name") or "unfiled"
    await query.edit_message_text(
        f"✅ Created task #{task['id']} '{task['title']}' under '{where}'{deadline_part}.",
        reply_markup=keyboards.task_list_keyboard([task]),
    )


async def _handle_remindtime_picked(query, context, chat_id: int, state: dict, code: str) -> None:
    if code == "custom":
        state["awaiting"] = "custom_remindtime"
        await query.message.reply_text(CUSTOM_DATETIME_HELP)
        return
    trigger_local = _remindtime_preset_to_local(code)
    await _finalize_reminder(query, context, chat_id, state, trigger_local)


async def _finalize_reminder(query, context, chat_id: int, state: dict, trigger_local: datetime) -> None:
    trigger_utc = trigger_local.astimezone(timezone.utc)
    reminder = database.create_reminder(
        chat_id=chat_id,
        trigger_datetime_utc=scheduler.format_utc_iso(trigger_utc),
        message=state["data"]["message"],
    )
    scheduler.schedule_reminder(
        context.job_queue, reminder["id"], chat_id, trigger_utc, reminder["message"]
    )
    _clear_state(context)
    local_str = trigger_local.strftime("%Y-%m-%d %H:%M")
    await query.edit_message_text(
        f"⏰ Reminder set for {local_str} ({_tz_name()}): '{reminder['message']}'.",
        reply_markup=keyboards.reminder_list_keyboard([reminder]),
    )


async def _handle_reference_picked(query, context, chat_id: int, state: dict, raw: str) -> None:
    data = state["data"]
    note = database.create_note(
        chat_id=chat_id,
        content=data["content"],
        topic_id=data.get("topic_id"),
        is_reference=(raw == "yes"),
    )
    _clear_state(context)
    tag = " (reference)" if note["is_reference"] else ""
    where = note.get("topic_name") or "unfiled"
    kb = None
    if note.get("topic_id") is not None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"📚 View {where}", callback_data=f"topic:open:{note['topic_id']}")]]
        )
    await query.edit_message_text(
        f"📝 Saved note under '{where}'{tag}.", reply_markup=kb
    )


# --- free-text reply interceptor -------------------------------------------

async def handle_flow_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Intercept a plain-text reply that answers a pending flow prompt
    (title/content/message, or a custom date/time). Returns True if
    consumed -- checked in bot.py's message_handler BEFORE brain.get_response,
    so none of this ever costs a Claude call."""
    state = _get_state(context)
    if state is None or state.get("awaiting") is None:
        return False

    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()
    awaiting = state["awaiting"]

    if keyboards.is_cancel_reply(text):
        _clear_state(context)
        await update.message.reply_text("Cancelled — nothing was saved.")
        return True

    if not text:
        await update.message.reply_text("That was empty -- try again, or tap ❌ Cancel above.")
        return True

    if awaiting == "title":
        state["data"]["title"] = text
        state["awaiting"] = None
        await update.message.reply_text(
            "Deadline?", reply_markup=keyboards.flow_deadline_keyboard()
        )
        return True

    if awaiting == "content":
        state["data"]["content"] = text
        state["awaiting"] = None
        await update.message.reply_text(
            "Reference material, or a regular note?",
            reply_markup=keyboards.flow_reference_keyboard(),
        )
        return True

    if awaiting == "message":
        state["data"]["message"] = text
        state["awaiting"] = None
        await update.message.reply_text(
            "When should I remind you?", reply_markup=keyboards.flow_remindtime_keyboard()
        )
        return True

    if awaiting == "custom_deadline":
        parsed = _parse_custom_local_datetime(text)
        if parsed is None:
            await update.message.reply_text("Didn't understand that. " + CUSTOM_DATETIME_HELP)
            return True
        state["data"]["deadline"] = scheduler.format_utc_iso(parsed)
        state["awaiting"] = None
        categories = database.list_task_categories(chat_id)
        state["data"]["_category_options"] = categories
        if not categories:
            await _finalize_task_plain(update, context, chat_id, state, category=None)
        else:
            await update.message.reply_text(
                "Category?", reply_markup=keyboards.flow_category_keyboard(categories)
            )
        return True

    if awaiting == "custom_remindtime":
        parsed = _parse_custom_local_datetime(text)
        if parsed is None:
            await update.message.reply_text("Didn't understand that. " + CUSTOM_DATETIME_HELP)
            return True
        if parsed <= _now_local():
            await update.message.reply_text(
                "That's in the past -- reply with a future date/time, or tap ❌ Cancel above."
            )
            return True
        state["awaiting"] = None
        await _finalize_reminder_plain(update, context, chat_id, state, parsed)
        return True

    return False


# --- plain-message variants of the finalize helpers -------------------
# Mirror _finalize_task / _finalize_reminder but send a fresh message
# (update.message.reply_text) instead of editing a callback's message --
# reached from the custom-date free-text path, where there's no callback
# query to edit.

async def _finalize_task_plain(update, context, chat_id: int, state: dict, category: str | None) -> None:
    data = state["data"]
    task = database.create_task(
        chat_id=chat_id,
        title=data["title"],
        topic_id=data.get("topic_id"),
        category=category,
        deadline=data.get("deadline"),
    )
    _clear_state(context)
    deadline_part = f", due {task['deadline']}" if task["deadline"] else ""
    where = task.get("topic_name") or "unfiled"
    await update.message.reply_text(
        f"✅ Created task #{task['id']} '{task['title']}' under '{where}'{deadline_part}.",
        reply_markup=keyboards.task_list_keyboard([task]),
    )


async def _finalize_reminder_plain(update, context, chat_id: int, state: dict, trigger_local: datetime) -> None:
    trigger_utc = trigger_local.astimezone(timezone.utc)
    reminder = database.create_reminder(
        chat_id=chat_id,
        trigger_datetime_utc=scheduler.format_utc_iso(trigger_utc),
        message=state["data"]["message"],
    )
    scheduler.schedule_reminder(
        context.job_queue, reminder["id"], chat_id, trigger_utc, reminder["message"]
    )
    _clear_state(context)
    local_str = trigger_local.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text(
        f"⏰ Reminder set for {local_str} ({_tz_name()}): '{reminder['message']}'.",
        reply_markup=keyboards.reminder_list_keyboard([reminder]),
    )