"""Self-test / diagnostic.

Run this when something looks wrong, or right after install to prove the whole
chain works:

    docker compose run --rm ticker-alerts python -m app.selftest

It checks every upstream the system depends on, exercises the classifier
against known filings, and (with --telegram) sends a real test alert.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

import feedparser
import httpx

from .classify import (
    CRITICAL,
    HIGH,
    INFO,
    classify_filing,
    classify_headline,
    scan_text,
)
from .config import cfg

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
results: list[tuple[str, str, str]] = []


def record(name: str, status: str, detail: str = "") -> None:
    results.append((name, status, detail))
    icon = {PASS: "  OK  ", FAIL: " FAIL ", WARN: " WARN "}[status]
    print(f"[{icon}] {name}" + (f" - {detail}" if detail else ""), flush=True)


# ---------------------------------------------------------------------------
# Offline checks
# ---------------------------------------------------------------------------


def check_config() -> None:
    if cfg.telegram_token and cfg.telegram_chat_ids:
        record("Telegram configured", PASS, f"{len(cfg.telegram_chat_ids)} chat(s)")
    else:
        record("Telegram configured", FAIL, "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_IDS missing")

    if cfg.sec_user_agent and "@" in cfg.sec_user_agent:
        record("SEC user agent", PASS, cfg.sec_user_agent)
    else:
        record("SEC user agent", FAIL, "must be 'Your Name your@email.com'")

    if cfg.alpaca_enabled:
        record("Alpaca keys", PASS, "real-time news stream + movers enabled")
    else:
        record("Alpaca keys", WARN, "not set — RSS news only, Nasdaq movers fallback")


def check_classifier() -> None:
    cases = [
        ("424B5", "", CRITICAL, "priced offering"),
        ("S-3", "", CRITICAL, "shelf registration"),
        ("S-3ASR", "", CRITICAL, "automatic shelf"),
        ("FWP", "", CRITICAL, "free writing prospectus"),
        ("F-3", "", CRITICAL, "foreign shelf"),
        ("8-K", "3.02", CRITICAL, "unregistered equity sale"),
        ("8-K", "1.01", CRITICAL, "material agreement"),
        ("8-K", "2.02", HIGH, "earnings"),
        ("8-K", "", HIGH, "generic 8-K"),
        ("6-K", "", HIGH, "foreign issuer"),
        ("10-Q", "", INFO, "quarterly"),
        ("4", "", INFO, "insider form 4"),
        ("WEIRD-FORM-99", "", INFO, "unknown form still passes through"),
    ]
    bad = []
    for form, items, expected, label in cases:
        got = classify_filing(form, items=items).severity
        if got != expected:
            bad.append(f"{form}({items or '-'}) -> {got}, expected {expected}")
    if bad:
        record("Filing classifier", FAIL, "; ".join(bad))
    else:
        record("Filing classifier", PASS, f"{len(cases)} cases")

    text_cases = [
        ("we may offer and sell shares in an at-the-market offering", "ATM program"),
        ("entered into a securities purchase agreement with certain investors",
         "Securities purchase agreement"),
        ("issuance of pre-funded warrants to purchase common stock",
         "Pre-funded warrants"),
        ("a one-for-ten reverse stock split of our common stock", "Reverse split"),
    ]
    bad = []
    for text, expected in text_cases:
        if expected not in scan_text(text):
            bad.append(f"{expected!r} not found in {text[:40]!r}")
    if bad:
        record("Document text scan", FAIL, "; ".join(bad))
    else:
        record("Document text scan", PASS, f"{len(text_cases)} cases")

    headline_cases = [
        ("Acme announces pricing of $50 million public offering", CRITICAL),
        ("Acme Corp announces at-the-market offering program", CRITICAL),
        ("Acme receives Nasdaq delisting notice", CRITICAL),
        ("Acme wins $10M government contract", HIGH),
        ("Acme names new CFO", "news"),
    ]
    bad = []
    for headline, expected in headline_cases:
        got = classify_headline(headline).severity
        if got != expected:
            bad.append(f"{headline[:35]!r} -> {got}, expected {expected}")
    if bad:
        record("Headline classifier", FAIL, "; ".join(bad))
    else:
        record("Headline classifier", PASS, f"{len(headline_cases)} cases")


def check_timestamps() -> None:
    """Guard the two SEC time formats, which disagree in a non-obvious way."""
    import calendar

    from .sources.sec import _parse_acceptance
    from .util import fmt_et

    # data.sec.gov stamps Eastern time but labels it Z. Taking the Z literally
    # shifts filings 4-5 hours into the past and can push them past the
    # staleness cutoff, so this must render back as 10:47 AM, not 6:47 AM.
    got = fmt_et(_parse_acceptance("2026-07-29T10:47:32.000Z"))
    if got.startswith("10:47:32 AM"):
        record("Submissions timestamp", PASS, got)
    else:
        record("Submissions timestamp", FAIL, f"got {got!r}, expected 10:47:32 AM ET")

    # The atom feed carries a real offset; feedparser normalises to UTC.
    import email.utils

    parsed = email.utils.parsedate_tz("Wed, 29 Jul 2026 14:47:32 +0000")
    ts = calendar.timegm(parsed[:9])
    got2 = fmt_et(ts)
    if got2.startswith("10:47:32 AM"):
        record("Atom feed timestamp", PASS, got2)
    else:
        record("Atom feed timestamp", FAIL, f"got {got2!r}, expected 10:47:32 AM ET")


def check_ticker_attribution() -> None:
    """Yahoo's per-ticker feed carries unrelated market stories."""
    from .sources.news_rss import mentions_ticker

    # Real headlines pulled from NVDA's Yahoo feed during live testing that
    # have nothing to do with NVDA.
    not_about = [
        ("NVDA", "NVIDIA CORP", "DoorDash Gets FAA Approval to Fly Delivery Drones"),
        ("NVDA", "NVIDIA CORP", "Iran Just Launched a Surprise Missile Attack on U.S. Forces"),
        ("NVDA", "NVIDIA CORP", "IonQ, Rigetti, and D-Wave Quantum Are Down 30% in a Month."),
    ]
    about = [
        ("NVDA", "NVIDIA CORP", "Nvidia Reports Record Data Center Revenue"),
        ("NVDA", "NVIDIA CORP", "Why $NVDA Slipped After Hours"),
        ("AMD", "ADVANCED MICRO DEVICES INC", "AMD unveils new MI400 accelerator"),
        ("RDGT", "Ridgetech Inc.", "Ridgetech announces pricing of public offering"),
    ]
    false_pos = [h for t, n, h in not_about if mentions_ticker(t, n, h)]
    missed = [h for t, n, h in about if not mentions_ticker(t, n, h)]
    if false_pos or missed:
        record(
            "News ticker attribution", FAIL,
            f"wrongly_attributed={false_pos} missed={missed}",
        )
    else:
        record(
            "News ticker attribution", PASS,
            f"{len(not_about)} unrelated demoted, {len(about)} real kept",
        )


