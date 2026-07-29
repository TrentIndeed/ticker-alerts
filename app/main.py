"""Entry point: wires every source together and supervises them.

Each source runs as its own asyncio task under `resilient_loop`, which means a
failure in one (say, Yahoo's RSS going down) cannot stop the SEC pollers. The
supervisor's only job is to start everything and stay out of the way.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

import httpx

from . import __version__
from .alerts import dispatcher, emit_system
from .classify import INFO
from .config import ConfigError, cfg
from .health import daily_summary, health_server, housekeeping, watchdog
from .sources import movers as movers_mod
from .sources import news_alpaca, news_rss
from .sources import sec as sec_mod
from .store import store
from .tickers import backfill_ciks, index
from .util import resilient_loop

log = logging.getLogger("ticker-alerts")

# Shared handles a few commands need to reach (e.g. /movers).
runtime: dict = {}


def setup_logging() -> None:
    # Alert text contains emoji. On a non-UTF-8 console (Windows cp1252) that
    # makes every log line raise inside the logging handler, which buries the
    # lines you actually need when diagnosing a problem.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    logging.basicConfig(
        level=getattr(logging, cfg.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


async def _startup_banner() -> None:
    watchlist = sorted(store.tickers())
    detail = [
        f"Version {__version__}",
        f"Alert window {cfg.alert_start_hour:02d}:00–{cfg.alert_end_hour:02d}:00 ET",
        f"SEC index: {len(index)} symbols",
        f"News stream: {'Alpaca (live)' if cfg.alpaca_enabled else 'RSS only'}",
        f"Mover scanner: {'on' if cfg.movers_enabled else 'off'}",
    ]
    if watchlist:
        detail.append("Watching: " + ", ".join(f"${t}" for t in watchlist[:30]))
    else:
        detail.append("Watchlist is empty — add tickers with /add NVDA AMD")

    await emit_system("✅ Ticker Alerts started", detail, severity=INFO,
                      dedupe_key=f"startup:{asyncio.get_event_loop().time()}")


async def run() -> None:
    cfg.validate()

    limits = httpx.Limits(max_connections=30, max_keepalive_connections=15)
    async with httpx.AsyncClient(
        limits=limits, follow_redirects=True, timeout=30
    ) as client:
        from . import telegram_bot

        telegram_bot.set_client(client)

        # Resolve tickers before any poller starts, so the first pass already
        # knows which CIKs matter.
        try:
            await index.refresh(client)
            await backfill_ciks(client)
        except Exception as exc:  # noqa: BLE001
            log.error("could not load SEC ticker index at startup: %s", exc)

        await telegram_bot.set_commands_menu()

        global_feed = sec_mod.FeedWatcher(client, form=None, count=100)
        submissions = sec_mod.SubmissionsWatcher(client)
        form_feeds = [
            sec_mod.FeedWatcher(client, form=f, count=40) for f in sec_mod.WATCHED_FORMS
        ]
        ticker_news = news_rss.TickerNewsWatcher(client)
        wires = news_rss.WireWatcher(client)
        sector = news_rss.SectorNewsWatcher(client)
        scanner = movers_mod.MoversWatcher(client)

        runtime["movers"] = scanner
        runtime["client"] = client

        tasks: list[asyncio.Task] = [
            asyncio.create_task(dispatcher(), name="dispatcher"),
            asyncio.create_task(telegram_bot.poll_commands(), name="telegram"),
            asyncio.create_task(health_server(), name="health-server"),
            asyncio.create_task(watchdog(), name="watchdog"),
            asyncio.create_task(daily_summary(), name="daily-summary"),
            asyncio.create_task(housekeeping(client), name="housekeeping"),
            asyncio.create_task(
                resilient_loop(global_feed.name, cfg.sec_global_poll, global_feed.poll),
                name="sec-global",
            ),
            asyncio.create_task(
                resilient_loop(
                    submissions.name, cfg.sec_submissions_poll, submissions.poll
                ),
                name="sec-submissions",
            ),
            asyncio.create_task(
                resilient_loop(ticker_news.name, cfg.rss_poll, ticker_news.poll),
                name="rss-ticker",
            ),
            asyncio.create_task(
                resilient_loop(wires.name, cfg.rss_poll, wires.poll), name="rss-wires"
            ),
            asyncio.create_task(
                resilient_loop(sector.name, max(120, cfg.rss_poll * 3), sector.poll),
                name="rss-sector",
            ),
            asyncio.create_task(
                resilient_loop(scanner.name, cfg.movers_poll, scanner.poll),
                name="movers",
            ),
            asyncio.create_task(news_alpaca.run(), name="alpaca-news"),
        ]

        # Stagger the per-form feeds so all 13 don't fire in the same instant.
        for i, watcher in enumerate(form_feeds):
            tasks.append(
                asyncio.create_task(
                    _delayed(
                        i * 1.0,
                        resilient_loop(watcher.name, cfg.sec_form_poll, watcher.poll),
                    ),
                    name=f"sec-form-{watcher.form}",
                )
            )

        log.info("started %d tasks", len(tasks))
        await _startup_banner()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:
                # Windows: fall back to KeyboardInterrupt handling below.
                pass

        await stop.wait()
        log.info("shutting down…")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _delayed(seconds: float, coro) -> None:
    await asyncio.sleep(seconds)
    await coro


def main() -> None:
    setup_logging()
    try:
        asyncio.run(run())
    except ConfigError as exc:
        log.error("%s", exc)
        sys.exit(2)
    except KeyboardInterrupt:
        log.info("interrupted")


if __name__ == "__main__":
    main()
