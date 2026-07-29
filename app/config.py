"""Configuration loaded from environment / .env.

Every setting has a safe default except the ones that genuinely cannot be
guessed (Telegram token, chat IDs, SEC user agent). Those are validated at
startup so the container fails loudly instead of running half-blind.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

ET = ZoneInfo("America/New_York")


def _s(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(_s(key) or default)
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(_s(key) or default)
    except ValueError:
        return default


def _b(key: str, default: bool) -> bool:
    raw = _s(key).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _list(key: str, default: str = "") -> list[str]:
    raw = _s(key) or default
    return [p.strip() for p in raw.split(",") if p.strip()]


class ConfigError(RuntimeError):
    pass


@dataclass
class Config:
    telegram_token: str = field(default_factory=lambda: _s("TELEGRAM_BOT_TOKEN"))
    telegram_chat_ids: list[str] = field(
        default_factory=lambda: _list("TELEGRAM_CHAT_IDS")
    )
    sec_user_agent: str = field(default_factory=lambda: _s("SEC_USER_AGENT"))

    alpaca_key: str = field(default_factory=lambda: _s("ALPACA_API_KEY"))
    alpaca_secret: str = field(default_factory=lambda: _s("ALPACA_API_SECRET"))

    alert_start_hour: int = field(default_factory=lambda: _i("ALERT_START_HOUR", 4))
    alert_end_hour: int = field(default_factory=lambda: _i("ALERT_END_HOUR", 20))
    alert_weekends: bool = field(default_factory=lambda: _b("ALERT_WEEKENDS", True))

    sec_global_poll: int = field(default_factory=lambda: _i("SEC_GLOBAL_POLL_SEC", 8))
    sec_form_poll: int = field(default_factory=lambda: _i("SEC_FORM_POLL_SEC", 15))
    sec_submissions_poll: int = field(
        default_factory=lambda: _i("SEC_SUBMISSIONS_POLL_SEC", 30)
    )
    sec_wide_net: bool = field(default_factory=lambda: _b("SEC_WIDE_NET", False))
    rss_poll: int = field(default_factory=lambda: _i("RSS_POLL_SEC", 45))
    movers_poll: int = field(default_factory=lambda: _i("MOVERS_POLL_SEC", 300))

    movers_enabled: bool = field(default_factory=lambda: _b("MOVERS_ENABLED", True))
    movers_max: int = field(default_factory=lambda: _i("MOVERS_MAX_TICKERS", 25))
    movers_min_price: float = field(
        default_factory=lambda: _f("MOVERS_MIN_PRICE", 0.40)
    )
    movers_max_price: float = field(default_factory=lambda: _f("MOVERS_MAX_PRICE", 60.0))
    movers_min_pct: float = field(default_factory=lambda: _f("MOVERS_MIN_PCT", 6.0))
    movers_min_volume: int = field(
        default_factory=lambda: _i("MOVERS_MIN_VOLUME", 400_000)
    )

    sector_news_enabled: bool = field(
        default_factory=lambda: _b("SECTOR_NEWS_ENABLED", True)
    )
    sector_news_queries: list[str] = field(
        default_factory=lambda: _list(
            "SECTOR_NEWS_QUERIES",
            "NASDAQ AI stocks,artificial intelligence chip stocks,"
            "AI semiconductor news,small cap tech offering",
        )
    )

    filter_opinion: bool = field(default_factory=lambda: _b("FILTER_OPINION", True))
    dedupe_window: int = field(default_factory=lambda: _i("DEDUPE_WINDOW_SEC", 86_400))
    watchdog_stale: int = field(default_factory=lambda: _i("WATCHDOG_STALE_SEC", 420))
    daily_summary_hour: int = field(
        default_factory=lambda: _i("DAILY_SUMMARY_HOUR", 20)
    )

    db_path: str = field(default_factory=lambda: _s("DB_PATH", "/app/data/alerts.db"))
    log_level: str = field(default_factory=lambda: _s("LOG_LEVEL", "INFO").upper())
    health_port: int = field(default_factory=lambda: _i("HEALTH_PORT", 8080))

    def validate(self) -> None:
        missing = []
        if not self.telegram_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.telegram_chat_ids:
            missing.append("TELEGRAM_CHAT_IDS")
        if not self.sec_user_agent or "@" not in self.sec_user_agent:
            missing.append("SEC_USER_AGENT (must be 'Name email@domain.com')")
        if missing:
            raise ConfigError(
                "Missing required settings in .env: "
                + ", ".join(missing)
                + "\nSee .env.example for how to get each one."
            )

    @property
    def alpaca_enabled(self) -> bool:
        return bool(self.alpaca_key and self.alpaca_secret)

    # Poll intervals are clamped so a typo in .env can't get us rate-limited
    # into a ban by the SEC.
    def __post_init__(self) -> None:
        self.sec_global_poll = max(5, self.sec_global_poll)
        self.sec_form_poll = max(5, self.sec_form_poll)
        self.sec_submissions_poll = max(10, self.sec_submissions_poll)
        self.rss_poll = max(20, self.rss_poll)
        self.movers_poll = max(60, self.movers_poll)


cfg = Config()
