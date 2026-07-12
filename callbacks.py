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
import scheduler

logger = logging.getLogger(__name__)


def _queue_nav_note(context: ContextTypes.DEFAULT_TYPE, note: str) -> None:
    context.chat_data.setdefault("pending_nav_notes", []).append(note)


async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        # Strip a trailing ":evt:{id}" origin suffix unconditionally, BEFORE
        # any action-specific parsing. A fixed-index check (e.g. parts[3] ==
        # "evt") breaks for "status", whose own code already occupies index
        # 3 (task:status:5:ns vs. task:status:5:ns:evt:12) -- stripping from
        # the end works regardless of how many parts the action itself uses.
        origin_event_id = None
        if len(parts) >= 2 and parts[-2] == "evt":
            origin_event_id = int(parts[-1])
            parts = parts[:-2]

        action = parts[1]
        task_id = int(parts[2])

        def _render_list():
            if origin_event_id is not None:
                return commands.build_events_detail_view(chat_id, origin_event_id)
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
                    task_id, origin_event_id=origin_event_id
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
                linked_event_id=reminder["linked_event_id"],
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
        elif action == "mod":
            text, kb = commands.build_topics_module_view(chat_id, int(parts[2]))
        elif action == "open":
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

        # pop() -- confirming or cancelling consumes the stash so a double-tap
        # can't import twice.
        result = context.chat_data.get("pending_pdf_import", {}).pop(import_id, None)
        if result is None:
            await query.edit_message_text(
                "This import has expired or was already handled. Re-upload the PDF to try again."
            )
            await query.answer()
            return

        if action == "cancel":
            await query.edit_message_text("Import cancelled — nothing was saved.")
            await query.answer()
            return

        # action == "confirm"
        title_by_code = {
            c.get("course_code"): c.get("title")
            for c in result["courses"]
            if c.get("course_code")
        }
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
                module_name = (title_by_code.get(code) or code or "").strip()
                if not module_name:
                    failed.append(f"{label} — no course code")
                    continue
                module = database.get_or_create_module(chat_id, module_name)
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
                    module_name=module_name,
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
