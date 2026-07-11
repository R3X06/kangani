# Kangani — Prompt: Semester Week Anchor, Alternating Weeks, /today and /week

Start in **Plan mode**. Present the plan (files touched, function signatures, migration approach) before writing any code, so I can review it before you move to Edit mode.

## Context

`schedule_blocks` currently has no concept of "which semester week is this." Some classes run every week, others alternate (e.g. `Wk2,4,6,8,10,12`), and the only way to know is a human reading the label. Kangani needs to compute this itself so `/today` and `/week` only show classes that actually happen. This requires one anchor: the calendar date that week 1 starts on (e.g. "August 13th is week 1"). Everything else derives from that.

`/today` and `/week` are **deterministic slash commands**, not LLM tool calls — same pattern as `/tasks` and `/reminders` in `commands.py`. No Claude round-trip needed for a fixed-format render.

## 1. Schema changes (`database.py`)

- New table `chat_settings`: `chat_id INTEGER PRIMARY KEY`, `semester_week1_start_date TEXT` (a plain `YYYY-MM-DD`, no time component — it's a calendar anchor, not an instant). One row per chat. Kept as its own small table (rather than crammed into an existing one) since more settings will likely land here later (roadmap mentions energy-aware scheduling, etc.) — extensible without another migration.
- `schedule_blocks` gains `week_pattern TEXT NOT NULL DEFAULT 'every'`. Add it the same additive, non-destructive way `class_type` was added last time (`_ensure_column`, not a version-bump recreate — this one doesn't need to touch existing rows' data, just add a column with a safe default). Valid values: `'every'`, `'odd'`, `'even'`, or an explicit comma-separated list of week numbers (e.g. `'1,3,5,7,9,11,13'`). Validate the format in `create_schedule_block` and raise `ValueError` on anything else.

## 2. Data-layer changes

- `set_semester_anchor(chat_id, start_date)` / `get_semester_anchor(chat_id)` in `database.py` — upsert/read the single row in `chat_settings`.
- New helper `compute_week_number(anchor_date: date, target_date: date) -> int | None` (put it in `scheduler.py`, next to the other date/time utilities) — `((target_date - anchor_date).days // 7) + 1`, clamped to `1..13`; return `None` if `target_date` is before the anchor or the computed number exceeds 13 (outside the semester).
- `create_schedule_block(..., week_pattern='every')` — pass through and validate.
- `_expand_occurrences` in `tools.py` needs a week_pattern filter step: for each candidate occurrence, resolve the semester week number for its date via `get_semester_anchor` + `compute_week_number`, then keep it only if `week_pattern` is `'every'`, matches odd/even parity, or the resolved week number is in the explicit list. **If no anchor is set for this chat yet, don't silently show everything or silently hide everything** — surface a clear message ("Set your semester start date first — tell me which date week 1 begins") instead of guessing.

## 3. Tools & brain.py

- New tool schema `set_semester_start(start_date)` — Claude calls this when the user tells it which date week 1 begins (e.g. "week 1 starts August 13th," "the week of Aug 13 is week 1"). Idempotent — calling it again just updates the anchor.
- System prompt addition: explain the anchor's purpose in one or two sentences, and instruct Claude to ask for it (once, not repeatedly) if the user tries to create a schedule block with a non-`'every'` `week_pattern` and no anchor is set yet for this chat.

## 4. `/today` and `/week` commands

Both render a single Telegram message using `parse_mode="HTML"` with the whole body wrapped in `<pre>...</pre>` for monospace alignment — **HTML-escape every piece of dynamic text** (module titles, locations, task titles, reminder messages) before interpolating, since raw `<`/`>`/`&` in user content would otherwise break Telegram's HTML parser.

**`build_today_view(chat_id) -> str`** (`commands.py`):
- Resolve today's local date (`TIMEZONE` env, same convention as everywhere else) and its semester week number (blank/omitted header if no anchor set — don't block the rest of the view on it).
- Section 1: today's classes — expand `schedule_blocks` for today's single-day range through the existing `_expand_occurrences` (now week_pattern-aware), sorted by `start_time`.
- Section 2: tasks whose `deadline` falls today (reuse `query_tasks` with `deadline_from`/`deadline_to` both set to today's UTC bounds).
- Section 3: reminders whose `trigger_data` falls today (new small query or filter over `list_pending_reminders`).
- Omit a section entirely (not just "none") when it's empty, except classes — keep "No classes today" if the schedule section would otherwise be blank, since that's the primary reason someone runs `/today`.

**`build_week_view(chat_id, week_number=None) -> str`** (`commands.py`):
- If `week_number` is given, resolve its Monday–Sunday date range from the anchor; if omitted, use the current calendar week. If no anchor is set and `week_number` was requested, return the same "set your semester start date" message as above.
- Expand occurrences across the 7-day range (week_pattern-aware), group by day, render each day as a header + indented time/class/location lines, or "— nothing" if empty. Match the mockup's layout (day header line, `HH:MM Title - Location` rows, blank day fallback text) — I approved that format already, so mirror it exactly rather than reinterpreting it.

Thin handlers + registration:
- `today_command`, `week_command` in `commands.py` (the latter parses an optional integer arg from `context.args` for an explicit week number).
- Register both in `bot.py`: `CommandHandler("today", commands.today_command)`, `CommandHandler("week", commands.week_command)`, plus `BotCommand` entries in `BOT_COMMANDS` so they show in Telegram's `/` menu.

## Not in scope for this prompt

- PDF schedule import (rasterize-and-read parsing pipeline, document upload handler, confirmation-before-commit step) — separate prompt, since it's a substantial standalone piece of work that *depends* on `week_pattern` and the anchor already existing here.
- The 7:30am daily digest job — a future prompt, though it can reuse `build_today_view`'s formatting once it exists; don't wire up any scheduling here.
- Reactive judgment and companion voice — untouched, as before.
