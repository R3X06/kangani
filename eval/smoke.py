"""Smoke check: every seed state builds into a real database.

Run by CI, and runnable by hand (`python eval/smoke.py`). This exists as a
script rather than an inline CI step because an inline step is only ever
tested by pushing it, and a shell heredoc nested inside a YAML block scalar
is exactly the kind of thing that fails on the first run for reasons that
have nothing to do with the code being checked.

What it asserts, and what it deliberately does not: it asserts each state
produces a file with tables in it. It does NOT assert row counts. SQLite
writes a valid header for a database with no tables, so file size alone
proves nothing -- but the `empty` state is empty on purpose, so a row-count
assertion would be testing the assertion rather than the fixture.
"""

import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
SEED = EVAL_DIR / "seed.py"
STATES = ["full", "single", "conflicting", "empty"]


def check(state: str, out_path: Path) -> str:
    result = subprocess.run(
        [sys.executable, str(SEED), "--state", state, "--out", str(out_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return f"{state}: seed.py exited {result.returncode}\n{result.stderr.strip()}"
    if not out_path.exists():
        return f"{state}: seed.py succeeded but wrote no file at {out_path}"

    conn = sqlite3.connect(out_path)
    try:
        tables = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
    finally:
        conn.close()
    if tables == 0:
        return f"{state}: {out_path} has no tables"

    print(f"{state}: ok, {tables} tables")
    return ""


def main() -> int:
    # A temporary directory, not eval/db/, so the check never depends on or
    # disturbs a fixture someone is mid-measurement against.
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        for state in STATES:
            failures.append(check(state, Path(tmp) / f"{state}.db"))
    failures = [f for f in failures if f]
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
