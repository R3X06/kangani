"""Tool schemas and handlers Claude can call from brain.py's tool-use loop."""

import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

import database
import scheduler

logger = logging.getLogger(__name__)

TOOL_SCHEMAS = [
    {
        "name": "create_task",
        "description": (
            "Create a new task, optionally attached to a topic anywhere in the "
            "tree (a module, an event, or any freeform topic) via topic_id, or "
            "left unattached. Look the topic_id up via list_topics first, or "
            "create it with create_topic; never guess an id. A task without a "
            "clear home can be left unattached -- ask once if a topic seems "
            "wanted, but don't force one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title"},
                "topic_id": {
                    "type": "integer",
                    "description": (
                        "Optional id of the topic to attach this task to "
                        "(module/event/any topic). Look it up via list_topics "
                        "or create it via create_topic. Omit to leave the task "
                        "unattached."
                    ),
                },
                "deadline": {
                    "type": "string",
                    "description": (
                        "ISO-8601 datetime WITH AN EXPLICIT OFFSET, e.g. "
                        "2026-07-10T18:30:00+08:00 (local) or "
                        "2026-07-10T10:30:00Z (UTC). Omit if no deadline. "
                        "Resolve relative dates (next Friday, tomorrow) "
                        "against the current date/time given in the system "
                        "prompt, and reuse the offset shown there."
                    ),
                },
                "status": {
                    "type": "string",
                    "enum": ["not_started", "in_progress", "blocked", "done"],
                    "description": "Defaults to not_started if omitted",
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Optional user-defined subcategory for this task, e.g. "
                        "'assignment', 'reading', 'admin'. Call "
                        "list_task_categories FIRST and reuse an existing "
                        "category if one fits (matching is case-insensitive) "
                        "rather than minting a near-synonym. If proposing a NEW "
                        "category, confirm the name with the user before "
                        "creating -- but create the task immediately regardless "
                        "(uncategorized if they haven't confirmed yet), never "
                        "block saving the task on the category question."
                    ),
                },
            },
            "required": ["title"],
        },
    },
    {
        "name": "query_tasks",
        "description": (
            "List tasks matching optional filters. Use this to answer "
            "questions like what's due this week or show my in-progress tasks."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["not_started", "in_progress", "blocked", "done"],
                },
                "topic_id": {
                    "type": "integer",
                    "description": (
                        "Optional -- restrict to tasks attached to this topic "
                        "(look it up via list_topics). By default this INCLUDES "
                        "tasks on every topic nested under it (the subtree), so "
                        "passing a semester or year topic_id returns everything "
                        "beneath it. Set include_subtopics=false for only tasks "
                        "on this exact topic."
                    ),
                },
                "include_subtopics": {
                    "type": "boolean",
                    "description": (
                        "Default true. Only relevant when topic_id is given -- "
                        "false restricts to the exact topic, excluding its "
                        "subtree."
                    ),
                },
                "category": {
                    "type": "string",
                    "description": (
                        "Optional -- restrict to tasks in this category (see "
                        "list_task_categories). Case-insensitive."
                    ),
                },
                "show_tags": {
                    "type": "boolean",
                    "description": (
                        "Default false. When true, each task's hidden tag is "
                        "shown. Only set when the user explicitly asks for tags."
                    ),
                },
                "deadline_from": {
                    "type": "string",
                    "description": "ISO-8601 UTC datetime, inclusive lower bound",
                },
                "deadline_to": {
                    "type": "string",
                    "description": "ISO-8601 UTC datetime, inclusive upper bound",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results, default 20",
                },
            },
            "required": [],
        },
    },
    {
        "name": "update_task_status",
        "description": (
            "Update a task's status and/or progress percentage. Look up the "
            "task_id via query_tasks first if the user refers to the task by name."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer"},
                "status": {
                    "type": "string",
                    "enum": ["not_started", "in_progress", "blocked", "done"],
                },
                "progress_pct": {"type": "integer", "description": "0-100"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "create_reminder",
        "description": (
            "Schedule a one-time reminder message to be sent at a specific "
            "future date/time. Optionally link it to an existing task."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "trigger_datetime": {
                    "type": "string",
                    "description": (
                        "ISO-8601 datetime WITH AN EXPLICIT OFFSET for when "
                        "to send the reminder, e.g. "
                        "2026-07-10T18:30:00+08:00 (local) or "
                        "2026-07-10T10:30:00Z (UTC). Must be in the future."
                    ),
                },
                "message": {
                    "type": "string",
                    "description": "The reminder text to send the user",
                },
                "linked_task_id": {
                    "type": "integer",
                    "description": (
                        "Optional task this reminder relates to. Mutually "
                        "exclusive with linked_topic_id."
                    ),
                },
                "linked_topic_id": {
                    "type": "integer",
                    "description": (
                        "Optional topic (e.g. an event) this reminder relates "
                        "to. Mutually exclusive with linked_task_id."
                    ),
                },
            },
            "required": ["trigger_datetime", "message"],
        },
    },
    {
        "name": "list_topic_kinds",
        "description": (
            "List the topic 'kind' labels already in use for this chat "
            "(canonical ones like course/year/semester/module/component/event "
            "first). ALWAYS call this before inventing a new kind when creating "
            "a topic, and reuse an existing kind if one fits (matching is "
            "case-insensitive) rather than minting a near-duplicate."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_task_categories",
        "description": (
            "List the task categories already in use for this chat. ALWAYS "
            "call this before setting a NEW category on a task, and reuse an "
            "existing one if it fits (case-insensitive) rather than minting a "
            "near-synonym. Confirm a genuinely new category with the user "
            "before creating it (but never block saving the task on that)."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_lesson_types",
        "description": (
            "List the lesson types (class_type -- lecture, tutorial, lab, "
            "seminar, etc.) already in use for this chat. ALWAYS call this "
            "before setting a NEW lesson type on a schedule block, and reuse "
            "an existing one if it fits (case-insensitive). Also use it to "
            "resolve a user's lesson-type filter word ('labs', 'tutorials') to "
            "the exact stored spelling before passing lesson_types to "
            "query_schedule."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "query_reminders",
        "description": (
            "List the chat's pending reminders. scope='general' returns only "
            "freestanding reminders (linked to no task and no topic) -- use "
            "for 'my general reminders'. scope='all' (default) returns every "
            "pending reminder. topic_id (with include_subtopics, default true) "
            "restricts to reminders linked to that topic and its subtree -- "
            "e.g. 'Y3S1 reminders'. Combine with query_schedule / query_tasks "
            "/ query_notes for a full combined calendar. Set show_tags=true "
            "only when the user explicitly asks to see hidden tags."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {
                    "type": "string",
                    "enum": ["general", "all"],
                    "description": (
                        "'general' = unlinked only; 'all' (default) = every "
                        "pending reminder."
                    ),
                },
                "topic_id": {
                    "type": "integer",
                    "description": (
                        "Optional -- restrict to reminders linked to this topic "
                        "and its subtree (look it up via list_topics)."
                    ),
                },
                "include_subtopics": {
                    "type": "boolean",
                    "description": (
                        "Default true. false restricts to the exact topic."
                    ),
                },
                "show_tags": {
                    "type": "boolean",
                    "description": (
                        "Default false. When true, each reminder's hidden tag "
                        "is shown. Only set when the user explicitly asks."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "add_event_reminder",
        "description": (
            "Add another reminder for an event topic (one that has an "
            "event_datetime), firing the given number of minutes before it. "
            "Event topics already get default reminders when created; use this "
            "to bolt on an extra lead time (e.g. a day before). Look the "
            "topic_id up via list_topics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {"type": "integer"},
                "offset_minutes": {
                    "type": "integer",
                    "description": "Minutes before the event to fire the reminder",
                },
            },
            "required": ["topic_id", "offset_minutes"],
        },
    },
    {
        "name": "create_topic",
        "description": (
            "Create a topic anywhere in the unified tree, or return the "
            "matching one if it already exists (same name, same parent, "
            "case-insensitive). Everything is a topic: a course, a module, an "
            "event, a freeform life area, or a subtopic of any of these. Pass "
            "parent_topic_id to nest it (look the parent up via list_topics -- "
            "names aren't unique); omit it for a top-level topic. Set `kind` to "
            "classify it -- call list_topic_kinds first and reuse an existing "
            "kind rather than inventing a near-duplicate. Use kind='module' for "
            "an academic subject that appears on the timetable (it gets an "
            "auto-assigned color). Set event_datetime for a one-off event to "
            "auto-create reminders before it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Short display name/code, e.g. SC2001 or Backpropagation "
                        "-- used for lookups, breadcrumbs, and the 'code' "
                        "timetable label format."
                    ),
                },
                "full_name": {
                    "type": "string",
                    "description": (
                        "Optional official/long name, e.g. 'Data Structures and "
                        "Algorithms'. Separate from `name` -- both are kept, "
                        "never overwriting each other. Use set_topic_names to "
                        "edit either after creation."
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": (
                        "Free-string classifier (canonical: course, year, "
                        "semester, module, component, event). Call "
                        "list_topic_kinds first and reuse an existing one if it "
                        "fits. 'module' triggers timetable color assignment; "
                        "'event' (or 'event:<type>') marks a one-off event."
                    ),
                },
                "status": {
                    "type": "string",
                    "description": "Optional free-string status for this topic",
                },
                "event_datetime": {
                    "type": "string",
                    "description": (
                        "For an event topic: ISO-8601 datetime WITH AN EXPLICIT "
                        "OFFSET (e.g. 2026-09-01T14:00:00+08:00). Setting it "
                        "auto-creates reminders before the event."
                    ),
                },
                "reminder_offsets_minutes": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": (
                        "Optional lead times (minutes before event_datetime) "
                        "for the auto-created reminders; defaults to [60, 30]. "
                        "Only meaningful with event_datetime."
                    ),
                },
                "parent_topic_id": {
                    "type": "integer",
                    "description": (
                        "id of the parent topic, if this is a subtopic. Look "
                        "it up via list_topics first -- topic names are not "
                        "unique, so parents must be identified by id."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "set_topic_names",
        "description": (
            "Edit a topic's name (short display/code), full_name (official "
            "long title), and/or nickname (a custom short label the user "
            "chose). Only pass the field(s) being changed -- omitted fields "
            "are left as-is. This OVERWRITES the given field(s), unlike "
            "create_topic's fill-only behavior on an existing match."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "integer",
                    "description": "Look this up via list_topics",
                },
                "name": {"type": "string"},
                "full_name": {"type": "string"},
                "nickname": {"type": "string"},
            },
            "required": ["topic_id"],
        },
    },
    {
        "name": "move_topic",
        "description": (
            "Reparent an existing topic -- move it to sit under a different "
            "parent (or to the root, if new_parent_topic_id is omitted). Use "
            "this when a topic was created in the wrong place (e.g. a module "
            "imported from a PDF landed at the root and needs to move under a "
            "semester). Look up both ids via list_topics first. Refuses to "
            "create a cycle (can't move a topic under itself or its own "
            "subtopic)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "integer",
                    "description": "The topic to move.",
                },
                "new_parent_topic_id": {
                    "type": "integer",
                    "description": "Omit to move the topic to the root.",
                },
            },
            "required": ["topic_id"],
        },
    },
    {
        "name": "set_timetable_label_format",
        "description": (
            "Set the STANDING default label format used for /dayimage, "
            "/weekimage, /monthimage, and /today /week (these are direct "
            "slash commands, not routed through you, so they always use this "
            "saved preference rather than a per-message instruction). Call "
            "this when the user asks to change how modules are labeled going "
            "forward, e.g. 'show my timetable images with just course codes' "
            "or 'use my nicknames on the timetable'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label_format": {
                    "type": "string",
                    "enum": ["code", "full_name", "nickname", "code_nickname", "code_full_name"],
                    "description": (
                        "code = SC2001. full_name = the official long title. "
                        "nickname = the user's custom short label. "
                        "code_nickname = 'SC2001: <nickname>'. "
                        "code_full_name = 'SC2001: <full title>'. Falls back to "
                        "code if the requested field isn't set on a given topic."
                    ),
                },
            },
            "required": ["label_format"],
        },
    },
    {
        "name": "list_topics",
        "description": (
            "List topics for this chat, optionally filtered to one `kind`. "
            "Each result includes its id, kind, and a full breadcrumb path, "
            "e.g. 'Machine Learning > Neural Nets > Backpropagation'. Call this "
            "before create_topic with a parent_topic_id, before add_note or "
            "query_notes (to find the target topic_id), before attaching a "
            "task/reminder to a topic, and whenever you're unsure whether a "
            "similar topic already exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "description": "Optional -- restrict to topics of this kind",
                },
            },
            "required": [],
        },
    },
    {
        "name": "add_note",
        "description": (
            "Save a note. Attach it to an existing topic via topic_id (look it "
            "up via list_topics first, or create the topic with create_topic if "
            "none exists), OR omit topic_id entirely to save a GENERAL note "
            "attached to nothing -- for a thought or reminder-to-self that "
            "doesn't belong under any topic. Offer a topic once if one seems "
            "fitting, but don't force one. Mark is_reference=true for reference "
            "material (a link, a textbook excerpt, a definition worth keeping) "
            "vs a transient note (is_reference=false, the default)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "integer",
                    "description": (
                        "Optional. Omit for a general note attached to no topic."
                    ),
                },
                "content": {"type": "string", "description": "The note text"},
                "source": {
                    "type": "string",
                    "description": (
                        "Optional origin, e.g. a URL, book title, or lecture name"
                    ),
                },
                "is_reference": {
                    "type": "boolean",
                    "description": (
                        "True for reference material, false (default) for a "
                        "regular note"
                    ),
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "query_notes",
        "description": (
            "Retrieve notes. With no topic_id, returns ALL notes for the chat "
            "(general and topic-linked alike). With a topic_id, returns notes "
            "under that topic AND everything nested beneath it (its subtree) by "
            "default -- so a semester or year topic_id gathers all notes below "
            "it. Set include_subtopics=false for only notes directly on that "
            "exact topic. Set show_tags=true only if the user explicitly asks "
            "to see the hidden reference tags (e.g. 'notes -tag')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {
                    "type": "integer",
                    "description": "Look this up via list_topics",
                },
                "include_subtopics": {
                    "type": "boolean",
                    "description": (
                        "Default true. Only relevant when topic_id is given -- "
                        "false restricts to the exact topic, excluding its "
                        "subtree."
                    ),
                },
                "is_reference": {
                    "type": "boolean",
                    "description": (
                        "Filter to reference-only (true) or non-reference-only "
                        "(false). Omit for both."
                    ),
                },
                "show_tags": {
                    "type": "boolean",
                    "description": (
                        "Default false. When true, each note's hidden reference "
                        "tag is shown. Only set when the user explicitly asks "
                        "for tags."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results, default 20",
                },
            },
            "required": [],
        },
    },
    {
        "name": "create_schedule_block",
        "description": (
            "Create a recurring weekly block (day_of_week) or a one-off "
            "block (specific_date) -- exactly one of the two must be given. "
            "Optionally tag it to a study module; omit module_name entirely "
            "for blocks that aren't study-related (gym, an errand, a "
            "personal appointment) -- unlike tasks, schedule blocks do not "
            "need a module."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day_of_week": {
                    "type": "string",
                    "enum": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                    "description": (
                        "Set for a block that recurs every week on this day. "
                        "Do not also set specific_date."
                    ),
                },
                "specific_date": {
                    "type": "string",
                    "description": (
                        "ISO-8601 date YYYY-MM-DD for a block that happens "
                        "exactly once. Do not also set day_of_week."
                    ),
                },
                "start_time": {
                    "type": "string",
                    "description": (
                        "24-hour local wall-clock time, HH:MM (e.g. 14:00). "
                        "No timezone offset -- this is not an absolute instant."
                    ),
                },
                "end_time": {
                    "type": "string",
                    "description": "24-hour local wall-clock time, HH:MM.",
                },
                "module_name": {
                    "type": "string",
                    "description": "Optional. Omit if this block has no study module.",
                },
                "class_type": {
                    "type": "string",
                    "description": (
                        "Optional lesson type, e.g. Lecture, Tutorial, Lab, "
                        "Seminar, Workshop. Call list_lesson_types FIRST and "
                        "reuse an existing one (matching is case-insensitive) "
                        "rather than minting a near-synonym ('Tut' vs "
                        "'Tutorial'); confirm a genuinely NEW lesson type with "
                        "the user before creating. Set this for timetable "
                        "classes so a module's lecture and tutorial stay "
                        "distinguishable. Omit for non-lesson blocks (gym, "
                        "errands)."
                    ),
                },
                "location": {
                    "type": "string",
                    "description": "Optional room/location.",
                },
                "week_pattern": {
                    "type": "string",
                    "description": (
                        "Which semester weeks this recurring class actually "
                        "runs. Defaults to 'every' (every week). Use 'odd' or "
                        "'even' for classes that alternate by week parity, or "
                        "an explicit comma-separated list of week numbers "
                        "(e.g. '2,4,6,8,10,12') for anything else. Only "
                        "meaningful with day_of_week; leave as 'every' for "
                        "one-off (specific_date) blocks. Non-'every' patterns "
                        "require the chat's semester start date to be set "
                        "first via set_semester_start."
                    ),
                },
            },
            "required": ["start_time", "end_time"],
        },
    },
    {
        "name": "query_schedule",
        "description": (
            "List schedule blocks (lessons). If BOTH date_from and date_to are "
            "given, recurring blocks are expanded into actual dated occurrences "
            "within that range and the result is date-ordered (use this for "
            "'what's on today/this week' -- compute the bounds yourself from "
            "the current date shown in the system prompt). If omitted, returns "
            "the raw weekly timetable plus one-off blocks, unexpanded. "
            "topic_id (with include_subtopics, default true) restricts to "
            "lessons under a topic and everything nested beneath it -- this is "
            "how a 'Y3S1 lesson calendar' works: resolve the topic, pass its "
            "id. lesson_types further narrows to specific types (e.g. just "
            "tutorial + lab). This tool covers the LESSON portion of a "
            "calendar only; combine it with query_tasks / query_notes / "
            "reminders for a full combined calendar."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "ISO-8601 date YYYY-MM-DD, inclusive lower bound.",
                },
                "date_to": {
                    "type": "string",
                    "description": "ISO-8601 date YYYY-MM-DD, inclusive upper bound.",
                },
                "topic_id": {
                    "type": "integer",
                    "description": (
                        "Optional -- restrict to lessons under this topic and "
                        "its subtree (look it up via list_topics). Prefer this "
                        "over module_name for anything above module level (a "
                        "semester, a year), since it walks the whole subtree."
                    ),
                },
                "include_subtopics": {
                    "type": "boolean",
                    "description": (
                        "Default true. false restricts to the exact topic, "
                        "excluding its subtree."
                    ),
                },
                "lesson_types": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional -- restrict to these lesson types (e.g. "
                        "['tutorial','lab']). Case-insensitive. See "
                        "list_lesson_types for what's in use."
                    ),
                },
                "module_name": {
                    "type": "string",
                    "description": (
                        "Optional single-module shortcut. Does NOT walk a "
                        "subtree -- use topic_id for that."
                    ),
                },
                "label_format": {
                    "type": "string",
                    "enum": ["code", "full_name", "nickname", "code_nickname", "code_full_name"],
                    "description": (
                        "Optional ad-hoc override for THIS request only (e.g. "
                        "the user asks 'show full names just this once'). "
                        "Omit to use the chat's saved default (see "
                        "set_timetable_label_format)."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "delete_schedule_block",
        "description": (
            "Permanently remove a schedule block (recurring or one-off), "
            "e.g. when a class changed room/time or was cancelled. Call "
            "query_schedule first to find the correct schedule_block_id -- "
            "never guess it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"schedule_block_id": {"type": "integer"}},
            "required": ["schedule_block_id"],
        },
    },
    {
        "name": "set_semester_start",
        "description": (
            "Set (or update) the calendar date that semester week 1 begins "
            "for this chat -- the single anchor Kangani uses to compute which "
            "semester week any date falls in, so alternating classes (odd/even "
            "weeks, or specific week lists) resolve correctly. Call this when "
            "the user tells you the start date (e.g. 'week 1 starts August "
            "13th', 'the week of Aug 13 is week 1'). Pass the FIRST DAY "
            "(Monday) of week 1. Idempotent -- calling again just updates the "
            "anchor."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "start_date": {
                    "type": "string",
                    "description": (
                        "ISO-8601 date YYYY-MM-DD -- the Monday that semester "
                        "week 1 begins."
                    ),
                },
            },
            "required": ["start_date"],
        },
    },
    {
        "name": "set_recess_weeks",
        "description": (
            "Mark one or more recess / reading / break weeks (weeks with no "
            "teaching) so Kangani's week numbers stay aligned with the school's "
            "OFFICIAL numbering -- recess weeks are then skipped automatically "
            "when counting weeks, and classes are hidden during them. Identify "
            "each recess week by ANY single calendar date that falls within it "
            "(e.g. its Monday) -- you do NOT need to work out its week number. "
            "This marks a WHOLE week as recess; it is not for skipping an "
            "individual class (use a week_pattern for that). For a multi-week "
            "break, pass one date from each week. Requires the semester start "
            "date to be set first. Idempotent -- calling again replaces the "
            "recess set entirely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recess_dates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "ISO-8601 dates YYYY-MM-DD, one per recess week (any "
                        "date within the week)."
                    ),
                },
            },
            "required": ["recess_dates"],
        },
    },
]


