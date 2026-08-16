# Deploying Kangani

Kangani is a long-polling Telegram bot (no inbound HTTP), so it runs as a
**background worker**, not a web service — there's no port to expose. The one
thing that needs care is **persistence**: the SQLite database must live on a
mounted volume, or every redeploy wipes your reminders, tasks, and notes.

These are the steps that live *outside* the repo (platform config). The repo
itself is already set up for them: `database.py` reads `DB_PATH` and creates its
parent directory at startup, the `Dockerfile` defaults `DB_PATH=/data/kangani.db`,
and WAL mode is enabled for concurrent reads while a write is in flight.

## Railway (reference deployment)

1. **Create the service** from this repo. Railway builds the `Dockerfile`
   automatically. No port/health check needed — it's a worker.

2. **Attach a volume** (this is the critical step):
   - Command Palette (⌘K) → *Create Volume*, or right-click the service.
   - Set the **mount path to `/data`** — this must match the directory in
     `DB_PATH` (`/data/kangani.db`). Volumes mount at container *start*, not
     build time, so the DB is created on the volume on first run.

3. **Set environment variables** (Service → Variables):

   | Variable | Value | Notes |
   |---|---|---|
   | `TELEGRAM_BOT_TOKEN` | *(from @BotFather)* | required |
   | `ANTHROPIC_API_KEY` | *(from console.anthropic.com)* | required; workspace needs an active payment method |
   | `TIMEZONE` | `Asia/Singapore` | IANA name |
   | `DB_PATH` | `/data/kangani.db` | already defaulted in the Dockerfile; set it explicitly if you change the mount path |

   `RAILWAY_RUN_UID=0` is already set in the Dockerfile — Railway volumes mount
   as **root** while the container would otherwise run as a non-root user, so
   without this the process can't write to `/data` and SQLite fails with a
   permission error.

4. **Deploy.** On first boot the log should read
   `Kangani initialized: database ready, reminders rescheduled.` and the bot
   answers `/start` on Telegram.

## Verifying persistence

Create a reminder, trigger a redeploy, then check it survived
(`/reminders`). If it's gone, the volume mount path and `DB_PATH` don't agree,
or the volume isn't attached to this service.

## Backups (recommended)

A volume protects against redeploys but not against an accidental
delete-all, a bad migration, or a platform incident. Because everything is one
SQLite file, a backup is just a copy of `/data/kangani.db` (plus the `-wal` /
`-shm` sidecars if present). The Railway CLI can pull it down:

```bash
railway volume browse   # interactive TUI: browse / download the .db file
```

Run it on a schedule you're comfortable losing work back to (e.g. weekly) and
keep the copy somewhere off-platform.

## Other platforms

The repo isn't Railway-specific. On any container host: mount persistent
storage somewhere, then set `DB_PATH` to a file inside that mount. The only
Railway-specific bit is `RAILWAY_RUN_UID`, which is harmless elsewhere (it's
just an unused env var).

Because the timetable images shell out to Playwright/Chromium and PDF import
needs `poppler-utils`, deploy from the `Dockerfile` (which installs both)
rather than a bare buildpack.