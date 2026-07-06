"""SQLite persistence layer for Kangani.

Implements the full data model up front (modules, topics, tasks, events,
schedule_blocks, notes, progress_logs, reminders) even though Phase 1 only
actively reads/writes modules, tasks, and reminders -- this avoids schema
migrations when later phases add topics/notes/events functionality.
"""

import re
import sqlite3
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).parent / "kangani.db"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Small fixed palette auto-assigned to new modules that don't specify a color,
# cycled by how many modules the chat already has.
MODULE_COLOR_PALETTE = [
    "#EF6F6C", "#5B8DEF", "#5FBB63", "#F2B134", "#9B6FE0", "#3ABAB4",
]

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS modules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    name        TEXT NOT NULL,
    color       TEXT,
    created_at  TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (chat_id, name COLLATE NOCASE)
);

CREATE TABLE IF NOT EXISTS topics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    module_id        INTEGER NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    parent_topic_id  INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    module_id     INTEGER NOT NULL REFERENCES modules(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    deadline      TEXT,
    status        TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (status IN ('not_started','in_progress','blocked','done')),
    progress_pct  INTEGER NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     INTEGER NOT NULL,
    title       TEXT NOT NULL,
    type        TEXT CHECK (type IN ('talk','hackathon','other')),
    start_date  TEXT,
    end_date    TEXT,
    location    TEXT,
    created_at  TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS schedule_blocks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    module_id      INTEGER REFERENCES modules(id) ON DELETE CASCADE,
    day_of_week    TEXT CHECK (day_of_week IN ('MON','TUE','WED','THU','FRI','SAT','SUN')),
    specific_date  TEXT,
    start_time     TEXT NOT NULL,
    end_time       TEXT NOT NULL,
    location       TEXT,
    created_at     TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK ((day_of_week IS NULL) <> (specific_date IS NULL))
);

CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id      INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    source        TEXT,
    content       TEXT NOT NULL,
    is_reference  INTEGER NOT NULL DEFAULT 0 CHECK (is_reference IN (0,1)),
    created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS progress_logs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id           INTEGER NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    type               TEXT,
    timestamp          TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    confidence_rating  INTEGER CHECK (confidence_rating BETWEEN 1 AND 5)
);

CREATE TABLE IF NOT EXISTS reminders (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id           INTEGER NOT NULL,
    type              TEXT NOT NULL CHECK (type IN ('time','location')),
    trigger_data      TEXT NOT NULL,
    linked_task_id    INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    linked_event_id   INTEGER REFERENCES events(id) ON DELETE CASCADE,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','fired','cancelled')),
    message           TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (linked_task_id IS NULL OR linked_event_id IS NULL)
);