def check_opinion_filter() -> None:
    from .sources.news_rss import is_opinion

    junk = [
        "Should You Buy Nvidia Stock Before Earnings?",
        "3 Reasons to Buy AMD Stock Right Now",
        "Prediction: This AI Stock Will Soar in 2027",
        "Better Buy: AMD vs. Nvidia — Which Wins?",
        "Where Will Palantir Stock Be in 5 Years?",
    ]
    real = [
        "Acme Corp Announces Pricing of Public Offering",
        "Nvidia Reports Record Q3 Revenue of $35.1 Billion",
        "Acme Receives FDA Clearance for Diagnostic Platform",
        "Acme Announces $200 Million At-The-Market Offering",
    ]
    missed = [h for h in junk if not is_opinion(h)]
    false_pos = [h for h in real if is_opinion(h)]
    if missed or false_pos:
        record(
            "Opinion filter", FAIL,
            f"missed={missed} false_positives={false_pos}",
        )
    else:
        record("Opinion filter", PASS, f"{len(junk)} junk blocked, {len(real)} real kept")


# ---------------------------------------------------------------------------
# Live upstream checks
# ---------------------------------------------------------------------------


async def check_sec(client: httpx.AsyncClient) -> None:
    from .sources.sec import _TITLE_RE, GETCURRENT, _headers
    from .tickers import index

    try:
        t0 = time.monotonic()
        resp = await client.get(
            GETCURRENT.format(count=20), headers=_headers(), timeout=45
        )
        resp.raise_for_status()
        parsed = feedparser.parse(resp.text)
        matched = sum(1 for e in parsed.entries if _TITLE_RE.match(getattr(e, "title", "")))
        dt = time.monotonic() - t0
        if matched >= max(1, len(parsed.entries) - 2):
            record("EDGAR live feed", PASS,
                   f"{matched}/{len(parsed.entries)} parsed in {dt:.1f}s")
        else:
            record("EDGAR live feed", FAIL,
                   f"only {matched}/{len(parsed.entries)} titles parsed")
    except Exception as exc:  # noqa: BLE001
        record("EDGAR live feed", FAIL, f"{type(exc).__name__}: {exc}")

    try:
        resp = await client.get(
            GETCURRENT.format(count=10) + "&type=424B5", headers=_headers(), timeout=45
        )
        parsed = feedparser.parse(resp.text)
        forms = {
            (_TITLE_RE.match(getattr(e, "title", "")) or [None, ""])
            and _TITLE_RE.match(getattr(e, "title", "")).group("form")
            for e in parsed.entries
            if _TITLE_RE.match(getattr(e, "title", ""))
        }
        if forms and forms <= {"424B5"}:
            record("EDGAR form filter", PASS, f"{len(parsed.entries)} x 424B5")
        else:
            record("EDGAR form filter", WARN, f"unexpected forms: {forms}")
    except Exception as exc:  # noqa: BLE001
        record("EDGAR form filter", FAIL, f"{type(exc).__name__}: {exc}")

    try:
        n = await index.refresh(client, force=True)
        cik = index.cik_for("NVDA")
        if n > 5000 and cik == "0001045810":
            record("SEC ticker index", PASS, f"{n} symbols, NVDA -> {cik}")
        else:
            record("SEC ticker index", FAIL, f"{n} symbols, NVDA -> {cik}")
    except Exception as exc:  # noqa: BLE001
        record("SEC ticker index", FAIL, f"{type(exc).__name__}: {exc}")

    try:
        from .sources.sec import SUBMISSIONS, _data_headers

        resp = await client.get(
            SUBMISSIONS.format(cik="0001045810"), headers=_data_headers(), timeout=30
        )
        resp.raise_for_status()
        recent = resp.json()["filings"]["recent"]
        record("SEC submissions API", PASS,
               f"{len(recent.get('form', []))} recent filings for NVDA")
    except Exception as exc:  # noqa: BLE001
        record("SEC submissions API", FAIL, f"{type(exc).__name__}: {exc}")


