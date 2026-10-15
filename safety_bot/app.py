import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import random
import signal
import tempfile

import aiohttp
from aiogram import Bot
from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter
from aiogram.types import FSInputFile, LinkPreviewOptions, ReplyParameters

from .config import Config
from .db import Store
from .events import MONITORED, labels, media_items, notification, selected
from .samsara import ApiError, Samsara, telegram_size
from .speeding import speed_samples
from .webhook import start_webhook

log = logging.getLogger("safety_bot")


class Application:
    def __init__(self, cfg, store, api, bot):
        self.cfg, self.store, self.api, self.bot = cfg, store, api, bot
        self.wake = asyncio.Event()
        self.stop = asyncio.Event()
        self.speed_assets = {}
        self.speed_assets_loaded = 0

    async def poll(self):
        failures = 0
        while not self.stop.is_set():
            self.wake.clear()
            delay = self.cfg.interval
            try:
                # Fairness: page limit per pass; cursor persists every page.
                for _ in range(100):
                    old_cursor = self.store.get("cursor")
                    page = await self.api.stream(self.store.get("start_time"), old_cursor)
                    events = page["data"]
                    for event in events:
                        unknown = set(labels(event)) - MONITORED - {"Invalid", "DefensiveDriving", "Idling", "OperationalEvent", "ContextConstructionOrWorkZone", "ContextSnowyOrIcy", "ContextVulnerableRoadUser", "ContextWet"}
                        if unknown:
                            log.warning("Unmapped behavior labels: %s", ",".join(sorted(unknown)))
                    pagination = page["pagination"]
                    cursor = pagination.get("endCursor")
                    if pagination["hasNextPage"] and (not cursor or cursor == old_cursor):
                        raise ValueError("Pagination did not advance")
                    self.store.save_page([x for x in events if selected(x)], cursor, live_speeding=self.cfg.live_speeding)
                    if not pagination["hasNextPage"]:
                        break
                    if self.stop.is_set():
                        break
                else:
                    delay = 1
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(900, self.cfg.interval * 2 ** min(failures, 4)) + random.random()
                if isinstance(exc, ApiError):
                    delay = max(delay, exc.delay)
                    if exc.status == 400 and self.store.get("cursor"):
                        # Invalid/expired cursor: replay from the durable original start.
                        # Previously delivered IDs still prevent routine duplicates.
                        with self.store.db:
                            self.store.db.execute("DELETE FROM meta WHERE key='cursor'")
                    log.error("Polling Samsara HTTP %s; retry %.0fs", exc.status, delay)
                else:
                    log.error("Polling failed (%s); retry %.0fs", type(exc).__name__, delay)
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def deliver(self, row):
        event_id = row["id"]
        event = json.loads(row["payload"])
        text_id = row["message_id"]
        if text_id is None:
            self.store.check_connection()
            message = await self.bot.send_message(
                self.cfg.group_id, notification(event, self.cfg.company, self.cfg.timezone),
                parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            text_id = message.message_id
            self.store.sent_text(event_id, text_id)
            log.info("Alert delivered event=%s", event_id)
            await asyncio.sleep(1.1)
        # Refresh every attempt to obtain current signed URLs and late media.
        official_id = event.get("_samsaraEventId") if event.get("_source") == "live-speeding" else event_id
        fresh = await self.api.event(official_id) if official_id else None
        if fresh:
            if event.get("_source") == "live-speeding":
                fresh = dict(fresh, id=event_id, _source="live-speeding", _samsaraEventId=official_id)
            event = fresh
            self.store.save_page([event], None)
        for item in media_items(event):
            camera = item["input"]
            if self.store.video_sent(event_id, camera):
                continue
            with tempfile.TemporaryDirectory(prefix="samsara-") as temporary:
                path = Path(temporary) / "event.mp4"
                await self.api.download(item["url"], path)
                path = await telegram_size(path)
                self.store.check_connection()
                message = await self.bot.send_video(
                    self.cfg.group_id, FSInputFile(path), supports_streaming=True,
                    caption=notification(event, self.cfg.company, self.cfg.timezone, video=True),
                    parse_mode="HTML",
                    reply_parameters=ReplyParameters(message_id=text_id, allow_sending_without_reply=True),
                    request_timeout=180,
                )
                self.store.sent_video(event_id, camera, message.message_id)
                log.info("Video delivered event=%s input=%s", event_id, camera)
            await asyncio.sleep(3.1)
        # No permanent 'no video' conclusion: late/offline camera uploads remain eligible.
        # After success check sparse updates; stream changes wake the row immediately.
        self.store.schedule(event_id, 6 * 3600 if media_items(event) else 15 * 60)

    async def poll_speeding(self):
        import time
        failures = 0
        while not self.stop.is_set():
            delay = self.cfg.interval
            try:
                if not self.speed_assets or time.monotonic() - self.speed_assets_loaded > 3600:
                    self.speed_assets = {str(x["id"]): x for x in await self.api.vehicles()}
                    self.speed_assets_loaded = time.monotonic()
                readings = await self.api.speed_readings()
                samples = speed_samples(readings)
                created = self.store.record_speed_samples(samples, self.speed_assets)
                log.info("Speeding check: fresh vehicles=%s new alerts=%s", len(samples), created)
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(900, self.cfg.interval * 2 ** min(failures, 4))
                if isinstance(exc, ApiError):
                    delay = max(delay, exc.delay)
                log.error("Live speeding check failed (%s%s); retry %ss", type(exc).__name__, f" HTTP {exc.status}" if isinstance(exc, ApiError) else "", delay)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def worker(self):
        while not self.stop.is_set():
            rows = self.store.due()
            for row in rows:
                if self.stop.is_set():
                    break
                try:
                    await self.deliver(row)
                except asyncio.CancelledError:
                    raise
                except TelegramMigrateToChat as exc:
                    # Persist supergroup migration across container restarts.
                    self.cfg.group_id = exc.migrate_to_chat_id
                    with self.store.db:
                        self.store.put("active_group", str(exc.migrate_to_chat_id))
                    self.store.schedule(row["id"], 1)
                    log.warning("Group migrated to supergroup; active ID saved in database")
                except Exception as exc:
                    delay = min(6 * 3600, 30 * 2 ** min(row["failures"], 10)) + random.uniform(0, 5)
                    if isinstance(exc, TelegramRetryAfter):
                        delay = max(delay, exc.retry_after)
                        # Telegram's rate limit may cover the entire group/bot.
                        await asyncio.sleep(exc.retry_after)
                    if isinstance(exc, ApiError):
                        delay = max(delay, exc.delay)
                    # Never log exception text: it can contain tokens or signed URLs.
                    code = f"HTTP{exc.status}" if isinstance(exc, ApiError) else type(exc).__name__
                    self.store.schedule(row["id"], delay, code)
                    log.error("Delivery event=%s failed (%s); retry %.0fs", row["id"], code, delay)
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=2)
            except TimeoutError:
                pass


