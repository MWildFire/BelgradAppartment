"""QR-login authorization for a Telethon userbot session.

Bypasses SMS / in-app login codes entirely: an already-authorized Telegram app
(your phone) scans the QR and vouches for the new session. Use when login codes
are not delivered — e.g. Telegram withholds app-type codes after all sessions
were terminated and there is still a live app session intercepting them.

Run (single line; no interactive stdin needed unless 2FA is on):
    ssh -t root@HOST 'sudo -u otz bash -c "cd /opt/otz && .venv/bin/python /tmp/auth_qr.py"'

If a 2FA cloud password is set, pass it via env so no stdin is required:
    ... bash -c "cd /opt/otz && OTZ_2FA=yourpass .venv/bin/python /tmp/auth_qr.py"
"""
from __future__ import annotations

import asyncio
import os

import qrcode
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from src.config import get_settings

PNG_PATH = "/tmp/otz_login_qr.png"


def render(url: str) -> None:
    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)
    try:
        qrcode.make(url).save(PNG_PATH)
        print(f"(QR also saved as image: {PNG_PATH})", flush=True)
    except Exception:
        pass
    print(flush=True)


async def main() -> int:
    settings = get_settings()
    # OTZ_SESSION overrides the session name so the same tool can re-auth the
    # logger userbot ("otz_logger") as well as the monitor (settings default).
    session_name = os.environ.get("OTZ_SESSION") or settings.tg_session_name
    session_path = str(settings.data_dir / session_name)
    print(f"session -> {session_path}.session", flush=True)

    client = TelegramClient(session_path, settings.tg_api_id, settings.tg_api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorized as: {me.first_name} (id={me.id})", flush=True)
        await client.disconnect()
        return 0

    qr = await client.qr_login()
    print("=== Scan this QR: iPhone -> Settings -> Devices -> Link Desktop Device ===", flush=True)
    render(qr.url)

    user = None
    for _ in range(40):  # ~20 min of 30s windows
        try:
            user = await qr.wait(timeout=30)
            break
        except asyncio.TimeoutError:
            await qr.recreate()
            print("(QR refreshed -- scan the current one on screen)", flush=True)
            render(qr.url)
        except SessionPasswordNeededError:
            password = os.environ.get("OTZ_2FA")
            if not password:
                print("!! 2FA is enabled. Re-run with the cloud password in the OTZ_2FA env var.", flush=True)
                await client.disconnect()
                return 1
            user = await client.sign_in(password=password)
            break

    if user is None:
        print("Timed out -- QR was not scanned.", flush=True)
        await client.disconnect()
        return 1
    me = await client.get_me()
    print(f"AUTHENTICATED as: {me.first_name} (id={me.id}, username=@{me.username})", flush=True)
    await client.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
