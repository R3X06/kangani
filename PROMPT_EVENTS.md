# Kangani — Prompt 1 of 4: Events & Attachment Model

Start in **Plan mode**. Present the plan (files touched, function signatures, migration approach) before writing any code, so I can review it before you move to Edit mode.

## Context

Right now `tasks` and `topics` can only attach to a `module` (`module_id` is `NOT NULL` on both). Non-module activities — hackathons, talks, one-off things that need their own tasks and notes but aren't a recurring academic module — have nowhere to attach. The `events` table already exists in the schema but nothing writes to it or reads from it.

Goal: `tasks` and `topics` can attach to *either* a `module` or an `event`, exactly one of the two. `notes` and `progress_logs` are unaffected — they always go through `topic_id`, and a topic resolves to whichever container it's under.

## 1. Schema changes (`database.py`)

Mirror the dual-nullable-FK + CHECK pattern already used elsewhere in this schema (see `reminders.linked_task_id`/`linked_event_id` and `schedule_blocks.day_of_week`/`specific_date`).

**`topics`**: `module_id` becomes nullable, add nullable `event_id INTEGER REFERENCES events(id) ON DELETE CASCADE`, add `CHECK ((module_id IS NULL) <> (event_id IS NULL))`.

**`tasks`**: same shape — nullable `module_id`, new nullable `event_id INTEGER REFERENCES events(id) ON DELETE CASCADE`, same CHECK.

**`events`**: no column changes needed, just start using it.

**Migration note:** SQLite can't relax a `NOT NULL` or add a `CHECK` to an existing table via `ALTER TABLE` — it needs the create-new-table-copy-drop-rename dance, or a full recreate. Since `kangani.db` is local, gitignored, personal dev data with nothing at stake, the pragmatic move is: bump `PRAGMA user_version`, and if the stored version is behind, just drop and recreate the affected tables (or the whole DB) rather than building a real migration framework for pre-launch data. Flag this decision explicitly in your plan rather than silently picking one — I may want to keep what's in there.

## 2. Data-layer fixes — required, not optional

Every existing query that does `JOIN modules ON modules.id = tasks.module_id` (an `INNER JOIN`) will **silently drop every event-linked row** once `module_id` can be `NULL`. Change these to `LEFT JOIN modules ... LEFT JOIN events ...`, and compute a container label from whichever one is non-null:

- `create_task`, `update_task_status`, `get_task`, `query_tasks` — all need the join fix. `query_tasks` also needs a new optional `event_id` filter param (used by the event detail view).
- `get_or_create_topic` — when `parent_topic_id` is given, the parent lookup needs to resolve the parent's *container* (module or event) via `LEFT JOIN` on both, not just modules, to inherit whichever one applies onto the child row (same denormalization pattern already used for `module_id` inheritance). When no parent is given, require **exactly one** of `module_name` / `event_id` (raise `ValueError` if both or neither) — validate the event belongs to this `chat_id` if `event_id` is given. Update the existing-topic-match query to match on whichever container column is set (module+null-event, or event+null-module), not just `module_id`.
- `list_topics` — same `LEFT JOIN` fix, return an `event_title` alongside the now-nullable `module_name`. Update `build_path()` so the breadcrumb root uses `module_name` if set, else `event_title`.
- New: `create_event(chat_id, title, type, start_date=None, end_date=None, location=None)`.
- New: `query_events(chat_id, type=None, upcoming_only=True)` — when `upcoming_only`, exclude events whose `end_date` (or `start_date` if no `end_date`) is in the past. Order by `start_date` ascending.
- New: `get_event(chat_id, event_id)` — needed for the ownership-validation checks above and for the detail view.

## 3. Tools (`tools.py`, `brain.py`)

- New tool schemas: `create_event`, `query_events` — same style as existing schemas.
- `create_task` and `create_topic` schemas gain an alternate `event_id` (integer) parameter, mutually exclusive with `module_name`/`module_name`. Document that Claude should call `query_events` first to get the right id — same discipline already required for topics — rather than auto-creating an event by name the way modules get auto-created.
- Update `brain.py`'s system prompt: add guidance on when to use an event vs. a module (a time-boxed activity like a hackathon or talk that needs its own tasks/notes, vs. a recurring academic module), and the "look up the id first" rule for events.

## 4. Nav UI

**`keyboards.py`**
- New `EVENTS_LABEL = "🗓️ Events"`, add to `NAV_LABELS`.
- `persistent_reply_keyboard()` becomes 3 rows: `[TASKS_LABEL, REMINDERS_LABEL]`, `[TOPICS_LABEL, NOTES_LABEL]`, `[EVENTS_LABEL]`.
- `task_list_keyboard` and `task_edit_menu_keyboard` both gain an `origin_event_id=None` param, appended to every `callback_data` string when set (e.g. `task:complete:5:evt:12`).
- New `event_list_keyboard(events)` — one row per event, `callback_data=f"event:open:{event_id}"`.
- New `event_detail_keyboard(event_id, tasks, topics)` — task rows (Complete/Edit, `origin_event_id` set), then a row per top-level topic (`callback_data=f"topic:open:{topic_id}"`, reusing the existing topic callback unchanged), then a `⬅️ Back` row to `event:root`.

**`commands.py`**
- `build_events_root_view(chat_id)` — list upcoming events via `query_events(upcoming_only=True)`; friendly empty message if none.
- `build_events_detail_view(chat_id, event_id)` — event info (title/type/dates/location) + its tasks (`query_tasks(event_id=...)`) + its top-level topics (filter `list_topics(chat_id)` locally to `event_id` match and `parent_topic_id is None`, same style `build_topics_module_view` already uses for modules).
- `events_command` thin handler; add to `nav_button_pressed` dispatch; add the Events button/command to `help_command`'s text.
- `build_topics_detail_view`'s `back_target` logic needs a third branch: if the topic has no parent and no `module_id` (i.e. it's event-rooted), back target is `f"event:open:{topic['event_id']}"` instead of the current module-only fallback.

**`bot.py`**
- Add `BotCommand("events", "Browse your events")` to `BOT_COMMANDS`.
- Add `CommandHandler("events", commands.events_command)`.
- Add `CallbackQueryHandler(callbacks.event_callback, pattern=r"^event:")`.

**`callbacks.py`**
- New `event_callback` — handles `event:root` and `event:open:{id}`, same edit-in-place style as `topic_callback`.
- `task_callback` — parse variable-length `callback_data` (3 parts normally, 5 when an `:evt:{id}` origin is present). After a complete/status-change/back action, re-render `build_events_detail_view(chat_id, origin_event_id)` instead of the global `build_tasks_view(chat_id)` when an origin is present. The `menu` action (opening the status submenu) needs to pass the origin through to `task_edit_menu_keyboard` too, so the submenu's own Back button stays scoped.

## Not in scope for this prompt

Daily digest, reactive-judgment prompt changes, and voice/personality changes are separate prompts — don't touch `brain.py`'s response-synthesis behavior or add any scheduling jobs here.