async def run():
    cfg = Config.load()
    store = Store(cfg.db_path, cfg.database_url)
    with store.db:
        configured_group = store.get("configured_group")
        if configured_group and configured_group != str(cfg.group_id):
            raise ValueError("Group differs from database. Use a separate database for a new destination.")
        store.put("configured_group", str(cfg.group_id))
        cfg.group_id = int(store.get("active_group") or cfg.group_id)
        if not store.get("start_time"):
            store.put("start_time", (datetime.now(timezone.utc) - timedelta(hours=cfg.lookback_hours)).isoformat())
    bot = Bot(cfg.telegram_token)
    runner = None
    try:
        async with aiohttp.ClientSession() as session:
            api = Samsara(session, cfg.samsara_token, cfg.base_url)
            app = Application(cfg, store, api, bot)
            if cfg.speeding_api_check:
                # Read-only scope/data probe; other safety monitoring keeps running.
                try:
                    readings = await api.speed_readings()
                    log.info("Speeding API probe: Read Readings OK; readings=%s fresh pairs=%s", len(readings), len(speed_samples(readings)))
                except Exception as exc:
                    log.error("Speeding API probe: Read Readings failed (%s%s)", type(exc).__name__, f" HTTP {exc.status}" if isinstance(exc, ApiError) else "")
                try:
                    assets = await api.vehicles()
                    log.info("Speeding API probe: Read Assets OK; vehicles=%s", len(assets))
                except Exception as exc:
                    log.error("Speeding API probe: Read Assets failed (%s%s)", type(exc).__name__, f" HTTP {exc.status}" if isinstance(exc, ApiError) else "")
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, lambda: (app.stop.set(), app.wake.set()))
                except NotImplementedError:
                    pass
            if cfg.webhook_secret:
                runner = await start_webhook(cfg.webhook_secret, cfg.webhook_port, app.wake)
            log.info("Started; timezone=%s poll=%ss webhook=%s live_speeding=%s", cfg.timezone, cfg.interval, bool(cfg.webhook_secret), cfg.live_speeding)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(app.poll())
                tasks.create_task(app.worker())
                if cfg.live_speeding:
                    tasks.create_task(app.poll_speeding())
    finally:
        if runner:
            await runner.cleanup()
        await bot.session.close()
        store.close()
