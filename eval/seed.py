"""Deterministic fixture generator for the evaluation harness.

Builds one of four database states. Every value is fixed or derived from a
seeded Random -- no wall-clock reads, no uuid, no unseeded shuffling -- so two
runs a week apart produce byte-comparable databases and any difference in
results is the agent, not the fixture.

    python eval/seed.py --state full --out eval/db/full.db

States
    full         a realistic semester: a week of timetable, tasks in all four
                 states, notes long enough to chunk, planted near-duplicates
    empty        schema only, zero rows -- the cold-start path
    single       exactly one row in each populated table
    conflicting  deliberate ambiguity: duplicate names, overlapping blocks,
                 same-title tasks with different deadlines

DATES ARE ANCHORED, NOT RELATIVE. Semester week 1 starts on a fixed Monday and
every deadline is an offset from it. Seeding against `today` would make "what's
due this week" mean something different on every run, which quietly destroys
reproducibility while looking fine.
"""

import argparse
import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import database

SEED = 20260901
CHAT_ID = 999_000_001

# Fixed anchor. A Monday, chosen once, never derived from the clock.
SEMESTER_START = date(2026, 8, 10)
# "Now" for the fixture, mid-week 3. Reminders are placed either side of it so
# both the pending and the fired paths have rows.
FIXTURE_NOW = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)

WEEKDAYS = ["MON", "TUE", "WED", "THU", "FRI"]

MODULES = [
    ("CZ3005", "Artificial Intelligence"),
    ("CZ3001", "Advanced Computer Architecture"),
    ("CZ3004", "Multi-disciplinary Design Project"),
]

# A full teaching week: every weekday covered, both lecture and tutorial for at
# least one module so class_type is a distinguishing field and not a constant.
TIMETABLE = [
    ("CZ3005", "MON", "09:30", "11:20", "Lecture", "LT19", "every"),
    ("CZ3001", "MON", "13:30", "15:20", "Lecture", "LT2A", "every"),
    ("CZ3005", "TUE", "10:30", "11:20", "Tutorial", "TR+15", "every"),
    ("CZ3004", "TUE", "14:30", "17:20", "Lab", "Hardware Lab 2", "odd"),
    ("CZ3001", "WED", "09:30", "10:20", "Tutorial", "TR+31", "every"),
    ("CZ3005", "WED", "15:30", "17:20", "Lab", "SWLab 1", "even"),
    ("CZ3001", "THU", "11:30", "13:20", "Lab", "Hardware Lab 1", "every"),
    ("CZ3004", "FRI", "09:30", "12:20", "Lecture", "LT8", "every"),
]

# Four statuses, all represented, plus a mix of dated and undated so the
# "deadline IS NULL" ordering branch in query_tasks is reachable.
TASKS = [
    ("CZ3005", "Finish A* search assignment", "assignment", 18, "not_started"),
    ("CZ3005", "Read Russell & Norvig ch. 3", "reading", None, "in_progress"),
    ("CZ3001", "Cache coherence problem set", "assignment", 12, "done"),
    ("CZ3001", "Pipeline hazards revision", "revision", 25, "not_started"),
    ("CZ3004", "Wire up the ultrasonic sensor", "build", 9, "blocked"),
    ("CZ3004", "Write up week 3 progress report", "report", 5, "in_progress"),
    ("CZ3005", "Submit peer review", "admin", 2, "done"),
]

# --- notes -----------------------------------------------------------------
#
# The near-duplicate pairs are the point of this fixture, not decoration. Each
# pair says the same thing twice; the first member shares vocabulary with the
# query, the second does not. A lexical ranker puts one high and misses the
# other entirely, which is the behaviour the analysis has to be able to
# demonstrate rather than assert. If a semantic arm is ever added, the same
# pairs are what it should recover.