async def check_news(client: httpx.AsyncClient) -> None:
    from .sources.news_rss import WIRES, YAHOO, _fetch_feed

    try:
        parsed = await _fetch_feed(client, YAHOO.format(t="NVDA"))
        if parsed.entries:
            record("Yahoo ticker RSS", PASS, f"{len(parsed.entries)} headlines")
        else:
            record("Yahoo ticker RSS", WARN, "no entries returned")
    except Exception as exc:  # noqa: BLE001
        record("Yahoo ticker RSS", FAIL, f"{type(exc).__name__}: {exc}")

    for label, url in WIRES:
        try:
            parsed = await _fetch_feed(client, url)
            if parsed.entries:
                record(f"Wire: {label}", PASS, f"{len(parsed.entries)} items")
            else:
                record(f"Wire: {label}", WARN, "no entries")
        except Exception as exc:  # noqa: BLE001
            record(f"Wire: {label}", WARN, f"{type(exc).__name__}: {exc}")


async def check_movers(client: httpx.AsyncClient) -> None:
    from .sources.movers import MoversWatcher

    watcher = MoversWatcher(client)
    if cfg.alpaca_enabled:
        try:
            rows = await watcher._from_alpaca()
            if rows:
                top = ", ".join(f"{r['ticker']} {r['pct']:+.1f}%" for r in rows[:5])
                record("Alpaca screener", PASS, f"{len(rows)} rows — {top}")
            else:
                record("Alpaca screener", WARN, "no rows (market may be closed)")
        except Exception as exc:  # noqa: BLE001
            record("Alpaca screener", WARN, f"{type(exc).__name__}: {exc}")

    try:
        rows = await watcher._from_nasdaq()
        record("Nasdaq screener fallback", PASS if rows else WARN, f"{len(rows)} rows")
    except Exception as exc:  # noqa: BLE001
        record("Nasdaq screener fallback", WARN, f"{type(exc).__name__}: {exc}")


