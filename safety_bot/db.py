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
