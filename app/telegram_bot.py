"""Telegram: outbound alerts and the inbound command interface.

Deliberately built on plain HTTP long-polling rather than a bot framework.
Fewer moving parts, no second event loop to fight with, and when something goes
wrong the failure is a readable HTTP status instead of a library traceback.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

import httpx

from .config import cfg
from .store import store
from .util import ago, describe, fmt_et, fmt_et_full, in_alert_window

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"

_client: httpx.AsyncClient | None = None


def set_client(client: httpx.AsyncClient) -> None:
    global _client
    _client = client


async def _call(method: str, **params):
    assert _client is not None, "telegram client not initialised"
    url = API.format(token=cfg.telegram_token, method=method)
    resp = await _client.post(url, json=params, timeout=40)
    if resp.status_code == 429:
        retry = 3
        try:
            retry = int(resp.json().get("parameters", {}).get("retry_after", 3))
        except Exception:  # noqa: BLE001
            pass
        log.warning("Telegram rate limited; sleeping %ss", retry)
        await asyncio.sleep(retry + 1)
        resp = await _client.post(url, json=params, timeout=40)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {payload}")
    return payload.get("result")


async def send_message(
    text: str,
    chat_id: str | None = None,
    *,
    disable_preview: bool = True,
    silent: bool = False,
) -> None:
    """Send to one chat, or to every configured chat when chat_id is None."""
    targets = [chat_id] if chat_id else cfg.telegram_chat_ids
    for target in targets:
        for attempt in range(3):
            try:
                await _call(
                    "sendMessage",
                    chat_id=target,
                    text=text[:4096],
                    parse_mode="HTML",
                    disable_web_page_preview=disable_preview,
                    disable_notification=silent,
                )
                break
            except Exception as exc:  # noqa: BLE001
                if attempt == 2:
                    log.error("failed to send to %s: %s", target, exc)
                else:
                    await asyncio.sleep(1.5 * (attempt + 1))


def _authorized(chat_id: str) -> bool:
    return str(chat_id) in {str(c) for c in cfg.telegram_chat_ids}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

HELP = """<b>Ticker Alerts — commands</b>

<b>Watchlist</b>
<code>/add NVDA AMD SMCI</code> — start watching (space or comma separated)
<code>/remove NVDA</code> — stop watching
<code>/list</code> — show everything being watched right now

<b>Status</b>
<code>/status</code> — is everything alive, when did each feed last work
<code>/movers</code> — the scanner's current top movers
<code>/recent</code> — last 10 alerts sent
<code>/test</code> — send yourself a test alert

<b>Quiet</b>
<code>/mute 60</code> — mute non-critical alerts for 60 minutes
<code>/unmute</code> — turn alerts back on

Dilution filings (offerings, shelfs, ATMs) are <b>never</b> muted.

