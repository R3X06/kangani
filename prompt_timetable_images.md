# Kangani — Prompt: Timetable Image Generator (Daily / Weekly / Monthly)

Start in **Plan mode**. Present the plan (files touched, function signatures, new dependencies) before writing any code, so I can review it before you move to Edit mode.

## Before you run this

Copy the three approved mockup files into the repo first, so the exact design is available to work from:

```
design/mockups/kangani_weekly_mockup.html
design/mockups/kangani_daily_mockup.html
design/mockups/kangani_monthly_mockup.html
```

**Design fidelity is the point of this prompt — convert these into Jinja2 templates, don't redesign them.** Every color, font pairing, spacing value, the torn-paper tab, the hand-drawn date circle, the shape-per-class-type markers, the two-part legend — all of it carries over unchanged. The only thing that changes is replacing hardcoded content (the specific classes, dates, module names) with Jinja2 variables and loops. If anything about the approved design is ambiguous when you get to a dynamic case it didn't cover (see edge cases below), flag it in your plan rather than inventing a new visual treatment.

## 1. Dependencies

- `playwright` (renders the HTML templates to PNG via headless Chromium — far more faithful to real CSS/webfont rendering than a PIL-based approach, and it's the same engine that would've shown you the mockups accurately).
- `jinja2` (templating).
- Add both to `requirements.txt`. Note in your plan that after `pip install playwright`, the browser binary itself needs a one-time separate install (`playwright install chromium`) — flag this as a manual step for me, same as the `poppler-utils` note from the PDF import prompt. Don't attempt it yourself.

## 2. Module color palette — single source of truth

`database.py`'s `MODULE_COLOR_PALETTE` currently holds a different, unrelated 6-color set. Replace it with the 8-color palette from the approved monthly mockup (the `--m-*` CSS variables), so a module's color is consistent everywhere it's ever shown, not just in these images:

```python
MODULE_COLOR_PALETTE = [
    "#B5646B", "#748264", "#5E7A93", "#C79A44",
    "#8C5B7C", "#4F8074", "#A8763E", "#9B5A5A",
]
```

Existing modules already have a color assigned from the old palette — that's fine to leave as-is (no migration needed, this only affects newly created modules going forward) unless you'd rather reassign existing ones for consistency; flag that choice in your plan rather than picking silently, since it'd change colors on modules I've already gotten used to.

## 3. Templates (`templates/daily.html`, `templates/weekly.html`, `templates/monthly.html`)

Convert each mockup 1:1 into a Jinja2 template:

- **`daily.html`** — variables for day name, date, week number; a loop over the timeline `slot`s (time, module title, class type, location); a loop over the "Also today" list (tasks due + reminders firing, matching the mockup's ring/dot row styles).
- **`weekly.html`** — a loop over 7 `day` blocks; within each, a loop over that day's classes, or the "free day" fallback text when empty; the `today` class applied to whichever day matches the render date.
- **`monthly.html`** — a loop building the full grid including leading/trailing blank cells so the 1st of the month lands in the correct weekday column; per-day marker loop (shape by `class_type`, color by the module's assigned `MODULE_COLOR_PALETTE` entry); the deadline flag icon when a task's `deadline` falls on that date; the `today` hand-drawn circle; and a legend built from **only the modules that actually appear in this month's view** (not every module the user has ever created).

**Marker shape mapping** (from your last confirmed direction): `Lecture`/`Lecture` → filled circle, `Tutorial` → horizontal dash, `Lab` → asterisk (three crossing lines, as in the mockup), everything else (`Seminar`, `Design`, `Project`) → diamond, matching the "Seminar / other" legend entry we settled on.

**Edge case — more markers than fit in a monthly cell.** The mockup's real data never needed more than 4 markers in one cell. Decide a sane cap (the mockup's own layout suggests ~4–5 before crowding) and show a small `+N` overflow indicator past that, styled quietly, consistent with the existing legend's muted tone — flag your chosen cap and treatment in the plan.

## 4. Data assembly (new `timetable_data.py`)

Three functions producing the Jinja context for each template, reusing what already exists rather than re-querying from scratch:

- `build_daily_context(chat_id, target_date)` — reuses the (now-relocated, per the earlier cleanup prompt) occurrence-expansion helper for one day, plus `query_tasks`/reminder-by-date, same sources `/today` already uses.
- `build_weekly_context(chat_id, week_number=None)` — reuses the same week-range resolution `/week` already has.
- `build_monthly_context(chat_id, year, month)` — new range logic (first day to last day of the month), expanding occurrences across it and collecting task deadlines per day.

Each returns a plain dict ready to hand straight to `template.render(**context)`.

## 5. Rendering pipeline (new `timetable_image.py`)

- `_render_html_to_png(html: str) -> bytes` — loads the HTML string into a Playwright page (`set_content`), sets viewport width `1080` with `device_scale_factor=2` (crisp on phone screens), and takes a `full_page=True` screenshot so height adapts to content automatically rather than being hardcoded.
- **Reuse one Playwright browser instance for the bot's lifetime** rather than launching Chromium per request — cold-launching a browser per image would make every image command noticeably slow. Launch it once in `bot.py`'s `post_init` (alongside the existing `database.init_db()`/`scheduler.reschedule_pending_reminders()` calls) and store it somewhere accessible to the command handlers (e.g. `application.bot_data['browser']`); close it cleanly on shutdown.
- `render_daily_image(chat_id, target_date) -> bytes`, `render_weekly_image(...)`, `render_monthly_image(...)` — each combines the matching `timetable_data.py` context-builder, Jinja render, and `_render_html_to_png`.

## 6. Commands

New slash commands, thin handlers in `commands.py`, registered in `bot.py`'s `BOT_COMMANDS` and `CommandHandler`s:

- `/dayimage` — today's daily image. No args.
- `/weekimage [week_number]` — this week's image by default, or a specific week if an integer arg is given (same argument-parsing pattern as `/week`).
- `/monthimage [month]` — current month by default; an optional month name or number for a different one.

Each handler calls the matching `render_*_image` function and sends it via `context.bot.send_photo(chat_id, photo=png_bytes)`. If no anchor is set (same `AnchorNotSetError` case `/week` already handles) or there's nothing to show, reply with text instead of attempting an empty image.

## Not in scope for this prompt

- Any change to the existing text-based `/today` and `/week` — they stay as-is; these are new, additional commands, not replacements.
- The topic-tree revamp, daily digest, reactive judgment, companion voice — untouched, as always.