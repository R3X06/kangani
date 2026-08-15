"""General file storage for Kangani, backed by Telegram's own file servers.

When a user uploads a document/photo (that isn't a timetable PDF), we store
only Telegram's file_id -- a durable handle to bytes that live on Telegram's
servers -- plus metadata (which topic, a nickname, the original name). To
"retrieve" a file we re-send that file_id and Telegram serves the bytes.

Caveat, by design: a file_id is only valid for THIS bot token. Migrating to a
different bot would orphan every stored file. Acceptable for a personal bot;
it's why this isn't what a multi-user product would choose.

The upload flow mirrors pdf_import's "ask first, via a chat_data flag that the
message_handler intercepts" pattern, because the AI tool loop can't reach
context.chat_data to run a multi-step Telegram interaction.
"""

import logging

from telegram import Update
from telegram.ext import ContextTypes

import database
import keyboards

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 50 * 1024 * 1024  # Telegram bot download cap is ~20MB, but a
# stored file_id can point at a larger already-uploaded file; 50MB is a sane
# metadata-side guard against absurd inputs.


def _extract_file(update: Update) -> dict | None:
    """Pull a uniform file descriptor from whichever kind of upload this is --
    a document, a photo (largest size), an audio, or a video. Returns None if
    the message carries no storable file."""
    msg = update.message
    if msg.document is not None:
        d = msg.document
        return {
            "file_id": d.file_id,
            "file_unique_id": d.file_unique_id,
            "file_name": d.file_name,
            "mime_type": d.mime_type,
            "file_size": d.file_size,
        }
    if msg.photo:
        p = msg.photo[-1]  # highest-resolution size
        return {
            "file_id": p.file_id,
            "file_unique_id": p.file_unique_id,
            "file_name": None,
            "mime_type": "image/jpeg",
            "file_size": p.file_size,
        }
    if msg.video is not None:
        v = msg.video
        return {
            "file_id": v.file_id,
            "file_unique_id": v.file_unique_id,
            "file_name": v.file_name,
            "mime_type": v.mime_type,
            "file_size": v.file_size,
        }
    if msg.audio is not None:
        a = msg.audio
        return {
            "file_id": a.file_id,
            "file_unique_id": a.file_unique_id,
            "file_name": a.file_name,
            "mime_type": a.mime_type,
            "file_size": a.file_size,
        }
    return None


async def handle_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A non-PDF document, photo, video, or audio arrived -- offer to store it
    against a topic. (PDFs are caught earlier by the timetable-import handler;
    a PDF the user wants to STORE rather than import is handled by the
    'store this instead' button in that flow.)"""
    file = _extract_file(update)
    if file is None:
        return
    if file["file_size"] and file["file_size"] > MAX_FILE_BYTES:
        await update.message.reply_text(
            "That file is very large — I can store files up to about 50 MB."
        )
        return

    context.chat_data["file_upload_pending"] = file
    display = file["file_name"] or "this file"
    await update.message.reply_text(
        f"Where should I file {display}? Reply with a topic name "
        "(e.g. \"Study Notes\"), or \"none\" to keep it unfiled."
    )


async def handle_file_upload_reply(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> bool:
    """Intercept the plain-text reply naming which topic an uploaded file goes
    under. Returns True if it consumed the message, False if nothing's pending.
    """
    pending = context.chat_data.get("file_upload_pending")
    if not pending:
        return False

    chat_id = update.effective_chat.id
    text = (update.message.text or "").strip()

    topic_id = None
    topic_label = "unfiled"
    if text.lower() not in ("none", "no", "skip", "unfiled"):
        # Reuse the same exact-name/nickname matcher the PDF import uses.
        matches = _match_topics(chat_id, text)
        if not matches:
            await update.message.reply_text(
                f"No topic named '{text}'. Try again, or reply \"none\" to keep "
                "it unfiled."
            )
            return True
        if len(matches) > 1:
            context.chat_data["file_upload_pending_pick"] = {
                "text": text, "matches": [m["id"] for m in matches],
            }
            lines = "\n".join(f"#{m['id']} {m['path']}" for m in matches)
            await update.message.reply_text(
                f"{len(matches)} topics named '{text}':\n{lines}\n\n"
                "Reply with the # id of the one you mean."
            )
            return True
        topic_id = matches[0]["id"]
        topic_label = matches[0]["path"]

    saved = database.create_file(
        chat_id=chat_id,
        file_id=pending["file_id"],
        topic_id=topic_id,
        file_unique_id=pending.get("file_unique_id"),
        file_name=pending.get("file_name"),
        mime_type=pending.get("mime_type"),
        file_size=pending.get("file_size"),
    )
    context.chat_data.pop("file_upload_pending", None)
    context.chat_data.pop("file_upload_pending_pick", None)
    name = saved["file_name"] or "file"
    await update.message.reply_text(
        f"Saved {name} under {topic_label}. You can nickname it or ask me to "
        "bring it back any time."
    )
    return True


def _match_topics(chat_id: int, text: str) -> list[dict]:
    """Exact name/nickname match, then Y1S1-style shorthand -- delegates to the
    same resolver the PDF import uses so both upload flows behave identically."""
    import pdf_import
    return pdf_import.match_topics_by_name(chat_id, text)


async def send_files(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, files: list[dict]
) -> None:
    """Re-send stored files back to the chat by their Telegram file_id. Called
    from the retrieval tool once Claude has resolved which files to bring. Each
    file_id is served by Telegram directly -- we never held the bytes."""
    for f in files:
        caption_bits = []
        if f.get("nickname"):
            caption_bits.append(f["nickname"])
        elif f.get("file_name"):
            caption_bits.append(f["file_name"])
        if f.get("topic_name"):
            caption_bits.append(f"[{f['topic_name']}]")
        caption = " ".join(caption_bits) or None
        try:
            await context.bot.send_document(
                chat_id=chat_id, document=f["file_id"], caption=caption
            )
        except Exception:
            # A file_id can fail if the file aged out of Telegram's cache or the
            # bot token changed -- report which file rather than silently
            # dropping it.
            logger.exception("Failed to re-send file %s", f.get("id"))
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"Couldn't retrieve {f.get('nickname') or f.get('file_name') or 'a file'} "
                "— it may no longer be available.",
            )