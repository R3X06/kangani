# Kangani — Prompt: Recess Week Support (official vs. continuous week numbers)

Start in **Plan mode**. Present the plan (files touched, function signatures, migration approach) before writing any code, so I can review it before you move to Edit mode.

## Context

The semester anchor currently counts continuously from week 1 with no way to skip a calendar week for recess/reading week. This causes two real problems:

1. A class with `week_pattern='every'` has no bound at all right now except the anchor itself — it'll show on every matching weekday indefinitely, including during recess, since `'every'` never gets checked against the semester's week structure.
2. There's no way to align Kangani's week count with the school's *official* week numbering once a recess week is inserted — school "week 8" and Kangani's 8th continuous week since the anchor stop being the same week, permanently, from that point in the semester onward.

Right now the only workaround is Claude manually tracking a conversion offset in conversation — which depends on it correctly recalling and reapplying arithmetic every time, indefinitely. This prompt replaces that with a real, deterministic calculation.

**Key design decision:** recess weeks are identified by **date**, not by a week number you'd have to already know how to compute — you tell Kangani "the week of September 28th is recess" (any date that falls within that calendar week), same pattern as `set_semester_start`. Internally, that date resolves to a *continuous* week number (relative to the anchor, uncapped, no recess adjustment — recess weeks don't depend on each other) and that's what gets stored.

## 1. Schema changes (`database.py`)

- `chat_settings` gains `recess_weeks TEXT` (nullable) — a comma-separated list of *continuous* week numbers (e.g. `"7"` or `"7,14"`), same storage convention as `schedule_blocks.week_pattern`. Add via the existing additive `_ensure_column` pattern, not a version-bump recreate.
- New functions, mirroring `set_semester_anchor`/`get_semester_anchor`:
  - `set_recess_weeks(chat_id, recess_dates: list[str]) -> list[int]` — for each ISO date given, resolve its continuous week number relative to the stored anchor (raise `ValueError` if no anchor is set yet — recess weeks are meaningless without one) and store the resulting set. Idempotent — calling again replaces the stored set entirely, it doesn't accumulate.
  - `get_recess_weeks(chat_id) -> set[int]` — returns the stored continuous week numbers, empty set if none.

## 2. Week-number math (`scheduler.py`)

This is where the actual fix lives. Restructure around two distinct concepts that are currently conflated: a **continuous** week count (raw weeks since the anchor, no adjustment) and an **official** week number (continuous, minus however many recess weeks fall before it, which is what `/week N`, `week_pattern`, and everything user-facing should mean by "week N").

- `_raw_week_number(anchor_date, target_date) -> int | None` — `((target_date - anchor_date).days // 7) + 1`, or `None` if `target_date` is before the anchor. No cap, no recess adjustment — this is the continuous count recess weeks themselves are stored in terms of.
- `compute_week_number(anchor_date, target_date, recess_weeks=frozenset()) -> int | None` — rewritten to: get the raw continuous week; return `None` if it's `None` or if it *is* a recess week (a recess week has no official number); otherwise subtract however many recess weeks fall before it, and return that only if it lands in `1..13` (the semester's official teaching-week span), else `None`.
- `official_to_continuous(official_week: int, recess_weeks: frozenset()) -> int` — the inverse: given an official week number, find the continuous week it corresponds to. Recess weeks shift everything after them by one, so this needs to account for however many recess weeks fall at or before the resolved continuous week — a small fixed-point loop is the simplest correct approach (recess sets are always small, so this converges in a couple iterations; don't overengineer it).
- `resolve_week_range(anchor_date, today, week_number, recess_weeks=frozenset())` — when `week_number` is given, convert it through `official_to_continuous` first, *then* compute the Monday from that continuous count. When it's `None` (current week), resolve today's Monday and label it via `compute_week_number` as before, now recess-aware.
- `expand_occurrences(blocks, date_from, date_to, anchor_date, recess_weeks=frozenset())` — gains the `recess_weeks` param, threaded into every `compute_week_number` call inside `_keep`. **This is also where the `'every'`-pattern bug from our last exchange gets fixed as part of the same change**: `'every'` should mean "every official teaching week," so once an anchor is set, a date whose `compute_week_number` resolves to `None` (pre-semester, post-semester, *or now also a recess week*) should not show the class, regardless of pattern. Only skip the bounds check entirely when no anchor is set at all (nothing to bound against yet).

## 3. Tools & brain.py

- New tool schema `set_recess_weeks(recess_dates: array of ISO date strings)` — description should make clear this is for marking a reading/recess week as a whole (pass any single date that falls within it, e.g. the Monday), not for skipping individual classes.
- System prompt addition: explain that Kangani's week numbers are the school's official numbers (recess weeks excluded automatically) once `set_recess_weeks` has been called for a chat — Claude should never manually track or apply a week-offset conversion in conversation; that's exactly what this system now does deterministically. If the user mentions a break/recess/reading week, call `set_recess_weeks` rather than reasoning about the offset.

## 4. Threading the change through existing call sites

Every place that currently resolves `anchor_date` and calls `compute_week_number`/`resolve_week_range`/`expand_occurrences` needs to also fetch `recess_weeks` and pass it through — `commands.py` (`build_today_view`, `build_week_view`, the three image command handlers), `timetable_data.py` (all three `build_*_context` functions), and `tools.py`'s `_handle_query_schedule`. None of these need new logic of their own — they're just passing one more value down to functions that already do the real work, so keep this mechanical rather than reinventing anything at each call site.

## Not in scope for this prompt

- The topic-tree revamp, daily digest, reactive judgment, companion voice — still untouched, as always.
- Multiple recess periods spanning more than one consecutive week (e.g. a 2-week break) — `recess_dates` accepting one date per recess *week* already handles this fine (just pass a date from each of the two weeks), so no special-casing needed, but don't build any "recess range" shorthand beyond that unless it comes up naturally in your plan.