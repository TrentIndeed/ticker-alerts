"""Small shared helpers: time windows, rate limiting, resilient loops."""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime

import httpx

from .config import ET, cfg

log = logging.getLogger(__name__)


# A Telegram bot token (digits:secret) shows up inside request URLs, so any error that quotes the URL
# (httpx puts it in HTTPStatusError messages) would otherwise write the token to the logs.
_BOT_TOKEN = re.compile(r"(?:(?<=bot)|(?<![A-Za-z0-9]))\d{6,12}:[A-Za-z0-9_-]{30,}")   # bare, or in .../bot<token>/...


def redact(text) -> str:
    """Mask bot tokens in any text headed for a log, the health table or a chat message."""
    return _BOT_TOKEN.sub("<redacted-token>", str(text))


def describe(exc: BaseException) -> str:
    """One short, safe line for an exception. httpx timeouts have an empty message, so keep the type."""
    msg = str(exc).strip()
    return redact(f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__)


class RedactingFormatter(logging.Formatter):
    """Formats as usual, then masks tokens in the whole record (message and traceback)."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def now_et() -> datetime:
    return datetime.now(ET)


def in_alert_window(when: datetime | None = None) -> bool:
    """True if we should be sending alerts right now."""
    t = when or now_et()
    if not cfg.alert_weekends and t.weekday() >= 5:
        return False
    return cfg.alert_start_hour <= t.hour < cfg.alert_end_hour


def is_market_hours(when: datetime | None = None) -> bool:
    """Regular session, used only to decide how loud the watchdog should be."""
    t = when or now_et()
    if t.weekday() >= 5:
        return False
    minutes = t.hour * 60 + t.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


def fmt_et(ts: float) -> str:
    # Built by hand rather than with "%-I": the no-pad flag is a glibc
    # extension that raises ValueError on Windows and musl.
    dt = datetime.fromtimestamp(ts, ET)
    return f"{dt.hour % 12 or 12}:{dt:%M:%S %p} ET"


def fmt_et_full(ts: float) -> str:
    dt = datetime.fromtimestamp(ts, ET)
    return f"{dt.hour % 12 or 12}:{dt:%M:%S %p} ET, {dt:%a %b %d}"


def ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s ago"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


class RateLimiter:
    """Simple async token bucket.

    The SEC publishes a hard ceiling of 10 requests/second per client. We run
    at 7/s so that a burst from several pollers at once still can't trip it —
    getting blocked by EDGAR is the single worst failure mode this system has.
    """

    def __init__(self, rate_per_sec: float, burst: int | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = burst if burst is not None else max(1, int(rate_per_sec))
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(
                    self.capacity, self._tokens + (now - self._updated) * self.rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self._tokens) / self.rate)


sec_limiter = RateLimiter(rate_per_sec=7.0, burst=7)


async def resilient_loop(name: str, interval: float, fn, *, jitter: float = 0.15):
    """Run `fn` forever on an interval. Never let an exception kill the task.

    Backs off exponentially on repeated failure so a broken upstream doesn't
    turn into a request flood, and recovers immediately once it works again.
    """
    from .store import store  # local import avoids a circular import at module load

    failures = 0
    while True:
        started = time.monotonic()
        try:
            await fn()
            if failures:
                log.info("%s recovered after %d failure(s)", name, failures)
            failures = 0
            store.mark_health(name, ok=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            # A timeout or dropped connection is routine (EDGAR times out a few times a day and the next poll
            # recovers): one line, no traceback. Anything unexpected still gets its traceback, once.
            transient = isinstance(exc, httpx.TransportError)
            log.warning("%s failed (%d): %s", name, failures, describe(exc), exc_info=failures == 1 and not transient)
            store.mark_health(name, ok=False, err=describe(exc))

        delay = interval
        if failures:
            delay = min(interval * (2 ** min(failures, 5)), 600)
        delay *= 1.0 + random.uniform(-jitter, jitter)
        elapsed = time.monotonic() - started
        await asyncio.sleep(max(1.0, delay - elapsed))


def chunked(seq, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]