def _task_label(row: dict) -> str:
    """A task's attachment label: its topic name, or 'unfiled' if unattached."""
    return row.get("topic_name") or "unfiled"


def _handle_create_task(tool_input: dict, chat_id: int, job_queue) -> str:
    deadline_raw = tool_input.get("deadline")
    deadline_utc = (
        scheduler.format_utc_iso(scheduler.parse_iso_datetime(deadline_raw))
        if deadline_raw
        else None
    )

    task = database.create_task(
        chat_id=chat_id,
        title=tool_input["title"],
        topic_id=tool_input.get("topic_id"),
        category=tool_input.get("category"),
        deadline=deadline_utc,
        status=tool_input.get("status", "not_started"),
    )
    deadline_part = f", due {task['deadline']}" if task["deadline"] else ""
    category_part = f", category {task['category']}" if task["category"] else ""
    return (
        f"Created task #{task['id']} '{task['title']}' ({_task_label(task)})"
        f"{category_part}{deadline_part}. Status: {task['status']}."
    )


def _handle_query_tasks(tool_input: dict, chat_id: int, job_queue) -> str:
    deadline_from_raw = tool_input.get("deadline_from")
    deadline_to_raw = tool_input.get("deadline_to")
    show_tags = tool_input.get("show_tags", False)

    tasks = database.query_tasks(
        chat_id=chat_id,
        status=tool_input.get("status"),
        topic_id=tool_input.get("topic_id"),
        include_subtopics=tool_input.get("include_subtopics", True),
        category=tool_input.get("category"),
        deadline_from=(
            scheduler.format_utc_iso(scheduler.parse_iso_datetime(deadline_from_raw))
            if deadline_from_raw
            else None
        ),
        deadline_to=(
            scheduler.format_utc_iso(scheduler.parse_iso_datetime(deadline_to_raw))
            if deadline_to_raw
            else None
        ),
        limit=tool_input.get("limit", 20),
    )
    if not tasks:
        return "No tasks match those filters."
    lines = []
    for t in tasks:
        deadline_part = f", deadline {t['deadline']}" if t["deadline"] else ""
        category_part = f", {t['category']}" if t["category"] else ""
        tag_part = f" (tag {t['tag']})" if show_tags else ""
        lines.append(
            f"#{t['id']}{tag_part} [{_task_label(t)}] {t['title']} — "
            f"status: {t['status']}, progress: {t['progress_pct']}%"
            f"{category_part}{deadline_part}"
        )
    return "\n".join(lines)


