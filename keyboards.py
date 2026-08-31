"""Reply-keyboard and inline-keyboard builders for Kangani's navigation
layer. Pure builder functions only -- no I/O, no database calls.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

TODAY_LABEL = "\U0001F4C5 Today"
TASKS_LABEL = "\U0001F4CB Tasks"
REMINDERS_LABEL = "⏰ Reminders"
TOPICS_LABEL = "\U0001F4DA Topics"
NOTES_LABEL = "\U0001F4DD Notes"
EVENTS_LABEL = "\U0001F5D3️ Events"
ADD_LABEL = "➕ Add"

# Single source of truth for the reply-keyboard button labels, so the labels
# used to BUILD the keyboard and the labels used to MATCH incoming
# button-press text (an ordinary text message, as far as Telegram/PTB is
# concerned) can never drift apart.
NAV_LABELS = frozenset(
    {TODAY_LABEL, TASKS_LABEL, REMINDERS_LABEL, TOPICS_LABEL, NOTES_LABEL,
     EVENTS_LABEL, ADD_LABEL}
)

# Words that back out of ANY pending prompt. Every pending-reply interceptor
# (pdf import, file upload, topic edit, quick-add flows) checks this first, so
# there is always a way out by typing. Without it a prompt that only accepts
# one kind of answer traps the conversation: replying "Cancel" to the PDF
# import's root question just came back as "No topic found named 'Cancel'",
# over and over, with no way to escape short of answering it.
CANCEL_WORDS = frozenset({
    "cancel", "cancel it", "cancel that", "cancel import", "nvm", "nevermind",
    "never mind", "forget it", "stop", "abort", "quit", "exit", "go back",
})


def is_cancel_reply(text: str | None) -> bool:
    """True if a reply to a pending prompt means "back out"."""
    if not text:
        return False
    return text.strip().strip(".!/").casefold() in CANCEL_WORDS


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
            # Today is the daily-driver "what do I do right now" view -- given
            # its own full-width top row so it reads as the primary action
            # rather than one browser among five.
            [TODAY_LABEL],
            [TASKS_LABEL, REMINDERS_LABEL],
            [TOPICS_LABEL, NOTES_LABEL],
            [EVENTS_LABEL, ADD_LABEL],
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
    # Per reminder: a push-back row (reschedule the still-pending reminder in
    # place, reusing SNOOZE_DURATIONS' codes) plus a Cancel row. "pushback" is a
    # distinct action from the fired-reminder "snooze" because it MOVES the
    # existing reminder rather than creating a fresh one -- see reminder_callback.
    rows = []
    for r in reminders:
        rows.append(
            [
                InlineKeyboardButton(
                    "⏰ +10m", callback_data=f"rem:pushback:{r['id']}:10m"
                ),
                InlineKeyboardButton(
                    "⏰ +1h", callback_data=f"rem:pushback:{r['id']}:1h"
                ),
                InlineKeyboardButton(
                    f"❌ Cancel #{r['id']}", callback_data=f"rem:cancel:{r['id']}"
                ),
            ]
        )
    return InlineKeyboardMarkup(rows)


def notes_list_keyboard(notes: list[dict]) -> InlineKeyboardMarkup | None:
    """One row per FILED note, jumping to its parent topic (reusing the topic
    callback unchanged). General notes (topic_id is None) get no button -- there
    is nowhere to jump to. Returns None if nothing is jumpable, so the caller
    can send a plain message rather than an empty keyboard."""
    rows = []
    for n in notes:
        topic_id = n.get("topic_id")
        if topic_id is None:
            continue
        where = n.get("topic_name") or "topic"
        # Short preview so the button is recognisable without echoing the whole
        # note; Telegram truncates long button labels anyway.
        preview = (n.get("content") or "").strip().replace("\n", " ")
        if len(preview) > 24:
            preview = preview[:23] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    f"📚 #{n['id']} → {where}: {preview}",
                    callback_data=f"topic:open:{topic_id}",
                )
            ]
        )
    return InlineKeyboardMarkup(rows) if rows else None


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
            InlineKeyboardButton("➕ Task", callback_data=f"flow:new:task:{topic_id}"),
            InlineKeyboardButton("📝 Note", callback_data=f"flow:new:note:{topic_id}"),
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


# Timetable label-format options, in the order the settings toggle cycles them.
# Values must match database.set_timetable_label_format's accepted enum.
LABEL_FORMAT_ORDER = ["code", "nickname", "full_name", "code_nickname", "code_full_name"]
LABEL_FORMAT_NAMES = {
    "code": "Code (SC2001)",
    "nickname": "Nickname (DSA)",
    "full_name": "Full name (Algorithms)",
    "code_nickname": "Code + nickname (SC2001: DSA)",
    "code_full_name": "Code + full name (SC2001: Algorithms)",
}


def settings_keyboard(label_format: str = "code") -> InlineKeyboardMarkup:
    name = LABEL_FORMAT_NAMES.get(label_format, label_format)
    return InlineKeyboardMarkup(
        [
            # Benign control first: a one-tap cycle through timetable label
            # formats, showing the current value so it's self-documenting. Kept
            # visually separate from -- and above -- the destructive delete-all,
            # so the only interactive control isn't the nuclear one.
            [InlineKeyboardButton(f"🏷 Labels: {name}", callback_data="settings:label:next")],
            [InlineKeyboardButton("🗑 Delete all my data", callback_data="settings:delall:1")],
        ]
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


# --- quick-add flows (flows.py) -----------------------------------------
# All button-driven, no Claude call. Every keyboard below carries a Cancel
# button so a flow can always be abandoned cleanly.

def flow_new_item_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📋 Task", callback_data="flow:new:task"),
                InlineKeyboardButton("⏰ Reminder", callback_data="flow:new:reminder"),
                InlineKeyboardButton("📝 Note", callback_data="flow:new:note"),
            ]
        ]
    )


def flow_topic_picker_keyboard(topics: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t["path"], callback_data=f"flow:topic:{t['id']}")]
        for t in topics
    ]
    rows.append(
        [InlineKeyboardButton("🗂 No topic (unfiled)", callback_data="flow:topic:none")]
    )
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(rows)


FLOW_DEADLINE_PRESETS = [
    ("today", "Today"),
    ("tomorrow", "Tomorrow"),
    ("fri", "This Friday"),
    ("none", "No deadline"),
]


def flow_deadline_keyboard() -> InlineKeyboardMarkup:
    preset_buttons = [
        InlineKeyboardButton(label, callback_data=f"flow:deadline:{code}")
        for code, label in FLOW_DEADLINE_PRESETS
    ]
    rows = [preset_buttons[i:i + 2] for i in range(0, len(preset_buttons), 2)]
    rows.append([InlineKeyboardButton("📅 Custom date", callback_data="flow:deadline:custom")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(rows)


FLOW_REMINDER_PRESETS = [
    ("10m", "In 10 min"),
    ("1h", "In 1 hour"),
    ("3h", "In 3 hours"),
    ("tom9", "Tomorrow 9am"),
]


def flow_remindtime_keyboard() -> InlineKeyboardMarkup:
    preset_buttons = [
        InlineKeyboardButton(label, callback_data=f"flow:remindtime:{code}")
        for code, label in FLOW_REMINDER_PRESETS
    ]
    rows = [preset_buttons[i:i + 2] for i in range(0, len(preset_buttons), 2)]
    rows.append([InlineKeyboardButton("📅 Custom date/time", callback_data="flow:remindtime:custom")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(rows)


def flow_category_keyboard(categories: list[str]) -> InlineKeyboardMarkup:
    # Categories referenced by INDEX into the list already stashed in the
    # flow's chat_data state (flows._get_state(...)["data"]["_category_options"]),
    # not by their raw text -- a category name could itself contain ':' and
    # break the callback_data parser, and Telegram caps callback_data at 64
    # bytes anyway, so an index is both safer and shorter.
    cat_buttons = [
        InlineKeyboardButton(c, callback_data=f"flow:category:{i}")
        for i, c in enumerate(categories)
    ]
    rows = [cat_buttons[i:i + 2] for i in range(0, len(cat_buttons), 2)]
    rows.append([InlineKeyboardButton("No category", callback_data="flow:category:none")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="flow:cancel")])
    return InlineKeyboardMarkup(rows)


def flow_reference_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("📌 Reference", callback_data="flow:ref:yes"),
                InlineKeyboardButton("📝 Regular note", callback_data="flow:ref:no"),
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="flow:cancel")],
        ]
    )

def manual_root_keyboard(titles: list[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(t, callback_data=f"manual:sec:{i}")]
        for i, t in enumerate(titles)
    ]
    return InlineKeyboardMarkup(rows)


def manual_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("⬅️ Contents", callback_data="manual:root")]]
    )