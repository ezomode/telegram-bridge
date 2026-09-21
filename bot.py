#!/usr/bin/env python3
"""Telegram bridge poller for Brio.

Long-polls Telegram and drops incoming messages into queue/inbox/ as one JSON
file per message. A separate tick (cron, every minute) processes the inbox and
writes replies to queue/outbox/; this process delivers them.

Thread model: every distinct (chat_id, thread_id) is one conversation thread.
  - DM with the bot            -> one private thread
  - Supergroup with Topics on  -> one thread per topic (message_thread_id)
  - Plain group                -> one thread for the whole group

Only the tick touches transcripts; only this process sends from the outbox.

Env (.env next to this file):
  TELEGRAM_BOT_TOKEN   required
  TELEGRAM_OWNER_ID    optional; if unset, the first DM sender becomes the owner
                       and everyone else is locked out.
"""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# Sandbox quirk: NO_PROXY/no_proxy contain bracketed IPv6 entries (e.g. [::1])
# that httpx cannot parse, crashing client construction. The bridge only talks
# to api.telegram.org (never a no_proxy host), so drop the bracketed entries.
for _k in ("no_proxy", "NO_PROXY"):
    _v = os.environ.get(_k, "")
    if _v:
        os.environ[_k] = ",".join(
            p for p in _v.split(",") if "[" not in p and "]" not in p
        )

HERE = Path(__file__).resolve().parent


def load_env() -> dict:
    env: dict = {}
    p = HERE / ".env"
    if p.exists():
        for line in p.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


ENV = load_env()
TOKEN = ENV.get("TELEGRAM_BOT_TOKEN", "").strip()
INBOX = HERE / "queue" / "inbox"
OUTBOX = HERE / "queue" / "outbox"
DONE = HERE / "queue" / "done"
FAILED = HERE / "queue" / "failed"
OWNER_FILE = HERE / "owner.json"

for d in (INBOX, OUTBOX, DONE, FAILED):
    d.mkdir(parents=True, exist_ok=True)


def utcnow():
    return datetime.now(timezone.utc)


def log(*a):
    print(utcnow().strftime("%H:%M:%S"), *a, flush=True)


def get_owner() -> int | None:
    if ENV.get("TELEGRAM_OWNER_ID", "").strip().isdigit():
        return int(ENV["TELEGRAM_OWNER_ID"].strip())
    if OWNER_FILE.exists():
        try:
            return int(json.loads(OWNER_FILE.read_text()).get("owner_id"))
        except Exception:
            return None
    return None


def set_owner(user_id: int, name: str):
    OWNER_FILE.write_text(json.dumps({"owner_id": user_id, "name": name}))


def thread_id_of(msg) -> int:
    """Normalize Telegram thread ids: General topic (None or 1) -> 0."""
    tid = getattr(msg, "message_thread_id", None)
    return 0 if tid in (None, 1) else tid


def drop_inbox(payload: dict):
    name = f"{utcnow().strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}.json"
    (INBOX / name).write_text(json.dumps(payload, ensure_ascii=False))


def chat_label(chat) -> str:
    return chat.title or chat.username or chat.first_name or str(chat.id)


async def check_owner(update, context) -> bool:
    """True if the sender is the owner (or becomes the owner)."""
    msg = update.effective_message
    user = update.effective_user
    if user is None or user.is_bot:
        return False
    owner = get_owner()
    chat = update.effective_chat
    if owner is None and chat and chat.type == "private":
        set_owner(user.id, user.full_name)
        log(f"owner paired: {user.full_name} ({user.id})")
        return True
    if owner is not None and user.id == owner:
        return True
    try:
        await msg.reply_text("🔒 This bot is paired with its owner.")
    except Exception:
        pass
    return False


async def cmd_start(update, context):
    if not await check_owner(update, context):
        return
    await update.effective_message.reply_text(
        "Hey — I'm Brio's Telegram front door.\n\n"
        "• DM me: private thread, just us.\n"
        "• In a supergroup with Topics on: each topic is its own thread.\n\n"
        "Commands:\n"
        "/new — start this thread fresh\n"
        "/id — show chat / thread / user ids"
    )


async def cmd_id(update, context):
    if not await check_owner(update, context):
        return
    msg = update.effective_message
    await msg.reply_text(
        f"chat_id: {msg.chat_id}\n"
        f"thread_id: {thread_id_of(msg)}\n"
        f"your user id: {update.effective_user.id}"
    )


async def cmd_new(update, context):
    if not await check_owner(update, context):
        return
    msg = update.effective_message
    drop_inbox({
        "type": "command",
        "command": "new",
        "id": uuid.uuid4().hex,
        "chat_id": msg.chat_id,
        "thread_id": thread_id_of(msg),
        "date": utcnow().isoformat(),
    })
    # Confirmation comes back through the outbox once the tick resets the thread.


async def on_text(update, context):
    if not await check_owner(update, context):
        return
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    tid = thread_id_of(msg)
    drop_inbox({
        "type": "message",
        "id": uuid.uuid4().hex,
        "chat_id": msg.chat_id,
        "thread_id": tid,
        "chat_type": chat.type,
        "chat_title": chat_label(chat),
        "sender": {"id": user.id, "name": user.full_name,
                   "username": user.username},
        "text": msg.text,
        "date": utcnow().isoformat(),
    })
    log(f"inbox <- {chat_label(chat)} [{tid}] {user.full_name}: {msg.text[:60]!r}")


async def on_voice(update, context):
    if not await check_owner(update, context):
        return
    await update.effective_message.reply_text(
        "🎤 Voice notes aren't wired up yet — send text for now."
    )


def chunk(text: str, n: int = 4000):
    while len(text) > n:
        cut = text.rfind("\n", 0, n)
        cut = cut if cut > 0 else n
        yield text[:cut]
        text = text[cut:].lstrip("\n")
    yield text


async def send_outbox(context):
    from telegram.error import TelegramError
    for path in sorted(OUTBOX.glob("*.json")):
        try:
            payload = json.loads(path.read_text())
            chat_id = payload["chat_id"]
            thread_id = int(payload.get("thread_id") or 0)
            kwargs = {"message_thread_id": thread_id} if thread_id else {}
            for part in chunk(payload["text"]):
                await context.bot.send_message(chat_id=chat_id, text=part, **kwargs)
            path.unlink()
            log(f"outbox -> {chat_id} [{thread_id}] sent, {len(payload['text'])} chars")
        except TelegramError as e:
            log(f"outbox send failed for {path.name}: {e}; moved to failed/")
            path.rename(FAILED / path.name)
        except Exception as e:
            log(f"outbox bad file {path.name}: {e}; moved to failed/")
            path.rename(FAILED / path.name)


async def post_init(app):
    me = await app.bot.get_me()
    log(f"polling as @{me.username} ({me.id})")


def main():
    if not TOKEN:
        log("TELEGRAM_BOT_TOKEN missing in .env — exiting.")
        sys.exit(2)
    from telegram.ext import (
        ApplicationBuilder, CommandHandler, MessageHandler, filters,
    )
    from telegram.request import HTTPXRequest

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .request(HTTPXRequest(
            connect_timeout=15.0, read_timeout=20.0,
            write_timeout=20.0, pool_timeout=10.0,
        ))
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.VOICE, on_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(send_outbox, interval=2.0, first=2.0)

    log("bridge poller started")
    app.run_polling(drop_pending_updates=True, bootstrap_retries=5)


if __name__ == "__main__":
    main()
