"""PDF schedule import: rasterize an NTU registration PDF and read it with a
Claude vision call, then let the user confirm before anything is written.

NTU's registration PDFs embed Type-3 fonts with custom encoding, so text
extraction comes out as garbage -- the pages are only legible when rasterized
and viewed. So this pipeline renders each page to an image and asks Claude to
transcribe it into structured JSON. The vision call's ONLY job is faithful
transcription; the week-range arithmetic (e.g. "Wk2-13" -> a week_pattern) is
done here in Python, not trusted to the model.
"""

import asyncio
import base64
import html
import io
import json
import logging
import os
import re
import secrets
import shutil
import tempfile

from telegram import Update
from telegram.ext import ContextTypes

import brain
import keyboards

logger = logging.getLogger(__name__)

MAX_PDF_BYTES = 15 * 1024 * 1024
MAX_PAGES = 5
DPI = 150
EXTRACTION_MODEL = "claude-sonnet-5"

# NTU timetable class-type codes -> the class_type labels Kangani stores. The
# vision prompt is told to use the PDF's own legend where present and fall back
# to this map otherwise. normalize_class_type() also applies this map as a
# Python-side safety net (see there) -- LEC/STU is the literal compound code
# NTU prints (not two separate LEC and STU entries), so it needs its own key
# rather than relying on the model to split it.
CLASS_TYPE_MAP = {
    "LEC": "Lecture",
    "STU": "Lecture",   # "studio" lecture-style slot
    "LEC/STU": "Lecture",
    "TUT": "Tutorial",
    "LAB": "Lab",
    "SEM": "Seminar",
    "DES": "Design",
    "PRJ": "Project",
}

_DAY_ORDER = ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
_DAY_NAMES = {
    "MON": "Monday", "TUE": "Tuesday", "WED": "Wednesday", "THU": "Thursday",
    "FRI": "Friday", "SAT": "Saturday", "SUN": "Sunday",
}

EXTRACTION_SYSTEM_PROMPT = (
    "You are a precise document transcriber. You are given ONE page image from "
    "an NTU course registration PDF. Transcribe exactly what is printed -- do "
    "not infer, compute, or normalize anything.\n\n"
    "Respond with ONLY a single JSON object (no prose, no markdown fences) with "
    "this shape:\n"
    "{\n"
    '  "courses": [\n'
    '    {"course_code": str, "title": str, "index_no": str, "au": str, '
    '"status": "Registered"|"Waitlist"}\n'
    "  ],\n"
    '  "schedule_entries": [\n'
    '    {"course_code": str, "class_type": str, "day_of_week": '
    '"MON"|"TUE"|"WED"|"THU"|"FRI"|"SAT"|"SUN", "start_time": "HH:MM", '
    '"end_time": "HH:MM", "location": str|null, "week_label_raw": str|null}\n'
    "  ]\n"
    "}\n\n"
    "Rules:\n"
    "- If the page has no course list, return an empty \"courses\" array; if it "
    "has no timetable, return an empty \"schedule_entries\" array.\n"
    "- class_type: map the printed code to a friendly label using the legend on "
    "the page if present, otherwise: LEC/STU->Lecture, TUT->Tutorial, "
    "LAB->Lab, SEM->Seminar, DES->Design, PRJ->Project.\n"
    "- start_time/end_time: 24-hour HH:MM.\n"
    "- week_label_raw: copy the week-range text printed in the cell VERBATIM "
    "(e.g. \"Wk2-13\", \"Wk1,3,5,7,9,11,13\"). Use null if the cell shows no "
    "week label at all. Do NOT expand or interpret it.\n"
    "- A single time cell may stack multiple classes (e.g. a lecture and a lab) "
    "-- emit one schedule_entries object per class, never merged."
)


# --- week-label normalization (owned by Python, not the vision model) ------

def normalize_week_label(raw: str | None) -> str:
    """Turn a printed week label into a valid schedule_block week_pattern.

    None/blank -> 'every'; a single number ("Wk1") -> '1'; a comma list
    ("Wk1,3,5") -> '1,3,5'; a range ("Wk2-13") -> '2,3,...,13'. Anything else
    raises ValueError so the caller can flag that entry for manual review
    rather than guessing.
    """
    if raw is None:
        return "every"
    s = raw.strip()
    if not s:
        return "every"
    # Drop a leading Wk / Week / Weeks prefix (case-insensitive).
    body = re.sub(r"(?i)^w(ee)?ks?\s*", "", s).strip()

    if re.fullmatch(r"\d+", body):
        return str(int(body))
    if re.fullmatch(r"\d+(\s*,\s*\d+)+", body):
        return ",".join(str(int(x)) for x in body.split(","))
    rng = re.fullmatch(r"(\d+)\s*-\s*(\d+)", body)
    if rng:
        lo, hi = int(rng.group(1)), int(rng.group(2))
        if lo <= hi:
            return ",".join(str(i) for i in range(lo, hi + 1))
    raise ValueError(f"unparseable week label: {raw!r}")


