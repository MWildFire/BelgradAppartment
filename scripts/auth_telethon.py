"""One-off Telethon authentication — creates the .session file used by the monitor.

Run interactively:
    uv run python scripts/auth_telethon.py
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
        print("Get them at https://my.telegram.org/apps", file=sys.stderr)
        return 1

    session_path = str(settings.data_dir / settings.tg_session_name)
    async with TelegramClient(session_path, settings.tg_api_id, settings.tg_api_hash) as client:
        me = await client.get_me()
        print(f"Authenticated as: {me.first_name} (id={me.id}, username=@{me.username})")
        print(f"Session saved to: {session_path}.session")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