def _handle_update_task_status(tool_input: dict, chat_id: int, job_queue) -> str:
    task_id = tool_input["task_id"]
    task = database.update_task_status(
        chat_id=chat_id,
        task_id=task_id,
        status=tool_input.get("status"),
        progress_pct=tool_input.get("progress_pct"),
    )
    if task is None:
        raise ValueError(f"No task with id {task_id} found for this chat.")
    return (
        f"Updated task #{task['id']} '{task['title']}': "
        f"status={task['status']}, progress={task['progress_pct']}%."
    )


def _handle_create_reminder(tool_input: dict, chat_id: int, job_queue) -> str:
    trigger_raw = tool_input["trigger_datetime"]
    message = tool_input["message"]

    trigger_dt_utc = scheduler.parse_iso_datetime(trigger_raw)
    trigger_utc_str = scheduler.format_utc_iso(trigger_dt_utc)

    tz_name = os.environ.get("TIMEZONE", "UTC")
    trigger_local_str = trigger_dt_utc.astimezone(ZoneInfo(tz_name)).strftime(
        "%Y-%m-%d %H:%M %z"
    )
    logger.info(
        "create_reminder: Claude sent %r -> normalized UTC=%s, %s local=%s",
        trigger_raw, trigger_utc_str, tz_name, trigger_local_str,
    )

    now_utc = datetime.now(timezone.utc)
    if trigger_dt_utc <= now_utc:
        logger.warning(
            "create_reminder: computed trigger %s is not in the future "
            "(current UTC is %s) -- reminder will fire almost immediately",
            trigger_utc_str, scheduler.format_utc_iso(now_utc),
        )

    reminder = database.create_reminder(
        chat_id=chat_id,
        trigger_datetime_utc=trigger_utc_str,
        message=message,
        linked_task_id=tool_input.get("linked_task_id"),
        linked_topic_id=tool_input.get("linked_topic_id"),
    )
    scheduler.schedule_reminder(
        job_queue, reminder["id"], chat_id, trigger_dt_utc, message
    )
    return (
        f"Reminder scheduled for {trigger_local_str} ({tz_name}) / "
        f"{trigger_utc_str} UTC: '{message}'."
    )


