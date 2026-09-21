# Telegram bridge → Brio

A minimal Telegram bot that fronts a personal AI assistant. One bot, many
threads: every distinct **(chat, topic)** is its own conversation.

| Where | Thread |
|---|---|
| DM with the bot | private 1:1 thread |
| Supergroup with **Topics** on | one thread per topic |
| Plain group | one thread for the group |

Channels are broadcast-only and Communities are just folders — neither fits a
conversation, so the bridge ignores them by design.

## How it works

`bot.py` long-polls Telegram and drops every incoming message as one JSON
file into `queue/inbox/`. A separate tick (cron, every minute) answers the
inbox and writes replies into `queue/outbox/`; the poller delivers them.
Transcripts live in `threads/<chat_id>_<thread_id>.md`, one per thread.

Commands: `/start` (help), `/new` (fresh thread), `/id` (ids).

The first person to DM the bot becomes its owner; everyone else is locked
out (or set `TELEGRAM_OWNER_ID` yourself — message **@userinfobot** to learn
your numeric id).

## Setup

**1. Create the bot** — in Telegram, message **@BotFather**:
- `/newbot` → pick a display name → pick a username ending in `bot` → copy the token
- `/setprivacy` → choose your bot → **Disable** (lets it see group/topic messages, not just mentions)

**2. Install** (needs Python 3.10+):

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

**3. Configure** — copy `.env.example` to `.env` and fill in your token:

```sh
cp .env.example .env
```

**4. Run** — the poller in one process, and something that ticks the inbox
every minute (cron, systemd timer, …) writing replies to `queue/outbox/`:

```sh
python3 bot.py
```

**5. Pair** — message the bot once from your own Telegram account; that
account becomes the owner and everyone else gets locked out.

## Files

```
bot.py            the poller + outbox sender
requirements.txt  python-telegram-bot
.env.example      TELEGRAM_BOT_TOKEN / TELEGRAM_OWNER_ID template
.env              your token (gitignored, you create it)
queue/inbox/      incoming messages (tick consumes)
queue/outbox/     replies to send (poller delivers)
queue/done/      processed inbox
queue/failed/    messages Telegram rejected
owner.json        paired owner id (auto-created on first DM, gitignored)
threads/          per-thread transcripts (gitignored)
```
