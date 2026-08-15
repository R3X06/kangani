"""CallbackQueryHandler dispatchers for Kangani's inline keyboards.

Each function parses a compact "<namespace>:<action>[:<id>[:<extra>]]"
callback_data string and calls database.py (and scheduler.py, for
reminders) directly -- never through tools.py, which is shaped for the
Claude tool-use protocol, not Telegram message editing.
"""

import logging
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

import commands
import database
import keyboards
import pdf_import
import scheduler

logger = logging.getLogger(__name__)


def _queue_nav_note(context: ContextTypes.DEFAULT_TYPE, note: str) -> None:
    context.chat_data.setdefault("pending_nav_notes", []).append(note)


async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        # Strip a trailing ":top:{id}" origin suffix unconditionally, BEFORE
        # any action-specific parsing. A fixed-index check (e.g. parts[3] ==
        # "top") breaks for "status", whose own code already occupies index
        # 3 (task:status:5:ns vs. task:status:5:ns:top:12) -- stripping from
        # the end works regardless of how many parts the action itself uses.
        origin_topic_id = None
        if len(parts) >= 2 and parts[-2] == "top":
            origin_topic_id = int(parts[-1])
            parts = parts[:-2]

        action = parts[1]
        task_id = int(parts[2])

        def _render_list():
            if origin_topic_id is not None:
                return commands.build_events_detail_view(chat_id, origin_topic_id)
            return commands.build_tasks_view(chat_id)

        if action == "complete":
            task = database.update_task_status(
                chat_id, task_id, status="done", progress_pct=100
            )
            if task is not None:
                _queue_nav_note(
                    context, f"User marked task #{task_id} ('{task['title']}') as done via a button tap."
                )
            text, kb = _render_list()
            await query.edit_message_text(text, reply_markup=kb)

        elif action == "menu":
            task = database.get_task(chat_id, task_id)
            if task is None:
                await query.edit_message_text("That task no longer exists.")
                await query.answer()
                return
            await query.edit_message_text(
                f"Editing task #{task_id}: {task['title']}\nCurrent status: {task['status']}",
                reply_markup=keyboards.task_edit_menu_keyboard(
                    task_id, origin_topic_id=origin_topic_id
                ),
            )

        elif action == "status":
            code = parts[3]
            status = keyboards.STATUS_CODES[code]
            progress_pct = 100 if status == "done" else None
            task = database.update_task_status(
                chat_id, task_id, status=status, progress_pct=progress_pct
            )
            if task is not None:
                _queue_nav_note(
                    context,
                    f"User changed task #{task_id} ('{task['title']}') status to "
                    f"'{status}' via a button tap.",
                )
            text, kb = _render_list()
            await query.edit_message_text(text, reply_markup=kb)

        elif action == "back":
            text, kb = _render_list()
            await query.edit_message_text(text, reply_markup=kb)

        await query.answer()
    except (IndexError, ValueError, KeyError):
        await query.answer("Something went wrong with that button.", show_alert=True)


async def reminder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        action = parts[1]
        reminder_id = int(parts[2])

        if action == "done":
            await query.edit_message_reply_markup(reply_markup=None)

        elif action == "snooze":
            code = parts[3]
            _, seconds = keyboards.SNOOZE_DURATIONS[code]
            reminder = database.get_reminder(chat_id, reminder_id)
            if reminder is None:
                await query.edit_message_text("That reminder no longer exists.")
                await query.answer()
                return

            new_trigger_dt = datetime.now(timezone.utc) + timedelta(seconds=seconds)
            new_trigger_str = scheduler.format_utc_iso(new_trigger_dt)
            new_reminder = database.create_reminder(
                chat_id=chat_id,
                trigger_datetime_utc=new_trigger_str,
                message=reminder["message"],
                linked_task_id=reminder["linked_task_id"],
                linked_topic_id=reminder["linked_topic_id"],
            )
            scheduler.schedule_reminder(
                context.job_queue,
                new_reminder["id"],
                chat_id,
                new_trigger_dt,
                reminder["message"],
            )
            _queue_nav_note(
                context,
                f"User snoozed a reminder ('{reminder['message']}') by {code} via a button tap.",
            )
            duration_label = keyboards.SNOOZE_DURATIONS[code][0]
            await query.edit_message_text(
                f"{query.message.text}\n\n(snoozed {duration_label})", reply_markup=None
            )

        elif action == "cancel":
            reminder = database.cancel_reminder(chat_id, reminder_id)
            if reminder is not None:
                for job in context.job_queue.get_jobs_by_name(f"reminder-{reminder_id}"):
                    job.schedule_removal()
                _queue_nav_note(
                    context,
                    f"User cancelled a reminder ('{reminder['message']}') via a button tap.",
                )
            text, kb = commands.build_reminders_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb)

        await query.answer()
    except (IndexError, ValueError, KeyError):
        await query.answer("Something went wrong with that button.", show_alert=True)


