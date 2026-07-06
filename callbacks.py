"""CallbackQueryHandler dispatchers for Kangani's inline keyboards.

Each function parses a compact "<namespace>:<action>[:<id>[:<extra>]]"
callback_data string and calls database.py (and scheduler.py, for
reminders) directly -- never through tools.py, which is shaped for the
Claude tool-use protocol, not Telegram message editing.
"""

from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.ext import ContextTypes

import commands
import database
import keyboards
import scheduler


def _queue_nav_note(context: ContextTypes.DEFAULT_TYPE, note: str) -> None:
    context.chat_data.setdefault("pending_nav_notes", []).append(note)


async def task_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id

    try:
        parts = query.data.split(":")
        action = parts[1]
        task_id = int(parts[2])

        if action == "complete":
            task = database.update_task_status(
                chat_id, task_id, status="done", progress_pct=100
            )
            if task is not None:
                _queue_nav_note(
                    context, f"User marked task #{task_id} ('{task['title']}') as done via a button tap."
                )
            text, kb = commands.build_tasks_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb)

        elif action == "menu":
            task = database.get_task(chat_id, task_id)
            if task is None:
                await query.edit_message_text("That task no longer exists.")
                return
            await query.edit_message_text(
                f"Editing task #{task_id}: {task['title']}\nCurrent status: {task['status']}",
                reply_markup=keyboards.task_edit_menu_keyboard(task_id),
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
            text, kb = commands.build_tasks_view(chat_id)
            await query.edit_message_text(text, reply_markup=kb)

        elif action == "back":
            text, kb = commands.build_tasks_view(chat_id)
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