def _handle_list_topic_kinds(tool_input: dict, chat_id: int, job_queue) -> str:
    kinds = database.list_topic_kinds(chat_id)
    if not kinds:
        return "No topic kinds in use yet."
    return ", ".join(kinds)


def _handle_list_task_categories(tool_input: dict, chat_id: int, job_queue) -> str:
    cats = database.list_task_categories(chat_id)
    if not cats:
        return "No task categories in use yet."
    return ", ".join(cats)


def _handle_list_lesson_types(tool_input: dict, chat_id: int, job_queue) -> str:
    types = database.list_class_types(chat_id)
    if not types:
        return "No lesson types in use yet."
    return ", ".join(types)


def _handle_set_topic_names(tool_input: dict, chat_id: int, job_queue) -> str:
    topic = database.set_topic_names(
        chat_id=chat_id,
        topic_id=tool_input["topic_id"],
        name=tool_input.get("name"),
        full_name=tool_input.get("full_name"),
        nickname=tool_input.get("nickname"),
    )
    parts = [f"name={topic['name']}"]
    if topic.get("full_name"):
        parts.append(f"full_name={topic['full_name']}")
    if topic.get("nickname"):
        parts.append(f"nickname={topic['nickname']}")
    return f"Updated topic #{topic['id']}: " + ", ".join(parts)


