"""Real-time news via Alpaca's free news websocket (Benzinga-sourced).

This is the fastest free headline feed available to retail: it is a push
stream, so there is no polling interval to lose seconds to. A free paper
account is enough — no funding, no market data subscription.

We subscribe to the firehose ("*") rather than a symbol list on purpose. It
means a ticker the scanner discovers at 9:47am is covered instantly, with no
resubscribe round-trip, and filtering locally costs nothing.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import websockets

from ..alerts import Alert, emit
from ..classify import classify_headline
from ..config import cfg
from ..store import store

log = logging.getLogger(__name__)

WS_URL = "wss://stream.data.alpaca.markets/v1beta1/news"
NAME = "alpaca_news"


def _parse_ts(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


async def _handle_news(item: dict) -> None:
    symbols = [str(s).upper() for s in (item.get("symbols") or [])]
    if not symbols:
        return

    watched = store.tickers()
    hits = [s for s in symbols if s in watched]
    if not hits:
        return

    headline = (item.get("headline") or "").strip()
    if not headline:
        return

    summary = (item.get("summary") or "").strip()
    verdict = classify_headline(headline, summary)
    news_id = item.get("id") or item.get("url") or headline

    # A headline naming several of your tickers is one event, not three.
    primary = hits[0]
    also = [s for s in hits[1:]]
    detail = list(verdict.reasons)
    if also:
        detail.append("Also mentions: " + ", ".join(f"${s}" for s in also))

    await emit(
        Alert(
            severity=verdict.severity,
            kind="news",
            ticker=primary,
            title=headline,
            url=item.get("url") or "",
            source=item.get("source") or "Alpaca/Benzinga",
            detail=detail,
            filed_ts=_parse_ts(item.get("created_at")),
            dedupe_key=f"news:{news_id}",
        )
    )


async def run() -> None:
    """Maintain the websocket forever, reconnecting with backoff."""
    if not cfg.alpaca_enabled:
        log.warning(
            "Alpaca keys not set — real-time news stream disabled. "
            "SEC filings and RSS news are unaffected."
        )
        return

    backoff = 1.0
    while True:
        try:
            async with websockets.connect(
                WS_URL, ping_interval=20, ping_timeout=20, close_timeout=5
            ) as ws:
                await ws.send(
                    json.dumps(
                        {
                            "action": "auth",
                            "key": cfg.alpaca_key,
                            "secret": cfg.alpaca_secret,
                        }
                    )
                )
                await ws.send(json.dumps({"action": "subscribe", "news": ["*"]}))
                log.info("Alpaca news stream connected")
                store.mark_health(NAME, ok=True)
                backoff = 1.0

                async for raw in ws:
                    try:
                        payload = json.loads(raw)
                    except ValueError:
                        continue
                    if isinstance(payload, dict):
                        payload = [payload]
                    for msg in payload:
                        mtype = msg.get("T")
                        if mtype == "n":
                            store.mark_health(NAME, ok=True)
                            await _handle_news(msg)
                        elif mtype == "error":
                            log.error("Alpaca stream error: %s", msg)
                            store.mark_health(NAME, ok=False, err=str(msg))
                            if msg.get("code") in (401, 402, 403, 404, 409):
                                # Bad credentials or plan — retrying fast is
                                # pointless and can get the key throttled.
                                await asyncio.sleep(300)
                        elif mtype == "subscription":
                            log.info("Alpaca subscription: %s", msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            store.mark_health(NAME, ok=False, err=f"{type(exc).__name__}: {exc}")
            log.warning("Alpaca stream dropped (%s); reconnecting in %.0fs", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
