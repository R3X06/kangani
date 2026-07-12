"""Assembles the Jinja render context for each timetable image.

Every builder reuses the same data sources the text views (`/today`, `/week`)
already use -- `database.list_schedule_blocks` + `scheduler.expand_occurrences`
for classes, `database.query_tasks` for deadlines, and
`database.list_pending_reminders` for reminders -- so an image and its text
equivalent never disagree. Each returns a plain dict ready to hand straight to
`template.render(**context)`.

A module's marker/strip/dot color comes from its stored `modules.color`
(the shared MODULE_COLOR_PALETTE); `list_schedule_blocks`/`query_tasks` only
carry `module_name`, so we build a name->color map once per call.
"""

import calendar
import os
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import database
import scheduler

# Marker cap per monthly cell before a quiet "+N" overflow indicator. The
# approved mockup never needed more than 4 in one cell, and 4 is where the
# cell's own layout starts to crowd, so that's the cap.
_MARKER_CAP = 4

# Neutral fallback (--ink-soft) for a block with no module or an uncolored one.
_FALLBACK_COLOR = "#6B5D4C"


def _tz() -> ZoneInfo:
    return ZoneInfo(os.environ.get("TIMEZONE", "UTC"))


def _anchor_date(chat_id: int) -> date | None:
    anchor = database.get_semester_anchor(chat_id)
    return date.fromisoformat(anchor) if anchor else None


def _color_map(chat_id: int) -> dict[str, str]:
    return {m["name"]: m["color"] for m in database.list_modules(chat_id)}


def _color(color_map: dict[str, str], module_name: str | None) -> str:
    return color_map.get(module_name) or _FALLBACK_COLOR


def _title(occ: dict) -> str:
    bits = [b for b in (occ.get("module_name"), occ.get("class_type")) if b]
    return " ".join(bits) or "Block"


def _time_range(occ: dict) -> str:
    return f"{occ['start_time']}–{occ['end_time']}"  # en dash


def _marker_shape(class_type: str | None) -> str:
    """Marker shape by class type: Lecture->filled circle, Tutorial->dash,
    Lab->asterisk, everything else (Seminar/Design/Project/unset)->diamond."""
    ct = (class_type or "").strip().lower()
    if ct == "lecture":
        return "circle"
    if ct == "tutorial":
        return "dash"
    if ct == "lab":
        return "asterisk"
    return "diamond"


def _day_bounds_utc(tz: ZoneInfo, day_from: date, day_to: date) -> tuple[str, str]:
    """UTC ISO bounds spanning [day_from 00:00 .. day_to 23:59:59] local --
    the same deadline-window trick `/today` uses to select tasks by local day."""
    start = scheduler.format_utc_iso(datetime.combine(day_from, time.min, tzinfo=tz))
    end = scheduler.format_utc_iso(datetime.combine(day_to, time.max, tzinfo=tz))
    return start, end


def build_daily_context(chat_id: int, target_date: date) -> dict:
    tz = _tz()
    iso = target_date.isoformat()
    anchor_date = _anchor_date(chat_id)
    color_map = _color_map(chat_id)

    blocks = database.list_schedule_blocks(chat_id=chat_id, date_from=iso, date_to=iso)
    occ = scheduler.expand_occurrences(blocks, iso, iso, anchor_date)
    slots = [
        {
            "start_time": o["start_time"],
            "title": _title(o),
            "location": o.get("location"),
            "time_range": _time_range(o),
            "color": _color(color_map, o.get("module_name")),
        }
        for o in occ
    ]

    wk = (
        scheduler.compute_week_number(anchor_date, target_date)
        if anchor_date
        else None
    )

    start_utc, end_utc = _day_bounds_utc(tz, target_date, target_date)
    tasks = database.query_tasks(
        chat_id=chat_id, deadline_from=start_utc, deadline_to=end_utc
    )
    due_tasks = [{"title": t["title"]} for t in tasks]

    reminders = []
    for r in database.list_pending_reminders(chat_id):
        local_dt = scheduler.parse_iso_datetime(r["trigger_data"]).astimezone(tz)
        if local_dt.date() == target_date:
            reminders.append((local_dt, r))
    reminders.sort(key=lambda pair: pair[0])
    reminder_ctx = [
        {"time": ldt.strftime("%H:%M"), "message": r["message"]}
        for ldt, r in reminders
    ]

    return {
        "day_name": target_date.strftime("%A"),
        "date_label": target_date.strftime("%d %B"),
        "week_number": wk,
        "slots": slots,
        "due_tasks": due_tasks,
        "reminders": reminder_ctx,
    }


