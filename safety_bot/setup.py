"""Local setup helpers. Never prints credentials or sends Telegram messages."""
import asyncio
import os
import sys

import aiohttp
from aiogram import Bot
from dotenv import load_dotenv

from .config import Config
from .samsara import Samsara
from datetime import datetime, timedelta, timezone


async def main(args=None):
    load_dotenv()
    args = sys.argv[1:] if args is None else args
    if args == ["group-id"]:
        async with Bot(os.environ["TELEGRAM_BOT_TOKEN"]) as bot:
            me = await bot.get_me()
            print(f"Telegram bot: @{me.username}", flush=True)
            updates = await bot.get_updates(timeout=0)
            groups = {}
            for update in updates:
                message = update.message or update.my_chat_member
                if message and message.chat.type in ("group", "supergroup"):
                    groups[message.chat.id] = message.chat.title
            for group_id, title in groups.items():
                print(f"{group_id}: {title}")
            if not groups:
                print(f"Adaugă botul în grup și trimite /groupid@{me.username}, apoi repetă verificarea.")
        return
    if args != ["check"]:
        raise SystemExit("Usage: python -m safety_bot.setup group-id|check")
    cfg = Config.load()
    async with Bot(cfg.telegram_token) as bot:
        me = await bot.get_me()
        chat = await bot.get_chat(cfg.group_id)
        member = await bot.get_chat_member(cfg.group_id, me.id)
        print(f"Telegram: @{me.username}; group={chat.id}; membership={member.status}")
    async with aiohttp.ClientSession() as session:
        api = Samsara(session, cfg.samsara_token, cfg.base_url)
        start = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        page = await api.stream(start, None)
        events = page["data"]
        media_count = sum(bool(x.get("media")) for x in events)
        print(f"Samsara: OK; first page events={len(events)}; with media={media_count}")
        print("Read-only check; nu a fost trimis niciun mesaj și nu s-a modificat cursorul.")
        print("Un răspuns fără media nu confirmă permisiunea Read Camera Media; verific-o în dashboard.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as exc:
        # Credential-bearing exception messages must never reach console output.
        raise SystemExit(f"Setup failed: {type(exc).__name__}. Verifică .env, permisiunile și rețeaua.") from None