async def topic_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        action = parts[1]

        if action == "root":
            text, kb = commands.build_topics_root_view(chat_id)
        elif action in ("mod", "open"):
            # 'mod' kept as an alias so any stale in-chat buttons still resolve.
            text, kb = commands.build_topics_detail_view(chat_id, int(parts[2]))
        elif action == "notes":
            text, kb = commands.build_topics_notes_view(chat_id, int(parts[2]))
        else:
            await query.answer()
            return

        await query.edit_message_text(text, reply_markup=kb)
        await query.answer()
    except (IndexError, ValueError, KeyError):
        await query.answer("Something went wrong with that button.", show_alert=True)


def _entry_label(e: dict) -> str:
    bits = [
        e.get("course_code") or "?",
        e.get("class_type") or "",
        e.get("day_of_week") or "",
        e.get("start_time") or "",
    ]
    return " ".join(b for b in bits if b).strip()


async def pdf_import_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        action = parts[1]
        import_id = parts[2]

        # Only confirm/cancel consume the stash (pop) -- every other action
        # (list/edit/field/delete/rootpick) is a mid-flow navigation step that
        # must leave the pending import in place for the NEXT tap.
        if action in ("confirm", "cancel"):
            result = context.chat_data.get("pending_pdf_import", {}).pop(import_id, None)
        else:
            result = context.chat_data.get("pending_pdf_import", {}).get(import_id)

        if result is None:
            await query.edit_message_text(
                "This import has expired or was already handled. Re-upload the PDF to try again."
            )
            await query.answer()
            return

        if action == "cancel":
            context.chat_data.pop("pdf_import_awaiting", None)
            await query.edit_message_text("Import cancelled — nothing was saved.")
            await query.answer()
            return

        if action == "rootpick":
            topic_id = int(parts[3])
            topic = database.get_topic(chat_id, topic_id)
            if topic is None:
                await query.edit_message_text("That topic no longer exists.")
                await query.answer()
                return
            topics = database.list_topics(chat_id)
            path = next((t["path"] for t in topics if t["id"] == topic_id), topic["name"])
            result["target_parent_topic_id"] = topic_id
            result["target_parent_path"] = path
            context.chat_data.pop("pdf_import_awaiting", None)
            note = result.pop("_page_note", "")
            await query.edit_message_text(
                pdf_import.build_import_preview(result) + note,
                parse_mode="HTML",
                reply_markup=keyboards.pdf_import_entry_list_keyboard(import_id, result["schedule_entries"]),
            )
            await query.answer()
            return

        if action == "list":
            note = result.pop("_page_note", "")
            await query.edit_message_text(
                pdf_import.build_import_preview(result) + note,
                parse_mode="HTML",
                reply_markup=keyboards.pdf_import_entry_list_keyboard(import_id, result["schedule_entries"]),
            )
            await query.answer()
            return

        if action == "edit":
            entry_idx = int(parts[3])
            entry = next((e for e in result["schedule_entries"] if e.get("_idx") == entry_idx), None)
            if entry is None:
                await query.edit_message_text("That entry no longer exists.")
                await query.answer()
                return
            await query.edit_message_text(
                f"Editing entry #{entry_idx}: {pdf_import._entry_row(entry)}\n\nChoose a field to edit:",
                parse_mode="HTML",
                reply_markup=keyboards.pdf_import_entry_edit_keyboard(import_id, entry_idx),
            )
            await query.answer()
            return

        if action == "field":
            entry_idx = int(parts[3])
            field = parts[4]
            label, _ = pdf_import.EDITABLE_FIELDS[field]
            context.chat_data["pdf_import_awaiting"] = {
                "kind": "field", "import_id": import_id,
                "entry_idx": entry_idx, "field": field,
            }
            await query.answer()
            await query.message.reply_text(f"Reply with the new {label.lower()}.")
            return

        if action == "delete":
            entry_idx = int(parts[3])
            result["schedule_entries"] = [
                e for e in result["schedule_entries"] if e.get("_idx") != entry_idx
            ]
            pdf_import._renumber(result["schedule_entries"])
            note = result.pop("_page_note", "")
            await query.edit_message_text(
                pdf_import.build_import_preview(result) + note,
                parse_mode="HTML",
                reply_markup=keyboards.pdf_import_entry_list_keyboard(import_id, result["schedule_entries"]),
            )
            await query.answer()
            return

        # action == "confirm"
        title_by_code = {
            c.get("course_code"): c.get("title")
            for c in result["courses"]
            if c.get("course_code")
        }
        target_parent_topic_id = result.get("target_parent_topic_id")
        created = 0
        skipped = 0
        failed: list[str] = []

        for e in result["schedule_entries"]:
            label = _entry_label(e)
            if e.get("needs_review"):
                failed.append(
                    f"{label} — unreadable week label '{e.get('week_label_raw')}'"
                )
                continue
            try:
                code = e.get("course_code")
                if not code:
                    failed.append(f"{label} — no course code")
                    continue
                # Tree-wide, not root-scoped: once a module is filed under a
                # semester topic, get_or_create_topic(parent=None) would miss it
                # and fork a second module topic -- whose new id then also fails
                # find_matching_schedule_block below, duplicating every class on
                # a re-import. target_parent_topic_id only affects a GENUINELY
                # NEW module (see resolve_module_topic's docstring) -- one that
                # already exists elsewhere in the tree is reused as-is, never
                # moved by an import.
                module = database.resolve_module_topic(
                    chat_id, code, full_name=title_by_code.get(code),
                    parent_topic_id=target_parent_topic_id,
                )
                if database.find_matching_schedule_block(
                    chat_id,
                    module["id"],
                    e.get("day_of_week"),
                    e.get("start_time"),
                    e.get("end_time"),
                    e.get("class_type"),
                ):
                    skipped += 1
                    continue
                database.create_schedule_block(
                    chat_id=chat_id,
                    start_time=e.get("start_time"),
                    end_time=e.get("end_time"),
                    day_of_week=e.get("day_of_week"),
                    module_name=code,
                    class_type=e.get("class_type"),
                    location=e.get("location"),
                    week_pattern=e.get("week_pattern", "every"),
                )
                created += 1
            except Exception as exc:
                logger.exception("Failed to import a schedule entry")
                failed.append(f"{label} — {exc}")

        summary = [
            f"Import complete: {created} created, {skipped} skipped (already existed)."
        ]
        if failed:
            summary.append("")
            summary.append(f"{len(failed)} could not be imported:")
            summary.extend(f"• {f}" for f in failed)
        # Proactive, not reactive: a week-numbered pattern (Wk2-13, Wk4,5...)
        # is meaningless until the semester start date is set -- otherwise
        # this only ever surfaces later as an AnchorNotSetError when the user
        # happens to ask for a specific week, with no link back to "this is
        # why your imported classes aren't showing up."
        has_week_numbered = any(
            (e.get("week_pattern") or "every") != "every"
            for e in result["schedule_entries"]
        )
        if has_week_numbered and database.get_semester_anchor(chat_id) is None:
            summary.append("")
            summary.append(
                "Some of these classes only run in specific weeks (e.g. Wk2-13) "
                "-- tell me which date week 1 starts so I can work out real "
                "calendar dates for them, e.g. \"week 1 starts 12 August\"."
            )
        await query.edit_message_text("\n".join(summary))
        _queue_nav_note(
            context,
            f"User imported a schedule PDF: {created} classes created, "
            f"{skipped} skipped, {len(failed)} failed.",
        )
        await query.answer()
    except (IndexError, ValueError, KeyError):
        await query.answer("Something went wrong with that button.", show_alert=True)


async def event_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        action = parts[1]

        if action == "root":
            text, kb = commands.build_events_root_view(chat_id)
        elif action == "open":
            text, kb = commands.build_events_detail_view(chat_id, int(parts[2]))
        else:
            await query.answer()
            return

        await query.edit_message_text(text, reply_markup=kb)
        await query.answer()
    except (IndexError, ValueError, KeyError):
        await query.answer("Something went wrong with that button.", show_alert=True)