def build_weekly_context(chat_id: int, week_number: int | None = None) -> dict:
    tz = _tz()
    today = datetime.now(tz).date()
    anchor_date = _anchor_date(chat_id)
    color_map = _color_map(chat_id)

    monday, sunday, wk_label = scheduler.resolve_week_range(
        anchor_date, today, week_number
    )

    occ = scheduler.expand_occurrences(
        database.list_schedule_blocks(
            chat_id=chat_id,
            date_from=monday.isoformat(),
            date_to=sunday.isoformat(),
        ),
        monday.isoformat(),
        sunday.isoformat(),
        anchor_date,
    )
    by_day: dict[str, list[dict]] = {}
    for o in occ:  # already sorted by (occurrence_date, start_time)
        by_day.setdefault(o["occurrence_date"], []).append(o)

    days = []
    for i in range(7):
        d = monday + timedelta(days=i)
        classes = [
            {
                "time_range": _time_range(o),
                "color": _color(color_map, o.get("module_name")),
                "title": _title(o),
                "location": o.get("location"),
            }
            for o in by_day.get(d.isoformat(), [])
        ]
        days.append(
            {
                "name": d.strftime("%A"),
                "datenum": d.day,
                "is_today": d == today,
                "classes": classes,
            }
        )

    range_label = f"{monday.strftime('%d %b')} – {sunday.strftime('%d %b')}"
    return {"week_label": wk_label, "range_label": range_label, "days": days}


def build_monthly_context(chat_id: int, year: int, month: int) -> dict:
    tz = _tz()
    today = datetime.now(tz).date()
    anchor_date = _anchor_date(chat_id)
    color_map = _color_map(chat_id)

    first = date(year, month, 1)
    last_dom = calendar.monthrange(year, month)[1]
    last = date(year, month, last_dom)

    occ = scheduler.expand_occurrences(
        database.list_schedule_blocks(
            chat_id=chat_id, date_from=first.isoformat(), date_to=last.isoformat()
        ),
        first.isoformat(),
        last.isoformat(),
        anchor_date,
    )
    marks_by_day: dict[str, list[dict]] = {}
    modules_seen: dict[str, str] = {}  # only modules that appear this month
    for o in occ:
        color = _color(color_map, o.get("module_name"))
        marks_by_day.setdefault(o["occurrence_date"], []).append(
            {"shape": _marker_shape(o.get("class_type")), "color": color}
        )
        name = o.get("module_name")
        if name and name not in modules_seen:
            modules_seen[name] = color

    start_utc, end_utc = _day_bounds_utc(tz, first, last)
    deadline_days: set[date] = set()
    for t in database.query_tasks(
        chat_id=chat_id, deadline_from=start_utc, deadline_to=end_utc, limit=1000
    ):
        if t["deadline"]:
            deadline_days.add(
                scheduler.parse_iso_datetime(t["deadline"]).astimezone(tz).date()
            )

    # Sunday-first grid: blank cells before the 1st so it lands in its weekday
    # column (Python weekday() is Mon=0..Sun=6; shift to Sun=0..Sat=6).
    lead = (first.weekday() + 1) % 7
    cells: list[dict] = [{"is_blank": True} for _ in range(lead)]
    for dnum in range(1, last_dom + 1):
        d = date(year, month, dnum)
        markers = marks_by_day.get(d.isoformat(), [])
        dow = (d.weekday() + 1) % 7
        cells.append(
            {
                "is_blank": False,
                "datenum": dnum,
                "is_weekend": dow in (0, 6),
                "is_today": d == today,
                "markers": markers[:_MARKER_CAP],
                "overflow": max(0, len(markers) - _MARKER_CAP),
                "has_deadline": d in deadline_days,
            }
        )
    while len(cells) % 7 != 0:  # trailing blanks to complete the last row
        cells.append({"is_blank": True})

    legend_modules = [
        {"name": name, "color": color}
        for name, color in sorted(modules_seen.items())
    ]

    return {
        "month_name": first.strftime("%B"),
        "year": year,
        "cells": cells,
        "legend_modules": legend_modules,
    }
