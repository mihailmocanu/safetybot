import json
from pathlib import Path
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid5


class PostgresConnection:
    """Small adapter: shared SQL uses ? parameters; PostgreSQL uses %s.

    Autocommit keeps reads out of idle transactions. The explicit contexts below
    are transactions, not psycopg connection contexts (which close on exit).
    A session lock prevents overlapping Railway deployments from running two bots.
    Network loss is allowed to fail the process, so Railway can restart it safely.
    """

    def __init__(self, url):
        import psycopg
        from psycopg.rows import dict_row

        self.connection = psycopg.connect(
            url, autocommit=True, row_factory=dict_row, connect_timeout=10,
            options="-c statement_timeout=15000 -c lock_timeout=10000",
        )
        locked = self.connection.execute(
            "SELECT pg_try_advisory_lock(1935764833, 1) AS acquired"
        ).fetchone()["acquired"]
        if not locked:
            self.connection.close()
            raise RuntimeError("Another bot instance is already using this PostgreSQL database")
        self.transactions = []

    def execute(self, query, params=()):
        return self.connection.execute(query.replace("?", "%s"), params)

    def __enter__(self):
        transaction = self.connection.transaction()
        transaction.__enter__()
        self.transactions.append(transaction)
        return self

    def __exit__(self, *args):
        return self.transactions.pop().__exit__(*args)

    def close(self):
        self.connection.close()


