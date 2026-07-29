"""Alert model, priority queue, formatting and dispatch.

Sources never talk to Telegram directly — they push an Alert onto a priority
queue and move on. That keeps a slow or rate-limited Telegram API from ever
adding latency to the SEC pollers, and it means a burst of 40 filings at 4:05pm
drains in severity order: dilution first, routine filings last.
"""

from __future__ import annotations

import asyncio
import html
import itertools
import logging
import time
from dataclasses import dataclass, field

from .classify import CRITICAL, HIGH, INFO, SEV_EMOJI, SEV_ORDER
from .store import store
from .util import fmt_et, in_alert_window

log = logging.getLogger(__name__)

_counter = itertools.count()

KIND_LABEL = {
    "filing": "SEC FILING",
    "news": "NEWS",
    "sector": "SECTOR",
    "mover": "MOVER",
    "system": "SYSTEM",
}


@dataclass(order=True)
class Alert:
    sort_key: tuple = field(init=False, repr=False)

    severity: str
    kind: str  # filing | news | sector | mover | system
    ticker: str
    title: str
    url: str = ""
    source: str = ""
    detail: list[str] = field(default_factory=list)
    company: str = ""
    filed_ts: float | None = None
    dedupe_key: str = ""
    bypass_window: bool = False

    def __post_init__(self) -> None:
        self.sort_key = (SEV_ORDER.get(self.severity, 9), next(_counter))


queue: asyncio.PriorityQueue[Alert] = asyncio.PriorityQueue()


async def emit(alert: Alert) -> bool:
    """Queue an alert. Returns False if it was suppressed as a duplicate."""
    key = alert.dedupe_key or f"{alert.kind}:{alert.ticker}:{alert.url or alert.title}"
    if store.seen(key):
        log.debug("duplicate suppressed: %s", key)
        return False
    await queue.put(alert)
    return True


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------


def _esc(text: str) -> str:
    return html.escape(str(text or ""), quote=False)


def format_plain(alert: Alert) -> str:
    """Last-resort rendering that cannot itself fail.

    Used when format_alert raises. A degraded alert is always better than a
    swallowed one — the whole point of this system is not missing filings, and
    a formatting bug is no reason to lose one.
    """
    bits = [
        f"[{alert.severity.upper()}] {alert.ticker or alert.kind}",
        str(alert.title or "")[:300],
    ]
    if alert.url:
        bits.append(str(alert.url))
    return _esc("\n".join(bits))


def format_alert(alert: Alert) -> str:
    """Telegram HTML. Deliberately terse — ticker, what, link, in that order."""
    emoji = SEV_EMOJI.get(alert.severity, "⚪")
    kind = KIND_LABEL.get(alert.kind, alert.kind.upper())

    if alert.severity == CRITICAL and alert.kind == "filing":
        header = f"{emoji} <b>DILUTION RISK — ${_esc(alert.ticker)}</b>"
    elif alert.ticker:
        header = f"{emoji} <b>${_esc(alert.ticker)}</b> · {kind}"
    else:
        header = f"{emoji} <b>{kind}</b>"

    lines = [header, f"<b>{_esc(alert.title)}</b>"]

    if alert.company:
        lines.append(_esc(alert.company))

    if alert.detail:
        # Cap the reason list — this is an alert, not a research note.
        shown = alert.detail[:6]
        lines.append("• " + "\n• ".join(_esc(d) for d in shown))

    meta = []
    if alert.filed_ts:
        age = max(0, int(time.time() - alert.filed_ts))
        meta.append(f"{fmt_et(alert.filed_ts)} ({age}s ago)")
    if alert.source:
        meta.append(_esc(alert.source))
    if meta:
        lines.append("<i>" + " · ".join(meta) + "</i>")

    if alert.url:
        lines.append(f'<a href="{_esc(alert.url)}">Open filing/article →</a>')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


def _muted_until() -> float:
    try:
        return float(store.get("muted_until", "0") or 0)
    except ValueError:
        return 0.0


def is_muted() -> bool:
    return time.time() < _muted_until()


def should_send(alert: Alert) -> tuple[bool, str]:
    if alert.bypass_window:
        return True, ""
    if is_muted():
        # A mute is for headline noise. Dilution filings still get through —
        # that is the one thing the user said must never be missed.
        if alert.severity == CRITICAL and alert.kind == "filing":
            return True, ""
        return False, "muted"
    if not in_alert_window():
        if alert.severity == CRITICAL:
            return True, ""
        return False, "outside alert window"
    return True, ""


async def dispatcher() -> None:
    """Drain the queue to Telegram, paced so we never hit Telegram's limits."""
    from .telegram_bot import send_message

    last_sent = 0.0
    min_gap = 1.1  # Telegram tolerates ~1 message/sec to a single chat

    while True:
        alert = await queue.get()
        try:
            ok, reason = should_send(alert)
            if not ok:
                log.info(
                    "suppressed (%s): %s %s", reason, alert.ticker, alert.title[:60]
                )
                store.log_alert(
                    alert.ticker, alert.severity, alert.kind + ":suppressed",
                    alert.title, alert.url,
                )
                continue

            gap = time.time() - last_sent
            if gap < min_gap:
                await asyncio.sleep(min_gap - gap)

            try:
                body = format_alert(alert)
            except Exception as exc:
                log.error(
                    "format_alert failed for %s (%s) — sending plain fallback",
                    alert.ticker, exc, exc_info=True,
                )
                body = format_plain(alert)

            await send_message(
                body,
                disable_preview=alert.severity not in (CRITICAL, HIGH),
                silent=alert.severity == INFO,
            )
            last_sent = time.time()
            store.log_alert(
                alert.ticker, alert.severity, alert.kind, alert.title, alert.url
            )
            log.info(
                "sent [%s/%s] %s %s", alert.severity, alert.kind,
                alert.ticker, alert.title[:80],
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("dispatch failed for %s: %s", alert.ticker, exc, exc_info=True)
        finally:
            queue.task_done()


# ---------------------------------------------------------------------------
# Convenience constructors
# ---------------------------------------------------------------------------


async def emit_system(title: str, detail: list[str] | None = None,
                      severity: str = HIGH, dedupe_key: str = "") -> bool:
    return await emit(
        Alert(
            severity=severity,
            kind="system",
            ticker="",
            title=title,
            detail=detail or [],
            dedupe_key=dedupe_key or f"system:{title}:{int(time.time() // 900)}",
            bypass_window=True,
        )
    )
