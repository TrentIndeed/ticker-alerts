"""RSS news: per-ticker feeds, press-release wires, and sector headlines.

This is the backstop to the Alpaca stream, and the only news path that works at
all if Alpaca keys aren't configured. Press releases from the wires are where
small-cap news actually breaks first, so they get scanned for an exchange:ticker
string rather than relying on someone having tagged the story correctly.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from urllib.parse import quote_plus

import feedparser
import httpx

from ..alerts import Alert, emit
from ..classify import NEWS, classify_headline
from ..config import cfg
from ..store import store
from ..util import chunked

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

YAHOO = "https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}&region=US&lang=en-US"
GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"

# GlobeNewswire was dropped on 2026-10-07: since mid-August its site refuses non-browser clients (HTTP/2
# INTERNAL_ERROR or a hang, from the VPS and from a home connection alike; nothing arrived after Aug 10).
# Its releases still reach a watched ticker through the per-ticker Yahoo feed.
WIRES = [
    ("Business Wire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRVQ=="),
    ("PR Newswire", "https://www.prnewswire.com/rss/news-releases-list.rss"),
]

# Press releases nearly always carry "(NASDAQ: ABCD)" in the first paragraph.
_EXCHANGE_TICKER = re.compile(
    r"\b(?:NASDAQ|NYSE\s*American|NYSE|AMEX|OTCQB|OTCQX|CBOE)\s*[:\-]\s*([A-Z]{1,5})\b"
)


# Retail finance RSS is roughly half syndicated opinion columns. None of these
# are events — they are commentary about things that already happened, and they
# arrive at a rate that would drown the alerts that matter. Filed under
# "over-alerting is survivable, but only up to the point you stop reading".
_OPINION = re.compile(
    r"should you (buy|sell)|is\s+\w+\s+(stock\s+)?a\s+(buy|sell)|"
    r"\b\d+\s+(reasons?|things|stocks|top|best)\b|better buy|"
    r"^prediction\b|here'?s why|\bmotley fool\b|\bzacks\b|"
    r"could (soar|surge|double|make you)|where will .* be in|"
    r"\bmagnificent\b|wall street analysts|\bbillionaire\b|"
    r"\bbest stocks?\b|why i'?m\b|i'?m (buying|loading)|"
    r"stock split|dividend (stock|king|aristocrat)|"
    r"\bvs\.?\b.*which|what to know about",
    re.IGNORECASE,
)


def is_opinion(headline: str) -> bool:
    return bool(_OPINION.search(headline or ""))


# Words in a company name that identify nothing on their own.
_NAME_NOISE = {
    "inc", "inc.", "corp", "corp.", "corporation", "co", "co.", "company",
    "ltd", "ltd.", "limited", "plc", "holdings", "holding", "group", "the",
    "technologies", "technology", "tech", "systems", "solutions", "sciences",
    "pharmaceuticals", "pharma", "therapeutics", "international", "industries",
    "&", "and", "class", "common", "stock", "shares",
}


def mentions_ticker(ticker: str, name: str | None, text: str) -> bool:
    """Is this article actually about the ticker whose feed it came from?

    Yahoo's per-ticker feed mixes in general market stories, so an article
    about DoorDash or a missile strike arrives on NVDA's feed. Labelling those
    "$NVDA" is worse than generic noise — it reads as a signal about a position
    you may be holding. We keep the article either way, but only attribute it
    to the ticker when the ticker or a distinctive word from the company name
    actually appears.
    """
    blob = text.lower()
    if re.search(rf"\b\$?{re.escape(ticker.lower())}\b", blob):
        return True
    for word in re.split(r"[^A-Za-z0-9]+", (name or "")):
        if len(word) > 3 and word.lower() not in _NAME_NOISE:
            if word.lower() in blob:
                return True
    return False


def _entry_time(entry) -> float | None:
    # *_parsed are UTC struct_times; calendar.timegm is the correct inverse.
    # time.mktime would treat them as local and land an hour off under DST.
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None)
        if parsed:
            return calendar.timegm(parsed)
    return None


def _entry_id(entry, fallback: str) -> str:
    return (
        getattr(entry, "id", None)
        or getattr(entry, "link", None)
        or f"{fallback}:{getattr(entry, 'title', '')}"
    )


async def _fetch_feed(client: httpx.AsyncClient, url: str):
    for attempt in range(3):
        resp = await client.get(url, headers={"User-Agent": UA}, timeout=25)
        # PR Newswire's CDN now and then 301s the feed to the same path plus "/", which is a 404 (about one
        # request in four, Oct 2026); the next request normally gets the feed, so ask again.
        final = str(resp.url)
        if resp.status_code == 404 and final != url and final.rstrip("/") == url.rstrip("/") and attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        resp.raise_for_status()
        return feedparser.parse(resp.content)


class TickerNewsWatcher:
    """Per-ticker Yahoo Finance headlines."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.name = "rss_ticker_news"
        self._primed: set[str] = set()

    async def poll(self) -> None:
        tickers = sorted(store.tickers())
        if not tickers:
            return
        # Batch so we never open more than a handful of sockets at once.
        for batch in chunked(tickers, 8):
            await asyncio.gather(
                *(self._one(t) for t in batch), return_exceptions=True
            )
            await asyncio.sleep(0.5)

    async def _one(self, ticker: str) -> None:
        try:
            parsed = await _fetch_feed(self.client, YAHOO.format(t=quote_plus(ticker)))
        except Exception as exc:  # noqa: BLE001
            log.debug("yahoo feed failed for %s: %s", ticker, exc)
            return

        from ..tickers import index

        company_name = index.name_for(ticker)
        priming = ticker not in self._primed
        self._primed.add(ticker)

        for entry in (parsed.entries or [])[:15]:
            key = f"news:{_entry_id(entry, ticker)}"
            if priming:
                store.seen(key)
                continue

            ts = _entry_time(entry)
            if ts and (time.time() - ts) > 6 * 3600:
                store.seen(key)
                continue

            headline = (getattr(entry, "title", "") or "").strip()
            if not headline:
                continue
            if cfg.filter_opinion and is_opinion(headline):
                store.seen(key)
                log.debug("opinion filtered: %s", headline[:70])
                continue
            summary = (getattr(entry, "summary", "") or "")[:400]
            verdict = classify_headline(headline, summary)

            # Attribute to the ticker only if the article is really about it;
            # otherwise send it through as general market context rather than
            # claiming it's news about a name you may be holding.
            about_ticker = mentions_ticker(
                ticker, company_name, f"{headline} {summary}"
            )

            await emit(
                Alert(
                    severity=verdict.severity if about_ticker else NEWS,
                    kind="news" if about_ticker else "sector",
                    ticker=ticker if about_ticker else "",
                    title=headline,
                    url=getattr(entry, "link", "") or "",
                    source="Yahoo Finance"
                    if about_ticker
                    else f"Yahoo Finance · surfaced via ${ticker}",
                    detail=verdict.reasons,
                    filed_ts=ts,
                    dedupe_key=key,
                )
            )


