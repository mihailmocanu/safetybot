import json
from pathlib import Path
import sqlite3
import time


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

    def save_page(self, events, cursor):
        # Queue and stream cursor commit together: a crash cannot skip queued events.
        with self.db:
            for event in events:
                previous = self.db.execute("SELECT payload FROM events WHERE id=?", (event["id"],)).fetchone()
                payload = json.dumps(event, separators=(",", ":"))
                self.db.execute("""INSERT INTO events(id,payload) VALUES (?,?)
                    ON CONFLICT(id) DO UPDATE SET payload=excluded.payload""", (event["id"], payload))
                if previous is None or json.loads(previous["payload"]).get("media") != event.get("media"):
                    self.db.execute("UPDATE events SET next_attempt=0 WHERE id=?", (event["id"],))
            if cursor:
                self.put("cursor", cursor)

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
