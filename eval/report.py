"""Generates eval/METRICS.md from run artifacts.

    python eval/report.py

Every number in the write-up is produced here and nowhere else. README.md is
narrative and deliberately restates no figures: a hand-copied number drifts the
first time the suite is re-run, and a stale one is worse than a missing one
because it still looks authoritative.

Provenance is emitted alongside each table -- run id, model, effective
MAX_TOOL_ITERATIONS, cache hit/miss, and the SHA-256 of the fixture the run
executed against. Any figure can be traced back to exactly one run directory.

Datasets A (synthetic) and B (real) are reported in separate sections with
separate sample sizes and are never summed.
"""

import json
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parent
REPO_ROOT = EVAL_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

RUNS_DIR = EVAL_DIR / "runs"
REAL_DB = EVAL_DIR / "real" / "kangani.db"
OUT_PATH = EVAL_DIR / "METRICS.md"
MIN_LABELLED_FOR_REGRESSION = 40


def load_runs() -> list[dict]:
    runs = []
    for run_dir in sorted(RUNS_DIR.glob("*/")):
        results = run_dir / "results.jsonl"
        if not results.exists():
            continue
        manifest_path = run_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
        records = [
            json.loads(line)
            for line in results.read_text(encoding="utf-8").splitlines()
        ]
        runs.append({"dir": run_dir, "manifest": manifest, "records": records})
    return runs


