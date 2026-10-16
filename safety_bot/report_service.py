"""Durable speeding-history import and scheduled Telegram reports."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import random
import time

from aiogram.exceptions import TelegramMigrateToChat, TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

from .reports import format_report, report_windows
from .samsara import ApiError

log = logging.getLogger("safety_bot")
UTC = timezone.utc


def missed_windows(since, now, tz):
    """Enumerate each elapsed schedule boundary, including long restarts."""
    windows = {}
    checkpoint = datetime.fromtimestamp(since, UTC)
    end = datetime.fromtimestamp(now, UTC)
    while checkpoint < end:
        checkpoint = min(checkpoint + timedelta(days=1), end)
        for window in report_windows(checkpoint, tz):
            if since < window["end"] <= now:
                windows[window["key"]] = window
    return sorted(windows.values(), key=lambda x: (x["end"], x["kind"]))


class Reporting:
    def __init__(self, cfg, store, api, bot, stop):
        self.cfg, self.store, self.api, self.bot, self.stop = cfg, store, api, bot, stop

    def initialize(self):
        now = datetime.now(UTC)
        self.store.seed_speeding_history()
        with self.store.db:
            if not self.store.get("reports_started_at"):
                self.store.put("reports_started_at", str(now.timestamp()))
                self.store.put("report_schedule_until", str(now.timestamp()))
            if not self.store.get("report_history_start"):
                self.store.put("report_history_start", (now - timedelta(days=60)).isoformat())
                self.store.put("report_history_end", now.isoformat())
        log.info("Speeding reports enabled: weekly Sunday 20:00; monthly day 1 08:00; timezone=%s", self.cfg.timezone)

    async def pause(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def backfill(self):
        failures = 0
        while not self.stop.is_set() and not self.store.get("report_history_complete"):
            delay = 0
            try:
                cursor = self.store.get("report_history_cursor")
                page = await self.api.safety_history(
                    self.store.get("report_history_start"), self.store.get("report_history_end"), cursor,
                )
                pagination = page["pagination"]
                next_cursor = pagination.get("endCursor")
                if pagination["hasNextPage"] and (not next_cursor or next_cursor == cursor):
                    raise ValueError("History pagination did not advance")
                # History is an independent ledger import; no Telegram queue or
                # live stream cursor is changed, even when a page is replayed.
                records = page["data"]
                for offset in range(0, len(records), 10):
                    # Commit small batches and yield outside a transaction.
                    # A crash before saving the page cursor safely replays IDs.
                    self.store.import_speeding_history(records[offset:offset + 10])
                    await asyncio.sleep(0)
                    if self.stop.is_set():
                        return
                with self.store.db:
                    if next_cursor:
                        self.store.put("report_history_cursor", next_cursor)
                    if not pagination["hasNextPage"]:
                        self.store.put("report_history_complete", self.store.get("report_history_end"))
                failures = 0
                log.info("Speeding history page imported: records=%s more=%s", len(records), pagination["hasNextPage"])
                if not pagination["hasNextPage"]:
                    count = self.store.db.execute("SELECT COUNT(*) AS n FROM speeding_occurrences").fetchone()["n"]
                    log.info("Speeding history import complete: 60-day API window; recorded incidents=%s; historical alerts not queued", count)
                    return
                # Let live polling and delivery progress between history pages.
                delay = 0.1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(900, 30 * 2 ** min(failures - 1, 5))
                if isinstance(exc, ApiError):
                    delay = max(delay, exc.delay)
                    if exc.status == 400 and self.store.get("report_history_cursor"):
                        with self.store.db:
                            self.store.db.execute("DELETE FROM meta WHERE key='report_history_cursor'")
                code = f"HTTP{exc.status}" if isinstance(exc, ApiError) else type(exc).__name__
                log.error("Speeding history import failed (%s); retry %.0fs", code, delay)
            await self.pause(delay)

    async def deliver(self, job):
        chunks = json.loads(job["payload"]) if job["payload"] else None
        if chunks is None:
            # Wait for live polling to drain through this boundary, including
            # after an outage, before freezing the report's counts.
            caught_up = float(self.store.get("stream_caught_up_at") or 0)
            if caught_up < job["end_time"]:
                return
            window = {"kind": job["kind"], "start": job["start_time"],
                      "end": job["end_time"], "timezone": job["timezone"]}
            chunks = format_report(window, self.store.report_stats(job))
            self.store.freeze_report(job["key"], chunks)
        for part, text in enumerate(chunks):
            if self.stop.is_set():
                return
            if self.store.report_chunk_sent(job["key"], part):
                continue
            self.store.check_connection()
            message = await self.bot.send_message(
                self.cfg.group_id, text, parse_mode="HTML",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            self.store.mark_report_chunk(job["key"], part, message.message_id)
            await self.pause(3.1)
        self.store.finish_report(job["key"])
        log.info("Speeding report delivered key=%s", job["key"])

    async def worker(self):
        while not self.stop.is_set():
            if self.store.get("report_history_complete"):
                now = time.time()
                since = float(self.store.get("report_schedule_until"))
                self.store.schedule_reports(missed_windows(since, now, self.cfg.timezone), now)
                for job in self.store.due_reports():
                    if self.stop.is_set():
                        break
                    try:
                        await self.deliver(job)
                    except asyncio.CancelledError:
                        raise
                    except TelegramMigrateToChat as exc:
                        self.cfg.group_id = exc.migrate_to_chat_id
                        with self.store.db:
                            self.store.put("active_group", str(exc.migrate_to_chat_id))
                        self.store.retry_report(job["key"], 1, "GroupMigration")
                    except Exception as exc:
                        delay = min(6 * 3600, 30 * 2 ** min(job["failures"], 10)) + random.random()
                        if isinstance(exc, TelegramRetryAfter):
                            delay = max(delay, exc.retry_after)
                            await self.pause(exc.retry_after)
                        code = type(exc).__name__
                        self.store.retry_report(job["key"], delay, code)
                        log.error("Speeding report failed key=%s (%s); retry %.0fs", job["key"], code, delay)
            await self.pause(30)
