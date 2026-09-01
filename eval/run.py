"""Executes the prompt suite against a chosen fixture state and records what
happened.

    python eval/run.py --state full --repeats 5
    python eval/run.py --state full --repeats 5 --ceiling
    python eval/run.py --state full --replay          # no API calls at all

Three things make the numbers trustworthy, and each of them is a decision that
could have gone the other way:

RESTORE PER EXECUTION, NOT PER SUITE. 21 of the 31 tools mutate state. If
repeat 1 of "add a task" leaves a row behind, repeat 2 runs against a different
database and the dispatch variance being measured is partly fixture drift. The
fixture is copied back before every single execution.

THE CACHE KEY INCLUDES THE REPEAT INDEX. Caching purely on the request payload
would be self-defeating: repeats 2..N are byte-identical requests, so they
would all hit repeat 1's cached response and variance would read as exactly
zero. Each (prompt, repeat) pair gets its own cache slot. First run pays for
all of them; every replay afterward is free and preserves the spread.

INSTRUMENTATION IS DRAINED BEFORE THE RESTORE. brain writes turns and
tool_calls into whichever DB is live, which here is the scratch copy about to
be overwritten. Rows are read out immediately after each execution and appended
to the run's results file.

Nothing here fabricates a number. A prompt that errors is recorded as an error
with its traceback, not dropped.
"""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import sys
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import yaml
from dotenv import load_dotenv

# bot.py loads .env at startup; nothing else does, so a harness that imports
# brain directly gets an unauthenticated client and fails on the first API
# call with no obvious connection to the missing file.
load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

EVAL_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = EVAL_DIR / "db"
CACHE_DIR = EVAL_DIR / "cache"
RUNS_DIR = EVAL_DIR / "runs"
PROMPTS_PATH = EVAL_DIR / "prompts.yaml"
SCRATCH_PATH = EVAL_DIR / "db" / "_scratch.db"

CHAT_ID = 999_000_001

# Must match seed.py's FIXTURE_NOW. The fixture anchors semester week 1 and
# every deadline to fixed dates; if the agent is simultaneously told the real
# wall-clock date, "what's due this week" resolves against a calendar the
# fixture knows nothing about, and the answer drifts a little further every
# day the suite is re-run.
FIXTURE_NOW = datetime(2026, 8, 26, 9, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    """datetime with now()/utcnow() pinned to FIXTURE_NOW.

    Patched into brain and tools, which both do `from datetime import datetime`
    and so hold the class as a module attribute. This is what makes the system
    prompt byte-identical across runs -- without it the prompt carries a live
    timestamp, every request payload is unique, and the response cache can
    never hit.

    KNOWN LIMIT: SQL-side STRFTIME('now') is untouched. Column defaults are
    canonicalized by seed.py, but a query filtering on 'now' (for example
    get_pending_future_reminders) still sees the real clock. Any metric that
    depends on that comparison is not frozen, and the write-up should say so
    rather than implying the whole system is.
    """

    @classmethod
    def now(cls, tz=None):
        return FIXTURE_NOW.astimezone(tz) if tz else FIXTURE_NOW

    @classmethod
    def utcnow(cls):
        return FIXTURE_NOW.replace(tzinfo=None)


def freeze_clock(*modules) -> None:
    for module in modules:
        module.datetime = _FrozenDatetime


class StubJobQueue:
    """Stands in for PTB's JobQueue.

    Only two methods are ever reached from the tool layer: run_once (scheduling
    a reminder) and get_jobs_by_name (cancelling one). Scheduling is recorded
    rather than performed -- the harness has no event loop that outlives a
    single execution, and a real APScheduler would either fire mid-suite or
    leak threads across 400+ executions.
    """

    def __init__(self):
        self.scheduled: list[dict] = []

    def run_once(self, callback=None, when=None, chat_id=None, data=None,
                 name=None, **kwargs):
        self.scheduled.append({"name": name, "when": str(when), "data": data})
        return None

    def get_jobs_by_name(self, name):
        return []


class CachingClient:
    """Wraps the Anthropic client so every response is written to disk once and
    replayed thereafter.

    Presents the same .messages.create surface brain calls, and returns real SDK
    objects on a live call. On a cache hit it reconstructs the response from
    stored JSON via the SDK's own model_validate, so brain sees the same types
    either way and no branch in brain has to know this exists.
    """

    def __init__(self, real_client, cache_dir: Path, replay_only: bool):
        self._real = real_client
        self._cache_dir = cache_dir
        self._replay_only = replay_only
        self._slot: str | None = None
        self._call_index = 0
        self.hits = 0
        self.misses = 0
        self.messages = self._Messages(self)

    def begin(self, slot: str) -> None:
        """Open a cache slot for one execution. `slot` carries the repeat index,
        which is what keeps repeats from collapsing onto one another."""
        self._slot = slot
        self._call_index = 0

    def _path(self, payload_hash: str) -> Path:
        # Slot first, call index second: a single execution makes several API
        # calls (one per tool round) and they must not overwrite each other.
        return self._cache_dir / f"{self._slot}__{self._call_index:02d}__{payload_hash}.json"

    class _Messages:
        def __init__(self, outer):
            self._outer = outer

        async def create(self, **kwargs):
            o = self._outer
            payload = json.dumps(kwargs, sort_keys=True, default=str)
            payload_hash = hashlib.sha256(payload.encode()).hexdigest()[:16]
            path = o._path(payload_hash)
            o._call_index += 1

            if path.exists():
                o.hits += 1
                from anthropic.types import Message
                return Message.model_validate(
                    json.loads(path.read_text(encoding="utf-8"))["response"]
                )

            if o._replay_only:
                raise RuntimeError(
                    f"--replay given but no cached response at {path.name}. "
                    "Run once without --replay to populate the cache."
                )

            response = await o._real.messages.create(**kwargs)
            o.misses += 1
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "request": kwargs,
                        "response": response.model_dump(mode="json"),
                        "cached_at": datetime.now(timezone.utc).isoformat(),
                    },
                    default=str, indent=2,
                ),
                encoding="utf-8",
            )
            return response