# --- rasterize + per-page vision extraction --------------------------------

def _rasterize(path: str) -> tuple[list, int]:
    """Render the first MAX_PAGES of the PDF to PIL images; also return the
    PDF's true total page count so the caller can warn about truncation.
    Imported lazily so the rest of this module (and importing bot.py) doesn't
    hard-require poppler just to be loaded."""
    from pdf2image import convert_from_path, pdfinfo_from_path

    total_pages = int(pdfinfo_from_path(path).get("Pages", 0) or 0)
    images = convert_from_path(path, dpi=DPI, first_page=1, last_page=MAX_PAGES)
    return images, total_pages


def _encode_image(image) -> tuple[str, str]:
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85)
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()


def _strip_to_json(text: str) -> str:
    """Slice out the JSON object, tolerating stray prose or ``` fences around
    it by taking everything between the first '{' and the last '}'."""
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return text[start:end + 1]
    return text


EXTRACTION_TOOL = {
    "name": "record_extraction",
    "description": "Record the courses and schedule entries transcribed from this page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "courses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "course_code": {"type": "string"},
                        "title": {"type": "string"},
                        "index_no": {"type": "string"},
                        "au": {"type": "string"},
                        "status": {"type": "string", "enum": ["Registered", "Waitlist"]},
                    },
                    "required": ["course_code", "title", "status"],
                },
            },
            "schedule_entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "course_code": {"type": "string"},
                        "class_type": {"type": "string"},
                        "day_of_week": {
                            "type": "string",
                            "enum": ["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"],
                        },
                        "start_time": {"type": "string"},
                        "end_time": {"type": "string"},
                        "location": {"type": ["string", "null"]},
                        "week_label_raw": {"type": ["string", "null"]},
                    },
                    "required": ["course_code", "class_type", "day_of_week", "start_time", "end_time"],
                },
            },
        },
        "required": ["courses", "schedule_entries"],
    },
}


async def _extract_page(image) -> dict:
    """One-shot vision call for a single page image -> {courses, schedule_entries}.
    Forces the response through tool-use rather than freeform JSON-as-text --
    the API validates the shape itself, so there's no prose/fence to strip and
    no hand-parsing to break. A page that still fails is tolerated (logged,
    returns empty lists) so one bad page doesn't sink the whole import."""
    media_type, b64 = _encode_image(image)
    try:
        resp = await brain.get_client().messages.create(
            model=EXTRACTION_MODEL,
            max_tokens=8192,
            system=EXTRACTION_SYSTEM_PROMPT,
            tools=[EXTRACTION_TOOL],
            tool_choice={"type": "tool", "name": "record_extraction"},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64},
                        },
                        {"type": "text", "text": "Extract this page now."},
                    ],
                }
            ],
        )
        if resp.stop_reason == "max_tokens":
            logger.warning("Extraction hit max_tokens -- page likely truncated")
        tool_use = next((b for b in resp.content if b.type == "tool_use"), None)
        if tool_use is None:
            logger.error("No tool_use block in extraction response: %r", resp.content)
            return {"courses": [], "schedule_entries": []}
        data = tool_use.input
    except Exception:
        logger.exception("Vision extraction failed for a page")
        return {"courses": [], "schedule_entries": []}
    return {
        "courses": data.get("courses") or [],
        "schedule_entries": data.get("schedule_entries") or [],
    }


def normalize_class_type(raw: str | None) -> str | None:
    """Normalize a printed class-type code to Kangani's stored label, the same
    way normalize_week_label handles week ranges: deterministic, in Python,
    not fully trusted to the vision model.

    Unlike week labels, this does NOT raise on an unrecognized value -- a
    user's timetable can genuinely have a class type outside NTU's standard
    codes (SEM, DES, PRJ...), and rejecting those would flag every one of
    them for manual review for no reason. So: known codes (case/slash/space
    insensitive, e.g. "lec", "LEC/STU", "Lec / Stu") get mapped to their
    canonical label via CLASS_TYPE_MAP; anything else passes through
    unchanged (the vision model's own transcription, trusted as a fallback
    rather than discarded).
    """
    if not raw:
        return raw
    # "LEC/STU" or "Lec / Stu" -> "LEC/STU"; strips spaces around the slash
    # and any internal whitespace, then matches CLASS_TYPE_MAP case-insensitively.
    key = re.sub(r"\s*/\s*", "/", raw.strip()).upper()
    key = re.sub(r"\s+", "", key)
    return CLASS_TYPE_MAP.get(key, raw.strip())