def _handle_move_topic(tool_input: dict, chat_id: int, job_queue) -> str:
    topic = database.move_topic(
        chat_id=chat_id,
        topic_id=tool_input["topic_id"],
        new_parent_topic_id=tool_input.get("new_parent_topic_id"),
    )
    topics = database.list_topics(chat_id)
    path = next((t["path"] for t in topics if t["id"] == topic["id"]), topic["name"])
    return f"Moved topic #{topic['id']} -- now at: {path}"


def _handle_set_timetable_label_format(tool_input: dict, chat_id: int, job_queue) -> str:
    database.set_timetable_label_format(chat_id, tool_input["label_format"])
    return (
        f"Timetable label format set to '{tool_input['label_format']}' -- this "
        "applies to /dayimage, /weekimage, /monthimage, /today, and /week "
        "going forward."
    )


def _handle_query_reminders(tool_input: dict, chat_id: int, job_queue) -> str:
    show_tags = tool_input.get("show_tags", False)
    scope = tool_input.get("scope", "all")
    reminders = database.list_pending_reminders(
        chat_id=chat_id,
        scope=scope,
        topic_id=tool_input.get("topic_id"),
        include_subtopics=tool_input.get("include_subtopics", True),
    )
    if not reminders:
        if scope == "general":
            return "No general (unlinked) reminders pending."
        return "No pending reminders match those filters."
    tz_name = os.environ.get("TIMEZONE", "UTC")
    lines = []
    for r in reminders:
        local = scheduler.parse_iso_datetime(r["trigger_data"]).astimezone(
            ZoneInfo(tz_name)
        )
        tag_part = f" (tag {r['tag']})" if show_tags else ""
        lines.append(
            f"#{r['id']}{tag_part} {local.strftime('%Y-%m-%d %H:%M %z')}: {r['message']}"
        )
    return "\n".join(lines)


