"""Slash-command handlers and the reply-keyboard button router for
Kangani's navigation layer.

Each view is split into a `build_xxx_view(chat_id) -> (text, keyboard)`
function and a thin handler that sends it. `callbacks.py` reuses the same
`build_xxx_view` functions when editing a message in place (a callback query
needs `edit_message_text`, not `reply_text`), so a button tap and its
slash-command equivalent always render identically -- one query, one
keyboard build, multiple thin entry points.
"""

import os
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

import database
import keyboards
import scheduler


def _container_label(row: dict) -> str:
    return row["module_name"] if row["module_name"] is not None else row["event_title"]


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
    if not topics:
        return (
            "You don't have any topics yet. Create one by telling me about "
            "something you want to track.",
            None,
        )
    modules_seen: dict[int, str] = {}
    for t in topics:
        modules_seen.setdefault(t["module_id"], t["module_name"])
    modules = [{"id": mid, "name": name} for mid, name in modules_seen.items()]
    return "Choose a module to browse its topics:", keyboards.topic_root_keyboard(
        modules
    )


def build_topics_module_view(
    chat_id: int, module_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    topics = database.list_topics(chat_id)
    module_topics = [
        t for t in topics if t["module_id"] == module_id and t["parent_topic_id"] is None
    ]
    if not module_topics:
        return "No top-level topics in this module yet.", keyboards.topic_module_keyboard(
            []
        )
    module_name = module_topics[0]["module_name"]
    return (
        f"Topics in {module_name}:",
        keyboards.topic_module_keyboard(module_topics),
    )


def build_topics_detail_view(
    chat_id: int, topic_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    topics = database.list_topics(chat_id)
    by_id = {t["id"]: t for t in topics}
    topic = by_id.get(topic_id)
    if topic is None:
        return "That topic no longer exists.", None
    subtopics = [t for t in topics if t["parent_topic_id"] == topic_id]
    if topic["parent_topic_id"] is not None:
        back_target = f"topic:open:{topic['parent_topic_id']}"
    elif topic["module_id"] is not None:
        back_target = f"topic:mod:{topic['module_id']}"
    else:
        back_target = f"event:open:{topic['event_id']}"
    return topic["path"], keyboards.topic_detail_keyboard(
        topic_id, subtopics, back_target
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


def build_notes_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    notes = database.query_notes(chat_id=chat_id, limit=20)
    if not notes:
        return "You don't have any notes yet.", None
    lines = []
    for n in notes:
        tag = "[reference] " if n["is_reference"] else ""
        source_part = f" (source: {n['source']})" if n["source"] else ""
        lines.append(
            f"#{n['id']} [{_container_label(n)} > {n['topic_name']}] "
            f"{tag}{n['content']}{source_part}"
        )
    return "\n".join(lines), None


def build_events_root_view(chat_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
    events = database.query_events(chat_id, upcoming_only=True)
    if not events:
        return "You don't have any upcoming events yet.", None
    return "Choose an event:", keyboards.event_list_keyboard(events)


def build_events_detail_view(
    chat_id: int, event_id: int
) -> tuple[str, InlineKeyboardMarkup | None]:
    event = database.get_event(chat_id, event_id)
    if event is None:
        return "That event no longer exists.", None

    tasks = database.query_tasks(chat_id=chat_id, event_id=event_id)
    topics = [
        t
        for t in database.list_topics(chat_id)
        if t["event_id"] == event_id and t["parent_topic_id"] is None
    ]

    lines = [f"{event['title']} ({event['type']})"]
    if event["start_date"]:
        date_part = event["start_date"]
        if event["end_date"]:
            date_part += f" to {event['end_date']}"
        lines.append(date_part)
    if event["location"]:
        lines.append(f"Location: {event['location']}")
    text = "\n".join(lines)

    return text, keyboards.event_detail_keyboard(event_id, tasks, topics)


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
        "I'm KANGĀNi, your personal assistant.\n\n"
        "You can just talk to me naturally -- e.g. \"create a task to finish "
        "the report by Friday\", \"remind me to call mom at 6pm\", \"note "
        "under Backpropagation: ...\", or \"what's due this week?\".\n\n"
        "Or use the menu buttons / commands below for quick navigation:\n"
        f"{keyboards.TASKS_LABEL} / /tasks -- view and update your tasks\n"
        f"{keyboards.REMINDERS_LABEL} / /reminders -- view and cancel upcoming reminders\n"
        f"{keyboards.TOPICS_LABEL} / /topics -- browse your topics and notes\n"
        f"{keyboards.NOTES_LABEL} / /notes -- view your recent notes\n"
        f"{keyboards.EVENTS_LABEL} / /events -- browse your events\n"
        "/menu -- show this menu again\n"
        "/settings -- view your current settings"
    )
    await update.message.reply_text(text)


async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    tz_name = os.environ.get("TIMEZONE", "UTC")
    text = (
        f"Timezone: {tz_name} (server-configured)\n\n"
        "There are no user-editable settings yet."
    )
    await update.message.reply_text(text)


async def nav_button_pressed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