def table(headers: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def section_provenance(runs: list[dict]) -> str:
    rows = []
    for run in runs:
        m = run["manifest"]
        rows.append([
            f"`{run['dir'].name}`",
            m.get("state", "?"),
            m.get("repeats", "?"),
            m.get("max_tool_iterations", "?"),
            len(run["records"]),
            f"{m.get('cache_hits', 0)}/{m.get('cache_misses', 0)}",
            f"`{str(m.get('fixture_sha256', ''))[:12]}`",
        ])
    body = table(
        ["run", "state", "repeats", "max_iter", "executions",
         "cache hit/miss", "fixture sha256"],
        rows,
    )
    models = {r["manifest"].get("model") for r in runs}
    return (
        "## Provenance\n\n"
        f"Model: {', '.join(sorted(m for m in models if m)) or 'unknown'}. "
        "`max_iter` is the EFFECTIVE ceiling the run executed at, not the one "
        "requested -- during development those disagreed, and recording the "
        "effective value is what caught it.\n\n" + body
    )


def section_sample_sizes(runs: list[dict], real: dict | None) -> str:
    a_exec = sum(len(r["records"]) for r in runs)
    a_calls = sum(
        len(rec.get("instrumentation", {}).get("tool_calls") or [])
        for r in runs for rec in r["records"]
    )
    a_prompts = len({rec["prompt_id"] for r in runs for rec in r["records"]})
    rows = [
        ["A synthetic", "executions", a_exec],
        ["A synthetic", "tool calls", a_calls],
        ["A synthetic", "distinct prompts", a_prompts],
        ["A synthetic", "runs", len(runs)],
        ["B real", "turns", real["n_turns"] if real else 0],
        ["B real", "tool calls", real["n_calls"] if real else 0],
        ["B real", "labelled turns", real["n_labelled"] if real else 0],
    ]
    return (
        "## Sample sizes\n\n"
        "Stated separately per dataset. These are never summed: synthetic "
        "executions measure branch reachability against fixtures, real turns "
        "measure behaviour in use. Adding them would produce a number that "
        "describes neither.\n\n"
        + table(["dataset", "unit", "n"], rows)
    )


def section_coverage(runs: list[dict]) -> str:
    import tools as kangani_tools

    all_tools = sorted(kangani_tools.TOOL_HANDLERS)
    counts = Counter(
        call["tool_name"]
        for run in runs if not run["manifest"].get("ceiling_config")
        for rec in run["records"]
        for call in (rec.get("instrumentation", {}).get("tool_calls") or [])
    )
    dead = [t for t in all_tools if counts.get(t, 0) == 0]
    rows = [[t, counts.get(t, 0)] for t in
            sorted(all_tools, key=lambda t: -counts.get(t, 0))]
    return (
        "## A — Tool coverage\n\n"
        f"{len(all_tools) - len(dead)} of {len(all_tools)} registered tools "
        f"were invoked at least once across the default-config runs. "
        f"Never called: {', '.join(f'`{t}`' for t in dead) if dead else 'none'}.\n\n"
        "The suite contains a prompt written specifically for every registered "
        "tool, so a tool that is still never called was not reached even when "
        "aimed at directly. That points at the tool description, not at "
        "coverage.\n\n"
        + table(["tool", "calls"], rows)
    )


def section_routes(runs: list[dict]) -> str:
    lines = ["## A — Termination paths\n"]
    lines.append(
        "Share is over EVERY execution, including those that never reached "
        "Claude. A rate computed only over tool-using turns flatters itself.\n"
    )
    rows = []
    for run in runs:
        counts = Counter(rec["actual_route"] for rec in run["records"])
        total = sum(counts.values())
        rows.append([
            run["manifest"].get("state", "?"),
            "yes" if run["manifest"].get("ceiling_config") else "no",
            total,
            counts.get("answer", 0),
            counts.get("button_route", 0),
            counts.get("ceiling", 0),
            counts.get("truncated", 0),
            counts.get("error", 0),
        ])
    lines.append(table(
        ["state", "ceiling cfg", "n", "answer", "button_route", "ceiling",
         "truncated", "error"], rows))

    ceiling_runs = [r for r in runs if r["manifest"].get("ceiling_config")]
    if ceiling_runs:
        llm = [rec for r in ceiling_runs for rec in r["records"]
               if rec["actual_route"] != "button_route"]
        hits = sum(1 for rec in llm if rec["actual_route"] == "ceiling")
        cap = ceiling_runs[0]["manifest"].get("max_tool_iterations")
        lines.append(
            f"\nUnder the lowered ceiling (`MAX_TOOL_ITERATIONS={cap}`), "
            f"{hits} of {len(llm)} LLM turns "
            f"({hits / len(llm):.1%}) exited via the synthesis-on-ceiling path."
        )
    else:
        lines.append("\nNo lowered-ceiling run present.")
    return "\n".join(lines)


def section_dispatch(runs: list[dict]) -> str:
    base = [r for r in runs if not r["manifest"].get("ceiling_config")]
    by_tool: dict[str, list[bool]] = {}
    for run in base:
        for rec in run["records"]:
            if rec["expected_tool"]:
                by_tool.setdefault(rec["expected_tool"], []).append(
                    bool(rec["tool_hit"])
                )
    rows = sorted(
        ([t, len(v), f"{sum(v) / len(v):.2f}"] for t, v in by_tool.items()),
        key=lambda r: float(r[2]),
    )

    # Variance: did the same prompt pick the same tool set on every repeat?
    sets: dict[tuple, set] = {}
    for run in base:
        for rec in run["records"]:
            key = (rec["state"], rec["prompt_id"])
            tools_used = tuple(sorted(set(rec["actual_tools"])))
            sets.setdefault(key, set()).add(tools_used)
    unstable = sum(1 for v in sets.values() if len(v) > 1)

    return (
        "## A — Dispatch accuracy and variance\n\n"
        f"{unstable} of {len(sets)} (prompt, state) pairs chose a different "
        f"tool set across repeats ({unstable / max(len(sets), 1):.1%}). Each "
        "repeat runs against a freshly restored fixture, so fixture drift is "
        "excluded and what remains is model nondeterminism.\n\n"
        "`hit rate` is the share of repeats in which the expected tool "
        "appeared anywhere in the turn -- prerequisite lookups are not "
        "penalised.\n\n"
        + table(["expected tool", "n", "hit rate"], rows)
    )


def section_shortcut(runs: list[dict]) -> str:
    base = [r for r in runs if not r["manifest"].get("ceiling_config")]
    over, under = [], []
    for run in base:
        for rec in run["records"]:
            routed = rec["actual_route"] == "button_route"
            should = rec["expected_route"] == "button_route"
            if routed and not should:
                over.append((rec["state"], rec["prompt_id"], rec["message"]))
            elif should and not routed:
                under.append((rec["state"], rec["prompt_id"], rec["message"]))
    lines = [
        "## A — Is the deterministic shortcut ever wrong?\n",
        "Two directions, asymmetric in cost. **Over-routing** short-circuits a "
        "prompt that carried real scope, so the filter is dropped and the user "
        "gets a plausible-looking wrong answer. **Under-routing** sends a bare "
        "canonical phrase to Claude, costing one API call and nothing else.\n",
        f"- over-routed: **{len(set(over))}** distinct prompts",
        f"- under-routed: **{len(set(under))}** distinct prompts\n",
    ]
    if over:
        lines.append("Over-routed:\n")
        lines.append(table(["state", "prompt", "message"],
                           [list(x) for x in sorted(set(over))]))
    if under:
        lines.append("\nUnder-routed:\n")
        lines.append(table(["state", "prompt", "message"],
                           [list(x) for x in sorted(set(under))]))
    return "\n".join(lines)


def section_retrieval() -> str:
    import database
    import retrieval  # noqa: F401

    fixture = EVAL_DIR / "db" / "full.db"
    if not fixture.exists():
        return ("## A — Retrieval\n\nFixture `eval/db/full.db` not built; "
                "run `python eval/seed.py --state full --out eval/db/full.db`.")
    database.DB_PATH = fixture
    chat_id = 999_000_001
    probes = {
        "chain_rule": "chain rule gradient loss weights",
        "cache_miss": "cache line memory hierarchy stall",
        "admissible": "admissible heuristic never overestimates",
    }
    rows = []
    misses = 0
    for slug, query in probes.items():
        hits = database.search_notes(chat_id, query, limit=10)
        conn = database.get_connection()
        try:
            members = conn.execute(
                "SELECT content FROM notes WHERE source = ? ORDER BY id",
                (f"seed:{slug}",),
            ).fetchall()
        finally:
            conn.close()
        for position, (content,) in enumerate(members):
            role = "lexical" if position == 0 else "paraphrase"
            match = next(
                ((i + 1, h["score"]) for i, h in enumerate(hits)
                 if h["text"][:40] in content), (None, 0.0))
            if role == "paraphrase" and match[0] is None:
                misses += 1
            rows.append([slug, role,
                         match[0] if match[0] else "not retrieved",
                         f"{match[1]:.2f}"])
    return (
        "## A — Retrieval (BM25 only)\n\n"
        "**One ranker, not three.** The cosine and RRF arms were deferred "
        "pending a decision on an embedding source. A fusion table built from "
        "a ranker that does not exist is exactly the kind of number this "
        "harness is meant to make impossible.\n\n"
        "Each planted pair states the same fact twice: once sharing the "
        "query's vocabulary, once paraphrased.\n\n"
        + table(["pair", "member", "rank", "BM25 score"], rows)
        + f"\n\nParaphrases not retrieved at all: **{misses} of {len(probes)}**."
    )


def load_real() -> dict | None:
    if not REAL_DB.exists():
        return None
    conn = sqlite3.connect(REAL_DB)
    conn.row_factory = sqlite3.Row
    try:
        turns = [dict(r) for r in conn.execute("SELECT * FROM turns")]
        calls = [dict(r) for r in conn.execute("SELECT * FROM tool_calls")]
    finally:
        conn.close()
    labelled = [t for t in turns if t.get("label")]
    return {"turns": turns, "calls": calls, "n_turns": len(turns),
            "n_calls": len(calls), "n_labelled": len(labelled)}


def section_real(real: dict | None) -> str:
    if real is None or real["n_turns"] == 0:
        return (
            "## B — Real usage\n\n"
            f"No data. Expected a copy of the production database at "
            f"`{REAL_DB.relative_to(REPO_ROOT)}`.\n\n"
            "Dataset B accumulates only while the instrumented bot is running. "
            "Nothing in this section is estimated from dataset A: synthetic "
            "prompts were written to reach specific branches, so their "
            "frequencies describe the suite's design, not anyone's usage."
        )
    counts = Counter(t["terminated_by"] for t in real["turns"])
    tool_counts = Counter(c["tool_name"] for c in real["calls"])
    lines = [
        "## B — Real usage\n",
        f"{real['n_turns']} turns, {real['n_calls']} tool calls, "
        f"{real['n_labelled']} hand-labelled.\n",
        table(["termination path", "turns"],
              [[k, v] for k, v in counts.most_common()]),
        "\n### Tool frequency in real use\n",
        table(["tool", "calls"], [[k, v] for k, v in tool_counts.most_common()]),
    ]
    if real["n_labelled"] < MIN_LABELLED_FOR_REGRESSION:
        lines.append(
            f"\nThe failure regression is not fitted: {real['n_labelled']} "
            f"labelled turns is below the {MIN_LABELLED_FOR_REGRESSION}-row "
            "floor. Coefficients from this little data would have error bars "
            "wide enough to include zero in both directions, and reporting "
            "them would imply a finding that is not there."
        )
    else:
        lines.append(
            f"\nRegression fitted on {real['n_labelled']} labelled turns; "
            "coefficients are in `analysis.ipynb` section 7. No accuracy or "
            "AUC is reported -- at this n it would be a number about the "
            "train/test split rather than about the bot."
        )
    return "\n".join(lines)


def main() -> None:
    runs = load_runs()
    if not runs:
        raise SystemExit(f"No runs in {RUNS_DIR}. Run eval/run.py first.")
    real = load_real()

    parts = [
        "# Kangani evaluation — measured results",
        "",
        "*Generated by `eval/report.py`. Do not edit by hand: every number "
        "here is read from a run artifact, and an edited value would no "
        "longer trace to one.*",
        "",
        f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
        section_provenance(runs),
        "",
        section_sample_sizes(runs, real),
        "",
        section_coverage(runs),
        "",
        section_routes(runs),
        "",
        section_dispatch(runs),
        "",
        section_shortcut(runs),
        "",
        section_retrieval(),
        "",
        section_real(real),
        "",
    ]
    OUT_PATH.write_text("\n".join(parts), encoding="utf-8")
    print(f"wrote {OUT_PATH} ({len(runs)} runs, "
          f"{sum(len(r['records']) for r in runs)} executions)")


if __name__ == "__main__":
    main()