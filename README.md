# Kangani

A Telegram-based personal assistant bot for managing daily life and study — reminders, tasks, topics, and notes, built to feel like a polished app rather than a plain command-line bot.

> Originally prototyped under the placeholder name "Jarvis," renamed to **Kangani**.

## Overview

Kangani is a personal life and study management system that lives inside Telegram. The goal isn't just backend correctness — it's a bot that feels professional and well-structured, with proper navigation (slash commands, persistent keyboards, inline buttons) instead of raw text commands.

## Current features

- **Reminders** — time-based reminders via PTB's `JobQueue`
- **Tasks** — task tracking with status
- **Topics** — organizing study/life topics
- **Notes** — quick note capture

## Currently in progress

Building the navigation layer to make the bot feel polished:
- Slash command menu (`set_my_commands()`)
- Persistent reply keyboard
- Inline buttons

Open design question: should the persistent reply keyboard always be visible, or appear on demand (button-first vs. free-text-first interaction)?

## Tech stack

| Layer | Choice |
|---|---|
| Bot framework | `python-telegram-bot` (PTB) |
| Scheduling | PTB's built-in `JobQueue` |
| Database | SQLite (8-table schema) |
| AI | Anthropic API — Claude Sonnet |
| Language | Python |
| Timezone | Asia/Singapore |
| Dev environment | VS Code + Claude Code |

## Project structure

Five-file Python project structure. *(Fill in actual filenames/layout here as the structure solidifies.)*

## Setup

```bash
# 1. Clone the repo
git clone https://github.com/<your-username>/kangani.git
cd kangani

# 2. Create and activate a virtual environment
python -m venv venv
source venv/bin/activate   # on Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# then fill in your Telegram bot token and Anthropic API key

# 5. Run the bot
python main.py   # or your actual entry-point filename
```

## Roadmap

**Near-term**
- Slash command menu, persistent keyboard, inline buttons (navigation layer)

**Phase 2+**
- Location-based reminders with QR arrival tracking
- PDF schedule import and parsing
- Spaced repetition nudges and concept dependency mapping
- Energy-aware scheduling
- OCR for handwritten notes and voice transcription
- Google Calendar and LMS integration
- A central "context brain" layer that cross-references all data to answer *"what should I do right now?"*

## Known issues (resolved)

- ✅ Import ordering bug — `.env` was loading after module import
- ✅ Timezone bug — reminders were firing prematurely

## License

*(Add a license if you plan to make this public, e.g. MIT.)*
