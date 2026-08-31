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
import file_storage
import flows
import keyboards
import pdf_import
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
    BotCommand("dayimage", "Today's timetable as an image"),
    BotCommand("weekimage", "This week's timetable as an image"),
    BotCommand("monthimage", "This month's timetable as an image"),
    BotCommand("tasks", "View and update your tasks"),
    BotCommand("reminders", "View and cancel upcoming reminders"),
    BotCommand("topics", "Browse your topics and notes"),
    BotCommand("notes", "View your recent notes"),
    BotCommand("events", "Browse your events"),
    BotCommand("new", "Quickly add a task, reminder, or note"),
    BotCommand("help", "What Kangani can do"),
    BotCommand("manual", "The full user manual"),
    BotCommand("settings", "View your current settings"),
]


async def post_init(application: Application) -> None:
    database.init_db()
    scheduler.reschedule_pending_reminders(application.job_queue)
    await application.bot.set_my_commands(BOT_COMMANDS)

    # Launch one headless Chromium for the bot's lifetime -- cold-launching per
    # image request would make every /dayimage etc. noticeably slow. If it fails
    # (e.g. `playwright install chromium` hasn't been run), the bot still starts;
    # the image commands just report themselves unavailable.
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        application.bot_data["playwright"] = pw
        application.bot_data["browser"] = await pw.chromium.launch()
        logger.info("Playwright Chromium launched for timetable images.")
    except Exception:
        logger.exception(
            "Failed to launch Playwright Chromium -- image commands will be "
            "unavailable. Have you run `playwright install chromium`?"
        )

    logger.info("Kangani initialized: database ready, reminders rescheduled.")


async def post_shutdown(application: Application) -> None:
    browser = application.bot_data.get("browser")
    if browser is not None:
        await browser.close()
    pw = application.bot_data.get("playwright")
    if pw is not None:
        await pw.stop()


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Hi, I'm Kangani. Just talk to me naturally — \"remind me to call mom "
        "at 6pm\", \"add a task to finish the report by Friday\", or drop in "
        "your timetable PDF. Tap /help to see everything I can do, or use the "
        "buttons below.",
        reply_markup=keyboards.persistent_reply_keyboard(),
    )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None or update.message.text is None:
        # Edited messages, channel posts, and non-text updates route through
        # here too since filters.TEXT matches update.effective_message, not
        # specifically a NEW message -- update.message is None for those.
        # Silently ignore rather than respond to an edit as if it were new.
        return

    # A pending "where should this import go" / "new value for this field"
    # question takes priority over everything else -- these are plain text
    # replies to a prompt WE issued, not a general request for Claude, and
    # context.chat_data (where the pending import lives) isn't reachable from
    # the AI tool loop at all (see pdf_import.match_topics_by_name's
    # docstring). Consumed here means it never reaches brain.get_response.
    if await pdf_import.handle_pdf_import_reply(update, context):
        return

    # Same pattern: a reply naming which topic an uploaded file goes under is
    # a plain-text answer to OUR prompt, consumed here before brain sees it.
    if await file_storage.handle_file_upload_reply(update, context):
        return

    # Rename / nickname / add-subtopic replies answering a topic-screen prompt.
    if await callbacks.handle_topic_edit_reply(update, context):
        return

    # A pending quick-add flow (task title, note content, reminder message,
    # or a custom date/time) -- same pending-reply pattern as the three
    # interceptors above. Checked here too, before nav labels: if a flow is
    # mid-way through asking a question, a nav-label-looking reply answers
    # the question rather than being treated as navigation.
    if await flows.handle_flow_reply(update, context):
        return

    # Nav-button taps arrive as plain text too. Checked AFTER the pending-reply
    # interceptors above (not as a separately pre-registered handler) so a nav
    # tap sent while Kangani is mid-way through asking a question (PDF import
    # root, file-upload topic, topic rename/nickname/subtopic) never bypasses
    # that question -- it either answers it or, if the text happens to also be
    # a nav label, is treated as the answer, not a navigation. Only text that
    # isn't consumed by any pending prompt is checked against NAV_LABELS.
    if update.message.text in keyboards.NAV_LABELS:
        await commands.nav_button_pressed(update, context)
        return

    # Bare unscoped phrases that are exact equivalents of an existing slash
    # command ("tasks", "my reminders", "help") -- handled the same
    # deterministic way as a nav-button tap, no Claude call. Deliberately
    # checked AFTER the pending-reply interceptors above for the same reason
    # NAV_LABELS is: a reply answering a pending question must not be
    # reinterpreted as a shortcut just because it happens to match one.
    if await commands.dispatch_text_shortcut(update, context):
        return

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
        .post_shutdown(post_shutdown)
        .build()
    )

    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("today", commands.today_command))
    application.add_handler(CommandHandler("week", commands.week_command))
    application.add_handler(CommandHandler("dayimage", commands.dayimage_command))
    application.add_handler(CommandHandler("weekimage", commands.weekimage_command))
    application.add_handler(CommandHandler("monthimage", commands.monthimage_command))
    application.add_handler(CommandHandler("tasks", commands.tasks_command))
    application.add_handler(CommandHandler("reminders", commands.reminders_command))
    application.add_handler(CommandHandler("topics", commands.topics_command))
    application.add_handler(CommandHandler("notes", commands.notes_command))
    application.add_handler(CommandHandler("events", commands.events_command))
    application.add_handler(CommandHandler("new", flows.add_menu_command))
    application.add_handler(CommandHandler("menu", commands.menu_command))
    application.add_handler(CommandHandler("help", commands.help_command))
    application.add_handler(CommandHandler("manual", commands.manual_command))
    application.add_handler(CommandHandler("settings", commands.settings_command))

    # PDF uploads (schedule import) -- registered before the catch-all text
    # handler so a document upload is routed here, not into brain.get_response().
    application.add_handler(MessageHandler(filters.Document.PDF, pdf_import.handle_pdf_upload))
    # Non-PDF documents, photos, videos, audio -> general file storage. PDFs
    # are caught by the timetable handler above; this catches everything else.
    application.add_handler(
        MessageHandler(
            (filters.Document.ALL & ~filters.Document.PDF)
            | filters.PHOTO | filters.VIDEO | filters.AUDIO,
            file_storage.handle_file_upload,
        )
    )

    # Nav-button presses arrive as plain text -- dispatched from INSIDE
    # message_handler (after the pending-reply interceptors), not as a
    # separately pre-registered handler here. See message_handler's comment
    # for why: a pre-registered handler would match a nav label even while a
    # pdf_import/file_storage/topic_edit reply is pending, silently
    # swallowing that prompt's answer.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    application.add_handler(CallbackQueryHandler(callbacks.task_callback, pattern=r"^task:"))
    application.add_handler(CallbackQueryHandler(callbacks.reminder_callback, pattern=r"^rem:"))
    application.add_handler(CallbackQueryHandler(callbacks.topic_callback, pattern=r"^topic:"))
    application.add_handler(CallbackQueryHandler(callbacks.event_callback, pattern=r"^event:"))
    application.add_handler(CallbackQueryHandler(flows.flow_callback, pattern=r"^flow:"))
    application.add_handler(CallbackQueryHandler(callbacks.pdf_import_callback, pattern=r"^pdfimport:"))
    application.add_handler(CallbackQueryHandler(callbacks.settings_callback, pattern=r"^settings:"))
    application.add_handler(CallbackQueryHandler(callbacks.manual_callback, pattern=r"^manual:"))

    application.add_error_handler(error_handler)

    logger.info("Starting Kangani...")
    application.run_polling()


if __name__ == "__main__":
    main()