NEAR_DUPLICATE_PAIRS = [
    (
        "chain_rule",
        "Backpropagation applies the chain rule to compute the gradient of the "
        "loss with respect to every weight in the network. Each layer's local "
        "derivative is multiplied into the running product as the pass moves "
        "backward from the output.",
        "Computing derivatives of a composed function by working from the "
        "output toward the input is how error signal gets attributed to "
        "earlier parameters. The multiplication accumulates as you move "
        "through each stage.",
    ),
    (
        "cache_miss",
        "A cache miss occurs when the requested line is not resident and must "
        "be fetched from the next level of the memory hierarchy, stalling the "
        "pipeline for the duration of the fill.",
        "When the data a load needs is not already held nearby, the processor "
        "waits while it is brought in from slower storage further away. "
        "Execution cannot proceed until it arrives.",
    ),
    (
        "admissible",
        "A heuristic is admissible when it never overestimates the true cost "
        "to reach the goal, which is what guarantees A* returns an optimal "
        "path.",
        "If the estimate used by the search is always optimistic and never "
        "claims a route is longer than it really is, the algorithm is "
        "guaranteed to find the cheapest solution.",
    ),
]

# Long enough to chunk. MAX_CHUNK_CHARS is 400, so this must clear ~900 chars
# to produce three chunks and exercise the overlap path.
LONG_NOTE = (
    "Alpha-beta pruning improves on plain minimax by discarding branches that "
    "cannot influence the final decision. The algorithm carries two bounds "
    "through the search: alpha, the best value the maximizing player is "
    "already assured of, and beta, the best the minimizer is assured of. "
    "Whenever alpha becomes greater than or equal to beta at a node, the "
    "remaining children of that node are irrelevant, because a rational "
    "opponent would never allow play to reach them. The saving depends "
    "heavily on move ordering. With perfect ordering the effective branching "
    "factor drops to roughly the square root of the original, which doubles "
    "the depth reachable in a fixed time budget. With adversarial ordering "
    "the pruning saves nothing at all and the search degenerates back to "
    "plain minimax. In practice iterative deepening supplies a good ordering "
    "cheaply, because the principal variation from the previous shallower "
    "search is a strong guess at the best move for the next one."
)

PLAIN_NOTES = [
    ("CZ3005", "Prof said the exam will not cover constraint satisfaction.",
     None, False),
    ("CZ3001", "Amdahl's law: speedup is bounded by the serial fraction.",
     "Lecture 4", True),
    ("CZ3004", "Group meets Thursdays 6pm in the North Spine study area.",
     None, False),
    (None, "Recess week is week 7, so no lectures that week.", None, False),
]


def _connect(db_path: Path):
    """Point the database module at our fixture file.

    DB_PATH is read into a module constant at import, so it has to be
    reassigned rather than set through the environment after the fact.
    """
    database.DB_PATH = Path(db_path)
    database.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    database.init_db()


def _deadline(week_offset_days: int) -> str:
    dt = datetime.combine(
        SEMESTER_START + timedelta(days=week_offset_days),
        datetime.min.time(),
        tzinfo=timezone.utc,
    ).replace(hour=23, minute=59)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _module_topics(rng: random.Random) -> dict[str, int]:
    """Course > Year > Semester > Module, matching the real topic tree."""
    course = database.get_or_create_topic(
        CHAT_ID, "Computer Engineering", kind="course"
    )
    year = database.get_or_create_topic(
        CHAT_ID, "Year 3", kind="year", parent_topic_id=course["id"]
    )
    sem = database.get_or_create_topic(
        CHAT_ID, "Semester 1", kind="semester", parent_topic_id=year["id"]
    )
    out = {}
    for code, full_name in MODULES:
        topic = database.get_or_create_topic(
            CHAT_ID, code, kind="module", parent_topic_id=sem["id"],
            full_name=full_name,
        )
        out[code] = topic["id"]
    return out


