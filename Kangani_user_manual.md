# Kangani — User Manual

Kangani is a personal life-and-study assistant that lives inside Telegram. Everything you track — modules, tasks, notes, reminders, your timetable, one-off events — lives in **one tree of topics**, and you can reach any of it either by **tapping buttons** or by **just talking to it in plain English**.

> A note on the bot bio: Telegram limits the "About" text to 120 characters and the "Description" to 512 characters, so neither can hold a manual this size. See **"What to put in the bot bio"** at the end for ready-to-paste text plus how to link out to this document instead.

---

## 1. The two ways to use Kangani

**Buttons** — a persistent keyboard (Today / Tasks / Reminders / Topics / Notes / Events / ➕ Add) plus slash commands. Zero AI involved — instant, deterministic, and free.

**Talking naturally** — type a sentence and Kangani (Claude) figures out what you mean: what to create, where it belongs, what you're asking to see. This is the flexible path for anything the buttons don't cover directly.

You can mix both freely in the same conversation.

---

## 2. Quick nav — buttons & slash commands

| Button | Command | What it does |
|---|---|---|
| 📅 Today | `/today` | Today's lessons, tasks, and reminders in one view |
| — | `/week [N]` | This week's timetable; add a week number for a specific week, e.g. `/week 3` |
| — | `/dayimage` | Today's timetable rendered as an image |
| — | `/weekimage [N]` | This week as an image; add a number for a specific week |
| — | `/monthimage [Month]` | This month as an image; add a month name, e.g. `/monthimage September` |
| 📋 Tasks | `/tasks` | View and update tasks (✅ Complete / ✏️ Edit buttons per task) |
| ⏰ Reminders | `/reminders` | View, push back (+10m/+1h), or cancel upcoming reminders |
| 📚 Topics | `/topics` | Browse your whole topic tree — drill into any topic |
| 📝 Notes | `/notes` | View your most recent notes |
| 🗓️ Events | `/events` | Browse upcoming events (hackathons, talks, workshops) |
| ➕ Add | `/new` | Quick-add a task, reminder, or note by tapping through buttons |
| — | `/menu` | Show the button keyboard again |
| — | `/settings` | View and change settings (timezone, semester dates, label style) |
| — | `/help` | A condensed in-chat cheat sheet |

All of these are fully deterministic — none of them call Claude, so they're instant and cost nothing.

---

## 3. Quick-Add flows (➕ Add / /new)

The fastest way to create something without typing a full sentence. Tap **➕ Add**, pick **Task**, **Reminder**, or **Note**, and Kangani walks you through it with buttons for every decision that has a fixed set of options:

- **Task** → pick a topic (or "no topic") → type the title → pick a deadline (Today / Tomorrow / This Friday / No deadline / Custom date) → pick a category (or none) → done.
- **Reminder** → type what to be reminded of → pick when (In 10 min / In 1 hour / In 3 hours / Tomorrow 9am / Custom date & time) → done.
- **Note** → pick a topic (or "general") → type the content → mark it Reference or Regular → done.

Only the genuinely open-ended bits (a title, a reminder message, note content, or a custom date) need typing — everything else is a tap. If you start a flow from inside a topic's detail screen (via the ➕ Task / 📝 Note buttons there), it skips the topic-picker step entirely since the topic's already known. Every step has a ❌ Cancel button.

---

## 4. Talking to Kangani naturally

You don't need any special syntax. Some examples:

- *"add a task to finish the report by Friday"*
- *"remind me to call mom at 6pm"*
- *"note under Backpropagation: chain rule intuition"*
- *"SC2001 lecture Mondays 9–11am at LT1"*
- *"mark task 12 as done"*
- *"push my 3pm reminder back an hour"*

Kangani figures out where things belong, asks a quick clarifying question if something's genuinely ambiguous, and never forces an item into the wrong place just to avoid asking.

You can also just drop your **NTU registration PDF** into the chat and Kangani will read your whole timetable out of it (see §9).

---

## 5. Asking Kangani for things — the compositional query system

This is the part worth understanding, because it's more powerful than it looks. A request like *"Y3S1 lesson calendar"* or *"my labs this week"* is read as up to **four independent constraints**, each resolved separately, then combined:

1. **SCOPE** — a topic name (a year, semester, module, event, or any topic), including nested shorthand like "Y3S1" resolving to Semester 1 under Year 3, and matching either a topic's real name or a nickname you've given it. No topic named → scope is everything.
2. **CONTENT TYPE** — lessons / tasks / notes / reminders. Name one and you get only that; name none and you get everything combined. The bare word "calendar" or "schedule" is *not* a type — it means "combine everything."
3. **FILTER** — a narrower word within a type: a lesson type (lecture, tutorial, lab...), a task category, or "general" for unlinked reminders.
4. **TIME** — a date range ("today", "this week", "next Friday", "in August"), or "upcoming"/"soonest".

**Worked examples:**
- *"Y3S1 calendar"* → everything (lessons + tasks + reminders + notes) under Y3S1
- *"Y3S1 lesson calendar"* → just the lessons under Y3S1
- *"SC2001 labs"* → just SC2001's lab sessions
- *"labs"* (no scope) → every lab, across everything
- *"what's due"* → tasks that have a deadline, soonest first
- *"general reminders"* → reminders tied to nothing
- *"all reminders"* → every pending reminder