CREATE INDEX IF NOT EXISTS idx_tasks_chat_status     ON tasks(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline        ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_topics_module         ON topics(module_id);
CREATE INDEX IF NOT EXISTS idx_topics_parent         ON topics(parent_topic_id);
CREATE INDEX IF NOT EXISTS idx_notes_topic           ON notes(topic_id);
CREATE INDEX IF NOT EXISTS idx_progress_logs_topic   ON progress_logs(topic_id);
CREATE INDEX IF NOT EXISTS idx_schedule_blocks_chat  ON schedule_blocks(chat_id);
CREATE INDEX IF NOT EXISTS idx_events_chat           ON events(chat_id);
CREATE INDEX IF NOT EXISTS idx_reminders_chat_status ON reminders(chat_id, status);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    conn = get_connection()
    try:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
    finally:
        conn.close()


# --- modules ---------------------------------------------------------------

def get_or_create_module(chat_id: int, name: str, color: str | None = None) -> dict:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM modules WHERE chat_id = ? AND name = ? COLLATE NOCASE",
            (chat_id, name),
        ).fetchone()
        if row:
            return dict(row)

        if color is None:
            existing_count = conn.execute(
                "SELECT COUNT(*) FROM modules WHERE chat_id = ?", (chat_id,)
            ).fetchone()[0]
            color = MODULE_COLOR_PALETTE[existing_count % len(MODULE_COLOR_PALETTE)]

        try:
            cur = conn.execute(
                "INSERT INTO modules (chat_id, name, color) VALUES (?, ?, ?)",
                (chat_id, name, color),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost a create-vs-create race against another connection --
            # the module now exists, so fetch and return it instead of erroring.
            row = conn.execute(
                "SELECT * FROM modules WHERE chat_id = ? AND name = ? COLLATE NOCASE",
                (chat_id, name),
            ).fetchone()
            return dict(row)
        row = conn.execute(
            "SELECT * FROM modules WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_modules(chat_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM modules WHERE chat_id = ? ORDER BY name COLLATE NOCASE",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- tasks -------------------------------------------------------------

def create_task(
    chat_id: int,
    title: str,
    module_name: str,
    deadline: str | None = None,
    status: str = "not_started",
) -> dict:
    module = get_or_create_module(chat_id, module_name)
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (chat_id, module_id, title, deadline, status) "
            "VALUES (?, ?, ?, ?, ?)",
            (chat_id, module["id"], title, deadline, status),
        )
        conn.commit()
        row = conn.execute(
            "SELECT tasks.*, modules.name AS module_name FROM tasks "
            "JOIN modules ON modules.id = tasks.module_id WHERE tasks.id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def update_task_status(
    chat_id: int,
    task_id: int,
    status: str | None = None,
    progress_pct: int | None = None,
) -> dict | None:
    conn = get_connection()
    try:
        # A single atomic UPDATE (rather than SELECT-then-compute-then-UPDATE)
        # avoids a read-modify-write race between two concurrent updates on
        # the same task (e.g. a nav-layer button tap racing a conversational
        # update) silently losing one of the writes.
        cur = conn.execute(
            "UPDATE tasks SET status = COALESCE(?, status), "
            "progress_pct = COALESCE(?, progress_pct), "
            "updated_at = STRFTIME('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE id = ? AND chat_id = ?",
            (status, progress_pct, task_id, chat_id),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT tasks.*, modules.name AS module_name FROM tasks "
            "JOIN modules ON modules.id = tasks.module_id WHERE tasks.id = ?",
            (task_id,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_task(chat_id: int, task_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT tasks.*, modules.name AS module_name FROM tasks "
            "JOIN modules ON modules.id = tasks.module_id "
            "WHERE tasks.id = ? AND tasks.chat_id = ?",
            (task_id, chat_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def query_tasks(
    chat_id: int,
    status: str | None = None,
    module_name: str | None = None,
    deadline_from: str | None = None,
    deadline_to: str | None = None,
    limit: int = 20,
) -> list[dict]:
    conn = get_connection()
    try:
        clauses = ["tasks.chat_id = ?"]
        params: list = [chat_id]

        if status is not None:
            clauses.append("tasks.status = ?")
            params.append(status)
        if module_name is not None:
            clauses.append("modules.name = ? COLLATE NOCASE")
            params.append(module_name)
        if deadline_from is not None:
            clauses.append("tasks.deadline >= ?")
            params.append(deadline_from)
        if deadline_to is not None:
            clauses.append("tasks.deadline <= ?")
            params.append(deadline_to)

        params.append(limit)
        sql = (
            "SELECT tasks.*, modules.name AS module_name FROM tasks "
            "JOIN modules ON modules.id = tasks.module_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY tasks.deadline IS NULL, tasks.deadline ASC LIMIT ?"
        )
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- reminders ---------------------------------------------------------

def create_reminder(
    chat_id: int,
    trigger_datetime_utc: str,
    message: str,
    linked_task_id: int | None = None,
    linked_event_id: int | None = None,
) -> dict:
    conn = get_connection()
    try:
        if linked_task_id is not None:
            task_row = conn.execute(
                "SELECT id FROM tasks WHERE id = ? AND chat_id = ?",
                (linked_task_id, chat_id),
            ).fetchone()
            if task_row is None:
                raise ValueError(
                    f"No task with id {linked_task_id} found for this chat."
                )

        cur = conn.execute(
            "INSERT INTO reminders (chat_id, type, trigger_data, message, "
            "linked_task_id, linked_event_id) VALUES (?, 'time', ?, ?, ?, ?)",
            (chat_id, trigger_datetime_utc, message, linked_task_id, linked_event_id),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_pending_future_reminders() -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE status = 'pending' "
            "AND trigger_data > STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def mark_reminder_fired(reminder_id: int) -> None:
    conn = get_connection()
    try:
        conn.execute(
            "UPDATE reminders SET status = 'fired' WHERE id = ?", (reminder_id,)
        )
        conn.commit()
    finally:
        conn.close()


def get_reminder(chat_id: int, reminder_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ? AND chat_id = ?",
            (reminder_id, chat_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def cancel_reminder(chat_id: int, reminder_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ? AND chat_id = ?",
            (reminder_id, chat_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE id = ?", (reminder_id,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_pending_reminders(chat_id: int) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE chat_id = ? AND status = 'pending' "
            "ORDER BY trigger_data ASC",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- topics --------------------------------------------------------------

def get_or_create_topic(
    chat_id: int,
    name: str,
    module_name: str | None = None,
    parent_topic_id: int | None = None,
) -> dict:
    conn = get_connection()
    try:
        if parent_topic_id is not None:
            parent = conn.execute(
                "SELECT topics.*, modules.chat_id AS chat_id, "
                "modules.name AS module_name FROM topics "
                "JOIN modules ON modules.id = topics.module_id "
                "WHERE topics.id = ?",
                (parent_topic_id,),
            ).fetchone()
            if parent is None or parent["chat_id"] != chat_id:
                raise ValueError(
                    f"No topic with id {parent_topic_id} found for this chat."
                )
            module_id = parent["module_id"]
            if (
                module_name is not None
                and module_name.strip().casefold() != parent["module_name"].casefold()
            ):
                raise ValueError(
                    f"Parent topic #{parent_topic_id} belongs to module "
                    f"'{parent['module_name']}', not '{module_name}'. Subtopics "
                    "inherit their parent's module automatically -- omit "
                    "module_name or pass the matching one."
                )
        else:
            if not module_name:
                raise ValueError(
                    "module_name is required when creating a top-level topic "
                    "(no parent_topic_id)."
                )
            module = get_or_create_module(chat_id, module_name)
            module_id = module["id"]

        row = conn.execute(
            "SELECT * FROM topics WHERE module_id = ? AND name = ? COLLATE NOCASE "
            "AND parent_topic_id IS ?",
            (module_id, name, parent_topic_id),
        ).fetchone()
        if row:
            return dict(row)

        cur = conn.execute(
            "INSERT INTO topics (module_id, parent_topic_id, name) VALUES (?, ?, ?)",
            (module_id, parent_topic_id, name),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM topics WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_topics(chat_id: int, module_name: str | None = None) -> list[dict]:
    conn = get_connection()
    try:
        clauses = ["modules.chat_id = ?"]
        params: list = [chat_id]
        if module_name is not None:
            clauses.append("modules.name = ? COLLATE NOCASE")
            params.append(module_name)
        sql = (
            "SELECT topics.*, modules.name AS module_name FROM topics "
            "JOIN modules ON modules.id = topics.module_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY modules.name COLLATE NOCASE, topics.name COLLATE NOCASE"
        )
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()

    by_id = {r["id"]: r for r in rows}

    def build_path(topic: dict) -> str:
        # Defensive cycle guard: structurally impossible given this phase's
        # tools (a topic's parent_topic_id is fixed at insert time and there
        # is no reparent tool), but cheap insurance against a manual DB edit.
        names: list[str] = []
        cur = topic
        seen: set[int] = set()
        while cur is not None and cur["id"] not in seen:
            names.append(cur["name"])
            seen.add(cur["id"])
            pid = cur["parent_topic_id"]
            cur = by_id.get(pid) if pid is not None else None
        names.reverse()
        return " > ".join([topic["module_name"]] + names)

    for r in rows:
        r["path"] = build_path(r)
    return rows


# --- notes -----------------------------------------------------------------

def create_note(
    chat_id: int,
    topic_id: int,
    content: str,
    source: str | None = None,
    is_reference: bool = False,
) -> dict:
    conn = get_connection()
    try:
        topic = conn.execute(
            "SELECT topics.id, modules.chat_id AS chat_id FROM topics "
            "JOIN modules ON modules.id = topics.module_id WHERE topics.id = ?",
            (topic_id,),
        ).fetchone()
        if topic is None or topic["chat_id"] != chat_id:
            raise ValueError(f"No topic with id {topic_id} found for this chat.")

        cur = conn.execute(
            "INSERT INTO notes (topic_id, source, content, is_reference) "
            "VALUES (?, ?, ?, ?)",
            (topic_id, source, content, 1 if is_reference else 0),
        )
        conn.commit()
        row = conn.execute(
            "SELECT notes.*, topics.name AS topic_name, modules.name AS module_name "
            "FROM notes JOIN topics ON topics.id = notes.topic_id "
            "JOIN modules ON modules.id = topics.module_id WHERE notes.id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def query_notes(
    chat_id: int,
    topic_id: int | None = None,
    module_name: str | None = None,
    is_reference: bool | None = None,
    limit: int = 20,
) -> list[dict]:
    conn = get_connection()
    try:
        clauses = ["modules.chat_id = ?"]
        params: list = [chat_id]
        if topic_id is not None:
            clauses.append("notes.topic_id = ?")
            params.append(topic_id)
        if module_name is not None:
            clauses.append("modules.name = ? COLLATE NOCASE")
            params.append(module_name)
        if is_reference is not None:
            clauses.append("notes.is_reference = ?")
            params.append(1 if is_reference else 0)

        params.append(limit)
        sql = (
            "SELECT notes.*, topics.name AS topic_name, modules.name AS module_name "
            "FROM notes "
            "JOIN topics ON topics.id = notes.topic_id "
            "JOIN modules ON modules.id = topics.module_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY notes.created_at DESC LIMIT ?"
        )
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- schedule blocks ---------------------------------------------------

def create_schedule_block(
    chat_id: int,
    start_time: str,
    end_time: str,
    day_of_week: str | None = None,
    specific_date: str | None = None,
    module_name: str | None = None,
    location: str | None = None,
) -> dict:
    if (day_of_week is None) == (specific_date is None):
        raise ValueError(
            "Exactly one of day_of_week or specific_date must be given "
            "(recurring blocks use day_of_week, one-off blocks use specific_date)."
        )
    if not _TIME_RE.match(start_time):
        raise ValueError(f"start_time must be 24-hour HH:MM, got {start_time!r}")
    if not _TIME_RE.match(end_time):
        raise ValueError(f"end_time must be 24-hour HH:MM, got {end_time!r}")
    if specific_date is not None:
        try:
            date.fromisoformat(specific_date)
        except ValueError:
            raise ValueError(
                f"specific_date must be an ISO-8601 date YYYY-MM-DD, got {specific_date!r}"
            ) from None

    module_id = get_or_create_module(chat_id, module_name)["id"] if module_name else None
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO schedule_blocks (chat_id, module_id, day_of_week, "
            "specific_date, start_time, end_time, location) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, module_id, day_of_week, specific_date, start_time, end_time, location),
        )
        conn.commit()
        row = conn.execute(
            "SELECT schedule_blocks.*, modules.name AS module_name FROM schedule_blocks "
            "LEFT JOIN modules ON modules.id = schedule_blocks.module_id "
            "WHERE schedule_blocks.id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_schedule_blocks(
    chat_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    module_name: str | None = None,
) -> list[dict]:
    conn = get_connection()
    try:
        sql = (
            "SELECT schedule_blocks.*, modules.name AS module_name FROM schedule_blocks "
            "LEFT JOIN modules ON modules.id = schedule_blocks.module_id "
            "WHERE schedule_blocks.chat_id = ? "
            "AND (schedule_blocks.day_of_week IS NOT NULL "
            "     OR ((? IS NULL OR schedule_blocks.specific_date >= ?) "
            "         AND (? IS NULL OR schedule_blocks.specific_date <= ?))) "
            "AND (? IS NULL OR modules.name = ? COLLATE NOCASE) "
            "ORDER BY schedule_blocks.specific_date IS NULL, schedule_blocks.specific_date, "
            "schedule_blocks.day_of_week, schedule_blocks.start_time"
        )
        params = (
            chat_id,
            date_from, date_from,
            date_to, date_to,
            module_name, module_name,
        )
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_schedule_block(chat_id: int, schedule_block_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT schedule_blocks.*, modules.name AS module_name FROM schedule_blocks "
            "LEFT JOIN modules ON modules.id = schedule_blocks.module_id "
            "WHERE schedule_blocks.id = ? AND schedule_blocks.chat_id = ?",
            (schedule_block_id, chat_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "DELETE FROM schedule_blocks WHERE id = ?", (schedule_block_id,)
        )
        conn.commit()
        return dict(row)
    finally:
        conn.close()
