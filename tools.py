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
            "Create a new task and tag it to a module. Creates the module if "
            "it doesn't already exist for this chat."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short task title"},
                "module_name": {
                    "type": "string",
                    "description": (
                        "Module/category to tag this task to, e.g. Algorithms "
                        "or General if uncategorized"
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
            },
            "required": ["title", "module_name"],
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
                "module_name": {"type": "string"},
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
                    "description": "Optional task this reminder relates to",
                },
            },
            "required": ["trigger_datetime", "message"],
        },
    },
    {
        "name": "list_modules",
        "description": (
            "List all existing modules for this chat, with their names and "
            "colors. Use before creating a new module to avoid duplicates."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_topic",
        "description": (
            "Create a new topic under a module, or a subtopic under an "
            "existing topic (to any nesting depth). Creates the module if it "
            "doesn't already exist. If a matching topic (same name, same "
            "parent) already exists, returns that one instead of duplicating "
            "-- but call list_topics first if unsure, since matching is exact "
            "(case-insensitive) only."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Topic name, e.g. Backpropagation",
                },
                "module_name": {
                    "type": "string",
                    "description": (
                        "Module/category this topic belongs to, e.g. Machine "
                        "Learning. REQUIRED when creating a TOP-LEVEL topic "
                        "(parent_topic_id omitted). When creating a SUBTOPIC "
                        "(parent_topic_id given), omit this -- it inherits "
                        "its parent's module automatically."
                    ),
                },
                "parent_topic_id": {
                    "type": "integer",
                    "description": (
                        "id of the parent topic, if this is a subtopic. Look "
                        "it up via list_topics first -- topic names are not "
                        "unique, so parents must be identified by id, never "
                        "by name alone."
                    ),
                },
            },
            "required": ["name"],
        },
    },
    {
        "name": "list_topics",
        "description": (
            "List topics (and subtopics) for this chat, optionally filtered "
            "to one module. Each result includes its id and a full "
            "breadcrumb path, e.g. 'Machine Learning > Neural Nets > "
            "Backpropagation'. Call this before create_topic with a "
            "parent_topic_id, before add_note or query_notes (to find the "
            "target topic_id), and before creating a new topic if you're "
            "unsure whether a similar one already exists."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "module_name": {
                    "type": "string",
                    "description": "Optional -- restrict to one module",
                },
            },
            "required": [],
        },
    },
    {
        "name": "add_note",
        "description": (
            "Save a note under an existing topic. Look up the topic_id via "
            "list_topics first if the user refers to the topic by name -- "
            "create the topic first with create_topic if none exists yet. "
            "Mark is_reference=true for reference material (a link, a "
            "textbook excerpt, a definition worth keeping) as opposed to a "
            "transient study note or observation (is_reference=false, the "
            "default)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "topic_id": {"type": "integer"},
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
            "required": ["topic_id", "content"],
        },
    },
    {
        "name": "query_notes",
        "description": (
            "Retrieve notes, optionally filtered by topic, module, and/or "
            "whether they're reference material. Use this to answer "
            "questions like 'what notes do I have on backpropagation' or "
            "'show me my reference material for machine learning'. When "
            "topic_id is given, notes filed under that topic's subtopics are "
            "included by default (include_subtopics)."
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
                        "Default true. Only relevant when topic_id is given."
                    ),
                },
                "module_name": {"type": "string"},
                "is_reference": {
                    "type": "boolean",
                    "description": (
                        "Filter to reference-only (true) or non-reference-only "
                        "(false). Omit for both."
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
                "location": {
                    "type": "string",
                    "description": "Optional room/location.",
                },
            },
            "required": ["start_time", "end_time"],
        },
    },
    {
        "name": "query_schedule",
        "description": (
            "List schedule blocks. If BOTH date_from and date_to are given, "
            "recurring blocks are expanded into actual dated occurrences "
            "within that range and the result is date-ordered (use this for "
            "'what's on today/this week' -- compute the bounds yourself from "
            "the current date shown in the system prompt: 'today' is a "
            "single-day range, 'this week' is Monday-Sunday of the current "
            "calendar week). If omitted, returns the raw weekly timetable "
            "plus all one-off blocks, unexpanded (use this for 'what does "
            "my schedule look like in general')."
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
                "module_name": {
                    "type": "string",
                    "description": "Optional -- restrict to one module.",
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
]


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
        module_name=tool_input["module_name"],
        deadline=deadline_utc,
        status=tool_input.get("status", "not_started"),
    )
    deadline_part = f", due {task['deadline']}" if task["deadline"] else ""
    return (
        f"Created task #{task['id']} '{task['title']}' in module "
        f"'{task['module_name']}'{deadline_part}. Status: {task['status']}."
    )


def _handle_query_tasks(tool_input: dict, chat_id: int, job_queue) -> str:
    deadline_from_raw = tool_input.get("deadline_from")
    deadline_to_raw = tool_input.get("deadline_to")

    tasks = database.query_tasks(
        chat_id=chat_id,
        status=tool_input.get("status"),
        module_name=tool_input.get("module_name"),
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
        lines.append(
            f"#{t['id']} [{t['module_name']}] {t['title']} — "
            f"status: {t['status']}, progress: {t['progress_pct']}%{deadline_part}"
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
    )
    scheduler.schedule_reminder(
        job_queue, reminder["id"], chat_id, trigger_dt_utc, message
    )
    return (
        f"Reminder scheduled for {trigger_local_str} ({tz_name}) / "
        f"{trigger_utc_str} UTC: '{message}'."
    )


def _handle_list_modules(tool_input: dict, chat_id: int, job_queue) -> str:
    modules = database.list_modules(chat_id)
    if not modules:
        return "No modules yet."
    return "\n".join(f"{m['name']} ({m['color']})" for m in modules)


def _handle_create_topic(tool_input: dict, chat_id: int, job_queue) -> str:
    topic = database.get_or_create_topic(
        chat_id=chat_id,
        name=tool_input["name"],
        module_name=tool_input.get("module_name"),
        parent_topic_id=tool_input.get("parent_topic_id"),
    )
    topics = database.list_topics(chat_id)
    path = next((t["path"] for t in topics if t["id"] == topic["id"]), topic["name"])
    return f"Topic #{topic['id']} ready: {path}."


def _handle_list_topics(tool_input: dict, chat_id: int, job_queue) -> str:
    topics = database.list_topics(chat_id, module_name=tool_input.get("module_name"))
    if not topics:
        return "No topics yet."
    return "\n".join(f"#{t['id']} {t['path']}" for t in topics)


def _handle_add_note(tool_input: dict, chat_id: int, job_queue) -> str:
    note = database.create_note(
        chat_id=chat_id,
        topic_id=tool_input["topic_id"],
        content=tool_input["content"],
        source=tool_input.get("source"),
        is_reference=tool_input.get("is_reference", False),
    )
    tag = " (reference)" if note["is_reference"] else ""
    source_part = f", source: {note['source']}" if note["source"] else ""
    return (
        f"Saved note #{note['id']} under "
        f"'{note['module_name']} > {note['topic_name']}'{tag}{source_part}."
    )


def _handle_query_notes(tool_input: dict, chat_id: int, job_queue) -> str:
    topic_id = tool_input.get("topic_id")
    module_name = tool_input.get("module_name")
    is_reference = tool_input.get("is_reference")
    limit = tool_input.get("limit", 20)
    include_subtopics = tool_input.get("include_subtopics", True)

    topic_ids: list[int] | None = None
    if topic_id is not None:
        topic_ids = [topic_id]
        if include_subtopics:
            all_topics = database.list_topics(chat_id)
            children_by_parent: dict[int, list[int]] = {}
            for t in all_topics:
                if t["parent_topic_id"] is not None:
                    children_by_parent.setdefault(t["parent_topic_id"], []).append(
                        t["id"]
                    )
            frontier = [topic_id]
            while frontier:
                nxt = [
                    cid for tid in frontier for cid in children_by_parent.get(tid, [])
                ]
                topic_ids.extend(nxt)
                frontier = nxt

    if topic_ids is not None:
        notes = []
        for tid in topic_ids:
            notes.extend(
                database.query_notes(
                    chat_id=chat_id,
                    topic_id=tid,
                    module_name=module_name,
                    is_reference=is_reference,
                    limit=limit,
                )
            )
        notes.sort(key=lambda n: n["created_at"], reverse=True)
        notes = notes[:limit]
    else:
        notes = database.query_notes(
            chat_id=chat_id,
            module_name=module_name,
            is_reference=is_reference,
            limit=limit,
        )

    if not notes:
        return "No notes match those filters."
    lines = []
    for n in notes:
        tag = "[reference] " if n["is_reference"] else ""
        source_part = f" (source: {n['source']})" if n["source"] else ""
        lines.append(
            f"#{n['id']} [{n['module_name']} > {n['topic_name']}] "
            f"{tag}{n['content']}{source_part}"
        )
    return "\n".join(lines)


_WEEKDAY_CODES = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_MAX_SCHEDULE_QUERY_DAYS = 90


def _expand_occurrences(blocks: list[dict], date_from: str, date_to: str) -> list[dict]:
    d_from = date.fromisoformat(date_from)
    d_to = date.fromisoformat(date_to)
    occurrences = []
    for block in blocks:
        if block["specific_date"] is not None:
            block_date = date.fromisoformat(block["specific_date"])
            if d_from <= block_date <= d_to:
                occurrences.append({**block, "occurrence_date": block["specific_date"]})
        else:
            d = d_from
            while d <= d_to:
                if _WEEKDAY_CODES[d.weekday()] == block["day_of_week"]:
                    occurrences.append({**block, "occurrence_date": d.isoformat()})
                d += timedelta(days=1)
    occurrences.sort(key=lambda o: (o["occurrence_date"], o["start_time"]))
    return occurrences


def _handle_create_schedule_block(tool_input: dict, chat_id: int, job_queue) -> str:
    block = database.create_schedule_block(
        chat_id=chat_id,
        start_time=tool_input["start_time"],
        end_time=tool_input["end_time"],
        day_of_week=tool_input.get("day_of_week"),
        specific_date=tool_input.get("specific_date"),
        module_name=tool_input.get("module_name"),
        location=tool_input.get("location"),
    )
    when = block["day_of_week"] if block["day_of_week"] else block["specific_date"]
    module_part = f" [{block['module_name']}]" if block["module_name"] else ""
    location_part = f" at {block['location']}" if block["location"] else ""
    return (
        f"Created schedule block #{block['id']}{module_part}: {when} "
        f"{block['start_time']}-{block['end_time']}{location_part}."
    )


def _handle_query_schedule(tool_input: dict, chat_id: int, job_queue) -> str:
    date_from = tool_input.get("date_from")
    date_to = tool_input.get("date_to")
    module_name = tool_input.get("module_name")

    if date_from and date_to:
        if (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days > _MAX_SCHEDULE_QUERY_DAYS:
            raise ValueError(
                f"Range too large -- please ask for at most {_MAX_SCHEDULE_QUERY_DAYS} days at a time."
            )
        blocks = database.list_schedule_blocks(
            chat_id=chat_id, date_from=date_from, date_to=date_to, module_name=module_name
        )
        occurrences = _expand_occurrences(blocks, date_from, date_to)
        if not occurrences:
            return "No schedule blocks in that range."
        lines = []
        for o in occurrences:
            module_part = f" [{o['module_name']}]" if o["module_name"] else ""
            location_part = f" at {o['location']}" if o["location"] else ""
            lines.append(
                f"#{o['id']}{module_part} {o['occurrence_date']} "
                f"{o['start_time']}-{o['end_time']}{location_part}"
            )
        return "\n".join(lines)

    blocks = database.list_schedule_blocks(chat_id=chat_id, module_name=module_name)
    if not blocks:
        return "No schedule blocks yet."
    lines = []
    for b in blocks:
        when = b["day_of_week"] if b["day_of_week"] else b["specific_date"]
        module_part = f" [{b['module_name']}]" if b["module_name"] else ""
        location_part = f" at {b['location']}" if b["location"] else ""
        lines.append(
            f"#{b['id']}{module_part} {when} {b['start_time']}-{b['end_time']}{location_part}"
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


TOOL_HANDLERS: dict[str, Callable[[dict, int, object], str]] = {
    "create_task": _handle_create_task,
    "query_tasks": _handle_query_tasks,
    "update_task_status": _handle_update_task_status,
    "create_reminder": _handle_create_reminder,
    "list_modules": _handle_list_modules,
    "create_topic": _handle_create_topic,
    "list_topics": _handle_list_topics,
    "add_note": _handle_add_note,
    "query_notes": _handle_query_notes,
    "create_schedule_block": _handle_create_schedule_block,
    "query_schedule": _handle_query_schedule,
    "delete_schedule_block": _handle_delete_schedule_block,
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