class WireWatcher:
    """Press-release wires, matched to the watchlist by exchange:ticker string."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.name = "rss_wires"
        self._primed: set[str] = set()

    async def poll(self) -> None:
        watched = store.tickers()
        if not watched:
            return
        for label, url in WIRES:
            try:
                await self._one(label, url, watched)
            except Exception as exc:  # noqa: BLE001
                log.debug("wire feed %s failed: %s", label, exc)

    async def _one(self, label: str, url: str, watched: set[str]) -> None:
        parsed = await _fetch_feed(self.client, url)
        priming = label not in self._primed
        self._primed.add(label)

        for entry in (parsed.entries or [])[:60]:
            key = f"news:{_entry_id(entry, label)}"
            if priming:
                store.seen(key)
                continue

            headline = (getattr(entry, "title", "") or "").strip()
            summary = (getattr(entry, "summary", "") or "")[:1500]
            blob = f"{headline} {summary}"

            found = {m.upper() for m in _EXCHANGE_TICKER.findall(blob)}
            hits = sorted(found & watched)
            if not hits:
                continue

            ts = _entry_time(entry)
            if ts and (time.time() - ts) > 6 * 3600:
                store.seen(key)
                continue

            verdict = classify_headline(headline, summary)
            detail = list(verdict.reasons)
            if len(hits) > 1:
                detail.append("Also: " + ", ".join(f"${h}" for h in hits[1:]))

            await emit(
                Alert(
                    severity=verdict.severity,
                    kind="news",
                    ticker=hits[0],
                    title=headline,
                    url=getattr(entry, "link", "") or "",
                    source=f"{label} (press release)",
                    detail=detail,
                    filed_ts=ts,
                    dedupe_key=key,
                )
            )


class SectorNewsWatcher:
    """General NASDAQ / AI sector headlines not tied to a specific ticker."""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.name = "rss_sector"
        self._primed: set[str] = set()

    async def poll(self) -> None:
        if not cfg.sector_news_enabled:
            return
        for query in cfg.sector_news_queries:
            try:
                await self._one(query)
            except Exception as exc:  # noqa: BLE001
                log.debug("sector query %r failed: %s", query, exc)
            await asyncio.sleep(1.0)

    async def _one(self, query: str) -> None:
        parsed = await _fetch_feed(self.client, GOOGLE_NEWS.format(q=quote_plus(query)))
        priming = query not in self._primed
        self._primed.add(query)

        for entry in (parsed.entries or [])[:10]:
            key = f"sector:{_entry_id(entry, query)}"
            if priming:
                store.seen(key)
                continue

            ts = _entry_time(entry)
            if ts and (time.time() - ts) > 3 * 3600:
                store.seen(key)
                continue

            headline = (getattr(entry, "title", "") or "").strip()
            if not headline:
                continue
            if cfg.filter_opinion and is_opinion(headline):
                store.seen(key)
                continue

            verdict = classify_headline(headline)
            # Sector news is context, not a trade trigger. Only the genuinely
            # loud items get a normal-priority alert; the rest go out silent.
            severity = verdict.severity if verdict.severity != "high" else NEWS

            await emit(
                Alert(
                    severity=severity,
                    kind="sector",
                    ticker="",
                    title=headline,
                    url=getattr(entry, "link", "") or "",
                    source=f"Google News · {query}",
                    detail=verdict.reasons,
                    filed_ts=ts,
                    dedupe_key=key,
                )
            )