async def check_alpaca_stream() -> None:
    if not cfg.alpaca_enabled:
        return
    import json

    import websockets

    from .sources.news_alpaca import WS_URL

    try:
        async with websockets.connect(WS_URL, close_timeout=5) as ws:
            await asyncio.wait_for(ws.recv(), timeout=10)
            await ws.send(
                json.dumps(
                    {"action": "auth", "key": cfg.alpaca_key, "secret": cfg.alpaca_secret}
                )
            )
            reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            msgs = reply if isinstance(reply, list) else [reply]
            if any(m.get("T") == "success" and m.get("msg") == "authenticated" for m in msgs):
                record("Alpaca news websocket", PASS, "authenticated")
            else:
                record("Alpaca news websocket", FAIL, f"auth rejected: {msgs}")
    except Exception as exc:  # noqa: BLE001
        record("Alpaca news websocket", FAIL, f"{type(exc).__name__}: {exc}")


async def check_telegram(client: httpx.AsyncClient, send: bool) -> None:
    from . import telegram_bot

    if not cfg.telegram_token:
        return
    telegram_bot.set_client(client)
    try:
        me = await telegram_bot._call("getMe")
        record("Telegram bot reachable", PASS, f"@{me.get('username')}")
    except Exception as exc:  # noqa: BLE001
        record("Telegram bot reachable", FAIL, f"{type(exc).__name__}: {exc}")
        return

    if send:
        try:
            await telegram_bot.send_message(
                "🧪 <b>Self-test</b>\nIf you can read this, alerts are working.",
                disable_preview=True,
            )
            record("Telegram send", PASS, "check your phone")
        except Exception as exc:  # noqa: BLE001
            record("Telegram send", FAIL, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------


async def main_async(args) -> int:
    print("\nticker-alerts self-test\n" + "=" * 60)
    check_config()
    check_classifier()
    check_timestamps()
    check_ticker_attribution()
    check_opinion_filter()

    if not args.offline:
        print("-" * 60)
        async with httpx.AsyncClient(follow_redirects=True, timeout=45) as client:
            await check_sec(client)
            await check_news(client)
            await check_movers(client)
            await check_alpaca_stream()
            await check_telegram(client, args.telegram)

    print("=" * 60)
    failed = [r for r in results if r[1] == FAIL]
    warned = [r for r in results if r[1] == WARN]
    print(
        f"{len(results) - len(failed) - len(warned)} passed, "
        f"{len(warned)} warnings, {len(failed)} failed"
    )
    if failed:
        print("\nFailures — see RUNBOOK.md for each:")
        for name, _, detail in failed:
            print(f"  • {name}: {detail}")
        return 1
    print("\nAll good.")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="ticker-alerts self-test")
    parser.add_argument("--offline", action="store_true", help="skip network checks")
    parser.add_argument(
        "--telegram", action="store_true", help="actually send a test message"
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
