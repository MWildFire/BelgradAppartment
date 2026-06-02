"""One-off Telethon authentication for the log-shipping userbot.

Creates `data/otz_logger.session` — a SEPARATE session file from the live monitor so the two
can run concurrently without SQLite-lock conflicts. Both sit on the same Telegram account,
which is fine (Telegram allows multiple active sessions per account).

Run interactively on the VPS as the otz user:
    sudo -u otz /opt/otz/.venv/bin/python /opt/otz/scripts/auth_logger.py

You'll be prompted for:
- Phone number (international format, e.g. +381...)
- Login code (sent by Telegram to your existing session — check the official Telegram app)
- Optional 2FA password if enabled
"""

from __future__ import annotations

import asyncio
import sys

from telethon import TelegramClient

from src.config import get_settings


async def main() -> int:
    settings = get_settings()
    if not settings.tg_api_id or not settings.tg_api_hash:
        print("ERROR: TG_API_ID / TG_API_HASH are missing in .env", file=sys.stderr)
        return 1

    session_path = str(settings.data_dir / "otz_logger")
    async with TelegramClient(session_path, settings.tg_api_id, settings.tg_api_hash) as client:
        me = await client.get_me()
        print(f"✓ Logger userbot authenticated as: {me.first_name} (id={me.id}, username=@{me.username})")
        print(f"  Session file: {session_path}.session")
        # Sanity check: post a smoke-test message into Saved Messages.
        await client.send_message("me", "🧪 otz-logger online — log-shipping ready.")
        print("✓ Smoke-test message sent to Saved Messages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