def _schedule_event_reminders(
    topic: dict, offsets: list[int], chat_id: int, job_queue
) -> int:
    """Create + schedule reminders at each lead time (minutes) before a topic's
    event_datetime, skipping any that would fire in the past. Returns how many
    were actually scheduled."""
    if not topic.get("event_datetime"):
        return 0
    event_dt = scheduler.parse_iso_datetime(topic["event_datetime"])
    now = datetime.now(timezone.utc)
    made = 0
    for offset in offsets:
        trigger = event_dt - timedelta(minutes=offset)
        if trigger <= now:
            continue
        message = f"{topic['name']} is coming up (in {offset} min)."
        reminder = database.create_reminder(
            chat_id=chat_id,
            trigger_datetime_utc=scheduler.format_utc_iso(trigger),
            message=message,
            linked_topic_id=topic["id"],
        )
        scheduler.schedule_reminder(
            job_queue, reminder["id"], chat_id, trigger, message
        )
        made += 1
    return made


def _handle_add_event_reminder(tool_input: dict, chat_id: int, job_queue) -> str:
    topic = database.get_topic(chat_id, tool_input["topic_id"])
    if topic is None:
        raise ValueError(
            f"No topic with id {tool_input['topic_id']} found for this chat."
        )
    if not topic.get("event_datetime"):
        raise ValueError(
            f"Topic #{topic['id']} '{topic['name']}' has no event_datetime, so "
            "it can't take an event reminder."
        )
    offset = tool_input["offset_minutes"]
    made = _schedule_event_reminders(topic, [offset], chat_id, job_queue)
    if made == 0:
        return (
            f"That time ({offset} min before '{topic['name']}') is already past, "
            "so no reminder was scheduled."
        )
    return f"Added a reminder {offset} min before '{topic['name']}'."


