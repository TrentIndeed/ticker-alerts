"""Ticker <-> CIK resolution using the SEC's own published mapping.

The SEC publishes company_tickers.json (~10k entries, a few hundred KB). We
cache it in memory, refresh daily, and persist the last good copy to disk so a
restart during an SEC outage doesn't leave us unable to resolve anything.
"""

from __future__ import annotations

import json
import logging
import os
import time

import httpx

from .config import cfg
from .util import sec_limiter

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
CACHE_PATH = os.path.join(os.path.dirname(cfg.db_path), "company_tickers.json")
REFRESH_SEC = 24 * 3600


class TickerIndex:
    def __init__(self) -> None:
        self._by_ticker: dict[str, tuple[str, str]] = {}  # TICKER -> (cik10, name)
        self._by_cik: dict[str, str] = {}  # cik10 -> TICKER
        self._loaded_at: float = 0.0
        self._load_cache()

    # -------- persistence --------

    def _load_cache(self) -> None:
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as fh:
                self._ingest(json.load(fh))
            self._loaded_at = os.path.getmtime(CACHE_PATH)
            log.info("Loaded %d tickers from local cache", len(self._by_ticker))
        except (OSError, ValueError, KeyError):
            log.info("No usable local ticker cache; will fetch from SEC")

    def _save_cache(self, raw: dict) -> None:
        try:
            os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
            tmp = CACHE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(raw, fh)
            os.replace(tmp, CACHE_PATH)
        except OSError as exc:
            log.warning("Could not persist ticker cache: %s", exc)

    def _ingest(self, raw: dict) -> None:
        by_ticker, by_cik = {}, {}
        for entry in raw.values():
            ticker = str(entry.get("ticker", "")).upper().strip()
            cik = str(entry.get("cik_str", "")).zfill(10)
            name = str(entry.get("title", "")).strip()
            if not ticker or not cik.strip("0"):
                continue
            by_ticker[ticker] = (cik, name)
            # A CIK can carry several tickers (share classes). First wins,
            # which is the primary class in the SEC's own ordering.
            by_cik.setdefault(cik, ticker)
        if by_ticker:
            self._by_ticker, self._by_cik = by_ticker, by_cik

    # -------- refresh --------

    @property
    def stale(self) -> bool:
        return (time.time() - self._loaded_at) > REFRESH_SEC or not self._by_ticker

    async def refresh(self, client: httpx.AsyncClient, force: bool = False) -> int:
        if not force and not self.stale:
            return len(self._by_ticker)
        await sec_limiter.acquire()
        resp = await client.get(
            TICKERS_URL, headers={"User-Agent": cfg.sec_user_agent}, timeout=30
        )
        resp.raise_for_status()
        raw = resp.json()
        self._ingest(raw)
        self._save_cache(raw)
        self._loaded_at = time.time()
        log.info("Refreshed ticker index: %d symbols", len(self._by_ticker))
        return len(self._by_ticker)

    # -------- lookup --------

    def cik_for(self, ticker: str) -> str | None:
        entry = self._by_ticker.get(ticker.upper().strip())
        return entry[0] if entry else None

    def name_for(self, ticker: str) -> str | None:
        entry = self._by_ticker.get(ticker.upper().strip())
        return entry[1] if entry else None

    def ticker_for(self, cik: str) -> str | None:
        return self._by_cik.get(str(cik).zfill(10))

    def known(self, ticker: str) -> bool:
        return ticker.upper().strip() in self._by_ticker

    def __len__(self) -> int:
        return len(self._by_ticker)


index = TickerIndex()


async def backfill_ciks(client: httpx.AsyncClient) -> int:
    """Fill in CIKs for any watchlist rows that don't have one yet."""
    from .store import store

    filled = 0
    await index.refresh(client)
    for row in store.watchlist():
        if row["cik"]:
            continue
        cik = index.cik_for(row["ticker"])
        if cik:
            store.set_cik(row["ticker"], cik, index.name_for(row["ticker"]))
            filled += 1
    if filled:
        log.info("Backfilled CIK for %d ticker(s)", filled)
    return filled
