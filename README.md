# Kangani

A Telegram personal assistant for managing study and daily life — topics, tasks, reminders, notes, and a class timetable — built to feel like a polished app rather than a command-line bot. You talk to it naturally; Claude decides what to store and how to answer.

> Originally prototyped under the placeholder name "Jarvis," renamed to **Kangani**.

## What it does

- **One topic tree.** Everything you track — a course, a year, a semester, a module, a one-off event, a freeform life area — is a *topic*, nestable to any depth. Tasks, notes, and reminders attach to a topic (or to nothing at all).
- **Compositional calendar.** Ask for a topic and Kangani returns everything nested beneath it. `"Y3S1 calendar"` gives the full picture under Y3S1; `"Y3S1 lesson calendar"` narrows to just lessons; `"SC2001 tutorials"` narrows further. Every word before "calendar" is a filter that intersects with the others.
- **Timetable.** Recurring weekly classes or one-off blocks, with per-class week patterns (odd/even/specific weeks), semester-week numbering, and recess weeks. Rendered as text or as a styled image.
- **PDF import.** Drop in an NTU registration PDF; Kangani reads the timetable visually (the PDFs use custom-encoded fonts that defeat text extraction) and imports your classes after you confirm a preview.
- **File storage.** Send any document, photo, or audio and file it under a topic; Kangani keeps a durable handle (via Telegram's own file servers) and hands it back whenever you ask.
- **Categories & tags.** Tasks and lessons carry user-defined categories (assignment, lab, tutorial…) you can filter by. Every task, note, and reminder has a hidden stable tag — add `-tag` to any listing to reveal them.

## How a message flows

`Telegram → bot.py → brain.py (Claude + tool loop) → tools.py → database.py → back to you`

A nav-button tap short-circuits the LLM and renders a pre-built view directly. Everything else goes to Claude, which decides whether to answer in text or call one or more tools, then replies.

## Tech stack

| Layer | Choice |
|---|---|
| Bot framework | `python-telegram-bot` |
| Scheduling | PTB's built-in `JobQueue` (APScheduler) |
| Database | SQLite (unified topic tree) |
| AI | Anthropic API — Claude Sonnet, native tool use |
| Timetable images | Jinja2 templates + Playwright (Chromium) |
| PDF import | pdf2image + Claude vision |
| Timezone | Asia/Singapore |

## Project layout

| File | Responsibility |
|---|---|
| `bot.py` | Entry point; wires handlers, starts polling |
| `brain.py` | Claude integration, system prompt, tool-use loop |
| `tools.py` | Tool schemas + handlers Claude can call |
| `database.py` | SQLite persistence, schema, migrations |
| `commands.py` | Slash-command + nav-button views |
| `callbacks.py` | Inline-button (callback query) handling |
| `keyboards.py` | Reply- and inline-keyboard builders |
| `scheduler.py` | Reminder scheduling, week-number logic |
| `pdf_import.py` | NTU timetable PDF import (vision) |
| `file_storage.py` | Store/retrieve uploaded files under topics (via Telegram file IDs) |
| `timetable_data.py` | Assembles data for timetable rendering |
| `timetable_image.py` | Renders timetable HTML into an image |
| `templates/` | Daily / weekly / monthly timetable HTML |
| `test_scheduler_weeks.py` | Tests for week-number / recess logic |

## Setup

```bash
git clone https://github.com/R3X06/kangani.git
cd kangani

python -m venv venv
source venv/bin/activate            # Windows: venv\Scripts\activate

pip install -r requirements.txt
playwright install chromium         # required for timetable images

cp .env.example .env                # then fill in your tokens
python bot.py
```

`.env` needs `TELEGRAM_BOT_TOKEN` (from @BotFather), `ANTHROPIC_API_KEY` (from console.anthropic.com — make sure the workspace has billing set up), and `TIMEZONE`. `DB_PATH` is optional locally (defaults to `./kangani.db`) but matters when deploying — see below.

## Deployment

The bot runs as a long-polling **background worker** (no inbound HTTP, no port). The included `Dockerfile` installs everything it needs (poppler for PDF import, Chromium for timetable images). The one thing that needs care is persistence: the SQLite database must live on a mounted volume, or every redeploy wipes it. `DB_PATH` points the app at that volume (defaulted to `/data/kangani.db` in the Dockerfile). See **[DEPLOY.md](DEPLOY.md)** for the full Railway walkthrough (volume mount, env vars, the root-UID gotcha, and backups).

## Roadmap

- Proactive daily digest
- Location-based reminders
- Spaced-repetition nudges and concept dependency mapping
- Energy-aware scheduling
- OCR for handwritten notes, voice transcription
- Google Calendar / LMS integration
- A "context brain" that cross-references everything to answer *"what should I do right now?"*