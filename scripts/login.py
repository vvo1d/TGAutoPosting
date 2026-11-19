"""Одноразовый вход user-аккаунтом для чтения каналов-источников.

    python -m scripts.login        — по номеру телефона и коду
    python -m scripts.login --qr   — по QR-коду (если код не приходит)

Код приходит не по SMS, а в приложение Telegram: служебный чат «Telegram» на устройстве,
где этот аккаунт уже открыт.
"""
import asyncio
import getpass
import sys

from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError

from app.config import Settings


async def qr_login(client: TelegramClient) -> None:
    import qrcode

    qr = await client.qr_login()
    while True:
        print("\nОткройте Telegram на телефоне: Настройки → Устройства → Подключить устройство,")
        print("и отсканируйте этот QR-код:\n")
        code = qrcode.QRCode(border=1)
        code.add_data(qr.url)
        code.print_ascii(invert=True)
        try:
            await qr.wait(timeout=60)
            return
        except asyncio.TimeoutError:
            print("QR-код устарел, показываю новый…")
            await qr.recreate()
        except SessionPasswordNeededError:
            pw = getpass.getpass("Включена двухфакторная защита. Введите облачный пароль: ")
            await client.sign_in(password=pw)
            return


async def main() -> None:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(s.session_path), s.tg_api_id, s.tg_api_hash)
    if "--qr" in sys.argv:
        await client.connect()
        if not await client.is_user_authorized():
            await qr_login(client)
    else:
        print("Код придёт в приложение Telegram (чат «Telegram»), а не по SMS.")
        print("Если не приходит — прервите (Ctrl+C) и запустите: python -m scripts.login --qr\n")
        await client.start()
    me = await client.get_me()
    print(f"\nГотово: вошли как {me.first_name} (id {me.id}). Сессия: {s.session_path}.session")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
