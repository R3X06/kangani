"""Reply-keyboard and inline-keyboard builders for Kangani's navigation
layer. Pure builder functions only -- no I/O, no database calls.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

TASKS_LABEL = "\U0001F4CB Tasks"
REMINDERS_LABEL = "⏰ Reminders"
TOPICS_LABEL = "\U0001F4DA Topics"
NOTES_LABEL = "\U0001F4DD Notes"

# Single source of truth for the reply-keyboard button labels, so the labels
# used to BUILD the keyboard and the labels used to MATCH incoming
# button-press text (an ordinary text message, as far as Telegram/PTB is
# concerned) can never drift apart.
NAV_LABELS = frozenset({TASKS_LABEL, REMINDERS_LABEL, TOPICS_LABEL, NOTES_LABEL})

# 2-letter codes used in callback_data (e.g. "task:status:42:ip") instead of
# the full enum string, keeping payloads compact and decoding centralized.
STATUS_CODES = {
    "ns": "not_started",
    "ip": "in_progress",
    "bl": "blocked",
    "dn": "done",
}
STATUS_LABELS = {
    "ns": "Not started",
    "ip": "In progress",
    "bl": "Blocked",
    "dn": "Done",
}

SNOOZE_DURATIONS = {
    "10m": ("10 minutes", 10 * 60),
    "1h": ("1 hour", 60 * 60),
}


def persistent_reply_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            [TASKS_LABEL, REMINDERS_LABEL],
            [TOPICS_LABEL, NOTES_LABEL],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def task_list_keyboard(tasks: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for t in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ Complete #{t['id']}", callback_data=f"task:complete:{t['id']}"
                ),
                InlineKeyboardButton(
                    f"✏️ Edit #{t['id']}", callback_data=f"task:menu:{t['id']}"
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def task_edit_menu_keyboard(task_id: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                STATUS_LABELS[code], callback_data=f"task:status:{task_id}:{code}"
            )
        ]
        for code in ("ns", "ip", "bl", "dn")
    ]
    rows.append(
        [InlineKeyboardButton("⬅️ Back", callback_data=f"task:back:{task_id}")]
    )
    return InlineKeyboardMarkup(rows)


def reminder_fired_keyboard(reminder_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Done", callback_data=f"rem:done:{reminder_id}"),
                InlineKeyboardButton(
                    "Snooze 10m", callback_data=f"rem:snooze:{reminder_id}:10m"
                ),
                InlineKeyboardButton(
                    "Snooze 1h", callback_data=f"rem:snooze:{reminder_id}:1h"
                ),
            ]
        ]
    )


def reminder_list_keyboard(reminders: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"❌ Cancel #{r['id']}", callback_data=f"rem:cancel:{r['id']}"
            )
        ]
        for r in reminders
    ]
    return InlineKeyboardMarkup(rows)


def topic_root_keyboard(modules: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(m["name"], callback_data=f"topic:mod:{m['id']}")]
        for m in modules
    ]
    return InlineKeyboardMarkup(rows)


def topic_module_keyboard(topics: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t["name"], callback_data=f"topic:open:{t['id']}")]
        for t in topics
    ]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="topic:root")])
    return InlineKeyboardMarkup(rows)


def topic_detail_keyboard(
    topic_id: int, subtopics: list[dict], back_target: str
) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t["name"], callback_data=f"topic:open:{t['id']}")]
        for t in subtopics
    ]
    rows.append(
        [InlineKeyboardButton("\U0001F4DD Notes", callback_data=f"topic:notes:{topic_id}")]
    )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=back_target)])
    return InlineKeyboardMarkup(rows)


def topic_notes_keyboard(topic_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data=f"topic:open:{topic_id}")]]
    )
