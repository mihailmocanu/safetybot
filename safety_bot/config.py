import base64
import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass
class Config:
    samsara_token: str = field(repr=False)
    telegram_token: str = field(repr=False)
    group_id: int
    interval: int = 60
    company: str = "IDS Transportation"
    timezone: str = "America/New_York"
    db_path: str = "data/safety.sqlite3"
    database_url: str = field(default="", repr=False)
    lookback_hours: int = 24
    base_url: str = "https://api.samsara.com"
    webhook_secret: bytes = field(default=b"", repr=False)
    webhook_port: int = 8080
    live_speeding: bool = False
    speeding_api_check: bool = False

    @classmethod
    def load(cls):
        load_dotenv()
        for key in ("SAMSARA_API_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM_GROUP_ID"):
            if not os.getenv(key, "").strip():
                raise ValueError(f"Configurează {key} în .env")
        cfg = cls(
            samsara_token=os.environ["SAMSARA_API_TOKEN"].strip(),
            telegram_token=os.environ["TELEGRAM_BOT_TOKEN"].strip(),
            group_id=int(os.environ["TELEGRAM_GROUP_ID"]),
            interval=int(os.getenv("POLL_INTERVAL_SECONDS", "60")),
            company=os.getenv("COMPANY_NAME", "IDS Transportation"),
            timezone=os.getenv("TIMEZONE", "America/New_York"),
            db_path=os.getenv("DATABASE_PATH", "data/safety.sqlite3"),
            database_url=os.getenv("DATABASE_URL", "").strip(),
            lookback_hours=int(os.getenv("INITIAL_LOOKBACK_HOURS", "24")),
            base_url=os.getenv("SAMSARA_BASE_URL", "https://api.samsara.com").rstrip("/"),
            webhook_secret=base64.b64decode(os.getenv("SAMSARA_WEBHOOK_SECRET", ""), validate=True),
            webhook_port=int(os.getenv("PORT", os.getenv("WEBHOOK_PORT", "8080"))),
            live_speeding=os.getenv("LIVE_SPEEDING_ENABLED", "false").lower() == "true",
            speeding_api_check=os.getenv("SPEEDING_API_CHECK", "false").lower() == "true",
        )
        ZoneInfo(cfg.timezone)
        if cfg.interval < 1 or cfg.lookback_hours < 0:
            raise ValueError("Intervalul trebuie să fie pozitiv și lookback >= 0")
        if cfg.group_id >= 0:
            raise ValueError("TELEGRAM_GROUP_ID trebuie să fie ID-ul negativ al grupului")
        if os.getenv("RAILWAY_PROJECT_ID") and not cfg.database_url:
            raise ValueError("Pe Railway configurează DATABASE_URL către PostgreSQL pentru persistență")
        if cfg.database_url and not cfg.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("DATABASE_URL trebuie să fie un URL PostgreSQL")
        if cfg.base_url not in ("https://api.samsara.com", "https://api.eu.samsara.com"):
            raise ValueError("SAMSARA_BASE_URL trebuie să fie endpointul oficial US sau EU")
        return cfg
