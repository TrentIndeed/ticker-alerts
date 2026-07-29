"""SEC EDGAR watcher — the part that must not miss anything.

Three independent detection paths run concurrently and all deduplicate against
the same ledger by accession number:

  1. GLOBAL FEED    — EDGAR's "latest filings" atom feed, every ~8s. Broad net
                      across every filer; we keep the ones whose CIK is on the
                      watchlist.
  2. FORM FEEDS     — the same feed filtered to each dilution form type
                      (424B5, S-3, FWP, ...), every ~15s. Low volume, so it can
                      never overflow the 100-entry page the way the global feed
                      can during the 4-5pm filing rush. This is what catches an
                      offering from a ticker the scanner only just discovered.
  3. SUBMISSIONS    — data.sec.gov per-company JSON, round-robined across the
                      watchlist. Authoritative, includes 8-K item codes and the
                      exact acceptance timestamp, and keeps working if the
                      browse-edgar CGI has one of its periodic bad days.

Any one of the three alone would be adequate on a good day. Running all three
is what makes a missed filing require three simultaneous failures.
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from datetime import datetime

import feedparser
import httpx

from ..alerts import Alert, emit
from ..classify import (
    CRITICAL,
    HIGH,
    classify_filing,
    escalate_with_text,
    extract_offering_size,
)
from ..config import ET, cfg
from ..store import store
from ..tickers import index
from ..util import sec_limiter

log = logging.getLogger(__name__)

GETCURRENT = (
    "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"
    "&company=&dateb=&owner=include&start=0&count={count}&output=atom"
)
SUBMISSIONS = "https://data.sec.gov/submissions/CIK{cik}.json"

# Dilution-relevant form types, polled individually so they cannot be crowded
# out of the global feed. Measured rates (July 2026): 424B5 ~4/hour, 424B3
# ~2/hour, 424B2 ~230/hour, the rest a handful per day. Even the worst of them
# needs >9,000/hour to overflow a 40-entry page between 15s polls, so the
# anti-overflow guarantee holds comfortably across all of these.
WATCHED_FORMS = [
    "424B5",
    "424B3",
    "424B4",
    "424B2",
    "FWP",
    "S-3",
    "S-3ASR",
    "F-3",
    "S-1",
    "424B7",
    "EFFECT",
    "POS AM",
    "SUPPL",
]

# Forms dominated by bank structured-note programs rather than equity dilution.
# Excluded from the wide net (they stay fully active for watchlist tickers).
_HIGH_VOLUME_FORMS = {"424B2", "424B3"}

# Serial shelf issuers whose filings are structured notes, not dilution. Only
# consulted when the wide net is enabled.
_NOTE_ISSUERS = {
    "UBS", "GS", "MS", "JPM", "C", "BAC", "WFC", "BCS", "DB", "HSBC",
    "RY", "TD", "BMO", "BNS", "CM", "CS", "NMR", "MUFG", "SMFG", "IX",
}

_TITLE_RE = re.compile(r"^(?P<form>.+?)\s+-\s+(?P<name>.+?)\s+\((?P<cik>\d{4,10})\)")
_ACC_RE = re.compile(r"(\d{10}-?\d{2}-?\d{6})")


def _headers() -> dict[str, str]:
    return {
        "User-Agent": cfg.sec_user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "www.sec.gov",
    }


def _data_headers() -> dict[str, str]:
    return {
        "User-Agent": cfg.sec_user_agent,
        "Accept-Encoding": "gzip, deflate",
        "Host": "data.sec.gov",
    }


def _norm_accession(raw: str) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) != 18:
        return raw or ""
    return f"{digits[:10]}-{digits[10:12]}-{digits[12:]}"


def _parse_acceptance(raw: str | None) -> float | None:
    """Parse data.sec.gov's acceptanceDateTime.

    The field is published as e.g. "2026-07-29T10:47:32.000Z" — but the Z is
    wrong. Cross-checking one accession against the same filing in EDGAR's atom
    feed, which carries an explicit offset, shows the digits are Eastern, not
    UTC:

        atom:        2026-07-29T10:47:32-04:00
        submissions: 2026-07-29T10:47:32.000Z

    Taking the Z at face value shifts every filing 4-5 hours into the past,
    which both misreports the age and pushes genuinely recent filings past the
    staleness cutoff below. So we strip the suffix and localise to Eastern.
    """
    if not raw:
        return None
    text = str(raw).strip().replace("Z", "").replace("+00:00", "")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    return dt.replace(tzinfo=ET).timestamp()


def _doc_url(cik: str, accession: str, primary_doc: str | None) -> str:
    cik_int = str(int(cik))
    acc_nodash = accession.replace("-", "")
    if primary_doc:
        return f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/{primary_doc}"
    return (
        f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}/"
        f"{accession}-index.htm"
    )


async def _fetch_text(client: httpx.AsyncClient, url: str, cap: int = 400_000) -> str:
    """Download at most `cap` bytes of a filing document, stripped of tags."""
    await sec_limiter.acquire()
    chunks: list[bytes] = []
    total = 0
    async with client.stream("GET", url, headers=_headers(), timeout=25) as resp:
        resp.raise_for_status()
        async for chunk in resp.aiter_bytes(32_768):
            chunks.append(chunk)
            total += len(chunk)
            if total >= cap:
                break
    raw = b"".join(chunks).decode("utf-8", errors="ignore")
    text = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text)


# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


async def _emit_filing(
    client: httpx.AsyncClient,
    *,
    ticker: str,
    cik: str,
    company: str,
    form: str,
    accession: str,
    url: str,
    filed_ts: float | None,
    items: str = "",
    detected_by: str = "",
) -> None:
    """Alert on a filing, then enrich it in the background.

    The alert goes out on form type alone — that is available instantly. Reading
    the document to confirm ATM/offering language takes another second or two,
    so it happens after the fast alert and only sends a follow-up when it finds
    something that changes the picture.
    """
    verdict = classify_filing(form, items=items)

    sent = await emit(
        Alert(
            severity=verdict.severity,
            kind="filing",
            ticker=ticker,
            title=f"{form} — {verdict.label}",
            url=url,
            company=company,
            source=f"EDGAR{'/' + detected_by if detected_by else ''}",
            detail=verdict.reasons,
            filed_ts=filed_ts,
            dedupe_key=f"filing:{accession}",
        )
    )
    if not sent:
        return

    # Only bother reading the document when doing so could change the verdict
    # or add a number worth seeing.
    if verdict.severity not in (CRITICAL, HIGH):
        return

    asyncio.create_task(
        _enrich(client, ticker, company, form, accession, url, verdict, filed_ts)
    )


async def _enrich(
    client: httpx.AsyncClient,
    ticker: str,
    company: str,
    form: str,
    accession: str,
    url: str,
    verdict,
    filed_ts: float | None,
) -> None:
    try:
        text = await _fetch_text(client, url)
    except Exception as exc:  # noqa: BLE001
        log.debug("enrichment fetch failed for %s: %s", accession, exc)
        return

    upgraded = escalate_with_text(verdict, text)
    new_reasons = [r for r in upgraded.reasons if r not in verdict.reasons]
    size = extract_offering_size(text)

    escalated = upgraded.severity != verdict.severity
    if not new_reasons and not size and not escalated:
        return

    detail = new_reasons[:]
    if size:
        detail.insert(0, f"Size: {size}")

    title = f"{form} details — {ticker}"
    if escalated:
        title = f"ESCALATED to dilution risk — {ticker} {form}"

    await emit(
        Alert(
            severity=upgraded.severity if escalated else HIGH,
            kind="filing",
            ticker=ticker,
            title=title,
            url=url,
            company=company,
            source="EDGAR/document scan",
            detail=detail,
            filed_ts=filed_ts,
            dedupe_key=f"filing-detail:{accession}",
        )
    )


# ---------------------------------------------------------------------------
# Path 1 & 2: browse-edgar atom feeds
# ---------------------------------------------------------------------------


class FeedWatcher:
    """Polls a getcurrent atom feed and emits matching filings."""

    def __init__(self, client: httpx.AsyncClient, form: str | None, count: int = 100):
        self.client = client
        self.form = form
        self.count = count
        self.name = f"sec_feed[{form or 'ALL'}]"
        self._primed = False

    def _url(self) -> str:
        url = GETCURRENT.format(count=self.count)
        if self.form:
            url += f"&type={self.form.replace(' ', '+')}"
        return url

    def _wide_net_allows(self, ticker: str | None) -> bool:
        """Should we alert on a dilution form from a ticker we don't watch?

        Off by default, and for good reason. 424B2 alone runs ~230 filings an
        hour, nearly all of them bank structured notes — turning this on
        without the exclusions below buries a real 424B5 under a hundred UBS
        note filings, which is exactly how you end up ignoring the alerts.
        """
        if not cfg.sec_wide_net or self.form is None or not ticker:
            return False
        if self.form in _HIGH_VOLUME_FORMS:
            return False
        return ticker not in _NOTE_ISSUERS

    async def poll(self) -> None:
        await sec_limiter.acquire()
        # browse-edgar is occasionally slow under load; a generous timeout
        # costs nothing because resilient_loop backs off on repeated failure.
        resp = await self.client.get(self._url(), headers=_headers(), timeout=45)
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)

        entries = list(parsed.entries or [])
        if not entries:
            return

        # First pass after startup: remember what's already there, alert on none
        # of it. Otherwise every restart would replay the last 100 filings.
        priming = not self._primed
        self._primed = True

        watch_ciks = store.cik_map()
        watch_tickers = store.tickers()

        for entry in entries:
            try:
                await self._handle(entry, watch_ciks, watch_tickers, priming)
            except Exception as exc:  # noqa: BLE001
                log.debug("%s: entry parse failed: %s", self.name, exc)

    async def _handle(self, entry, watch_ciks, watch_tickers, priming) -> None:
        title = getattr(entry, "title", "") or ""
        link = getattr(entry, "link", "") or ""

        m = _TITLE_RE.match(title)
        if not m:
            return
        form = m.group("form").strip()
        company = m.group("name").strip()
        cik = m.group("cik").zfill(10)

        acc_match = _ACC_RE.search(link)
        accession = _norm_accession(acc_match.group(1)) if acc_match else link
        if not accession:
            return

        key = f"filing:{accession}"
        if priming:
            store.seen(key)
            return

        ticker = watch_ciks.get(cik) or index.ticker_for(cik)
        on_watchlist = cik in watch_ciks or (ticker and ticker in watch_tickers)

        if not on_watchlist and not self._wide_net_allows(ticker):
            return

        # updated_parsed is a UTC struct_time. calendar.timegm is the correct
        # inverse; time.mktime would read it as local time and land an hour off
        # during daylight saving.
        filed_ts = None
        updated = getattr(entry, "updated_parsed", None)
        if updated:
            filed_ts = calendar.timegm(updated)

        await _emit_filing(
            self.client,
            ticker=ticker or f"CIK{int(cik)}",
            cik=cik,
            company=company,
            form=form,
            accession=accession,
            url=link,
            filed_ts=filed_ts,
            detected_by="feed" if self.form is None else f"form:{self.form}",
        )


# ---------------------------------------------------------------------------
# Path 3: per-company submissions JSON
# ---------------------------------------------------------------------------


class SubmissionsWatcher:
    """Round-robins the watchlist through data.sec.gov/submissions.

    Each cycle covers every watchlist ticker. With a 30s cycle and a 7 req/s
    limiter this comfortably handles a watchlist of a few hundred names.
    """

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.name = "sec_submissions"
        self._primed: set[str] = set()

    async def poll(self) -> None:
        rows = [r for r in store.watchlist() if r["cik"]]
        if not rows:
            return

        for row in rows:
            try:
                await self._check(row["ticker"], str(row["cik"]).zfill(10))
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    log.debug("no submissions for %s", row["ticker"])
                else:
                    raise
            except Exception as exc:  # noqa: BLE001
                log.debug("submissions check failed for %s: %s", row["ticker"], exc)

    async def _check(self, ticker: str, cik: str) -> None:
        await sec_limiter.acquire()
        resp = await self.client.get(
            SUBMISSIONS.format(cik=cik), headers=_data_headers(), timeout=25
        )
        resp.raise_for_status()
        data = resp.json()

        recent = (data.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        if not forms:
            return

        company = data.get("name") or ""
        accessions = recent.get("accessionNumber") or []
        items_list = recent.get("items") or []
        primary_docs = recent.get("primaryDocument") or []
        acceptance = recent.get("acceptanceDateTime") or []

        priming = ticker not in self._primed
        self._primed.add(ticker)

        # Only the newest slice matters; the array is newest-first and can hold
        # a thousand historical filings.
        for i in range(min(len(forms), 25)):
            accession = _norm_accession(accessions[i] if i < len(accessions) else "")
            if not accession:
                continue
            key = f"filing:{accession}"
            if priming:
                store.seen(key)
                continue

            filed_ts = _parse_acceptance(
                acceptance[i] if i < len(acceptance) else None
            )

            # Anything older than 12h is backfill, not news.
            if filed_ts and (time.time() - filed_ts) > 12 * 3600:
                store.seen(key)
                continue

            await _emit_filing(
                self.client,
                ticker=ticker,
                cik=cik,
                company=company,
                form=forms[i],
                accession=accession,
                url=_doc_url(
                    cik, accession, primary_docs[i] if i < len(primary_docs) else None
                ),
                filed_ts=filed_ts,
                items=items_list[i] if i < len(items_list) else "",
                detected_by="submissions",
            )
