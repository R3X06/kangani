# Kangani — Prompt: Cleanup + PDF Schedule Import

Start in **Plan mode**. Present the plan (files touched, function signatures, new dependencies) before writing any code, so I can review it before you move to Edit mode.

## 0. Cleanup from the /today /week session (small, do first)

Two minor things from the last commit (`8b2d6f7`), neither broken, both worth tightening while you're in this code:

1. **`commands.py` reaches into `tools._expand_occurrences` directly** — an underscore-prefixed "private" function in another module. Promote it to a proper shared helper: move `_expand_occurrences` (and the `_week_matches` helper it uses) out of `tools.py` into `scheduler.py` alongside `compute_week_number`, drop the leading underscore, and have both `tools.py` and `commands.py` import it from there. `AnchorNotSetError` and `ANCHOR_NOT_SET_MESSAGE` move with it.
2. **Redundant anchor fetch** — `build_today_view`/`build_week_view` in `commands.py` and the (now-relocated) occurrence-expansion helper each independently call `get_semester_anchor`. Fetch it once per view function and pass the resolved `anchor_date` down instead of re-querying.

Small enough that if your plan finds a cleaner shape for either of these, go with it — the point is removing the layering crack and the duplicate query, not matching my wording exactly.

## Context for the import feature

The registration PDFs NTU issues (like the one I sent you) use Type 3 embedded fonts with custom encoding — `pdftotext`-style extraction comes out as garbage on these (I hit this myself checking the file: readable when rasterized and viewed, unreadable as extracted text). So this import has to rasterize each page to an image and read it visually via a Claude vision call, not parse the text layer.

Two sections per PDF matter: the **registered/waitlist course list** (code, title, index no., AU, status) and the **weekly timetable grid** (day × time cells containing class type, module, location, and a week-range label like `Wk2-13`, `Wk1,3,5,7,9,11,13`, or no label at all meaning every week).

## 1. Dependencies

Add `pdf2image` to `requirements.txt` (wraps `poppler-utils` for rasterizing PDF pages to images — cleaner than shelling out to `pdftoppm` directly). Note in your plan that the deployment environment needs `poppler-utils` installed at the OS level (`apt install poppler-utils` on Debian/Ubuntu) — `pdf2image` alone won't work without it. Flag this as a setup step for me, don't try to install system packages yourself.

## 2. Telegram document handler (`bot.py`)

- New `MessageHandler(filters.Document.PDF, pdf_import.handle_pdf_upload)`, registered before the catch-all text handler.
- Reject anything over a reasonable size cap (e.g. 15 MB) with a friendly message instead of attempting to process it.
- Download to a temp path via PTB's `File.download_to_drive` (use a per-upload temp dir, clean it up after processing — don't leave uploaded PDFs sitting on disk).

## 3. Parsing pipeline (new `pdf_import.py`)

- Rasterize each page via `pdf2image.convert_from_path` at ~150 DPI (matches what worked when I checked your file). Cap processing at the first 5 pages — warn the user if the PDF has more and only the first 5 were read.
- For each page image, make a **separate, one-shot Anthropic API call** (its own small client call, not routed through `brain.py`'s tool-use loop — this is a fixed extraction task, not a conversation) with the image as base64 input and a system prompt instructing Claude to respond with **JSON only**, extracting:
  - `courses`: `course_code`, `title`, `index_no`, `au`, `status` (`Registered`/`Waitlist`)
  - `schedule_entries`: `course_code`, `class_type` (map `LEC/STU`→`Lecture`, `TUT`→`Tutorial`, `LAB`→`Lab`, `SEM`→`Seminar`, `DES`→`Design`, `PRJ`→`Project`, using the legend on the PDF itself where present), `day_of_week` (MON–SUN), `start_time`, `end_time` (24-hour `HH:MM`), `location`, `week_label_raw` (the literal printed string, e.g. `"Wk2-13"`, `"Wk1,3,5,7,9,11,13"`, or `null` if no week label appears in the cell).
- **Do the week-label normalization in Python, not in the LLM call.** The vision call's only job is faithful transcription of what's printed — turning `"Wk2-13"` into the correct `week_pattern` string is arithmetic Kangani's own code should own, not something to trust a vision model to get right silently. Write `normalize_week_label(raw: str | None) -> str`:
  - `None` → `'every'`
  - a single number (`"Wk1"`) → that number as a string (`'1'`)
  - a comma list (`"Wk1,3,5,7,9,11,13"`) → passed through as the comma list (already valid `week_pattern` format)
  - a range (`"Wk2-13"`) → expand to the full comma-separated list `'2,3,4,5,6,7,8,9,10,11,12,13'`
  - anything unparseable → raise, and have the caller flag that one entry as needing manual review rather than guessing
- Merge results across pages into one extraction result: `{courses: [...], schedule_entries: [...]}`. A single time cell can hold multiple entries (I saw this in your PDF — a lecture and a lab stacked in the same slot); each becomes its own `schedule_entries` row, not merged.

## 4. Confirm-before-commit flow

Don't write anything to the database until the user confirms — a misread here writes wrong class times, which is worse than the current manual-entry status quo.

- After parsing, render a preview message: counts (`N courses found — X registered, Y waitlisted`, `Z weekly class blocks found`), then a compact list of the schedule entries grouped by day (reuse the day-header + indented-row style `build_week_view` already established, for visual consistency).
- Stash the parsed result in `context.chat_data['pending_pdf_import']` (same pattern as the existing `pending_nav_notes` stash in `bot.py`) keyed by a short generated id, so confirming doesn't require re-parsing the PDF.
- Inline keyboard: `✅ Import` / `❌ Cancel` → new `callbacks.py` handler `pdf_import_callback` (`pattern=r"^pdfimport:"`), registered in `bot.py`.
  - **Confirm**: for each course, `get_or_create_module`; for each schedule entry, `create_schedule_block` with the normalized `week_pattern`. Before inserting, check for an existing block with the same `chat_id`/`module_id`/`day_of_week`/`start_time`/`end_time` and skip it (report as "already existed, skipped") rather than creating a duplicate — re-running an import on the same PDF (or a corrected re-upload) shouldn't double every class. Report a final summary: created / skipped / failed, with any failed entries named explicitly so nothing silently vanishes.
  - **Cancel**: discard the stashed data, confirm cancellation, write nothing.
- If the extraction comes back with zero schedule entries (parsing failed entirely), say so plainly and suggest manual entry via the existing conversational path — don't show an empty confirm screen.

## Not in scope for this prompt

- Exam schedule / exam-date import (the PDF's `@Exam Schedule` column) — a plausible future extension (likely as one-off `specific_date` schedule blocks or reminders), but a separate prompt.
- Auto-creating tasks or notes from the registration status (e.g. a task per waitlisted course) — the registered/waitlist counts show up in the confirmation preview only for now; persisting them further is future scope.
- The topic-tree revamp — this still tags schedule blocks to `module_name` via the current `modules` table, not a `topic_id`. If the revamp lands first, this prompt's module-tagging step needs to be adapted to it; flag that dependency in your plan rather than silently picking one.
- Daily digest, reactive judgment, companion voice — untouched, as always.