def _seed_full(rng: random.Random) -> dict:
    counts = {}
    topics = _module_topics(rng)

    database.set_semester_anchor(CHAT_ID, SEMESTER_START.isoformat())
    # Recess is marked by DATE, not week number -- set_recess_weeks resolves
    # each date to a continuous week against the anchor. Week 7 of a semester
    # starting SEMESTER_START is 42 days in.
    database.set_recess_weeks(
        CHAT_ID, [(SEMESTER_START + timedelta(days=42)).isoformat()]
    )

    for code, day, start, end, class_type, location, pattern in TIMETABLE:
        database.create_schedule_block(
            chat_id=CHAT_ID, start_time=start, end_time=end, day_of_week=day,
            topic_id=topics[code], class_type=class_type, location=location,
            week_pattern=pattern,
        )
    # One dated one-off so the specific_date branch is populated too.
    database.create_schedule_block(
        chat_id=CHAT_ID, start_time="14:00", end_time="16:00",
        specific_date=(SEMESTER_START + timedelta(days=17)).isoformat(),
        topic_id=topics["CZ3004"], class_type="Consultation", location="N4-01",
    )
    counts["schedule_blocks"] = len(TIMETABLE) + 1

    task_ids = []
    for code, title, category, offset, status in TASKS:
        task = database.create_task(
            chat_id=CHAT_ID, title=title, topic_id=topics[code],
            category=category,
            deadline=_deadline(offset) if offset is not None else None,
            status=status,
        )
        task_ids.append(task["id"])
    counts["tasks"] = len(TASKS)

    note_ids = []
    for slug, lexical, paraphrase in NEAR_DUPLICATE_PAIRS:
        code = "CZ3005" if slug != "cache_miss" else "CZ3001"
        for content in (lexical, paraphrase):
            note_ids.append(
                database.create_note(
                    CHAT_ID, content, topic_id=topics[code],
                    source=f"seed:{slug}", is_reference=True,
                )["id"]
            )
    note_ids.append(
        database.create_note(
            CHAT_ID, LONG_NOTE, topic_id=topics["CZ3005"],
            source="seed:long", is_reference=True,
        )["id"]
    )
    for code, content, source, is_ref in PLAIN_NOTES:
        note_ids.append(
            database.create_note(
                CHAT_ID, content,
                topic_id=topics[code] if code else None,
                source=source, is_reference=is_ref,
            )["id"]
        )
    counts["notes"] = len(note_ids)

    # Reminders either side of FIXTURE_NOW so both statuses are represented.
    for days, message, linked in (
        (-2, "Submit peer review", task_ids[6]),
        (1, "A* assignment due tomorrow", task_ids[0]),
        (3, "MDP progress report", task_ids[5]),
    ):
        trigger = FIXTURE_NOW + timedelta(days=days)
        database.create_reminder(
            chat_id=CHAT_ID,
            trigger_datetime_utc=trigger.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            message=message, linked_task_id=linked,
        )
    counts["reminders"] = 3

    database.create_file(
        chat_id=CHAT_ID, file_id="SEEDFILEID0001",
        file_unique_id="SEEDUNIQUE0001", topic_id=topics["CZ3005"],
        file_name="lecture03_search.pdf", nickname="search slides",
        mime_type="application/pdf", file_size=482_119,
    )
    counts["files"] = 1

    counts["progress_logs"] = _seed_progress_logs(list(topics.values()))
    counts["topics"] = len(database.list_topics(CHAT_ID))
    counts["chat_settings"] = 1
    return counts


def _seed_progress_logs(topic_ids: list[int], limit: int | None = None) -> int:
    """Raw SQL, deliberately.

    progress_logs is in the schema and is migrated, counted and cascaded, but
    NOTHING in the codebase ever writes to it -- there is no create/log
    function and no tool that reaches it. Seeding it through the public API is
    therefore impossible. Populating it anyway keeps the "all 8 tables" promise
    honest, and the absence of a writer is itself a finding for the write-up:
    it is a dead table in the same sense the analysis will look for dead tools.
    """
    conn = database.get_connection()
    try:
        entries = [
            ("review", "2026-08-18T10:00:00.000Z", 3),
            ("practice", "2026-08-21T15:30:00.000Z", 4),
            ("review", "2026-08-24T09:15:00.000Z", 2),
        ]
        # Cycle rather than index: the single-record state has exactly one
        # topic, so fixed positions would IndexError there.
        if limit is not None:
            entries = entries[:limit]
        rows = [
            (topic_ids[i % len(topic_ids)], kind, ts, rating)
            for i, (kind, ts, rating) in enumerate(entries)
        ]
        conn.executemany(
            "INSERT INTO progress_logs (topic_id, type, timestamp, "
            "confidence_rating) VALUES (?, ?, ?, ?)", rows,
        )
        conn.commit()
        return len(rows)
    finally:
        conn.close()