def load_prompts(state: str) -> list[dict]:
    prompts = yaml.safe_load(PROMPTS_PATH.read_text(encoding="utf-8"))
    return [p for p in prompts if state in p["states"]]


def restore_fixture(state: str) -> Path:
    source = FIXTURE_DIR / f"{state}.db"
    if not source.exists():
        raise FileNotFoundError(
            f"{source} missing -- run: python eval/seed.py --state {state} "
            f"--out {source}"
        )
    for path in (SCRATCH_PATH,
                 SCRATCH_PATH.with_name(SCRATCH_PATH.name + "-wal"),
                 SCRATCH_PATH.with_name(SCRATCH_PATH.name + "-shm")):
        if path.exists():
            path.unlink()
    shutil.copyfile(source, SCRATCH_PATH)
    return SCRATCH_PATH


def drain_instrumentation(db_path: Path) -> dict:
    """Read the turn and its tool calls back out before the fixture is
    restored over them."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        turns = [dict(r) for r in conn.execute(
            "SELECT * FROM turns ORDER BY id"
        )]
        calls = [dict(r) for r in conn.execute(
            "SELECT * FROM tool_calls ORDER BY id"
        )]
    finally:
        conn.close()
    return {"turns": turns, "tool_calls": calls}


async def execute_one(
    brain, database, prompt: dict, repeat: int, state: str, client: CachingClient
) -> dict:
    restore_fixture(state)
    database.DB_PATH = SCRATCH_PATH

    conversation_id = f"{state}__{prompt['id']}__r{repeat}"
    client.begin(conversation_id)
    job_queue = StubJobQueue()

    record = {
        "prompt_id": prompt["id"],
        "state": state,
        "repeat": repeat,
        "message": prompt["message"],
        "expected_tool": prompt["expected_tool"],
        "expected_route": prompt["expected_route"],
        "conversation_id": conversation_id,
    }

    started = time.perf_counter()
    try:
        # The router runs first, exactly as bot.py orders it. Reproducing the
        # two checks here rather than importing message_handler is deliberate:
        # message_handler needs a live Update and Context, and faking those
        # convincingly is more code -- and more room to diverge -- than the two
        # lines it would save.
        import commands
        import keyboards
        text = prompt["message"]
        if text in keyboards.NAV_LABELS or \
                text.strip().lower().rstrip("?!.") in commands.TEXT_SHORTCUTS:
            record["actual_route"] = "button_route"
            record["reply"] = None
            record["instrumentation"] = {"turns": [], "tool_calls": []}
            record["error"] = None
        else:
            reply = await brain.get_response(
                CHAT_ID, text, [], job_queue,
                conversation_id=conversation_id, turn_index=1,
            )
            drained = drain_instrumentation(SCRATCH_PATH)
            record["reply"] = reply
            record["instrumentation"] = drained
            record["actual_route"] = (
                drained["turns"][0]["terminated_by"] if drained["turns"] else None
            )
            record["error"] = None
    except Exception as exc:
        record["actual_route"] = "error"
        record["reply"] = None
        record["error"] = f"{type(exc).__name__}: {exc}"
        record["traceback"] = traceback.format_exc()
        record["instrumentation"] = {"turns": [], "tool_calls": []}

    record["wall_ms"] = (time.perf_counter() - started) * 1000
    record["jobs_scheduled"] = len(job_queue.scheduled)
    calls = record["instrumentation"]["tool_calls"]
    record["actual_tools"] = [c["tool_name"] for c in calls]
    record["tool_hit"] = (
        prompt["expected_tool"] in record["actual_tools"]
        if prompt["expected_tool"] else None
    )
    record["route_hit"] = record["actual_route"] == prompt["expected_route"]
    return record


async def run(args) -> Path:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise SystemExit(
            "ANTHROPIC_API_KEY is not set. Put it in .env (as bot.py expects) "
            "or export it in this shell. Failing here rather than after the "
            "whole suite has run and errored."
        )
    os.environ["KANGANI_INSTRUMENTATION"] = "1"
    if args.ceiling:
        os.environ["KANGANI_MAX_TOOL_ITERATIONS"] = str(args.ceiling_iterations)
    # DB_PATH must be set before database is imported: it is read into a module
    # constant at import time.
    os.environ["DB_PATH"] = str(SCRATCH_PATH)
    restore_fixture(args.state)

    import brain
    import database
    import tools
    database.DB_PATH = SCRATCH_PATH
    freeze_clock(brain, tools)

    # Set the ceiling on the module directly, not only through the environment.
    # MAX_TOOL_ITERATIONS is read once at import; if anything imported brain
    # before this function ran, the env var arrives too late and the run
    # silently executes at the default 10 while claiming to test the ceiling.
    # That exact failure happened during development and was caught only
    # because the manifest records the EFFECTIVE value rather than the
    # requested one -- which is the argument for recording effective values
    # everywhere.
    if args.ceiling:
        brain.MAX_TOOL_ITERATIONS = args.ceiling_iterations
    if brain.MAX_TOOL_ITERATIONS != (
        args.ceiling_iterations if args.ceiling else 10
    ):
        print(
            f"WARNING: MAX_TOOL_ITERATIONS is {brain.MAX_TOOL_ITERATIONS}, "
            f"not the value this run intends.",
            file=sys.stderr,
        )

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"_ceil{args.ceiling_iterations}" if args.ceiling else ""
    run_dir = RUNS_DIR / f"{args.state}{suffix}_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = CACHE_DIR / (f"{args.state}{suffix}")

    client = CachingClient(brain._get_client(), cache_dir, args.replay)
    brain._get_client = lambda: client

    prompts = load_prompts(args.state)
    results_path = run_dir / "results.jsonl"
    total = len(prompts) * args.repeats
    done = 0
    # A misconfiguration fails identically on every prompt. Running all 400 of
    # them to find that out wastes time and, when the cause is upstream rather
    # than local, real money.
    consecutive_errors = 0
    ABORT_AFTER = 3

    with results_path.open("w", encoding="utf-8") as fh:
        for prompt in prompts:
            for repeat in range(1, args.repeats + 1):
                record = await execute_one(
                    brain, database, prompt, repeat, args.state, client
                )
                fh.write(json.dumps(record, default=str) + "\n")
                fh.flush()
                done += 1
                if record["error"] is None:
                    consecutive_errors = 0
                else:
                    consecutive_errors += 1
                mark = "." if record["error"] is None else "E"
                print(
                    f"[{done:>4}/{total}] {mark} {prompt['id']} r{repeat} "
                    f"-> {record['actual_route']}",
                    flush=True,
                )
                if consecutive_errors >= ABORT_AFTER:
                    print(
                        f"\nAborting: {ABORT_AFTER} consecutive failures. "
                        f"Last error was:\n  {record['error']}\n"
                        f"Full traceback in {results_path}",
                        file=sys.stderr,
                    )
                    raise SystemExit(1)

    manifest = {
        "run_id": run_id,
        "state": args.state,
        "repeats": args.repeats,
        "ceiling_config": bool(args.ceiling),
        "max_tool_iterations": brain.MAX_TOOL_ITERATIONS,
        "model": brain.MODEL,
        "prompts": len(prompts),
        "executions": total,
        "cache_hits": client.hits,
        "cache_misses": client.misses,
        "replay_only": args.replay,
        "fixture_sha256": hashlib.sha256(
            (FIXTURE_DIR / f"{args.state}.db").read_bytes()
        ).hexdigest(),
        "started_utc": run_id,
    }
    (run_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(f"\n{total} executions -> {results_path}")
    print(f"cache: {client.hits} hit / {client.misses} miss")
    return run_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True,
                        choices=["full", "empty", "single", "conflicting"])
    parser.add_argument("--repeats", type=int, default=3,
                        help="executions per prompt, for dispatch variance")
    parser.add_argument("--ceiling", action="store_true",
                        help="run with a lowered MAX_TOOL_ITERATIONS")
    parser.add_argument("--ceiling-iterations", type=int, default=2)
    parser.add_argument("--replay", action="store_true",
                        help="fail rather than make any live API call")
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()