def _merge_pages(pages: list[dict]) -> dict:
    """Concatenate courses + schedule_entries across pages and resolve each
    entry's week_pattern. Entries whose printed label can't be parsed are marked
    needs_review (never silently guessed)."""
    courses: list[dict] = []
    entries: list[dict] = []
    for page in pages:
        courses.extend(page.get("courses") or [])
        entries.extend(page.get("schedule_entries") or [])
    for e in entries:
        try:
            e["week_pattern"] = normalize_week_label(e.get("week_label_raw"))
        except ValueError:
            e["needs_review"] = True
        e["class_type"] = normalize_class_type(e.get("class_type"))
    return {"courses": courses, "schedule_entries": entries}


# --- preview rendering (mirrors build_week_view's day/row style) -----------

def _entry_row(e: dict) -> str:
    title_bits = [b for b in (e.get("course_code"), e.get("class_type")) if b]
    title = " ".join(title_bits) or "Class"
    row = f"{e.get('start_time', '?')}-{e.get('end_time', '?')} {title}"
    if e.get("location"):
        row += f" - {e['location']}"
    if e.get("needs_review"):
        row += f"  ⚠ needs review: {e.get('week_label_raw')}"
    elif e.get("week_pattern") and e["week_pattern"] != "every":
        row += f"  (weeks {e['week_pattern']})"
    return html.escape(row)


def _status_is(course: dict, prefix: str) -> bool:
    return (course.get("status") or "").strip().lower().startswith(prefix)


def build_import_preview(result: dict) -> str:
    courses = result["courses"]
    entries = result["schedule_entries"]
    registered = sum(1 for c in courses if _status_is(c, "reg"))
    waitlisted = sum(1 for c in courses if _status_is(c, "wait"))

    lines = [
        f"{len(courses)} courses found — {registered} registered, {waitlisted} waitlisted",
        f"{len(entries)} weekly class blocks found",
        "",
    ]

    by_day: dict[str, list[dict]] = {}
    for e in entries:
        by_day.setdefault(e.get("day_of_week"), []).append(e)

    for code in _DAY_ORDER:
        day_entries = by_day.get(code)
        if not day_entries:
            continue
        lines.append(_DAY_NAMES[code])
        for e in sorted(day_entries, key=lambda x: x.get("start_time") or ""):
            lines.append(f"  {_entry_row(e)}")

    # Entries with a missing/unrecognized day still need to be visible so the
    # user notices the misread rather than it vanishing.
    other = [e for e in entries if e.get("day_of_week") not in _DAY_ORDER]
    if other:
        lines.append("Unrecognized day")
        for e in other:
            lines.append(f"  {_entry_row(e)}")

    return f"<pre>{chr(10).join(lines)}</pre>"


# --- Telegram entry point --------------------------------------------------

async def handle_pdf_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    doc = update.message.document
    if doc.file_size and doc.file_size > MAX_PDF_BYTES:
        await update.message.reply_text(
            "That PDF is larger than 15 MB — too big for me to process. "
            "Try exporting a smaller file."
        )
        return

    tmpdir = tempfile.mkdtemp(prefix="kangani_pdf_")
    try:
        path = os.path.join(tmpdir, "upload.pdf")
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(path)

        try:
            images, total_pages = await asyncio.to_thread(_rasterize, path)
        except Exception:
            logger.exception("Rasterize failed")
            await update.message.reply_text(
                "I couldn't read that PDF — it may be corrupted, password-"
                "protected, or not a real PDF."
            )
            return

        await update.message.reply_text("Reading your schedule PDF… this can take a moment.")
        pages = [await _extract_page(img) for img in images]
        result = _merge_pages(pages)

        if not result["schedule_entries"]:
            await update.message.reply_text(
                "I couldn't find any timetable entries in that PDF. You can add "
                "classes by just telling me about them — e.g. \"Algorithms "
                "lecture Mondays 9–11am at LT1\"."
            )
            return

        import_id = secrets.token_hex(4)
        context.chat_data.setdefault("pending_pdf_import", {})[import_id] = result

        note = ""
        if total_pages > MAX_PAGES:
            note = (
                f"\n\n(Note: the PDF had {total_pages} pages; I only read the "
                f"first {MAX_PAGES}.)"
            )
        await update.message.reply_text(
            build_import_preview(result) + note,
            parse_mode="HTML",
            reply_markup=keyboards.pdf_import_confirm_keyboard(import_id),
        )
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)