def _seed_single(rng: random.Random) -> dict:
    """Exactly one row where a row is possible.

    Distinct from `full` in kind, not just size: a single-record corpus makes
    every BM25 IDF term degenerate (N=1, so df=1 for every term present), which
    is a real edge in the scorer and not reachable from the full state.
    """
    topic = database.get_or_create_topic(CHAT_ID, "CZ3005", kind="module")
    database.set_semester_anchor(CHAT_ID, SEMESTER_START.isoformat())
    database.create_schedule_block(
        chat_id=CHAT_ID, start_time="09:30", end_time="11:20",
        day_of_week="MON", topic_id=topic["id"], class_type="Lecture",
        location="LT19",
    )
    database.create_task(
        chat_id=CHAT_ID, title="Finish A* search assignment",
        topic_id=topic["id"], category="assignment", deadline=_deadline(18),
    )
    database.create_note(
        CHAT_ID, NEAR_DUPLICATE_PAIRS[0][1], topic_id=topic["id"],
        source="seed:single", is_reference=True,
    )
    database.create_reminder(
        chat_id=CHAT_ID,
        trigger_datetime_utc=(FIXTURE_NOW + timedelta(days=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.000Z"
        ),
        message="A* assignment due tomorrow",
    )
    database.create_file(
        chat_id=CHAT_ID, file_id="SEEDFILEID0002",
        file_unique_id="SEEDUNIQUE0002", topic_id=topic["id"],
        file_name="lecture03_search.pdf", nickname=None,
        mime_type="application/pdf", file_size=482_119,
    )
    return {
        "topics": len(database.list_topics(CHAT_ID)), "tasks": 1, "notes": 1,
        "reminders": 1, "files": 1, "schedule_blocks": 1, "chat_settings": 1,
        "progress_logs": _seed_progress_logs([topic["id"]], limit=1),
    }


def _seed_conflicting(rng: random.Random) -> dict:
    """Ambiguity the agent has to resolve rather than guess through.

    Every conflict here mirrors one the real bot can hit: two modules whose
    components share a name, two blocks claiming the same room-hour, two tasks
    with the same title and different deadlines, and two notes that are near
    textual duplicates rather than paraphrases.
    """
    ai = database.get_or_create_topic(CHAT_ID, "CZ3005", kind="module")
    arch = database.get_or_create_topic(CHAT_ID, "CZ3001", kind="module")

    # Same child name under two different parents -- list_topics returns both,
    # so any tool call that resolves a topic by name alone is ambiguous.
    for parent in (ai, arch):
        database.get_or_create_topic(
            CHAT_ID, "Assignment 1", kind="component",
            parent_topic_id=parent["id"],
        )

    # Overlapping blocks, same day, same hour, different modules.
    database.create_schedule_block(
        chat_id=CHAT_ID, start_time="09:30", end_time="11:20",
        day_of_week="MON", topic_id=ai["id"], class_type="Lecture",
        location="LT19",
    )
    database.create_schedule_block(
        chat_id=CHAT_ID, start_time="10:30", end_time="12:20",
        day_of_week="MON", topic_id=arch["id"], class_type="Lecture",
        location="LT2A",
    )

    # Identical titles, different deadlines and different parents.
    for topic, offset in ((ai, 12), (arch, 19)):
        database.create_task(
            chat_id=CHAT_ID, title="Assignment 1", topic_id=topic["id"],
            category="assignment", deadline=_deadline(offset),
        )

    # Textual near-duplicates: high lexical overlap, one differing clause. BM25
    # must separate these on a single term, which is a much finer distinction
    # than the paraphrase pairs in the full state.
    database.create_note(
        CHAT_ID,
        "A heuristic is admissible when it never overestimates the true cost "
        "to reach the goal.",
        topic_id=ai["id"], source="seed:conflict-a", is_reference=True,
    )
    database.create_note(
        CHAT_ID,
        "A heuristic is consistent when it never overestimates the step cost "
        "plus the remaining estimate.",
        topic_id=ai["id"], source="seed:conflict-b", is_reference=True,
    )

    return {
        "topics": len(database.list_topics(CHAT_ID)), "tasks": 2, "notes": 2,
        "reminders": 0, "files": 0, "schedule_blocks": 2,
        "chat_settings": 0, "progress_logs": 0,
    }


# Columns whose values come from the wall clock or from secrets.token_hex, and
# so differ on every build. Both leak into what the agent sees -- tags are
# printed in tool output as user-facing reference handles, and query_notes
# orders by created_at DESC, so equal-millisecond timestamps make the returned
# ORDER unstable between runs. Rewriting them is what makes "fixed seeds
# throughout" actually true rather than nearly true.
#
# The timestamp columns are DISCOVERED from the schema rather than listed. They
# are not consistently named -- created_at, updated_at, uploaded_at, timestamp
# -- and a hand-maintained list silently goes stale the next time a column is
# added, reintroducing nondeterminism that only shows up as an unexplained
# result drift weeks later. PRAGMA table_info exposes each column's default
# expression, and every one of these defaults to STRFTIME(...,'now').
_TAG_TABLES = ["notes", "tasks", "reminders", "files"]

# Tables the harness owns rather than the bot; excluded so a fixture rebuild
# never rewrites collected measurements.
_INSTRUMENTATION_TABLES = {"turns", "tool_calls"}

# Rows are stamped one minute apart ascending by id, so created_at ordering and
# insertion ordering agree and every DESC query has one stable answer.
_CANONICAL_EPOCH = datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc)


