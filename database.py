"""SQLite persistence layer for Kangani.

Implements the full data model up front (modules, topics, tasks, events,
schedule_blocks, notes, progress_logs, reminders) even though Phase 1 only
actively reads/writes modules, tasks, and reminders -- this avoids schema
migrations when later phases add topics/notes/events functionality.
"""

import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("DB_PATH", str(Path(__file__).parent / "kangani.db")))


def _now_utc_iso() -> str:
    """Canonical UTC 'YYYY-MM-DDTHH:MM:SS.mmmZ' timestamp -- the same format
    stored in tasks.deadline / reminders.trigger_data / topics.event_datetime,
    so lexicographic string comparison against those columns is valid."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# Small fixed palette auto-assigned to new modules that don't specify a color,
# cycled by how many modules the chat already has. These are the 8 --m-* hues
# from the approved monthly-timetable mockup, so a module's color is identical
# everywhere it appears (text views, and now the rendered timetable images).
MODULE_COLOR_PALETTE = [
    "#B5646B", "#748264", "#5E7A93", "#C79A44",
    "#8C5B7C", "#4F8074", "#A8763E", "#9B5A5A",
]

# Target schema for a FRESH database. Existing databases are brought to this
# shape by the data-preserving migration in _migrate_topics_v2 (NOT by a
# drop-and-recreate) -- `modules` and `events` are collapsed into the unified
# `topics` tree. Indexes live in _INDEXES (created after any migration, so they
# never reference a column that only exists post-migration on an existing DB).
SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS topics (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id          INTEGER NOT NULL,
    parent_topic_id  INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    name             TEXT NOT NULL,
    full_name        TEXT,
    nickname         TEXT,
    kind             TEXT,
    status           TEXT,
    event_datetime   TEXT,
    color            TEXT,
    created_at       TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
    -- Uniqueness deliberately does NOT live here as a table constraint. A
    -- table-level UNIQUE only exists on a FRESH database: _migrate_topics_v2
    -- builds topics_new and renames it over `topics`, after which
    -- CREATE TABLE IF NOT EXISTS is a permanent no-op, so a migrated DB would
    -- silently never get it. It also can't express what we actually want --
    -- SQLite treats NULLs as distinct, so UNIQUE(chat_id, parent_topic_id,
    -- name) is toothless at the root, which is exactly where modules and
    -- events live. See idx_topics_unique_name in _INDEXES.
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    category      TEXT,
    tag           TEXT,
    deadline      TEXT,
    status        TEXT NOT NULL DEFAULT 'not_started'
                    CHECK (status IN ('not_started','in_progress','blocked','done')),
    progress_pct  INTEGER NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
    created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS schedule_blocks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id        INTEGER NOT NULL,
    topic_id       INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    day_of_week    TEXT CHECK (day_of_week IN ('MON','TUE','WED','THU','FRI','SAT','SUN')),
    specific_date  TEXT,
    start_time     TEXT NOT NULL,
    end_time       TEXT NOT NULL,
    class_type     TEXT,
    location       TEXT,
    created_at     TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK ((day_of_week IS NULL) <> (specific_date IS NULL))
);

CREATE TABLE IF NOT EXISTS notes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    topic_id      INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    tag           TEXT,
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
    linked_topic_id   INTEGER REFERENCES topics(id) ON DELETE CASCADE,
    tag               TEXT,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','fired','cancelled')),
    message           TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (linked_task_id IS NULL OR linked_topic_id IS NULL)
);

CREATE TABLE IF NOT EXISTS chat_settings (
    chat_id                   INTEGER PRIMARY KEY,
    semester_week1_start_date TEXT,
    timetable_label_format    TEXT NOT NULL DEFAULT 'code'
);
"""

