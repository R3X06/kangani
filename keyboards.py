"""Reply-keyboard and inline-keyboard builders for Kangani's navigation
layer. Pure builder functions only -- no I/O, no database calls.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

TASKS_LABEL = "\U0001F4CB Tasks"
REMINDERS_LABEL = "⏰ Reminders"
TOPICS_LABEL = "\U0001F4DA Topics"
NOTES_LABEL = "\U0001F4DD Notes"
EVENTS_LABEL = "\U0001F5D3️ Events"

# Single source of truth for the reply-keyboard button labels, so the labels
# used to BUILD the keyboard and the labels used to MATCH incoming
# button-press text (an ordinary text message, as far as Telegram/PTB is
# concerned) can never drift apart.
NAV_LABELS = frozenset(
    {TASKS_LABEL, REMINDERS_LABEL, TOPICS_LABEL, NOTES_LABEL, EVENTS_LABEL}
)

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
            [EVENTS_LABEL],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def _origin_suffix(origin_topic_id: int | None) -> str:
    return f":top:{origin_topic_id}" if origin_topic_id is not None else ""


def task_list_keyboard(
    tasks: list[dict], origin_topic_id: int | None = None
) -> InlineKeyboardMarkup:
    suffix = _origin_suffix(origin_topic_id)
    rows = []
    for t in tasks:
        rows.append(
            [
                InlineKeyboardButton(
                    f"✅ Complete #{t['id']}",
                    callback_data=f"task:complete:{t['id']}{suffix}",
                ),
                InlineKeyboardButton(
                    f"✏️ Edit #{t['id']}", callback_data=f"task:menu:{t['id']}{suffix}"
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def task_edit_menu_keyboard(
    task_id: int, origin_topic_id: int | None = None
) -> InlineKeyboardMarkup:
    suffix = _origin_suffix(origin_topic_id)
    rows = [
        [
            InlineKeyboardButton(
                STATUS_LABELS[code],
                callback_data=f"task:status:{task_id}:{code}{suffix}",
            )
        ]
        for code in ("ns", "ip", "bl", "dn")
    ]
    rows.append(
        [InlineKeyboardButton("⬅️ Back", callback_data=f"task:back:{task_id}{suffix}")]
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


def topic_root_keyboard(topics: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t["name"], callback_data=f"topic:open:{t['id']}")]
        for t in topics
    ]
    return InlineKeyboardMarkup(rows)


def topic_detail_keyboard(
    topic_id: int, subtopics: list[dict], counts: dict, back_target: str
) -> InlineKeyboardMarkup:
    rows = []
    # Subtopics, each opening its own detail screen.
    for t in subtopics:
        rows.append(
            [InlineKeyboardButton(f"📁 {t['name']}", callback_data=f"topic:open:{t['id']}")]
        )
    # Tappable content counts -> drill into that filtered list. Only show a
    # count button when there's something to see, so the keyboard stays lean.
    count_buttons = []
    if counts.get("notes"):
        count_buttons.append(
            InlineKeyboardButton(f"📝 {counts['notes']} notes", callback_data=f"topic:notes:{topic_id}")
        )
    if counts.get("files"):
        count_buttons.append(
            InlineKeyboardButton(f"📎 {counts['files']} files", callback_data=f"topic:files:{topic_id}")
        )
    if count_buttons:
        rows.append(count_buttons)
    # Management actions.
    rows.append(
        [
            InlineKeyboardButton("✏️ Rename", callback_data=f"topic:rename:{topic_id}"),
            InlineKeyboardButton("🏷 Nickname", callback_data=f"topic:nick:{topic_id}"),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("➕ Subtopic", callback_data=f"topic:addsub:{topic_id}"),
            InlineKeyboardButton("🗑 Delete", callback_data=f"topic:del:{topic_id}"),
        ]
    )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=back_target)])
    return InlineKeyboardMarkup(rows)


def topic_notes_keyboard(topic_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Back", callback_data=f"topic:open:{topic_id}")]]
    )


def topic_files_keyboard(topic_id: int, files: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for f in files:
        name = f.get("nickname") or f.get("file_name") or "file"
        rows.append(
            [InlineKeyboardButton(f"⬇️ {name}", callback_data=f"topic:getfile:{f['id']}")]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"topic:open:{topic_id}")])
    return InlineKeyboardMarkup(rows)


def topic_delete_confirm_keyboard(topic_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🗑 Yes, delete it", callback_data=f"topic:delyes:{topic_id}")],
            [InlineKeyboardButton("Cancel", callback_data=f"topic:open:{topic_id}")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🗑 Delete all my data", callback_data="settings:delall:1")]]
    )


def settings_delete_all_confirm_keyboard() -> InlineKeyboardMarkup:
    # Step 2 of 2: the button itself only appears after the first tap, and it's
    # the ONLY path to the irreversible action -- no single tap can wipe data.
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Yes, permanently delete EVERYTHING", callback_data="settings:delall:2")],
            [InlineKeyboardButton("Cancel", callback_data="settings:delall:cancel")],
        ]
    )


def pdf_import_confirm_keyboard(import_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Import", callback_data=f"pdfimport:confirm:{import_id}"
                ),
                InlineKeyboardButton(
                    "❌ Cancel", callback_data=f"pdfimport:cancel:{import_id}"
                ),
            ]
        ]
    )


def pdf_import_root_pick_keyboard(import_id: str, candidates: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t["path"], callback_data=f"pdfimport:rootpick:{import_id}:{t['id']}")]
        for t in candidates
    ]
    return InlineKeyboardMarkup(rows)


def pdf_import_entry_list_keyboard(
    import_id: str, entries: list[dict]
) -> InlineKeyboardMarkup:
    # Two "Edit #N" buttons per row keeps a full semester's ~15 entries from
    # becoming an unreasonably tall keyboard.
    edit_buttons = [
        InlineKeyboardButton(
            f"✏️ #{e['_idx']}", callback_data=f"pdfimport:edit:{import_id}:{e['_idx']}"
        )
        for e in entries
    ]
    rows = [edit_buttons[i:i + 2] for i in range(0, len(edit_buttons), 2)]
    rows.append(
        [
            InlineKeyboardButton("✅ Import", callback_data=f"pdfimport:confirm:{import_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"pdfimport:cancel:{import_id}"),
        ]
    )
    return InlineKeyboardMarkup(rows)


def pdf_import_entry_edit_keyboard(import_id: str, entry_idx: int) -> InlineKeyboardMarkup:
    field_labels = [
        ("day_of_week", "Day"), ("start_time", "Start"), ("end_time", "End"),
        ("location", "Location"), ("class_type", "Type"),
    ]
    rows = [
        [InlineKeyboardButton(label, callback_data=f"pdfimport:field:{import_id}:{entry_idx}:{field}")]
        for field, label in field_labels
    ]
    rows.append(
        [InlineKeyboardButton("🗑 Delete entry", callback_data=f"pdfimport:delete:{import_id}:{entry_idx}")]
    )
    rows.append(
        [InlineKeyboardButton("⬅️ Back to list", callback_data=f"pdfimport:list:{import_id}")]
    )
    return InlineKeyboardMarkup(rows)


def event_list_keyboard(events: list[dict]) -> InlineKeyboardMarkup:
    # "events" are now topics with an event_datetime -- keyed by topic id.
    rows = [
        [InlineKeyboardButton(e["name"], callback_data=f"event:open:{e['id']}")]
        for e in events
    ]
    return InlineKeyboardMarkup(rows)


def event_detail_keyboard(
    topic_id: int, tasks: list[dict], subtopics: list[dict]
) -> InlineKeyboardMarkup:
    rows = list(task_list_keyboard(tasks, origin_topic_id=topic_id).inline_keyboard)
    for t in subtopics:
        rows.append(
            [InlineKeyboardButton(t["name"], callback_data=f"topic:open:{t['id']}")]
        )
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="event:root")])
    return InlineKeyboardMarkup(rows)