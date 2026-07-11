"""Kangani -- Telegram entry point. Wires up handlers and starts polling."""

import logging
import os

from dotenv import load_dotenv
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import brain
import callbacks
import commands
import database
import keyboards
import scheduler

logging.basicConfig(
    format="%(asctime)s %(name)s %(levelname)s %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("menu", "Show the quick-access menu"),
    BotCommand("today", "Today's classes, tasks and reminders"),
    BotCommand("week", "This week's timetable"),
    BotCommand("tasks", "View and update your tasks"),
    BotCommand("reminders", "View and cancel upcoming reminders"),
    BotCommand("topics", "Browse your topics and notes"),
    BotCommand("notes", "View your recent notes"),
    BotCommand("events", "Browse your events"),
    BotCommand("help", "What Kangani can do"),
    BotCommand("settings", "View your current settings"),
]


async def post_init(application: Application) -> None:
    database.init_db()
    scheduler.reschedule_pending_reminders(application.job_queue)
    await application.bot.set_my_commands(BOT_COMMANDS)
    logger.info("Kangani initialized: database ready, reminders rescheduled.")


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi, I'm Kangani. What can I help you track?",
        reply_markup=keyboards.persistent_reply_keyboard(),
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_text = update.message.text
    history = context.chat_data.setdefault("history", [])

    pending_notes = context.chat_data.pop("pending_nav_notes", [])
    if pending_notes:
        prefix = "\n".join(f"[{note}]" for note in pending_notes)
        user_text = f"{prefix}\n{user_text}"

    try:
        reply_text = await brain.get_response(
            chat_id, user_text, history, context.job_queue
        )
    except Exception:
        logger.exception("brain.get_response failed for chat %s", chat_id)
        reply_text = "Sorry, something went wrong on my end -- try again in a moment."

    await update.message.reply_text(reply_text)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception", exc_info=context.error)


def main() -> None:
    load_dotenv()

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set -- check your .env file.")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set -- check your .env file.")

    application = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("today", commands.today_command))
    application.add_handler(CommandHandler("week", commands.week_command))
    application.add_handler(CommandHandler("tasks", commands.tasks_command))
    application.add_handler(CommandHandler("reminders", commands.reminders_command))
    application.add_handler(CommandHandler("topics", commands.topics_command))
    application.add_handler(CommandHandler("notes", commands.notes_command))
    application.add_handler(CommandHandler("events", commands.events_command))
    application.add_handler(CommandHandler("menu", commands.menu_command))
    application.add_handler(CommandHandler("help", commands.help_command))
    application.add_handler(CommandHandler("settings", commands.settings_command))

    # Nav-button presses arrive as plain text -- this handler is registered
    # BEFORE the catch-all so PTB routes matching button labels here first,
    # never reaching brain.get_response() (no LLM call for pure navigation).
    application.add_handler(MessageHandler(filters.Text(keyboards.NAV_LABELS), commands.nav_button_pressed))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    application.add_handler(CallbackQueryHandler(callbacks.task_callback, pattern=r"^task:"))
    application.add_handler(CallbackQueryHandler(callbacks.reminder_callback, pattern=r"^rem:"))
    application.add_handler(CallbackQueryHandler(callbacks.topic_callback, pattern=r"^topic:"))
    application.add_handler(CallbackQueryHandler(callbacks.event_callback, pattern=r"^event:"))

    application.add_error_handler(error_handler)

    logger.info("Starting Kangani...")
    application.run_polling()


if __name__ == "__main__":
    main()