def _clock_columns(conn) -> list[tuple[str, str]]:
    out = []
    tables = [
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    for table in tables:
        if table in _INSTRUMENTATION_TABLES:
            continue
        cols = list(conn.execute(f"PRAGMA table_info({table})"))
        if not any(c["name"] == "id" for c in cols):
            continue  # no id to order by; nothing here defaults to a clock
        for col in cols:
            default = col["dflt_value"] or ""
            if "STRFTIME" in default.upper():
                out.append((table, col["name"]))
    return out


def _canonicalize(rng: random.Random) -> None:
    conn = database.get_connection()
    try:
        for table, column in _clock_columns(conn):
            ids = [r["id"] for r in conn.execute(
                f"SELECT id FROM {table} ORDER BY id"
            )]
            for offset, row_id in enumerate(ids):
                stamp = _CANONICAL_EPOCH + timedelta(minutes=offset)
                conn.execute(
                    f"UPDATE {table} SET {column} = ? WHERE id = ?",
                    (stamp.strftime("%Y-%m-%dT%H:%M:%S.000Z"), row_id),
                )
        for table in _TAG_TABLES:
            for row in conn.execute(f"SELECT id FROM {table} ORDER BY id"):
                conn.execute(
                    f"UPDATE {table} SET tag = ? WHERE id = ?",
                    (f"{rng.randrange(16**6):06x}", row["id"]),
                )
        conn.commit()
    finally:
        conn.close()


BUILDERS = {
    "full": _seed_full,
    "single": _seed_single,
    "conflicting": _seed_conflicting,
    "empty": lambda rng: {},
}


def build(state: str, out_path: Path) -> dict:
    if state not in BUILDERS:
        raise ValueError(f"state must be one of {sorted(BUILDERS)}, got {state!r}")
    out_path = Path(out_path)
    if out_path.exists():
        out_path.unlink()
    # WAL sidecars from a previous build would otherwise carry stale pages into
    # the new file.
    for suffix in ("-wal", "-shm"):
        sidecar = out_path.with_name(out_path.name + suffix)
        if sidecar.exists():
            sidecar.unlink()

    rng = random.Random(SEED)
    _connect(out_path)
    counts = BUILDERS[state](rng)
    _canonicalize(rng)

    conn = database.get_connection()
    try:
        chunks = conn.execute("SELECT COUNT(*) FROM note_chunks").fetchone()[0]
        postings = conn.execute("SELECT COUNT(*) FROM postings").fetchone()[0]
        # Checkpoint so the .db file is self-contained and can be snapshot-
        # copied by the runner without dragging the -wal file along.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    counts["note_chunks"] = chunks
    counts["postings"] = postings
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, choices=sorted(BUILDERS))
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    counts = build(args.state, Path(args.out))
    width = max(len(k) for k in counts) if counts else 0
    print(f"seeded '{args.state}' -> {args.out}  (seed={SEED}, chat_id={CHAT_ID})")
    for key in sorted(counts):
        print(f"  {key:<{width}}  {counts[key]}")


if __name__ == "__main__":
    main()