You can skip the slash — "add nvda" works too."""


async def _cmd_add(args: list[str], chat_id: str) -> str:
    from .tickers import index

    raw = [a.upper().strip(" ,$") for a in args]
    symbols = [s for s in raw if s]
    if not symbols:
        return "Give me at least one ticker. Example: <code>/add NVDA AMD</code>"

    added, already, unknown = [], [], []
    for sym in symbols:
        if not sym.isalpha() or len(sym) > 5:
            unknown.append(sym)
            continue
        cik = index.cik_for(sym)
        if not cik:
            # Still add it — news alerts work without a CIK, and the daily
            # ticker-index refresh may pick it up (new listings lag by a day).
            if store.add_ticker(sym, source="manual", note="no CIK yet"):
                added.append(f"{sym} (news only — no SEC match yet)")
            else:
                already.append(sym)
            continue
        if store.add_ticker(sym, cik=cik, name=index.name_for(sym), source="manual"):
            added.append(sym)
        else:
            already.append(sym)

    parts = []
    if added:
        parts.append("✅ Watching: " + ", ".join(f"<b>${a}</b>" for a in added))
    if already:
        parts.append("Already on the list: " + ", ".join(already))
    if unknown:
        parts.append("❓ Doesn't look like a ticker: " + ", ".join(unknown))
    return "\n".join(parts)


async def _cmd_remove(args: list[str], chat_id: str) -> str:
    symbols = [a.upper().strip(" ,$") for a in args if a.strip(" ,$")]
    if not symbols:
        return "Give me at least one ticker. Example: <code>/remove NVDA</code>"
    removed = [s for s in symbols if store.remove_ticker(s)]
    missing = [s for s in symbols if s not in removed]
    parts = []
    if removed:
        parts.append("🗑 Removed: " + ", ".join(removed))
    if missing:
        parts.append("Not on the list: " + ", ".join(missing))
    return "\n".join(parts)


async def _cmd_list(args: list[str], chat_id: str) -> str:
    rows = store.watchlist()
    if not rows:
        return "Watchlist is empty. Add one with <code>/add NVDA</code>"

    manual = [r for r in rows if r["source"] == "manual"]
    scanner = [r for r in rows if r["source"] != "manual"]

    lines = [f"<b>Watchlist — {len(rows)} ticker(s)</b>", ""]
    if manual:
        lines.append(f"<b>Yours ({len(manual)})</b>")
        for r in manual:
            flag = "" if r["cik"] else " ⚠️ no SEC match"
            lines.append(f"  ${r['ticker']}{flag}")
    if scanner:
        lines.append("")
        lines.append(f"<b>Auto-discovered today ({len(scanner)})</b>")
        for r in scanner:
            note = f" — {r['note']}" if r["note"] else ""
            lines.append(f"  ${r['ticker']}{html.escape(note)}")
        lines.append("<i>These clear at the end of the session.</i>")
    return "\n".join(lines)


async def _cmd_status(args: list[str], chat_id: str) -> str:
    from .alerts import is_muted, queue

    now = time.time()
    rows = store.health()
    lines = ["<b>System status</b>", ""]

    if not rows:
        lines.append("No feeds have reported in yet — still starting up.")
    for r in rows:
        last_ok = r["last_ok"]
        if last_ok is None:
            mark, when = "❌", "never"
        else:
            age = now - last_ok
            mark = "✅" if age < cfg.watchdog_stale else "⚠️"
            when = ago(age)
        lines.append(f"{mark} <code>{r['source']}</code> — {when}")
        if r["last_err"] and (r["last_err_ts"] or 0) > (last_ok or 0):
            lines.append(f"     <i>{html.escape(str(r['last_err'])[:120])}</i>")

    counts = store.alert_counts_since(now - 86400)
    total = sum(counts.values())
    lines += [
        "",
        f"<b>Watchlist:</b> {len(store.tickers())} ticker(s)",
        f"<b>Alerts (24h):</b> {total}"
        + (f" — {', '.join(f'{k}: {v}' for k, v in sorted(counts.items()))}" if counts else ""),
        f"<b>Queue depth:</b> {queue.qsize()}",
        f"<b>Alert window:</b> {cfg.alert_start_hour:02d}:00–{cfg.alert_end_hour:02d}:00 ET"
        + (" — <b>open</b>" if in_alert_window() else " — closed"),
        f"<b>Muted:</b> {'yes' if is_muted() else 'no'}",
        f"<b>Time now:</b> {fmt_et_full(time.time())}",
    ]
    return "\n".join(lines)


async def _cmd_movers(args: list[str], chat_id: str) -> str:
    from .main import runtime

    watcher = runtime.get("movers")
    if not watcher or not watcher.last_scan:
        return "No scan yet. The scanner runs every few minutes during the session."
    lines = ["<b>Top movers — latest scan</b>", ""]
    for r in watcher.last_scan[:20]:
        bits = [f"<b>${r['ticker']}</b> {r['pct']:+.1f}%"]
        if r["price"]:
            bits.append(f"${r['price']:.2f}")
        if r["volume"]:
            bits.append(f"{int(r['volume']):,} sh")
        lines.append(" · ".join(bits))
    return "\n".join(lines)


async def _cmd_recent(args: list[str], chat_id: str) -> str:
    rows = store.recent_alerts(10)
    if not rows:
        return "No alerts sent yet."
    lines = ["<b>Last 10 alerts</b>", ""]
    for r in rows:
        tick = f"${r['ticker']} " if r["ticker"] else ""
        lines.append(
            f"<i>{fmt_et(r['ts'])}</i> {tick}{html.escape(str(r['title'])[:90])}"
        )
    return "\n".join(lines)


async def _cmd_mute(args: list[str], chat_id: str) -> str:
    minutes = 60
    if args:
        try:
            minutes = max(1, min(int(args[0]), 24 * 60))
        except ValueError:
            return "Usage: <code>/mute 60</code> (minutes)"
    until = time.time() + minutes * 60
    store.set("muted_until", str(until))
    return (
        f"🔇 Muted for {minutes} min (until {fmt_et(until)}).\n"
        "Dilution filings will still come through."
    )


async def _cmd_unmute(args: list[str], chat_id: str) -> str:
    store.set("muted_until", "0")
    return "🔔 Alerts back on."


async def _cmd_test(args: list[str], chat_id: str) -> str:
    from .alerts import Alert, emit
    from .classify import CRITICAL

    await emit(
        Alert(
            severity=CRITICAL,
            kind="filing",
            ticker="TEST",
            title="424B5 — Prospectus supplement — PRICED OFFERING",
            url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent",
            company="Example Corp (this is a test)",
            source="EDGAR/test",
            detail=["ATM program", "Pre-funded warrants", "Size: $25.0M"],
            filed_ts=time.time(),
            dedupe_key=f"test:{time.time()}",
            bypass_window=True,
        )
    )
    return "Test alert queued — you should see it in a second."


COMMANDS = {
    "add": _cmd_add,
    "watch": _cmd_add,
    "remove": _cmd_remove,
    "rm": _cmd_remove,
    "delete": _cmd_remove,
    "unwatch": _cmd_remove,
    "list": _cmd_list,
    "ls": _cmd_list,
    "status": _cmd_status,
    "health": _cmd_status,
    "movers": _cmd_movers,
    "recent": _cmd_recent,
    "mute": _cmd_mute,
    "unmute": _cmd_unmute,
    "test": _cmd_test,
}


async def handle_text(text: str, chat_id: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    parts = text.replace(",", " ").split()
    head = parts[0].lstrip("/").lower()
    # /add@MyBotName in a group chat
    head = head.split("@")[0]
    args = parts[1:]

    if head in ("start", "help", "?"):
        await send_message(HELP, chat_id, disable_preview=True)
        return

    handler = COMMANDS.get(head)
    if not handler:
        await send_message(
            f"Don't know <code>{html.escape(head)}</code>. Send /help for the list.",
            chat_id,
        )
        return

    try:
        reply = await handler(args, chat_id)
    except Exception as exc:
        log.error("command %s failed: %s", head, exc, exc_info=True)
        reply = f"That command broke: {html.escape(str(exc)[:200])}"
    if reply:
        await send_message(reply, chat_id, disable_preview=True)


async def poll_commands() -> None:
    """Long-poll getUpdates forever."""
    try:
        offset = int(store.get("tg_offset", "0") or 0)
    except ValueError:
        offset = 0

    backoff = 1.0
    while True:
        try:
            updates = await _call(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates=["message"],
            )
            backoff = 1.0
            store.mark_health("telegram", ok=True)

            for update in updates or []:
                offset = max(offset, int(update.get("update_id", 0)) + 1)
                message = update.get("message") or {}
                chat_id = str((message.get("chat") or {}).get("id", ""))
                text = message.get("text") or ""
                if not chat_id or not text:
                    continue
                if not _authorized(chat_id):
                    log.warning("ignoring message from unauthorized chat %s", chat_id)
                    continue
                await handle_text(text, chat_id)

            store.set("tg_offset", str(offset))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            store.mark_health("telegram", ok=False, err=describe(exc))
            log.warning("getUpdates failed: %s (retry in %.0fs)", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def set_commands_menu() -> None:
    """Register the slash-command menu so Telegram autocompletes them."""
    try:
        await _call(
            "setMyCommands",
            commands=[
                {"command": "add", "description": "Watch ticker(s): /add NVDA AMD"},
                {"command": "remove", "description": "Stop watching: /remove NVDA"},
                {"command": "list", "description": "Show the watchlist"},
                {"command": "status", "description": "Are all feeds alive?"},
                {"command": "movers", "description": "Today's top movers"},
                {"command": "recent", "description": "Last 10 alerts"},
                {"command": "mute", "description": "Mute non-critical: /mute 60"},
                {"command": "unmute", "description": "Turn alerts back on"},
                {"command": "test", "description": "Send a test alert"},
                {"command": "help", "description": "Show all commands"},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not set command menu: %s", exc)