If a word doesn't match any known topic or filter, Kangani tells you it doesn't recognize it and asks — rather than silently dropping your constraint and showing you a broader answer than you asked for.

---

## 6. How everything is organized — the topic tree

There is **one unified tree**. A topic can be a course, a year, a semester, a module, an event, or any freeform life area — nestable to any depth (Year 3 → Semester 1 → SC2001 → Lectures, or "Fitness" → "Gym routine", or anything you like).

Each topic has three independent name fields:
- **name** — short code, e.g. "SC2001"
- **full name** — the official long title, e.g. "Data Structures and Algorithms"
- **nickname** — a short label *you* chose, e.g. "DSA"

Tasks, notes, and reminders attach to any topic by ID, or to nothing at all (a "general" item). Ask for a topic and Kangani pulls everything nested underneath it too.

If something ends up in the wrong place, just say so — Kangani can rename a topic, set/change its nickname, or move it elsewhere in the tree.

---

## 7. Tasks

- Attach to a topic or stay unfiled.
- Optional **category** (e.g. "Assignment", "Reading") — Kangani reuses an existing category rather than minting near-duplicate ones, and will show you what already exists before creating a new one.
- Optional deadline.
- Status: not started / in progress / blocked / done, with a progress percentage.
- From the Tasks list, tap ✅ Complete or ✏️ Edit on any task to change its status directly.

---

## 8. Reminders

- One-time, fire-and-forget messages at a specific date/time.
- Can be linked to a task or a topic (or nothing — "general").
- When a reminder fires, you get ✅ Done / Snooze 10m / Snooze 1h buttons.
- From the Reminders list (before it fires), you can push it back by +10m/+1h or cancel it outright.

---

## 9. Notes

- Attach to a topic, or stay general (unattached).
- Mark **is_reference = true** for material worth keeping for later lookup (a link, an excerpt, a definition) — say "reference" or use the ➕ Add flow's Reference button. Default is a regular, transient note.
- Optional **source** field (a URL, book title, lecture name).

---

## 10. Lessons & your timetable

- A recurring weekly lesson (`day_of_week`) or a one-off calendar item (`specific_date`) — never both.
- Each lesson can have a **type** (Lecture, Tutorial, Lab, Seminar, ...) — reused the same way categories are.
- **Alternating/specific-week classes:** set your **semester anchor** once (the calendar date week 1 begins), and Kangani can track odd/even-week or specific-week classes precisely, and knows which official week any date falls in.
- **Recess weeks:** tell Kangani about a reading/break week and it's automatically skipped when counting weeks and hidden from that week's schedule.
- **Timetable labels:** control whether the timetable shows a module's code, nickname, full name, or a combination — change the saved default in Settings, or ask for a one-off override ("show full names just this once").
- **Timetable images:** `/dayimage`, `/weekimage [N]`, `/monthimage [Month]` render an actual picture of your schedule.

---

## 11. Events

A time-boxed one-off activity (hackathon, talk, workshop) is just a topic with an event date attached. Creating one automatically sets up reminders 60 and 30 minutes before it — you can add more lead times (e.g. "remind me a day before") at any point. Tasks and notes attach to an event exactly like they would to any other topic.

---

## 12. Tags

Every task, note, and reminder has a short, hidden reference code (a "tag") that's stable for its whole life — a way to point at one specific item unambiguously later. Tags are hidden by default; add `-tag` to any listing request (e.g. *"notes -tag"*) to reveal them.

---

## 13. Importing your timetable from a PDF

Drop your NTU registration PDF straight into the chat. Kangani rasterizes each page and reads it with a vision model, then shows you the extracted schedule for **confirmation before writing anything** — you can edit individual entries (day, time, location, type) or delete ones that were read wrong, then confirm the import as a batch.

---

## 14. Storing files

Any non-PDF file, photo, video, or audio you send is stored (via Telegram's own file servers — nothing is re-uploaded elsewhere) and can be attached to a topic, nicknamed, retrieved, or deleted later.

---

## 15. Settings

`/settings` shows and lets you change:
- **Timezone** (server-configured)
- **Semester start date** (week 1's Monday)
- **Recess weeks**
- **Timetable label format** (tap 🏷 to cycle through code / nickname / full name / combinations)

Most of these are also changeable just by telling Kangani in plain English (e.g. *"week 1 starts 12 Aug"*, *"recess week is week 7"*).

---

## What to put in the bot bio

Telegram enforces hard limits here — 120 characters for **About**, 512 for **Description** — so paste one of these rather than the full manual:

**About (≤120 chars):**
> Your personal life & study assistant — tasks, reminders, notes, and your timetable, all in one chat. /help to start.

**Description (≤512 chars, shown before someone starts the chat):**
> Kangani keeps your tasks, notes, reminders, and class timetable in one place. Talk to it naturally ("remind me to call mom at 6pm", "SC2001 lecture Mondays 9-11am") or use the buttons below — Today, Tasks, Reminders, Topics, Notes, Events, and ➕ Add for quick-add flows. Drop in your NTU registration PDF and it reads your whole timetable automatically. Tap /help anytime for a cheat sheet, or /new to add something in a few taps.

If you want the full manual reachable from the bio itself, host this file somewhere (a pinned message in your own saved chat with the bot, a GitHub gist, Notion page, etc.) and add the link to the end of the Description field — there's no way to embed the full document in the bio directly.