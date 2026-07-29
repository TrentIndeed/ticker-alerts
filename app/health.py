"""Watchdog, daily summary, housekeeping, and a local health endpoint.

The watchdog exists because the dangerous failure mode here is silence. A
crashed poller looks exactly like a quiet news day, and you would not find out
until the day you needed it. So the system reports on itself: if a feed stops
succeeding, that becomes an alert in its own right.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx

from .alerts import emit_system
from .classify import CRITICAL, HIGH, INFO
from .config import cfg
from .store import store
from .util import ago, in_alert_window, is_market_hours, now_et

log = logging.getLogger(__name__)

# Sources that must stay alive for the system to be doing its job. Alpaca is
# excluded because it's optional; movers is excluded because it legitimately
# goes quiet outside the session.
CRITICAL_SOURCES = {
    "sec_feed[ALL]",
    "sec_submissions",
    "telegram",
}


async def watchdog() -> None:
    while True:
        await asyncio.sleep(60)
        try:
            await _check_once()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("watchdog failed: %s", exc, exc_info=True)


async def _check_once() -> None:
    if not in_alert_window():
        return

    now = time.time()
    stale = []
    for row in store.health():
        source = row["source"]
        last_ok = row["last_ok"]
        if source not in CRITICAL_SOURCES and not source.startswith("sec_"):
            continue
        age = now - last_ok if last_ok else None
        if age is None or age > cfg.watchdog_stale:
            stale.append((source, age, row["last_err"]))

    if not stale:
        return

    detail = []
    for source, age, err in stale:
        when = ago(age) if age else "never succeeded"
        line = f"{source}: last OK {when}"
        if err:
            line += f" — {str(err)[:100]}"
        detail.append(line)

    severity = CRITICAL if is_market_hours() else HIGH
    await emit_system(
        f"⚠️ FEED PROBLEM — {len(stale)} source(s) stale",
        detail
        + ["", "Filings may be going undetected. See RUNBOOK.md → 'Feed problem'."],
        severity=severity,
        # Re-warn every 15 minutes while it stays broken, not every minute.
        dedupe_key=f"watchdog:{sorted(s for s, _, _ in stale)}:{int(now // 900)}",
    )


async def daily_summary() -> None:
    """One message a day confirming the system is alive and what it did."""
    if cfg.daily_summary_hour < 0:
        return
    while True:
        await asyncio.sleep(300)
        try:
            now = now_et()
            if now.hour != cfg.daily_summary_hour:
                continue
            stamp = now.strftime("%Y-%m-%d")
            if store.get("last_summary") == stamp:
                continue
            store.set("last_summary", stamp)

            counts = store.alert_counts_since(time.time() - 86400)
            healthy = sum(
                1
                for r in store.health()
                if r["last_ok"] and (time.time() - r["last_ok"]) < cfg.watchdog_stale
            )
            total_sources = len(store.health())

            detail = [
                f"Feeds healthy: {healthy}/{total_sources}",
                f"Watchlist: {len(store.tickers())} ticker(s)",
            ]
            if counts:
                detail += [f"{k}: {v}" for k, v in sorted(counts.items())]
            else:
                detail.append("No alerts today")

            await emit_system(
                f"📊 Daily summary — {stamp}", detail, severity=INFO,
                dedupe_key=f"summary:{stamp}",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("daily summary failed: %s", exc, exc_info=True)


async def housekeeping(client: httpx.AsyncClient) -> None:
    """Hourly: prune the dedup ledger, refresh the SEC ticker index."""
    from .tickers import backfill_ciks, index

    while True:
        await asyncio.sleep(3600)
        try:
            pruned = store.prune_seen()
            if pruned:
                log.info("pruned %d old dedup entries", pruned)
            await index.refresh(client)
            await backfill_ciks(client)
            expired = store.expire_scanner_tickers()
            if expired:
                log.info("expired scanner tickers: %s", ", ".join(expired))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("housekeeping failed: %s", exc)


# ---------------------------------------------------------------------------
# Health endpoint (local only — used by Docker's HEALTHCHECK)
# ---------------------------------------------------------------------------


def _snapshot() -> dict:
    now = time.time()
    sources = {}
    degraded = []
    for row in store.health():
        age = now - row["last_ok"] if row["last_ok"] else None
        ok = age is not None and age < cfg.watchdog_stale
        sources[row["source"]] = {
            "ok": ok,
            "last_ok_age_sec": round(age) if age is not None else None,
            "errors": row["err_count"],
        }
        if not ok and row["source"] in CRITICAL_SOURCES:
            degraded.append(row["source"])
    return {
        "status": "degraded" if degraded else "ok",
        "degraded": degraded,
        "alert_window_open": in_alert_window(),
        "watchlist": len(store.tickers()),
        "alerts_24h": sum(store.alert_counts_since(now - 86400).values()),
        "sources": sources,
    }


async def _handle_http(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await asyncio.wait_for(reader.readline(), timeout=5)
        body = json.dumps(_snapshot(), indent=2).encode()
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() + b"\r\n"
            b"Connection: close\r\n\r\n" + body
        )
        await writer.drain()
    except Exception:  # noqa: BLE001
        pass
    finally:
        writer.close()


async def health_server() -> None:
    server = await asyncio.start_server(_handle_http, "0.0.0.0", cfg.health_port)
    log.info("health endpoint on :%d/healthz", cfg.health_port)
    async with server:
        await server.serve_forever()