class Store:
    def __init__(self, path="data/safety.sqlite3", database_url=""):
        self.postgres = bool(database_url)
        if self.postgres:
            self.db = PostgresConnection(database_url)
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(path)
            self.db.row_factory = sqlite3.Row
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                PRAGMA busy_timeout=5000;
            """)
        schema = """
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS events(
                id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                message_id BIGINT, next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0, last_error TEXT);
            CREATE TABLE IF NOT EXISTS videos(
                event_id TEXT NOT NULL, input TEXT NOT NULL, message_id BIGINT NOT NULL,
                PRIMARY KEY(event_id,input));
            CREATE INDEX IF NOT EXISTS due_events ON events(next_attempt);
            CREATE TABLE IF NOT EXISTS speed_state(
                asset_id TEXT PRIMARY KEY, last_sample_at DOUBLE PRECISION NOT NULL,
                episode_id TEXT);
            CREATE TABLE IF NOT EXISTS speed_episodes(
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
                started_at DOUBLE PRECISION NOT NULL, ended_at DOUBLE PRECISION);
            CREATE INDEX IF NOT EXISTS speed_episode_asset ON speed_episodes(asset_id, started_at);
            CREATE TABLE IF NOT EXISTS speed_media(
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL,
                started_at DOUBLE PRECISION NOT NULL, ended_at DOUBLE PRECISION NOT NULL,
                payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS speed_media_asset ON speed_media(asset_id, started_at);
            CREATE TABLE IF NOT EXISTS speeding_occurrences(
                id TEXT PRIMARY KEY, asset_id TEXT NOT NULL, truck_name TEXT NOT NULL,
                occurred_at DOUBLE PRECISION NOT NULL, source TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS speeding_occurrence_window ON speeding_occurrences(asset_id, occurred_at);
            CREATE TABLE IF NOT EXISTS speeding_aliases(
                source_id TEXT PRIMARY KEY, occurrence_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS alert_speed_counts(
                event_id TEXT PRIMARY KEY, count INTEGER NOT NULL, as_of DOUBLE PRECISION NOT NULL);
            CREATE TABLE IF NOT EXISTS report_jobs(
                key TEXT PRIMARY KEY, kind TEXT NOT NULL, start_time DOUBLE PRECISION NOT NULL,
                end_time DOUBLE PRECISION NOT NULL, previous_start DOUBLE PRECISION NOT NULL,
                rolling_start DOUBLE PRECISION NOT NULL, timezone TEXT NOT NULL,
                payload TEXT, sent_at DOUBLE PRECISION, next_attempt DOUBLE PRECISION NOT NULL DEFAULT 0,
                failures INTEGER NOT NULL DEFAULT 0, last_error TEXT);
            CREATE TABLE IF NOT EXISTS report_chunks(
                job_key TEXT NOT NULL, part INTEGER NOT NULL, message_id BIGINT NOT NULL,
                PRIMARY KEY(job_key,part));
        """
        with self.db:
            for statement in schema.split(";"):
                if statement.strip():
                    self.db.execute(statement)

    def get(self, key):
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def put(self, key, value):
        self.db.execute("INSERT INTO meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def _save_event(self, event):
        self._record_occurrence(event)
        previous = self.db.execute("SELECT payload FROM events WHERE id=?", (event["id"],)).fetchone()
        payload = json.dumps(event, separators=(",", ":"))
        self.db.execute("""INSERT INTO events(id,payload) VALUES (?,?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""", (event["id"], payload))
        if previous is None or json.loads(previous["payload"]).get("media") != event.get("media"):
            self.db.execute("UPDATE events SET next_attempt=0 WHERE id=?", (event["id"],))

    def save_page(self, events, cursor, live_speeding=False):
        # Queue and stream cursor commit together: a crash cannot skip queued events.
        with self.db:
            for event in events:
                from .speeding import speeding_only
                if live_speeding and speeding_only(event) and self._cache_speed_media(event):
                    continue
                self._save_event(event)
            if cursor:
                self.put("cursor", cursor)

    def _cache_speed_media(self, event):
        from .events import parse_time
        asset_id = (event.get("asset") or {}).get("id")
        try:
            start = parse_time(event["startMs"]).timestamp()
            end = parse_time(event.get("endMs") or event["startMs"]).timestamp()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return False
        if not asset_id or end < start:
            return False
        self.db.execute("""INSERT INTO speed_media VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, ended_at=excluded.ended_at""",
            (event["id"], asset_id, start, end, json.dumps(event)))
        return self._attach_speed_media(event, asset_id, start, end)

    def _attach_speed_media(self, event, asset_id, start, end):
        episode = self.db.execute("""SELECT id FROM speed_episodes
            WHERE asset_id=? AND started_at<=? AND (ended_at IS NULL OR ended_at>=?)
            ORDER BY ABS(started_at-?) LIMIT 1""", (asset_id, end + 120, start - 120, start)).fetchone()
        if not episode:
            return False
        self._record_occurrence(event, canonical_hint=episode["id"])
        current = self.db.execute("SELECT payload FROM events WHERE id=?", (episode["id"],)).fetchone()
        if current:
            pending = json.loads(current["payload"])
            previous_official = pending.get("_samsaraEventId")
            if previous_official and previous_official != event["id"]:
                # A continuous live episode may span several official events.
                # Keep its first usable clip source; an unrelated empty update
                # must not replace a video waiting to be delivered.
                if pending.get("media") or not event.get("media"):
                    return True
                if self.db.execute("SELECT 1 FROM videos WHERE event_id=? LIMIT 1", (episode["id"],)).fetchone():
                    return True
        # Preserve the original live alert's ID so late camera footage is a reply
        # to that alert, rather than a second severity-labelled notification.
        merged = dict(event, id=episode["id"], _source="live-speeding", _samsaraEventId=event["id"])
        self._save_event(merged)
        return True

    def record_speed_samples(self, samples, assets):
        created = 0
        with self.db:
            for sample in samples:
                asset_id, stamp = sample["asset_id"], sample["timestamp"]
                asset = assets.get(asset_id)
                if not asset or not asset.get("name"):
                    continue
                state = self.db.execute("SELECT * FROM speed_state WHERE asset_id=?", (asset_id,)).fetchone()
                if state and stamp <= state["last_sample_at"]:
                    continue
                episode_id = state["episode_id"] if state else None
                if sample["over"] and not episode_id:
                    # Adopt an already queued official event covering this sample,
                    # including its sent-message receipt. That event may arrive
                    # before the live poll, or during a readings outage.
                    prior = self.db.execute("""SELECT m.id, m.payload FROM speed_media m
                        JOIN events q ON q.id=m.id LEFT JOIN speed_episodes e ON e.id=m.id
                        WHERE m.asset_id=? AND m.started_at<=? AND m.ended_at>=? AND e.id IS NULL
                        ORDER BY ABS(m.started_at-?) LIMIT 1""", (asset_id, stamp + 120, stamp - 120, stamp)).fetchone()
                    episode_id = prior["id"] if prior else str(uuid5(NAMESPACE_URL, f"samsara-speeding:{asset_id}:{sample['time']}"))
                    self.db.execute("INSERT INTO speed_episodes VALUES (?,?,?,NULL)", (episode_id, asset_id, stamp))
                    event = {
                        "id": episode_id, "_source": "live-speeding", "asset": asset,
                        "startMs": sample["time"], "behaviorLabels": [{"label": "Speeding"}],
                        "speedingMetadata": {
                            "maxSpeedKilometersPerHour": sample["speed_kph"],
                            "postedSpeedLimitKilometersPerHour": sample["limit_kph"],
                        }, "media": [],
                    }
                    if sample.get("location"):
                        event["_location"] = sample["location"]
                    if prior:
                        event = dict(json.loads(prior["payload"]), _source="live-speeding", _samsaraEventId=prior["id"])
                    self._save_event(event)
                    created += 1
                    # An official camera event can arrive before the next live poll.
                    cached = self.db.execute("SELECT * FROM speed_media WHERE asset_id=? AND ended_at>=?", (asset_id, stamp - 120)).fetchall()
                    for media in cached:
                        self._attach_speed_media(json.loads(media["payload"]), asset_id, media["started_at"], media["ended_at"])
                elif not sample["over"] and episode_id:
                    self.db.execute("UPDATE speed_episodes SET ended_at=? WHERE id=?", (stamp, episode_id))
                    episode_id = None
                self.db.execute("""INSERT INTO speed_state VALUES (?,?,?)
                    ON CONFLICT(asset_id) DO UPDATE SET last_sample_at=excluded.last_sample_at,
                    episode_id=excluded.episode_id""", (asset_id, stamp, episode_id))
        return created

    def _record_occurrence(self, event, canonical_hint=None):
        from .events import CATEGORIES, labels, parse_time
        if not CATEGORIES["Speeding"].intersection(labels(event)):
            return
        asset = event.get("asset") or {}
        if not asset.get("id") or not event.get("id"):
            return
        try:
            stamp = parse_time(event["startMs"]).timestamp()
        except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return
        aliases = list(dict.fromkeys(x for x in (canonical_hint, event["id"], event.get("_samsaraEventId")) if x))
        existing = []
        for alias in aliases:
            row = self.db.execute("SELECT occurrence_id FROM speeding_aliases WHERE source_id=?", (alias,)).fetchone()
            if row:
                existing.append(row["occurrence_id"])
        canonical = existing[0] if existing else (canonical_hint or event["id"])
        self.db.execute("""INSERT INTO speeding_occurrences VALUES (?,?,?,?,?)
            ON CONFLICT(id) DO NOTHING""", (canonical, str(asset["id"]), str(asset.get("name") or ""), stamp, event.get("_source") or "samsara"))
        # Media updates cannot move an occurrence into a different report period.
        if asset.get("name"):
            self.db.execute("UPDATE speeding_occurrences SET truck_name=? WHERE id=?", (str(asset["name"]), canonical))
        for other in set(existing) - {canonical}:
            self.db.execute("UPDATE speeding_aliases SET occurrence_id=? WHERE occurrence_id=?", (canonical, other))
            self.db.execute("DELETE FROM speeding_occurrences WHERE id=?", (other,))
        for alias in aliases:
            self.db.execute("""INSERT INTO speeding_aliases VALUES (?,?)
                ON CONFLICT(source_id) DO UPDATE SET occurrence_id=excluded.occurrence_id""", (alias, canonical))

    def seed_speeding_history(self):
        # Upgrade existing delivery history without enqueueing or resending alerts.
        with self.db:
            rows = self.db.execute("SELECT payload FROM events").fetchall()
            for row in rows:
                self._record_occurrence(json.loads(row["payload"]))

    def import_speeding_history(self, events, cursor=None):
        from .events import parse_time
        from .speeding import speeding_only

        with self.db:
            for event in events:
                canonical_hint = None
                if speeding_only(event):
                    asset_id = (event.get("asset") or {}).get("id")
                    try:
                        start = parse_time(event["startMs"]).timestamp()
                        end = parse_time(event.get("endMs") or event["startMs"]).timestamp()
                    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                        pass
                    else:
                        if asset_id and end >= start:
                            episode = self.db.execute("""SELECT id FROM speed_episodes
                                WHERE asset_id=? AND started_at<=? AND (ended_at IS NULL OR ended_at>=?)
                                ORDER BY ABS(started_at-?) LIMIT 1""", (asset_id, end + 120, start - 120, start)).fetchone()
                            if episode:
                                canonical_hint = episode["id"]
                # Historical media must neither enqueue another alert nor alter
                # the live episode's pending video; only link its ledger alias.
                self._record_occurrence(event, canonical_hint=canonical_hint)
            if cursor:
                self.put("report_history_cursor", cursor)

    def alert_speeding_count(self, event_id, as_of=None):
        if not self.get("report_history_complete"):
            return None
        with self.db:
            previous = self.db.execute("SELECT count FROM alert_speed_counts WHERE event_id=?", (event_id,)).fetchone()
            if previous:
                return previous["count"]
            occurrence = self.db.execute("""SELECT o.* FROM speeding_occurrences o
                JOIN speeding_aliases a ON a.occurrence_id=o.id WHERE a.source_id=?""", (event_id,)).fetchone()
            if not occurrence:
                return None
            now = time.time() if as_of is None else as_of
            if not now - 30 * 86400 <= occurrence["occurred_at"] <= now:
                return None
            count = self.db.execute("""SELECT COUNT(*) AS n FROM speeding_occurrences
                WHERE asset_id=? AND occurred_at>=? AND occurred_at<=?""", (occurrence["asset_id"], now - 30 * 86400, now)).fetchone()["n"]
            self.db.execute("INSERT INTO alert_speed_counts VALUES (?,?,?)", (event_id, count, now))
            return count

    def schedule_reports(self, windows, through_time):
        with self.db:
            for item in windows:
                self.db.execute("""INSERT INTO report_jobs
                    (key,kind,start_time,end_time,previous_start,rolling_start,timezone)
                    VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO NOTHING""", (
                        item["key"], item["kind"], item["start"], item["end"],
                        item["previous_start"], item["rolling_start"], item["timezone"]))
            self.put("report_schedule_until", str(through_time))

    def due_reports(self):
        return self.db.execute("""SELECT * FROM report_jobs WHERE sent_at IS NULL
            AND next_attempt<=? ORDER BY end_time,kind LIMIT 5""", (time.time(),)).fetchall()

    def report_stats(self, job):
        def period(start, end):
            return self.db.execute("""SELECT asset_id, MAX(truck_name) AS truck_name, COUNT(*) AS n
                FROM speeding_occurrences WHERE occurred_at>=? AND occurred_at<? GROUP BY asset_id""", (start, end)).fetchall()
        current = period(job["start_time"], job["end_time"])
        previous = {x["asset_id"]: x["n"] for x in period(job["previous_start"], job["start_time"])}
        rolling = {x["asset_id"]: x["n"] for x in period(job["rolling_start"], job["end_time"])}
        ranking = [{"truck_name": x["truck_name"] or f"Asset {x['asset_id']}", "count": x["n"],
                    "past30": rolling.get(x["asset_id"], 0), "previous_count": previous.get(x["asset_id"], 0),
                    "asset_id": x["asset_id"]} for x in current]
        ranking.sort(key=lambda x: (-x["count"], x["truck_name"], x["asset_id"]))
        return {"total": sum(x["n"] for x in current), "trucks": len(current),
                "previous_total": sum(previous.values()), "rolling_total": sum(rolling.values()), "ranking": ranking[:10]}

    def freeze_report(self, key, chunks):
        with self.db:
            self.db.execute("UPDATE report_jobs SET payload=? WHERE key=? AND payload IS NULL", (json.dumps(chunks), key))

    def report_chunk_sent(self, key, part):
        return self.db.execute("SELECT 1 FROM report_chunks WHERE job_key=? AND part=?", (key, part)).fetchone() is not None

    def mark_report_chunk(self, key, part, message_id):
        with self.db:
            self.db.execute("INSERT INTO report_chunks VALUES (?,?,?)", (key, part, message_id))

    def finish_report(self, key):
        with self.db:
            self.db.execute("UPDATE report_jobs SET sent_at=?, last_error=NULL WHERE key=?", (time.time(), key))

    def retry_report(self, key, delay, error):
        with self.db:
            self.db.execute("UPDATE report_jobs SET next_attempt=?, failures=failures+1,last_error=? WHERE key=?", (time.time()+delay, error, key))

    def due(self, limit=20):
        return self.db.execute("SELECT * FROM events WHERE next_attempt<=? ORDER BY next_attempt,id LIMIT ?", (time.time(), limit)).fetchall()

    def sent_text(self, event_id, message_id):
        with self.db:
            self.db.execute("UPDATE events SET message_id=? WHERE id=?", (message_id, event_id))

    def video_sent(self, event_id, camera):
        return self.db.execute("SELECT 1 FROM videos WHERE event_id=? AND input=?", (event_id, camera)).fetchone() is not None

    def sent_video(self, event_id, camera, message_id):
        with self.db:
            self.db.execute("INSERT INTO videos VALUES (?,?,?)", (event_id, camera, message_id))

    def schedule(self, event_id, delay, error=None):
        with self.db:
            self.db.execute("UPDATE events SET next_attempt=?, failures=CASE WHEN CAST(? AS TEXT) IS NULL THEN 0 ELSE failures+1 END, last_error=? WHERE id=?", (time.time() + delay, error, error, event_id))

    def check_connection(self):
        # For PostgreSQL this also verifies that the singleton-lock session lives.
        self.db.execute("SELECT 1").fetchone()

    def close(self):
        self.db.close()