def _handle_create_topic(tool_input: dict, chat_id: int, job_queue) -> str:
    event_datetime_raw = tool_input.get("event_datetime")
    event_datetime = (
        scheduler.format_utc_iso(scheduler.parse_iso_datetime(event_datetime_raw))
        if event_datetime_raw
        else None
    )
    topic = database.get_or_create_topic(
        chat_id=chat_id,
        name=tool_input["name"],
        full_name=tool_input.get("full_name"),
        kind=tool_input.get("kind"),
        status=tool_input.get("status"),
        event_datetime=event_datetime,
        parent_topic_id=tool_input.get("parent_topic_id"),
    )
    topics = database.list_topics(chat_id)
    path = next((t["path"] for t in topics if t["id"] == topic["id"]), topic["name"])

    # Schedule the auto-reminders ONLY on the call that actually inserted the
    # row. create_topic is advertised to Claude as idempotent (it returns the
    # existing topic rather than duplicating), so it WILL get called twice for
    # the same event -- and keying off `topic["event_datetime"]` instead of
    # `topic["created"]` re-fires the offsets every single time, stacking a
    # fresh 60/30 pair on each call.
    reminder_note = ""
    schedule_for = None
    if topic["created"] and topic.get("event_datetime"):
        schedule_for = topic["event_datetime"]
    elif not topic["created"] and event_datetime and not topic.get("event_datetime"):
        # Existing topic, no date yet, user is supplying one now: fill it in
        # (otherwise the date is silently dropped -- get_or_create_topic returns
        # the existing row untouched) and schedule against it, once.
        if database.set_topic_event_datetime(chat_id, topic["id"], event_datetime):
            topic = {**topic, "event_datetime": event_datetime}
            schedule_for = event_datetime

    if schedule_for:
        offsets = tool_input.get("reminder_offsets_minutes") or \
            database.DEFAULT_EVENT_REMINDER_OFFSETS
        made = _schedule_event_reminders(topic, offsets, chat_id, job_queue)
        if made:
            reminder_note = f" Scheduled {made} reminder(s) before it."
    return f"Topic #{topic['id']} ready: {path}.{reminder_note}"


def _handle_list_topics(tool_input: dict, chat_id: int, job_queue) -> str:
    topics = database.list_topics(chat_id, kind=tool_input.get("kind"))
    if not topics:
        return "No topics yet."
    lines = []
    for t in topics:
        kind_part = f" [{t['kind']}]" if t["kind"] else ""
        lines.append(f"#{t['id']}{kind_part} {t['path']}")
    return "\n".join(lines)


def _handle_add_note(tool_input: dict, chat_id: int, job_queue) -> str:
    note = database.create_note(
        chat_id=chat_id,
        content=tool_input["content"],
        topic_id=tool_input.get("topic_id"),
        source=tool_input.get("source"),
        is_reference=tool_input.get("is_reference", False),
    )
    ref = " (reference)" if note["is_reference"] else ""
    source_part = f", source: {note['source']}" if note["source"] else ""
    where = f"under '{note['topic_name']}'" if note["topic_name"] else "as a general note"
    return f"Saved note #{note['id']} {where}{ref}{source_part}."


def _handle_query_notes(tool_input: dict, chat_id: int, job_queue) -> str:
    show_tags = tool_input.get("show_tags", False)
    notes = database.query_notes(
        chat_id=chat_id,
        topic_id=tool_input.get("topic_id"),
        include_subtopics=tool_input.get("include_subtopics", True),
        is_reference=tool_input.get("is_reference"),
        limit=tool_input.get("limit", 20),
    )
    if not notes:
        return "No notes match those filters."
    lines = []
    for n in notes:
        ref = "[reference] " if n["is_reference"] else ""
        source_part = f" (source: {n['source']})" if n["source"] else ""
        where = f"[{n['topic_name']}] " if n["topic_name"] else "[general] "
        tag_part = f" (tag {n['tag']})" if show_tags else ""
        lines.append(
            f"#{n['id']}{tag_part} {where}{ref}{n['content']}{source_part}"
        )
    return "\n".join(lines)


_MAX_SCHEDULE_QUERY_DAYS = 90


def _handle_create_schedule_block(tool_input: dict, chat_id: int, job_queue) -> str:
    block = database.create_schedule_block(
        chat_id=chat_id,
        start_time=tool_input["start_time"],
        end_time=tool_input["end_time"],
        day_of_week=tool_input.get("day_of_week"),
        specific_date=tool_input.get("specific_date"),
        module_name=tool_input.get("module_name"),
        class_type=tool_input.get("class_type"),
        location=tool_input.get("location"),
        week_pattern=tool_input.get("week_pattern", "every"),
    )
    when = block["day_of_week"] if block["day_of_week"] else block["specific_date"]
    module_part = f" [{block['module_name']}]" if block["module_name"] else ""
    class_part = f" {block['class_type']}" if block["class_type"] else ""
    location_part = f" at {block['location']}" if block["location"] else ""
    week_part = (
        f" (weeks: {block['week_pattern']})"
        if block.get("week_pattern") and block["week_pattern"] != "every"
        else ""
    )
    return (
        f"Created schedule block #{block['id']}{module_part}{class_part}: {when} "
        f"{block['start_time']}-{block['end_time']}{location_part}{week_part}."
    )


