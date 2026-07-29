"""Auto-discovery of the day's active tickers.

Finds what is actually moving, adds it to a temporary watchlist so filings and
news for those names flow through the normal alert path, and lets the entries
expire at the end of the session so tomorrow starts clean.

Primary source is Alpaca's free screener. If Alpaca isn't configured (or is
having a bad morning) it falls back to Nasdaq's public screener JSON, so the
scanner keeps working with no credentials at all.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta

import httpx

from ..alerts import Alert, emit
from ..classify import INFO
from ..config import ET, cfg
from ..store import store
from ..tickers import index

log = logging.getLogger(__name__)

NAME = "movers"

ALPACA_MOVERS = "https://data.alpaca.markets/v1beta1/screener/stocks/movers?top=50"
ALPACA_ACTIVES = (
    "https://data.alpaca.markets/v1beta1/screener/stocks/most-actives?by=volume&top=50"
)
NASDAQ_SCREENER = (
    "https://api.nasdaq.com/api/screener/stocks"
    "?tableonly=true&limit=250&offset=0&exchange=NASDAQ&download=true"
)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _session_expiry() -> float:
    """Unix ts for the end of the current alert window (Eastern)."""
    now = datetime.now(ET)
    end = now.replace(hour=cfg.alert_end_hour, minute=0, second=0, microsecond=0)
    if now >= end:
        end += timedelta(days=1)
    return end.timestamp()


def _num(raw) -> float:
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    cleaned = re.sub(r"[^\d.\-]", "", str(raw))
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


class MoversWatcher:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.name = NAME
        self.last_scan: list[dict] = []

    # ---------------- sources ----------------

    def _alpaca_headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": cfg.alpaca_key,
            "APCA-API-SECRET-KEY": cfg.alpaca_secret,
            "Accept": "application/json",
        }

    async def _from_alpaca(self) -> list[dict]:
        if not cfg.alpaca_enabled:
            return []
        out: dict[str, dict] = {}

        resp = await self.client.get(
            ALPACA_MOVERS, headers=self._alpaca_headers(), timeout=20
        )
        resp.raise_for_status()
        data = resp.json()
        for bucket in ("gainers", "losers"):
            for row in data.get(bucket) or []:
                sym = str(row.get("symbol", "")).upper()
                if not sym:
                    continue
                out[sym] = {
                    "ticker": sym,
                    "price": _num(row.get("price")),
                    "pct": _num(row.get("percent_change")),
                    "volume": 0,
                    "why": "gainer" if bucket == "gainers" else "loser",
                }

        try:
            resp = await self.client.get(
                ALPACA_ACTIVES, headers=self._alpaca_headers(), timeout=20
            )
            resp.raise_for_status()
            for row in resp.json().get("most_actives") or []:
                sym = str(row.get("symbol", "")).upper()
                if not sym:
                    continue
                vol = _num(row.get("volume"))
                if sym in out:
                    out[sym]["volume"] = vol
                else:
                    out[sym] = {
                        "ticker": sym,
                        "price": 0.0,
                        "pct": 0.0,
                        "volume": vol,
                        "why": "most active",
                    }
        except Exception as exc:  # noqa: BLE001
            log.debug("most-actives unavailable: %s", exc)

        return list(out.values())

    async def _from_nasdaq(self) -> list[dict]:
        resp = await self.client.get(
            NASDAQ_SCREENER,
            headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
            timeout=25,
        )
        resp.raise_for_status()
        rows = ((resp.json().get("data") or {}).get("rows")) or []
        out = []
        for row in rows:
            sym = str(row.get("symbol", "")).upper().strip()
            if not sym:
                continue
            out.append(
                {
                    "ticker": sym,
                    "price": _num(row.get("lastsale")),
                    "pct": _num(row.get("pctchange")),
                    "volume": _num(row.get("volume")),
                    "why": "nasdaq screener",
                }
            )
        return out

    # ---------------- filtering ----------------

    def _qualifies(self, row: dict) -> bool:
        price = row["price"]
        pct = abs(row["pct"])
        volume = row["volume"]

        if price and not (cfg.movers_min_price <= price <= cfg.movers_max_price):
            return False
        # A name can qualify on either a big move or heavy volume. Requiring
        # both would filter out the pre-market gappers that matter most.
        if pct >= cfg.movers_min_pct:
            return True
        if volume >= cfg.movers_min_volume and pct >= cfg.movers_min_pct / 2:
            return True
        return False

    # ---------------- main ----------------

    async def poll(self) -> None:
        if not cfg.movers_enabled:
            return

        expired = store.expire_scanner_tickers()
        if expired:
            log.info("expired %d scanner ticker(s): %s", len(expired), ", ".join(expired))

        rows: list[dict] = []
        try:
            rows = await self._from_alpaca()
        except Exception as exc:  # noqa: BLE001
            log.warning("Alpaca screener failed, falling back to Nasdaq: %s", exc)
        if not rows:
            rows = await self._from_nasdaq()
        if not rows:
            raise RuntimeError("no mover data from any source")

        candidates = [r for r in rows if self._qualifies(r)]
        candidates.sort(key=lambda r: (abs(r["pct"]), r["volume"]), reverse=True)
        self.last_scan = candidates[:50]

        existing = store.tickers()
        room = max(0, cfg.movers_max - store.scanner_count())
        expiry = _session_expiry()
        added: list[dict] = []

        for row in candidates:
            if room <= 0:
                break
            ticker = row["ticker"]
            if ticker in existing:
                continue
            # Only track names the SEC can resolve — without a CIK we cannot
            # watch filings for them, which is the whole point.
            cik = index.cik_for(ticker)
            if not cik:
                continue
            if store.add_ticker(
                ticker,
                cik=cik,
                name=index.name_for(ticker),
                source="scanner",
                expires_ts=expiry,
                note=f"{row['why']} {row['pct']:+.1f}%",
            ):
                added.append(row)
                room -= 1

        if not added:
            return

        log.info("scanner added %d ticker(s)", len(added))
        lines = [
            f"${r['ticker']} {r['pct']:+.1f}%"
            + (f" · ${r['price']:.2f}" if r["price"] else "")
            + (f" · {int(r['volume']):,} sh" if r["volume"] else "")
            for r in added[:15]
        ]
        await emit(
            Alert(
                severity=INFO,
                kind="mover",
                ticker="",
                title=f"Now watching {len(added)} new mover(s)",
                detail=lines,
                source="Scanner",
                filed_ts=time.time(),
                dedupe_key=f"movers:{int(time.time() // 60)}:{len(added)}",
            )
        )
