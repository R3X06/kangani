# Kangani — Revamp Prompt v2: Unified Topic Tree (updated for current codebase)

Start in **Plan mode**. Present the plan (files touched, function signatures, migration approach) before writing any code, so I can review it before you move to Edit mode.

## Context — why this version is different from the original design

This collapses `modules` and `events` into one arbitrarily-nestable `topics` tree — a task/topic/reminder can attach anywhere in it, or nowhere, instead of being forced to pick exactly one of `module_id`/`event_id`. That reasoning hasn't changed.

**What's changed is the stakes.** When this was first designed, `kangani.db` held nothing but test data, so a drop-and-recreate migration was the pragmatic call. That's no longer true: `schedule_blocks` now holds a real imported semester timetable (via the PDF import feature), `chat_settings` holds a real semester anchor and recess weeks, and there are real tasks and reminders. **This prompt requires an actual data-preserving migration for `modules`, `events`, `topics`, `tasks`, and `schedule_blocks` — not a drop-and-recreate.** `chat_settings` is untouched by this revamp entirely (it has no module/event/topic relationship) and must not be touched, dropped, or reset.

Three other features now depend on the module/event system that didn't exist when this was first scoped, and all three need to keep working after this revamp:
- **Timetable images** (`timetable_data.py`) source each class's color from `modules.color`.
- **PDF import** (`pdf_import.py`) creates schedule blocks tagged by `module_name`, which currently resolves through `get_or_create_module` inside `create_schedule_block`.
- **The events pillar's nav UI** (`keyboards.py`, `callbacks.py`, `commands.py`) has `origin_event_id` threading through task callbacks, and `build_events_root_view`/`build_events_detail_view` query the `events` table directly.

## 1. Schema changes — migrate, don't drop

**`topics`** — drop `module_id`/`event_id`/their CHECK. Add:
- `kind TEXT` (nullable, free string — see kind discipline below)
- `status TEXT` (nullable, free string)
- `event_datetime TEXT` (nullable, ISO-8601 UTC, same convention as `tasks.deadline`)
- `color TEXT` (nullable — see color migration below)

Uniqueness moves to `(parent_topic_id, name COLLATE NOCASE)`.

**`tasks`** — replace `module_id`/`event_id`/CHECK with a single nullable `topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE`.

**`schedule_blocks`** — replace `module_id` with nullable `topic_id INTEGER REFERENCES topics(id) ON DELETE CASCADE`. **Keep `create_schedule_block`'s external `module_name` parameter as a convenience wrapper** that resolves to `get_or_create_topic(name=module_name, kind='module')` internally, rather than requiring every caller (`pdf_import.py`, the natural-language tool schema) to be rewritten to pass `topic_id` directly. This keeps PDF import working with zero changes to `pdf_import.py` itself.

**`reminders`** — add nullable `linked_topic_id`, drop `linked_event_id`. CHECK: at most one of `linked_task_id`/`linked_topic_id` set.

**Drop `modules` and `events` table *definitions*, but migrate their data first:**

Write the migration as actual data-copying steps, run once, in this order (so foreign keys resolve correctly):
1. For every row in `modules`: insert a corresponding `topics` row with `kind='module'`, carrying over its `name` and `color`.
2. For every row in `events`: insert a corresponding `topics` row with `kind='event'`, carrying over its `title` (as `name`), and `start_date`/`end_date` mapped into `event_datetime` (use `start_date` as the datetime if present; flag in your plan how you're handling the type→something-sensible mapping, since the old `events.type` enum — talk/hackathon/other — has no direct new home unless you fold it into the new free-string `kind`, e.g. `kind='event:hackathon'` or keep a separate note; don't silently drop it without flagging the choice).
3. Build an old-`module_id`→new-`topic_id` and old-`event_id`→new-`topic_id` mapping from steps 1–2.
4. Re-point every `topics.module_id`/`topics.event_id` (old parent references), `tasks.module_id`/`tasks.event_id`, and `schedule_blocks.module_id` at the corresponding new `topic_id` using that mapping, before dropping the old columns.
5. Only after all data is copied and re-pointed, drop the old `modules`/`events` tables and the old columns.