# Created after the schema is in its final shape (post-migration), so every
# indexed column is guaranteed to exist on both fresh and migrated databases.
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_tasks_chat_status     ON tasks(chat_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_deadline        ON tasks(deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_topic           ON tasks(topic_id);
CREATE INDEX IF NOT EXISTS idx_topics_chat           ON topics(chat_id);
CREATE INDEX IF NOT EXISTS idx_topics_parent         ON topics(parent_topic_id);
CREATE INDEX IF NOT EXISTS idx_notes_topic           ON notes(topic_id);
CREATE INDEX IF NOT EXISTS idx_progress_logs_topic   ON progress_logs(topic_id);
CREATE INDEX IF NOT EXISTS idx_schedule_blocks_chat  ON schedule_blocks(chat_id);
CREATE INDEX IF NOT EXISTS idx_reminders_chat_status ON reminders(chat_id, status);
"""

# The topic-uniqueness guarantee, as an INDEX rather than a table constraint:
# an index applies identically to a fresh DB and to one that came through
# _migrate_topics_v2's table-rename, and COALESCE(parent_topic_id, -1) makes it
# bind at the root as well (a bare UNIQUE would not -- SQLite NULLs are
# distinct, so two root topics both named 'CZ2001' would both be accepted).
# Created separately from _INDEXES because it can legitimately FAIL on a DB that
# already accumulated duplicates under the old, unenforced schema.
_TOPIC_UNIQUE_INDEX = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_topics_unique_name "
    "ON topics (chat_id, COALESCE(parent_topic_id, -1), name COLLATE NOCASE)"
)

_SCHEMA_VERSION = 2
# Used only by the legacy drop-and-recreate path, which now fires solely for a
# brand-new/empty DB (version < _SCHEMA_VERSION). 'modules'/'events' stay listed
# so a stale pre-migration fresh DB still gets them dropped harmlessly.
_ALL_TABLES = [
    "reminders", "progress_logs", "notes", "schedule_blocks",
    "tasks", "topics", "events", "modules", "chat_settings",
]

# Default reminder lead times (minutes before event_datetime) auto-created for a
# topic that has an event_datetime.
DEFAULT_EVENT_REMINDER_OFFSETS = [60, 30]

# Valid schedule_block.week_pattern values (besides an explicit week-number list):
_FIXED_WEEK_PATTERNS = ("every", "odd", "even")
_MAX_SEMESTER_WEEK = 13


def _validate_week_pattern(week_pattern: str) -> str:
    """Validate and normalize a schedule_block week_pattern.

    Accepts 'every', 'odd', 'even', or a comma-separated list of week numbers
    each in 1.._MAX_SEMESTER_WEEK (e.g. '1,3,5,7,9,11,13'). Returns the
    canonical form (list normalized to comma-joined ints, no spaces); raises
    ValueError on anything else so a malformed pattern never reaches the DB.
    """
    if week_pattern in _FIXED_WEEK_PATTERNS:
        return week_pattern
    parts = [p.strip() for p in week_pattern.split(",")]
    if not parts or not all(
        p.isdigit() and 1 <= int(p) <= _MAX_SEMESTER_WEEK for p in parts
    ):
        raise ValueError(
            "week_pattern must be 'every', 'odd', 'even', or a comma-separated "
            f"list of week numbers 1-{_MAX_SEMESTER_WEEK}, got {week_pattern!r}"
        )
    return ",".join(str(int(p)) for p in parts)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    """Additively add a nullable column if it's missing -- an in-place,
    data-preserving migration (SQLite CAN add a column via ALTER TABLE, unlike
    relaxing NOT NULL or adding a CHECK). Idempotent, so it's safe to run on
    every startup, and it never drops or rewrites the table -- important
    because kangani.db may hold real user data.
    """
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _migrate_topics_v2(conn: sqlite3.Connection) -> None:
    """One-time, data-preserving migration collapsing `modules` + `events` into
    the unified `topics` tree. Idempotent: detected by the presence of the old
    `modules` table, which this drops on completion, so a second run is a no-op
    (as is a fresh DB, which never had `modules`).

    Real user data is at stake, so this uses the universally-safe
    create-new-table / copy-rows / drop-old / rename pattern (no reliance on a
    given SQLite version's ALTER ... DROP COLUMN support), wrapped in a single
    transaction with foreign keys off, and validated with foreign_key_check
    before it commits. Nothing is dropped until every row has been copied and
    re-pointed.
    """
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "modules" not in tables:
        return  # fresh DB, or already migrated

    prev_isolation = conn.isolation_level
    conn.isolation_level = None  # autocommit -> PRAGMA foreign_keys takes effect
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")

        for tmp in ("topics_new", "tasks_new", "schedule_blocks_new", "reminders_new"):
            conn.execute(f"DROP TABLE IF EXISTS {tmp}")

        # New topics table (same columns as SCHEMA's topics, built here so the
        # migration is self-contained and doesn't depend on statement order).
        conn.execute("""
            CREATE TABLE topics_new (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id          INTEGER NOT NULL,
                parent_topic_id  INTEGER REFERENCES topics_new(id) ON DELETE CASCADE,
                name             TEXT NOT NULL,
                kind             TEXT,
                status           TEXT,
                event_datetime   TEXT,
                color            TEXT,
                created_at       TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)

        # 1. modules -> topics(kind='module'), carrying name + color.
        mod_map: dict[int, int] = {}
        mod_chat: dict[int, int] = {}
        for m in conn.execute("SELECT * FROM modules").fetchall():
            cur = conn.execute(
                "INSERT INTO topics_new (chat_id, parent_topic_id, name, kind, color, "
                "created_at) VALUES (?, NULL, ?, 'module', ?, ?)",
                (m["chat_id"], m["name"], m["color"], m["created_at"]),
            )
            mod_map[m["id"]] = cur.lastrowid
            mod_chat[m["id"]] = m["chat_id"]

        # 2. events -> topics(kind='event[:type]'), title->name, start_date->
        #    event_datetime. The old talk/hackathon/other type is folded into the
        #    free-string kind (e.g. 'event:hackathon'); end_date/location have no
        #    topic column and are dropped (flagged: 0 event rows in the live DB).
        evt_map: dict[int, int] = {}
        evt_chat: dict[int, int] = {}
        if "events" in tables:
            for e in conn.execute("SELECT * FROM events").fetchall():
                kind = f"event:{e['type']}" if e["type"] else "event"
                cur = conn.execute(
                    "INSERT INTO topics_new (chat_id, parent_topic_id, name, kind, "
                    "event_datetime, created_at) VALUES (?, NULL, ?, ?, ?, ?)",
                    (e["chat_id"], e["title"], kind, e["start_date"], e["created_at"]),
                )
                evt_map[e["id"]] = cur.lastrowid
                evt_chat[e["id"]] = e["chat_id"]

        # 3. old topics -> topics_new. Old topics rooted at a module/event become
        #    children of the corresponding new module/event topic; nested topics
        #    keep their parent. Two passes so parents (which may sort after
        #    children) are always resolvable via the id map.
        old_topics = conn.execute("SELECT * FROM topics").fetchall()
        topic_map: dict[int, int] = {}
        for t in old_topics:
            chat = (mod_chat.get(t["module_id"]) if t["module_id"] is not None
                    else evt_chat.get(t["event_id"]))
            cur = conn.execute(
                "INSERT INTO topics_new (chat_id, parent_topic_id, name, created_at) "
                "VALUES (?, NULL, ?, ?)",
                (chat, t["name"], t["created_at"]),
            )
            topic_map[t["id"]] = cur.lastrowid
        for t in old_topics:
            if t["parent_topic_id"] is not None:
                parent = topic_map.get(t["parent_topic_id"])
            elif t["module_id"] is not None:
                parent = mod_map.get(t["module_id"])
            else:
                parent = evt_map.get(t["event_id"])
            conn.execute(
                "UPDATE topics_new SET parent_topic_id = ? WHERE id = ?",
                (parent, topic_map[t["id"]]),
            )

        # Notes / progress_logs reference topic ids, which just changed -> remap.
        for old_id, new_id in topic_map.items():
            conn.execute("UPDATE notes SET topic_id = ? WHERE topic_id = ?", (new_id, old_id))
            conn.execute("UPDATE progress_logs SET topic_id = ? WHERE topic_id = ?", (new_id, old_id))

        def _topic_for(module_id, event_id):
            if module_id is not None:
                return mod_map.get(module_id)
            if event_id is not None:
                return evt_map.get(event_id)
            return None

        # 4. tasks: module_id/event_id -> single topic_id.
        conn.execute("""
            CREATE TABLE tasks_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                topic_id      INTEGER REFERENCES topics_new(id) ON DELETE CASCADE,
                title         TEXT NOT NULL,
                deadline      TEXT,
                status        TEXT NOT NULL DEFAULT 'not_started'
                                CHECK (status IN ('not_started','in_progress','blocked','done')),
                progress_pct  INTEGER NOT NULL DEFAULT 0 CHECK (progress_pct BETWEEN 0 AND 100),
                created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
                updated_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)
        for t in conn.execute("SELECT * FROM tasks").fetchall():
            conn.execute(
                "INSERT INTO tasks_new (id, chat_id, topic_id, title, deadline, status, "
                "progress_pct, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (t["id"], t["chat_id"], _topic_for(t["module_id"], t["event_id"]),
                 t["title"], t["deadline"], t["status"], t["progress_pct"],
                 t["created_at"], t["updated_at"]),
            )

        # 5. schedule_blocks: module_id -> topic_id (keeps class_type/week_pattern).
        conn.execute("""
            CREATE TABLE schedule_blocks_new (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id        INTEGER NOT NULL,
                topic_id       INTEGER REFERENCES topics_new(id) ON DELETE CASCADE,
                day_of_week    TEXT CHECK (day_of_week IN ('MON','TUE','WED','THU','FRI','SAT','SUN')),
                specific_date  TEXT,
                start_time     TEXT NOT NULL,
                end_time       TEXT NOT NULL,
                class_type     TEXT,
                location       TEXT,
                week_pattern   TEXT NOT NULL DEFAULT 'every',
                created_at     TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
                CHECK ((day_of_week IS NULL) <> (specific_date IS NULL))
            )
        """)
        for s in conn.execute("SELECT * FROM schedule_blocks").fetchall():
            conn.execute(
                "INSERT INTO schedule_blocks_new (id, chat_id, topic_id, day_of_week, "
                "specific_date, start_time, end_time, class_type, location, week_pattern, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s["id"], s["chat_id"], _topic_for(s["module_id"], None), s["day_of_week"],
                 s["specific_date"], s["start_time"], s["end_time"], s["class_type"],
                 s["location"], s["week_pattern"], s["created_at"]),
            )

        # 6. reminders: linked_event_id -> linked_topic_id.
        conn.execute("""
            CREATE TABLE reminders_new (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id           INTEGER NOT NULL,
                type              TEXT NOT NULL CHECK (type IN ('time','location')),
                trigger_data      TEXT NOT NULL,
                linked_task_id    INTEGER REFERENCES tasks_new(id) ON DELETE CASCADE,
                linked_topic_id   INTEGER REFERENCES topics_new(id) ON DELETE CASCADE,
                status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','fired','cancelled')),
                message           TEXT NOT NULL,
                created_at        TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now')),
                CHECK (linked_task_id IS NULL OR linked_topic_id IS NULL)
            )
        """)
        for r in conn.execute("SELECT * FROM reminders").fetchall():
            conn.execute(
                "INSERT INTO reminders_new (id, chat_id, type, trigger_data, linked_task_id, "
                "linked_topic_id, status, message, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (r["id"], r["chat_id"], r["type"], r["trigger_data"], r["linked_task_id"],
                 evt_map.get(r["linked_event_id"]), r["status"], r["message"], r["created_at"]),
            )

        # 7. Only now, everything copied and re-pointed: drop old, rename new.
        conn.execute("DROP TABLE reminders")
        conn.execute("DROP TABLE schedule_blocks")
        conn.execute("DROP TABLE tasks")
        conn.execute("DROP TABLE topics")
        conn.execute("DROP TABLE IF EXISTS events")
        conn.execute("DROP TABLE modules")
        conn.execute("ALTER TABLE topics_new RENAME TO topics")
        conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
        conn.execute("ALTER TABLE schedule_blocks_new RENAME TO schedule_blocks")
        conn.execute("ALTER TABLE reminders_new RENAME TO reminders")

        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"topic migration left dangling references: {broken}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = prev_isolation


def _migrate_notes_general(conn: sqlite3.Connection) -> None:
    """One-time, data-preserving migration making `notes.topic_id` nullable
    (so a note can be "general" -- attached to nothing) and giving `notes`
    its own `chat_id`, since a topic-less note has no topic to derive one
    from.

    SQLite can't relax a column's NOT NULL via ALTER TABLE, so this uses the
    same create-new-table / copy-rows / drop-old / rename pattern as
    _migrate_topics_v2. Idempotent: detected by notes.topic_id still being
    NOT NULL, so a second run (or a fresh DB, built NOT NULL-free by SCHEMA
    already) is a no-op.
    """
    info = conn.execute("PRAGMA table_info(notes)").fetchall()
    topic_id_col = next((r for r in info if r["name"] == "topic_id"), None)
    if topic_id_col is None or topic_id_col["notnull"] == 0:
        return  # fresh DB, or already migrated

    prev_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        conn.execute("DROP TABLE IF EXISTS notes_new")
        conn.execute("""
            CREATE TABLE notes_new (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id       INTEGER NOT NULL,
                topic_id      INTEGER REFERENCES topics(id) ON DELETE CASCADE,
                tag           TEXT,
                source        TEXT,
                content       TEXT NOT NULL,
                is_reference  INTEGER NOT NULL DEFAULT 0 CHECK (is_reference IN (0,1)),
                created_at    TEXT NOT NULL DEFAULT (STRFTIME('%Y-%m-%dT%H:%M:%fZ','now'))
            )
        """)
        # Every pre-existing note has a topic_id (it was NOT NULL until now),
        # so chat_id can always be backfilled from its topic.
        conn.execute("""
            INSERT INTO notes_new (id, chat_id, topic_id, source, content, is_reference, created_at)
            SELECT notes.id, topics.chat_id, notes.topic_id, notes.source,
                   notes.content, notes.is_reference, notes.created_at
            FROM notes JOIN topics ON topics.id = notes.topic_id
        """)
        conn.execute("DROP TABLE notes")
        conn.execute("ALTER TABLE notes_new RENAME TO notes")

        broken = conn.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"notes migration left dangling references: {broken}")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = prev_isolation


# Hidden reference tags for notes/tasks/reminders: short, random, and stable
# for the item's whole life -- unlike the auto-increment id, deleting one
# item never causes another's tag to change or be reassigned. Deliberately
# NOT the id: an id reveals creation order and total count; a tag doesn't,
# and stays hidden from normal listings unless the user asks for it.
_TAG_TABLES = ("notes", "tasks", "reminders")


def _new_tag(conn: sqlite3.Connection, table: str) -> str:
    """Short random alphanumeric tag, unique within `table`. Collisions are
    astronomically unlikely at this scale (6 hex chars = 16M+ values), but
    the retry loop makes the guarantee real rather than assumed."""
    for _ in range(10):
        candidate = secrets.token_hex(3)
        exists = conn.execute(
            f"SELECT 1 FROM {table} WHERE tag = ?", (candidate,)
        ).fetchone()
        if not exists:
            return candidate
    raise RuntimeError(f"Could not generate a unique tag for {table} after 10 tries")


def _backfill_tags(conn: sqlite3.Connection) -> None:
    """Assign a tag to any pre-existing note/task/reminder that predates the
    tag column. Additive and idempotent -- only touches rows where tag IS
    NULL, so every run after the first is a no-op."""
    for table in _TAG_TABLES:
        rows = conn.execute(f"SELECT id FROM {table} WHERE tag IS NULL").fetchall()
        for row in rows:
            conn.execute(
                f"UPDATE {table} SET tag = ? WHERE id = ?",
                (_new_tag(conn, table), row["id"]),
            )
    conn.commit()


def init_db() -> None:
    """Create the schema, or reset it if it's behind _SCHEMA_VERSION.

    This is a local, gitignored, personal dev database -- when the schema
    changes in a way SQLite can't ALTER in place (relaxing a NOT NULL,
    adding a CHECK), the pragmatic move is to drop and recreate rather than
    build a real migration/data-preservation framework for pre-launch data.
    foreign_keys is turned OFF before the drop (and back ON after) so
    dropping a referenced table doesn't cascade-delete rows out of tables
    that haven't been dropped yet -- moot here since every table is dropped
    together, but cheap insurance.
    """
    conn = get_connection()
    try:
        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version < _SCHEMA_VERSION:
            conn.execute("PRAGMA foreign_keys = OFF")
            for table in _ALL_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()
            conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        # Additive, non-destructive migrations for schema tweaks that don't
        # need a full recreate -- these run on an EXISTING (possibly live) DB
        # without touching its rows, so they must NOT be gated behind the
        # drop-and-recreate _SCHEMA_VERSION bump above.
        _ensure_column(conn, "schedule_blocks", "class_type", "TEXT")
        _ensure_column(
            conn, "schedule_blocks", "week_pattern", "TEXT NOT NULL DEFAULT 'every'"
        )
        # Comma-separated CONTINUOUS week numbers marked as recess (no teaching),
        # e.g. "7" or "7,14"; NULL/empty means none. Nullable, added additively
        # so it never disturbs an existing (possibly live) chat_settings row.
        _ensure_column(conn, "chat_settings", "recess_weeks", "TEXT")
        # Task's user-set subcategory (mirrors topics.kind) and the hidden
        # reference tags -- additive/nullable so existing rows are untouched
        # until _backfill_tags runs below.
        _ensure_column(conn, "tasks", "category", "TEXT")
        _ensure_column(conn, "tasks", "tag", "TEXT")
        _ensure_column(conn, "reminders", "tag", "TEXT")
        # Separate naming fields: `name` stays the canonical/display identifier
        # (matching, breadcrumbs, and the 'code' label format); full_name is
        # the official long title; nickname is a user-chosen short label.
        # None of these ever overwrite each other -- see resolve_label.
        _ensure_column(conn, "topics", "full_name", "TEXT")
        _ensure_column(conn, "topics", "nickname", "TEXT")
        # DEFAULT only applies to NEW rows on ALTER TABLE ADD COLUMN in SQLite
        # -- pre-existing chat_settings rows get NULL here, so
        # get_timetable_label_format treats NULL the same as 'code'.
        _ensure_column(conn, "chat_settings", "timetable_label_format", "TEXT")
        conn.commit()
        # One-time, data-preserving collapse of modules/events into the topics
        # tree. Runs in place on an existing DB; a no-op once done or on a fresh
        # DB. Deliberately NOT gated behind a _SCHEMA_VERSION bump, because that
        # would trigger the destructive drop-all above on the live DB.
        _migrate_topics_v2(conn)
        # Same reasoning: notes.topic_id -> nullable, notes gets its own
        # chat_id. Also not gated behind _SCHEMA_VERSION -- see docstring.
        _migrate_notes_general(conn)
        conn.executescript(_INDEXES)
        _ensure_topic_unique_index(conn)
        _backfill_tags(conn)
        conn.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
        conn.commit()
    finally:
        conn.close()


def _ensure_topic_unique_index(conn: sqlite3.Connection) -> None:
    """Add the topic-uniqueness index, tolerating a DB that predates it.

    Duplicate topics were creatable before this index existed (a migrated DB
    had no uniqueness at all, and root topics were never covered even on a
    fresh one), so this can fail on a real database. That must not take the bot
    down on startup: log the offending rows loudly and carry on unindexed --
    get_or_create_topic's SELECT-then-INSERT still dedups correctly, the index
    is only the backstop. Merge the duplicates by hand and the index appears on
    the next start.
    """
    try:
        conn.execute(_TOPIC_UNIQUE_INDEX)
    except sqlite3.IntegrityError:
        dupes = conn.execute(
            "SELECT chat_id, parent_topic_id, name, COUNT(*) AS n, "
            "GROUP_CONCAT(id) AS ids FROM topics "
            "GROUP BY chat_id, COALESCE(parent_topic_id, -1), name COLLATE NOCASE "
            "HAVING n > 1"
        ).fetchall()
        logger.error(
            "Topic uniqueness index NOT created -- duplicate topics already "
            "exist: %s. Merge them, then restart to enforce uniqueness.",
            [(d["name"], d["ids"]) for d in dupes],
        )


# --- modules & events are gone --------------------------------------------
# A "module" is now a topic with kind='module' (auto-coloured); an "event" is a
# topic with kind='event[:type]' and an event_datetime. Both are created through
# get_or_create_topic / create_topic in the topics section below.


# --- tasks -------------------------------------------------------------

# Tasks now attach to a single optional topic_id (or nowhere). This SELECT
# carries the attached topic's name/kind for display labels.
_TASK_SELECT = (
    "SELECT tasks.*, topics.name AS topic_name, topics.kind AS topic_kind "
    "FROM tasks LEFT JOIN topics ON topics.id = tasks.topic_id "
)


def _require_topic(conn: sqlite3.Connection, chat_id: int, topic_id: int) -> None:
    """Raise ValueError unless topic_id exists and belongs to chat_id."""
    row = conn.execute(
        "SELECT 1 FROM topics WHERE id = ? AND chat_id = ?", (topic_id, chat_id)
    ).fetchone()
    if row is None:
        raise ValueError(f"No topic with id {topic_id} found for this chat.")


def get_topic_subtree_ids(conn: sqlite3.Connection, chat_id: int, topic_id: int) -> list[int]:
    """topic_id plus every topic nested under it, at any depth (BFS down
    parent_topic_id). This is THE mechanism behind "give me everything under
    Y3S1" -- every topic-scoped query (schedule, tasks, notes, reminders)
    walks the same subtree via this function, so "topic_id, and optionally
    its whole subtree" behaves identically everywhere instead of each query
    having its own bespoke (and previously inconsistent, sometimes broken)
    notion of what a topic "contains".

    Takes an open connection rather than opening its own, since every caller
    already has one open and wants this as one step inside a larger query,
    not a separate round trip.
    """
    ids = [topic_id]
    frontier = [topic_id]
    while frontier:
        placeholders = ",".join("?" * len(frontier))
        children = conn.execute(
            f"SELECT id FROM topics WHERE chat_id = ? AND parent_topic_id IN ({placeholders})",
            (chat_id, *frontier),
        ).fetchall()
        frontier = [r["id"] for r in children]
        ids.extend(frontier)
    return ids


def create_task(
    chat_id: int,
    title: str,
    topic_id: int | None = None,
    category: str | None = None,
    deadline: str | None = None,
    status: str = "not_started",
) -> dict:
    conn = get_connection()
    try:
        if topic_id is not None:
            _require_topic(conn, chat_id, topic_id)
        tag = _new_tag(conn, "tasks")
        cur = conn.execute(
            "INSERT INTO tasks (chat_id, topic_id, title, category, tag, deadline, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (chat_id, topic_id, title, category, tag, deadline, status),
        )
        conn.commit()
        row = conn.execute(
            _TASK_SELECT + "WHERE tasks.id = ?", (cur.lastrowid,)
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
            _TASK_SELECT + "WHERE tasks.id = ?", (task_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def get_task(chat_id: int, task_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            _TASK_SELECT + "WHERE tasks.id = ? AND tasks.chat_id = ?",
            (task_id, chat_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def query_tasks(
    chat_id: int,
    status: str | None = None,
    topic_id: int | None = None,
    include_subtopics: bool = True,
    category: str | None = None,
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
        if topic_id is not None:
            if include_subtopics:
                subtree = get_topic_subtree_ids(conn, chat_id, topic_id)
                clauses.append(f"tasks.topic_id IN ({','.join('?' * len(subtree))})")
                params.extend(subtree)
            else:
                clauses.append("tasks.topic_id = ?")
                params.append(topic_id)
        if category is not None:
            clauses.append("tasks.category = ? COLLATE NOCASE")
            params.append(category)
        if deadline_from is not None:
            clauses.append("tasks.deadline >= ?")
            params.append(deadline_from)
        if deadline_to is not None:
            clauses.append("tasks.deadline <= ?")
            params.append(deadline_to)

        params.append(limit)
        sql = (
            _TASK_SELECT
            + f"WHERE {' AND '.join(clauses)} "
            "ORDER BY tasks.deadline IS NULL, tasks.deadline ASC LIMIT ?"
        )
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_task_categories(chat_id: int) -> list[str]:
    """Distinct task categories already in use for this chat -- call before
    create_task with a new category so a synonym doesn't fragment the set
    (e.g. 'assignment' vs 'Assignment' vs 'homework' all meaning the same
    thing). Mirrors list_topic_kinds for the same reason."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT category FROM tasks "
            "WHERE chat_id = ? AND category IS NOT NULL AND category <> ''",
            (chat_id,),
        ).fetchall()
        return sorted({r["category"] for r in rows})
    finally:
        conn.close()


# --- reminders ---------------------------------------------------------

def create_reminder(
    chat_id: int,
    trigger_datetime_utc: str,
    message: str,
    linked_task_id: int | None = None,
    linked_topic_id: int | None = None,
) -> dict:
    if linked_task_id is not None and linked_topic_id is not None:
        raise ValueError("A reminder can link to a task or a topic, not both.")
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
        if linked_topic_id is not None:
            _require_topic(conn, chat_id, linked_topic_id)

        tag = _new_tag(conn, "reminders")
        cur = conn.execute(
            "INSERT INTO reminders (chat_id, type, trigger_data, message, "
            "linked_task_id, linked_topic_id, tag) VALUES (?, 'time', ?, ?, ?, ?, ?)",
            (chat_id, trigger_datetime_utc, message, linked_task_id, linked_topic_id, tag),
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


def list_pending_reminders(
    chat_id: int,
    scope: str = "all",
    topic_id: int | None = None,
    include_subtopics: bool = True,
) -> list[dict]:
    """scope='general' restricts to reminders with no linked task or topic --
    the freestanding ones, e.g. "just general reminders". scope='all' (the
    default) returns every pending reminder regardless of what it's linked
    to. topic_id further restricts to reminders linked to that topic (or its
    subtree, by default) -- mutually meaningful with scope='all' only, since
    a general reminder is by definition linked to no topic.
    """
    if scope not in ("general", "all"):
        raise ValueError(f"scope must be 'general' or 'all', got {scope!r}")
    conn = get_connection()
    try:
        clauses = ["chat_id = ?", "status = 'pending'"]
        params: list = [chat_id]
        if scope == "general":
            clauses.append("linked_task_id IS NULL AND linked_topic_id IS NULL")
        if topic_id is not None:
            if include_subtopics:
                subtree = get_topic_subtree_ids(conn, chat_id, topic_id)
                clauses.append(f"linked_topic_id IN ({','.join('?' * len(subtree))})")
                params.extend(subtree)
            else:
                clauses.append("linked_topic_id = ?")
                params.append(topic_id)
        sql = (
            f"SELECT * FROM reminders WHERE {' AND '.join(clauses)} "
            "ORDER BY trigger_data ASC"
        )
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- topics --------------------------------------------------------------

# Canonical topic kinds, surfaced first by list_topic_kinds so Claude reuses an
# existing kind rather than minting a near-duplicate. `kind` is otherwise a free
# string (no CHECK), so new kinds never need a schema change.
CANONICAL_TOPIC_KINDS = ["course", "year", "semester", "module", "component", "event"]


def get_or_create_topic(
    chat_id: int,
    name: str,
    kind: str | None = None,
    status: str | None = None,
    event_datetime: str | None = None,
    color: str | None = None,
    parent_topic_id: int | None = None,
    full_name: str | None = None,
) -> dict:
    """Fetch or create a topic anywhere in the tree.

    A topic attaches under parent_topic_id, or at the root (parent_topic_id
    None). Dedup is by (chat_id, parent, name) case-insensitively. A kind
    'module' topic with no explicit color is auto-assigned the next
    MODULE_COLOR_PALETTE hue (so timetable colors keep working); other kinds
    are left uncolored.

    full_name (the official long title, as opposed to `name` -- the short
    code/display identifier) is FILL-ONLY on an existing match: an automated
    caller (PDF import re-running) never clobbers a full_name the user may
    have hand-edited via set_topic_names. To overwrite it deliberately, use
    set_topic_names instead.
    """
    conn = get_connection()
    try:
        if parent_topic_id is not None:
            _require_topic(conn, chat_id, parent_topic_id)

        row = conn.execute(
            "SELECT * FROM topics WHERE chat_id = ? AND parent_topic_id IS ? "
            "AND name = ? COLLATE NOCASE",
            (chat_id, parent_topic_id, name),
        ).fetchone()
        if row:
            if full_name and not row["full_name"]:
                conn.execute(
                    "UPDATE topics SET full_name = ? WHERE id = ?",
                    (full_name, row["id"]),
                )
                conn.commit()
                row = conn.execute(
                    "SELECT * FROM topics WHERE id = ?", (row["id"],)
                ).fetchone()
            return {**dict(row), "created": False}

        # casefold, not ==: kind is a free string matched case-insensitively
        # everywhere else (list_topics, list_event_topics), so a kind of
        # 'Module' must still get a palette colour and must still count toward
        # the palette cycle -- otherwise two modules can land on the same hue.
        if (kind or "").casefold() == "module" and color is None:
            existing = conn.execute(
                "SELECT COUNT(*) FROM topics WHERE chat_id = ? "
                "AND kind COLLATE NOCASE = 'module'",
                (chat_id,),
            ).fetchone()[0]
            color = MODULE_COLOR_PALETTE[existing % len(MODULE_COLOR_PALETTE)]

        try:
            cur = conn.execute(
                "INSERT INTO topics (chat_id, parent_topic_id, name, full_name, "
                "kind, status, event_datetime, color) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chat_id, parent_topic_id, name, full_name, kind, status,
                 event_datetime, color),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            # Lost a create-vs-create race -- fetch the winner and return it.
            row = conn.execute(
                "SELECT * FROM topics WHERE chat_id = ? AND parent_topic_id IS ? "
                "AND name = ? COLLATE NOCASE",
                (chat_id, parent_topic_id, name),
            ).fetchone()
            return {**dict(row), "created": False}
        row = conn.execute(
            "SELECT * FROM topics WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        # `created` is a transient flag, not a column: callers that have a
        # side effect on creation (create_topic auto-scheduling event
        # reminders) MUST key off this rather than off the returned row's
        # contents, or a second identical call re-fires the side effect.
        return {**dict(row), "created": True}
    finally:
        conn.close()


def set_topic_names(
    chat_id: int,
    topic_id: int,
    name: str | None = None,
    full_name: str | None = None,
    nickname: str | None = None,
) -> dict:
    """Explicitly set a topic's name / full_name / nickname. Unlike
    get_or_create_topic's fill-only behavior (safe for automated imports),
    this OVERWRITES whatever's given -- it's only called from a direct user
    request, so overwriting is the correct, expected behavior here. Only the
    fields actually passed are touched; omit a field to leave it as-is."""
    conn = get_connection()
    try:
        _require_topic(conn, chat_id, topic_id)
        sets, params = [], []
        if name is not None:
            sets.append("name = ?"); params.append(name)
        if full_name is not None:
            sets.append("full_name = ?"); params.append(full_name)
        if nickname is not None:
            sets.append("nickname = ?"); params.append(nickname)
        if sets:
            params.extend([topic_id, chat_id])
            conn.execute(
                f"UPDATE topics SET {', '.join(sets)} WHERE id = ? AND chat_id = ?",
                params,
            )
            conn.commit()
        row = conn.execute(
            "SELECT * FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def move_topic(chat_id: int, topic_id: int, new_parent_topic_id: int | None) -> dict:
    """Reparent a topic -- the capability that was missing entirely before
    this. new_parent_topic_id=None moves it to the root. Refuses to create a
    cycle (moving a topic under itself or one of its own descendants), which
    would otherwise silently corrupt every subtree walk that depends on the
    tree actually being a tree."""
    conn = get_connection()
    try:
        _require_topic(conn, chat_id, topic_id)
        if new_parent_topic_id is not None:
            _require_topic(conn, chat_id, new_parent_topic_id)
            descendants = set(get_topic_subtree_ids(conn, chat_id, topic_id))
            if new_parent_topic_id in descendants:
                raise ValueError(
                    "Can't move a topic under itself or one of its own "
                    "subtopics -- that would create a cycle."
                )
        conn.execute(
            "UPDATE topics SET parent_topic_id = ? WHERE id = ? AND chat_id = ?",
            (new_parent_topic_id, topic_id, chat_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def resolve_label(row: dict, label_format: str) -> str | None:
    """Given a row with name/full_name/nickname (a topic, or a schedule_block
    joined against topics), return the display label for the requested
    format. Always falls back to `name` (the code) if the requested field
    isn't set -- a missing full_name/nickname should never render as blank
    or None in a timetable."""
    name = row.get("module_name") or row.get("name")
    full_name = row.get("module_full_name") or row.get("full_name")
    nickname = row.get("module_nickname") or row.get("nickname")
    if label_format == "full_name":
        return full_name or name
    if label_format == "nickname":
        return nickname or name
    if label_format == "code_nickname":
        return f"{name}: {nickname}" if nickname else name
    if label_format == "code_full_name":
        return f"{name}: {full_name}" if full_name else name
    return name  # 'code' (default) and any unrecognized format


def get_timetable_label_format(chat_id: int) -> str:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT timetable_label_format FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return (row["timetable_label_format"] if row else None) or "code"
    finally:
        conn.close()


def set_timetable_label_format(chat_id: int, label_format: str) -> None:
    valid = {"code", "full_name", "nickname", "code_nickname", "code_full_name"}
    if label_format not in valid:
        raise ValueError(f"label_format must be one of {valid}, got {label_format!r}")
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO chat_settings (chat_id, timetable_label_format) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET timetable_label_format = excluded.timetable_label_format",
            (chat_id, label_format),
        )
        conn.commit()
    finally:
        conn.close()


def set_topic_event_datetime(chat_id: int, topic_id: int, event_datetime: str) -> bool:
    """Set event_datetime on a topic that doesn't have one yet.

    Returns True if it was actually set. Deliberately refuses to overwrite an
    existing value: reminders are already scheduled against the old one, and
    silently moving the date underneath them would leave those reminders
    pointing at a time the user can no longer see. Rescheduling on change is a
    separate piece of work.
    """
    conn = get_connection()
    try:
        cur = conn.execute(
            "UPDATE topics SET event_datetime = ? WHERE id = ? AND chat_id = ? "
            "AND event_datetime IS NULL",
            (event_datetime, topic_id, chat_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def resolve_module_topic(
    chat_id: int,
    module_name: str,
    full_name: str | None = None,
    parent_topic_id: int | None = None,
) -> dict:
    """Find (or create) the kind='module' topic named `module_name`, ANYWHERE
    in the tree -- not just at the root.

    get_or_create_topic dedups on (chat_id, parent, name), so calling it with
    parent_topic_id=None only ever sees root topics. Once a module is filed
    under a 'semester' or 'year' topic -- the entire point of the unified tree
    -- a root-scoped lookup misses it and forks a second module topic with the
    same name and a different auto-colour, which then collides by name in
    timetable_data's colour map. So: search the whole tree by (name, kind), and
    only fall back to creating a root topic when there genuinely isn't one.

    full_name is filled in (never overwritten) on whichever match is found --
    this is how re-importing a schedule PDF backfills the official course
    title onto a module you already created and nested by hand.

    parent_topic_id ONLY affects where a genuinely NEW module gets created --
    an existing module found anywhere else in the tree is always reused as-is
    (never moved), so this can't fork a duplicate under the new parent. This
    is what lets a PDF import target "under S1" without re-creating a module
    you already have nested somewhere else.

    Raises on ambiguity rather than picking: two same-named module topics is
    already a broken state, and silently guessing one is how the timetable
    silently repaints itself.
    """
    matches = [
        t
        for t in list_topics(chat_id, kind="module")
        if t["name"].strip().casefold() == module_name.strip().casefold()
    ]
    if len(matches) > 1:
        paths = ", ".join(f"#{t['id']} {t['path']}" for t in matches)
        raise ValueError(
            f"Ambiguous module '{module_name}' -- {len(matches)} module topics "
            f"share that name ({paths}). Merge them, then retry."
        )
    if matches:
        if full_name and not matches[0].get("full_name"):
            return set_topic_names(chat_id, matches[0]["id"], full_name=full_name)
        return matches[0]
    return get_or_create_topic(
        chat_id, module_name, kind="module", full_name=full_name,
        parent_topic_id=parent_topic_id,
    )


def get_topic(chat_id: int, topic_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM topics WHERE id = ? AND chat_id = ?", (topic_id, chat_id)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_topics(chat_id: int, kind: str | None = None) -> list[dict]:
    """All of a chat's topics (each with a computed root->leaf `path`), or only
    those of a given kind. The path is always resolved against the full tree, so
    a kind-filtered result still shows correct ancestor names."""
    conn = get_connection()
    try:
        all_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM topics WHERE chat_id = ? ORDER BY name COLLATE NOCASE",
                (chat_id,),
            ).fetchall()
        ]
    finally:
        conn.close()

    by_id = {r["id"]: r for r in all_rows}

    def build_path(topic: dict) -> str:
        # Defensive cycle guard against a manual DB edit introducing a loop.
        names: list[str] = []
        cur = topic
        seen: set[int] = set()
        while cur is not None and cur["id"] not in seen:
            names.append(cur["name"])
            seen.add(cur["id"])
            pid = cur["parent_topic_id"]
            cur = by_id.get(pid) if pid is not None else None
        names.reverse()
        return " > ".join(names)

    for r in all_rows:
        r["path"] = build_path(r)

    if kind is not None:
        return [r for r in all_rows if (r["kind"] or "").casefold() == kind.casefold()]
    return all_rows


def list_topic_kinds(chat_id: int) -> list[str]:
    """Distinct topic kinds already in use for this chat, canonical ones first."""
    conn = get_connection()
    try:
        in_use = {
            r["kind"]
            for r in conn.execute(
                "SELECT DISTINCT kind FROM topics "
                "WHERE chat_id = ? AND kind IS NOT NULL AND kind <> ''",
                (chat_id,),
            ).fetchall()
        }
    finally:
        conn.close()
    ordered = [k for k in CANONICAL_TOPIC_KINDS if k in in_use]
    extras = sorted(k for k in in_use if k not in CANONICAL_TOPIC_KINDS)
    return ordered + extras


def list_event_topics(chat_id: int, upcoming_only: bool = True) -> list[dict]:
    """Topics that represent events (kind 'event' or 'event:<type>') with an
    event_datetime, ordered soonest-first; future-only by default."""
    events = [
        t
        for t in list_topics(chat_id)
        if (t["kind"] or "").casefold().startswith("event")
        and t["event_datetime"]
    ]
    if upcoming_only:
        now_iso = _now_utc_iso()
        events = [t for t in events if t["event_datetime"] >= now_iso]
    events.sort(key=lambda t: t["event_datetime"])
    return events


# --- notes -----------------------------------------------------------------

def create_note(
    chat_id: int,
    content: str,
    topic_id: int | None = None,
    source: str | None = None,
    is_reference: bool = False,
) -> dict:
    """topic_id omitted -> a "general" note, attached to nothing. Scoped by
    notes.chat_id directly rather than derived from a topic, since a general
    note has no topic to derive it from."""
    conn = get_connection()
    try:
        if topic_id is not None:
            _require_topic(conn, chat_id, topic_id)
        tag = _new_tag(conn, "notes")
        cur = conn.execute(
            "INSERT INTO notes (chat_id, topic_id, tag, source, content, is_reference) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, topic_id, tag, source, content, 1 if is_reference else 0),
        )
        conn.commit()
        row = conn.execute(
            "SELECT notes.*, topics.name AS topic_name "
            "FROM notes LEFT JOIN topics ON topics.id = notes.topic_id "
            "WHERE notes.id = ?",
            (cur.lastrowid,),
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def query_notes(
    chat_id: int,
    topic_id: int | None = None,
    include_subtopics: bool = True,
    is_reference: bool | None = None,
    limit: int = 20,
) -> list[dict]:
    """topic_id omitted -> every note for this chat, general and topic-linked
    alike. Pass topic_id with include_subtopics=False to see only notes on
    that exact topic, not ones nested underneath it."""
    conn = get_connection()
    try:
        clauses = ["notes.chat_id = ?"]
        params: list = [chat_id]
        if topic_id is not None:
            if include_subtopics:
                subtree = get_topic_subtree_ids(conn, chat_id, topic_id)
                clauses.append(f"notes.topic_id IN ({','.join('?' * len(subtree))})")
                params.extend(subtree)
            else:
                clauses.append("notes.topic_id = ?")
                params.append(topic_id)
        if is_reference is not None:
            clauses.append("notes.is_reference = ?")
            params.append(1 if is_reference else 0)

        params.append(limit)
        sql = (
            "SELECT notes.*, topics.name AS topic_name "
            "FROM notes "
            "LEFT JOIN topics ON topics.id = notes.topic_id "
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
    class_type: str | None = None,
    location: str | None = None,
    week_pattern: str = "every",
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
    week_pattern = _validate_week_pattern(week_pattern)

    # `module_name` is kept as a convenience for callers (the NL tool schema,
    # the PDF import path): a class's module is just a topic with kind='module',
    # resolved/created here so those callers never deal in topic_ids.
    topic_id = (
        resolve_module_topic(chat_id, module_name)["id"] if module_name else None
    )
    conn = get_connection()
    try:
        cur = conn.execute(
            "INSERT INTO schedule_blocks (chat_id, topic_id, day_of_week, "
            "specific_date, start_time, end_time, class_type, location, week_pattern) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (chat_id, topic_id, day_of_week, specific_date, start_time,
             end_time, class_type, location, week_pattern),
        )
        conn.commit()
        row = conn.execute(
            "SELECT schedule_blocks.*, topics.name AS module_name, "
            "topics.full_name AS module_full_name, topics.nickname AS module_nickname "
            "FROM schedule_blocks "
            "LEFT JOIN topics ON topics.id = schedule_blocks.topic_id "
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
    topic_id: int | None = None,
    include_subtopics: bool = True,
    class_types: list[str] | None = None,
) -> list[dict]:
    """topic_id (with include_subtopics, the default) is the general-purpose
    filter -- "everything under Y3S1" resolves Y3S1 to a topic_id and walks
    its subtree, catching every module and class nested underneath it.
    module_name is kept only as a quick single-module shortcut for simple
    cases; it does NOT walk a subtree the way topic_id does. class_types
    filters to specific lesson types (e.g. just tutorial + lab) once you
    already know which topics/modules you're looking at.
    """
    conn = get_connection()
    try:
        clauses = ["schedule_blocks.chat_id = ?"]
        params: list = [chat_id]
        clauses.append(
            "(schedule_blocks.day_of_week IS NOT NULL "
            "OR ((? IS NULL OR schedule_blocks.specific_date >= ?) "
            "    AND (? IS NULL OR schedule_blocks.specific_date <= ?)))"
        )
        params.extend([date_from, date_from, date_to, date_to])
        if topic_id is not None:
            if include_subtopics:
                subtree = get_topic_subtree_ids(conn, chat_id, topic_id)
                clauses.append(
                    f"schedule_blocks.topic_id IN ({','.join('?' * len(subtree))})"
                )
                params.extend(subtree)
            else:
                clauses.append("schedule_blocks.topic_id = ?")
                params.append(topic_id)
        elif module_name is not None:
            clauses.append("topics.name = ? COLLATE NOCASE")
            params.append(module_name)
        if class_types:
            placeholders = ",".join("?" * len(class_types))
            clauses.append(f"schedule_blocks.class_type COLLATE NOCASE IN ({placeholders})")
            params.extend(class_types)

        sql = (
            "SELECT schedule_blocks.*, topics.name AS module_name, "
            "topics.full_name AS module_full_name, topics.nickname AS module_nickname "
            "FROM schedule_blocks "
            "LEFT JOIN topics ON topics.id = schedule_blocks.topic_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY schedule_blocks.specific_date IS NULL, schedule_blocks.specific_date, "
            "schedule_blocks.day_of_week, schedule_blocks.start_time"
        )
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_class_types(chat_id: int) -> list[str]:
    """Distinct lesson types (class_type) already in use for this chat --
    call before create_schedule_block with a new one so 'Tut' and 'Tutorial'
    don't end up as two different types for the same thing. Mirrors
    list_topic_kinds / list_task_categories."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT DISTINCT class_type FROM schedule_blocks "
            "WHERE chat_id = ? AND class_type IS NOT NULL AND class_type <> ''",
            (chat_id,),
        ).fetchall()
        return sorted({r["class_type"] for r in rows})
    finally:
        conn.close()


def delete_schedule_block(chat_id: int, schedule_block_id: int) -> dict | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT schedule_blocks.*, topics.name AS module_name, "
            "topics.full_name AS module_full_name, topics.nickname AS module_nickname "
            "FROM schedule_blocks "
            "LEFT JOIN topics ON topics.id = schedule_blocks.topic_id "
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


def find_matching_schedule_block(
    chat_id: int,
    topic_id: int | None,
    day_of_week: str | None,
    start_time: str,
    end_time: str,
    class_type: str | None,
) -> dict | None:
    """Find an existing recurring block with the same slot, used to skip
    duplicates when re-importing a schedule PDF. Matches on the identity a
    timetable slot is defined by (module topic + day + start/end time + class_type).

    class_type is part of the key on purpose: a lecture and a lab can be stacked
    in the SAME module/day/time slot (differing only by type), and those are
    genuinely distinct classes that must both survive an import -- keying without
    class_type would wrongly treat the second as a duplicate of the first.
    week_pattern is deliberately NOT part of the key, so a corrected re-upload of
    the same class updates nothing rather than creating a second copy."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM schedule_blocks WHERE chat_id = ? "
            "AND topic_id IS ? AND day_of_week IS ? "
            "AND start_time = ? AND end_time = ? AND class_type IS ?",
            (chat_id, topic_id, day_of_week, start_time, end_time, class_type),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# --- chat settings (semester anchor) -----------------------------------

def set_semester_anchor(chat_id: int, start_date: str) -> str:
    """Upsert the single chat_settings row holding the semester week-1 anchor.

    start_date is a plain YYYY-MM-DD calendar date. Weeks are always
    Monday-Sunday elsewhere in this schema (query_schedule's "this week"
    range, resolve_week_range, etc.), so whatever date is given here is
    snapped to the Monday of its calendar week before storing -- this way
    "week 1 starts August 13th" (a Thursday) still anchors correctly to the
    Monday (Aug 10) that actually begins that week, regardless of which day
    of the week happened to get mentioned in conversation. Idempotent --
    calling again just overwrites it.
    """
    try:
        parsed = date.fromisoformat(start_date)
    except ValueError:
        raise ValueError(
            f"start_date must be an ISO-8601 date YYYY-MM-DD, got {start_date!r}"
        ) from None
    monday = (parsed - timedelta(days=parsed.weekday())).isoformat()
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO chat_settings (chat_id, semester_week1_start_date) "
            "VALUES (?, ?) ON CONFLICT(chat_id) DO UPDATE SET "
            "semester_week1_start_date = excluded.semester_week1_start_date",
            (chat_id, monday),
        )
        conn.commit()
        return monday
    finally:
        conn.close()

def get_semester_anchor(chat_id: int) -> str | None:
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT semester_week1_start_date FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
        return row["semester_week1_start_date"] if row else None
    finally:
        conn.close()


def set_recess_weeks(chat_id: int, recess_dates: list[str]) -> list[int]:
    """Mark recess weeks by DATE and store them as CONTINUOUS week numbers.

    Each given ISO date is resolved to its continuous week relative to the
    stored anchor (raw count from the anchor's Monday, no recess adjustment --
    recess weeks are numbered independently of one another). Requires an anchor
    to already be set, since a week number is meaningless without one. A date
    before the anchor has no week and is rejected. Idempotent: the stored set
    is replaced wholesale, not accumulated.
    """
    anchor = get_semester_anchor(chat_id)
    if anchor is None:
        raise ValueError(
            "Set the semester start date before marking recess weeks -- a recess "
            "week has no number without an anchor to count from."
        )
    anchor_date = date.fromisoformat(anchor)

    weeks: set[int] = set()
    for d in recess_dates:
        try:
            rd = date.fromisoformat(d)
        except (ValueError, TypeError):
            raise ValueError(
                f"recess date must be an ISO-8601 date YYYY-MM-DD, got {d!r}"
            ) from None
        if rd < anchor_date:
            raise ValueError(
                f"recess date {d} is before semester week 1 ({anchor}), so it "
                "has no week number."
            )
        weeks.add(((rd - anchor_date).days // 7) + 1)

    stored = ",".join(str(w) for w in sorted(weeks))
    conn = get_connection()
    try:
        # The chat_settings row already exists (anchor is set), so this always
        # takes the ON CONFLICT branch and leaves semester_week1_start_date be.
        conn.execute(
            "INSERT INTO chat_settings (chat_id, recess_weeks) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET recess_weeks = excluded.recess_weeks",
            (chat_id, stored),
        )
        conn.commit()
    finally:
        conn.close()
    return sorted(weeks)


def get_recess_weeks(chat_id: int) -> set[int]:
    """The stored continuous recess-week numbers for a chat; empty set if none."""
    conn = get_connection()
    try:
        row = conn.execute(
            "SELECT recess_weeks FROM chat_settings WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row["recess_weeks"]:
        return set()
    return {int(p) for p in row["recess_weeks"].split(",")}