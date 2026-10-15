import asyncio
import logging
import os
import signal

from dotenv import load_dotenv

from .app import run
from .db import Store
from .setup import main as setup


async def start():
    load_dotenv()
    if os.getenv("BOT_ENABLED", "true").lower() != "false":
        await run()
        return
    # Provision the database before API credentials are added. No Samsara/Telegram
    # calls and no cursor initialization until the user enables notifications.
    database_url = os.getenv("DATABASE_URL", "").strip()
    if os.getenv("RAILWAY_PROJECT_ID") and not database_url:
        raise ValueError("DATABASE_URL is required on Railway")
    store = Store(os.getenv("DATABASE_PATH", "data/safety.sqlite3"), database_url)
    store.close()
    action = os.getenv("SETUP_ACTION", "").strip()
    if action:
        if action not in ("group-id", "check"):
            raise ValueError("SETUP_ACTION must be group-id or check")
        await setup([action])
    logging.info("Database initialized. BOT_ENABLED=false: waiting for API configuration and activation.")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
try:
    asyncio.run(start())
except KeyboardInterrupt:
    pass
except Exception as exc:
    # Database URLs and signed URLs may appear in library exception messages.
    status = getattr(exc, "status", None)
    logging.error("Startup/runtime failed (%s%s). Check configuration and service availability.", type(exc).__name__, f" HTTP {status}" if isinstance(status, int) else "")
    raise SystemExit(1) from None