This is more work than the bump-`user_version`-and-recreate approach used everywhere else in this codebase — that's intentional and necessary now, given real data is at stake. If SQLite's ALTER TABLE limitations make an in-place column swap awkward, the standard create-new-table/copy-rows/drop-old/rename pattern is fine; just don't lose rows.

## 2. Color migration — keep timetable images working

Right now `timetable_data.py` builds its color map from `database.list_modules(chat_id)` reading `modules.color`, auto-assigned from `MODULE_COLOR_PALETTE` at module creation. After this revamp:
- `get_or_create_topic` takes over color auto-assignment **only when `kind='module'`** (a freeform life topic like the BBDC example from the original design doesn't need an auto-color) — same palette-cycling logic, just moved.
- `timetable_data.py`'s `_module_color_map` needs to source from `list_topics(chat_id, kind='module')` reading `topic.color` instead of `list_modules`. The occurrence dicts can keep using the key name `module_name` internally (even though it's now sourced from a topic) — no need to rename that field through `timetable_data.py`/the Jinja templates, since it's purely an internal dict key and renaming it is churn with no benefit.

## 3. Kind discipline (unchanged from original design)

- `kind`/`status` stay unconstrained `TEXT` — no CHECK, so nothing you invent later needs a schema change.
- New tool `list_topic_kinds(chat_id)` returns distinct `kind` values already in use, canonical ones (`course, year, semester, module, component, event`) first.
- System prompt: check `list_topic_kinds` before minting a new kind; matching is case-insensitive/trimmed.

## 4. Event reminders (unchanged from original design)

- Creating a topic with `event_datetime` set auto-creates reminders at `reminder_offsets_minutes` (default `[60, 30]`).
- New tool `add_event_reminder(topic_id, offset_minutes)` for bolting on more later.

## 5. Nav UI — reconcile with the already-shipped events pillar

This is new since the original draft: `keyboards.py`/`callbacks.py`/`commands.py` already have real events-pillar code (`origin_event_id` threading in `task_list_keyboard`/`task_edit_menu_keyboard`, `event_callback`, `build_events_root_view`/`build_events_detail_view` querying `events` directly).

- Rename `origin_event_id` → `origin_topic_id` throughout the callback chain (`keyboards.py`, `callbacks.py`'s `task_callback` parsing) — same rename flagged in the original design, now with actual shipped code to update rather than a hypothetical.
- `build_events_root_view`/`build_events_detail_view` in `commands.py` switch from querying `events`/`query_events` to `list_topics(chat_id, kind='event')` filtered to future `event_datetime`.
- `event_callback`/`topic_callback` merging is still optional, not required — flag your call in the plan rather than assuming either way, same as before.

## 6. Tools & brain.py

- Retire `create_event`, `query_events`, `list_modules` schemas.
- New: `list_topic_kinds`, `add_event_reminder`.
- `create_topic` gains `kind`, `status`, `event_datetime`, `reminder_offsets_minutes`.
- `create_task`/`create_reminder` replace `module_name`/`event_id` with optional `topic_id`.
- **`create_schedule_block`'s tool schema is unchanged** — it still takes `module_name` as far as Claude and the PDF import path are concerned; only its internal implementation now resolves through topics.
- System prompt: unified tree explanation, kind discipline, event auto-reminders, standalone-attachment nudge (ask once, don't force, don't re-ask if declined) — all as originally specced.

## Not in scope for this prompt

- Photo/document note uploads, auto-search of notes to answer general questions — separate prompts, unchanged from before.
- Daily digest, reactive judgment, companion voice — still untouched.
- Don't touch `chat_settings`, `week_pattern` matching logic, or anything in `scheduler.py`'s recess-week math — none of it has any relationship to modules/topics/events, and it's working, tested code as of the last commit.