def _handle_query_schedule(tool_input: dict, chat_id: int, job_queue) -> str:
    date_from = tool_input.get("date_from")
    date_to = tool_input.get("date_to")
    module_name = tool_input.get("module_name")
    topic_id = tool_input.get("topic_id")
    include_subtopics = tool_input.get("include_subtopics", True)
    lesson_types = tool_input.get("lesson_types")
    label_format = tool_input.get("label_format") or database.get_timetable_label_format(chat_id)

    if date_from and date_to:
        if (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days > _MAX_SCHEDULE_QUERY_DAYS:
            raise ValueError(
                f"Range too large -- please ask for at most {_MAX_SCHEDULE_QUERY_DAYS} days at a time."
            )
        blocks = database.list_schedule_blocks(
            chat_id=chat_id, date_from=date_from, date_to=date_to,
            module_name=module_name, topic_id=topic_id,
            include_subtopics=include_subtopics, class_types=lesson_types,
        )
        anchor = database.get_semester_anchor(chat_id)
        anchor_date = date.fromisoformat(anchor) if anchor else None
        recess = frozenset(database.get_recess_weeks(chat_id))
        try:
            occurrences = scheduler.expand_occurrences(
                blocks, date_from, date_to, anchor_date, recess
            )
        except scheduler.AnchorNotSetError:
            return scheduler.ANCHOR_NOT_SET_MESSAGE
        if not occurrences:
            return "No lessons in that range."
        lines = []
        for o in occurrences:
            label = database.resolve_label(o, label_format)
            module_part = f" [{label}]" if label else ""
            class_part = f" {o['class_type']}" if o["class_type"] else ""
            location_part = f" at {o['location']}" if o["location"] else ""
            lines.append(
                f"#{o['id']}{module_part}{class_part} {o['occurrence_date']} "
                f"{o['start_time']}-{o['end_time']}{location_part}"
            )
        return "\n".join(lines)

    blocks = database.list_schedule_blocks(
        chat_id=chat_id, module_name=module_name, topic_id=topic_id,
        include_subtopics=include_subtopics, class_types=lesson_types,
    )
    if not blocks:
        return "No lessons match those filters."
    lines = []
    for b in blocks:
        when = b["day_of_week"] if b["day_of_week"] else b["specific_date"]
        label = database.resolve_label(b, label_format)
        module_part = f" [{label}]" if label else ""
        class_part = f" {b['class_type']}" if b["class_type"] else ""
        location_part = f" at {b['location']}" if b["location"] else ""
        lines.append(
            f"#{b['id']}{module_part}{class_part} {when} "
            f"{b['start_time']}-{b['end_time']}{location_part}"
        )
    return "\n".join(lines)


def _handle_delete_schedule_block(tool_input: dict, chat_id: int, job_queue) -> str:
    schedule_block_id = tool_input["schedule_block_id"]
    block = database.delete_schedule_block(chat_id, schedule_block_id)
    if block is None:
        raise ValueError(
            f"No schedule block with id {schedule_block_id} found for this chat."
        )
    when = block["day_of_week"] if block["day_of_week"] else block["specific_date"]
    return f"Deleted schedule block #{schedule_block_id} ({when} {block['start_time']}-{block['end_time']})."


def _handle_set_semester_start(tool_input: dict, chat_id: int, job_queue) -> str:
    start_date = database.set_semester_anchor(chat_id, tool_input["start_date"])
    return (
        f"Semester week 1 set to start on {start_date}. I'll use this to work "
        "out which semester week any date falls in."
    )


def _handle_set_recess_weeks(tool_input: dict, chat_id: int, job_queue) -> str:
    weeks = database.set_recess_weeks(chat_id, tool_input["recess_dates"])
    if not weeks:
        return "No recess weeks recorded."
    week_list = ", ".join(str(w) for w in weeks)
    return (
        f"Recorded {len(weeks)} recess week(s) (continuous week {week_list}). "
        "I'll skip them in the official week numbering and hide classes during "
        "them from now on."
    )


TOOL_HANDLERS: dict[str, Callable[[dict, int, object], str]] = {
    "create_task": _handle_create_task,
    "query_tasks": _handle_query_tasks,
    "update_task_status": _handle_update_task_status,
    "create_reminder": _handle_create_reminder,
    "list_topic_kinds": _handle_list_topic_kinds,
    "list_task_categories": _handle_list_task_categories,
    "list_lesson_types": _handle_list_lesson_types,
    "query_reminders": _handle_query_reminders,
    "add_event_reminder": _handle_add_event_reminder,
    "create_topic": _handle_create_topic,
    "set_topic_names": _handle_set_topic_names,
    "move_topic": _handle_move_topic,
    "set_timetable_label_format": _handle_set_timetable_label_format,
    "list_topics": _handle_list_topics,
    "add_note": _handle_add_note,
    "query_notes": _handle_query_notes,
    "create_schedule_block": _handle_create_schedule_block,
    "query_schedule": _handle_query_schedule,
    "delete_schedule_block": _handle_delete_schedule_block,
    "set_semester_start": _handle_set_semester_start,
    "set_recess_weeks": _handle_set_recess_weeks,
}


async def execute_tool(
    name: str, tool_input: dict, chat_id: int, job_queue
) -> tuple[str, bool]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Unknown tool: {name}", True
    try:
        return handler(tool_input, chat_id, job_queue), False
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return f"Error running {name}: {exc}", True