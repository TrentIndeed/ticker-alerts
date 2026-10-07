"""SQLite-backed state: watchlist, dedup ledger, health, alert history.

Everything the system knows lives in one file so a backup is `cp alerts.db`.
Writes are tiny and infrequent enough that a plain synchronous connection
guarded by a lock is faster and far less fragile than an async driver.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time

from .config import cfg
from .util import redact

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen (
    key       TEXT PRIMARY KEY,
    ts        REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_ts ON seen(ts);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker     TEXT PRIMARY KEY,
    cik        TEXT,
    name       TEXT,
    source     TEXT NOT NULL DEFAULT 'manual',   -- 'manual' | 'scanner'
    added_ts   REAL NOT NULL,
    expires_ts REAL,                             -- NULL = permanent
    note       TEXT
);

CREATE TABLE IF NOT EXISTS health (
    source    TEXT PRIMARY KEY,
    last_ok   REAL,
    last_err  TEXT,
    last_err_ts REAL,
    ok_count  INTEGER NOT NULL DEFAULT 0,
    err_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS settings (
    k TEXT PRIMARY KEY,
    v TEXT
);

CREATE TABLE IF NOT EXISTS alert_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    ticker   TEXT,
    severity TEXT,
    kind     TEXT,
    title    TEXT,
    url      TEXT
);
CREATE INDEX IF NOT EXISTS idx_alert_ts ON alert_log(ts);
"""


class Store:
    def __init__(self, path: str) -> None:
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        # WAL keeps reads from ever blocking the alert path behind a write.
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        # Errors stored before redaction existed could hold the bot token: mask them once on start-up.
        for source, err in list(self._db.execute("SELECT source, last_err FROM health WHERE last_err IS NOT NULL")):
            if redact(err) != err:
                self._db.execute("UPDATE health SET last_err=? WHERE source=?", (redact(err), source))
        self._db.commit()

    # ---------------- dedup ----------------

    def seen(self, key: str) -> bool:
        """Return True if we've already handled `key`; otherwise record it.

        This is the single most important function in the system. It is the
        reason a filing that appears in three different feeds only pages you
        once, and the reason a feed replaying its backlog after a restart
        doesn't spam you.
        """
        now = time.time()
        with self._lock:
            cur = self._db.execute("SELECT 1 FROM seen WHERE key = ?", (key,))
            if cur.fetchone():
                return True
            self._db.execute(
                "INSERT OR REPLACE INTO seen(key, ts) VALUES (?, ?)", (key, now)
            )
            self._db.commit()
            return False

    def prune_seen(self, older_than: float | None = None) -> int:
        cutoff = time.time() - (older_than or max(cfg.dedupe_window, 7 * 86400))
        with self._lock:
            cur = self._db.execute("DELETE FROM seen WHERE ts < ?", (cutoff,))
            self._db.commit()
            return cur.rowcount

    # ---------------- watchlist ----------------

    def add_ticker(
        self,
        ticker: str,
        *,
        cik: str | None = None,
        name: str | None = None,
        source: str = "manual",
        expires_ts: float | None = None,
        note: str | None = None,
    ) -> bool:
        """Add a ticker. Returns True if newly added.

        A manual add always wins over a scanner entry: if the scanner already
        picked up a name and you then add it yourself, it stops expiring.
        """
        ticker = ticker.upper().strip()
        with self._lock:
            existing = self._db.execute(
                "SELECT source FROM watchlist WHERE ticker = ?", (ticker,)
            ).fetchone()
            if existing:
                if source == "manual" and existing["source"] != "manual":
                    self._db.execute(
                        "UPDATE watchlist SET source='manual', expires_ts=NULL,"
                        " added_ts=? WHERE ticker=?",
                        (time.time(), ticker),
                    )
                    self._db.commit()
                return False
            self._db.execute(
                "INSERT INTO watchlist(ticker, cik, name, source, added_ts,"
                " expires_ts, note) VALUES (?,?,?,?,?,?,?)",
                (ticker, cik, name, source, time.time(), expires_ts, note),
            )
            self._db.commit()
            return True

    def remove_ticker(self, ticker: str) -> bool:
        ticker = ticker.upper().strip()
        with self._lock:
            cur = self._db.execute("DELETE FROM watchlist WHERE ticker = ?", (ticker,))
            self._db.commit()
            return cur.rowcount > 0

    def set_cik(self, ticker: str, cik: str, name: str | None = None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE watchlist SET cik = ?, name = COALESCE(?, name)"
                " WHERE ticker = ?",
                (cik, name, ticker.upper()),
            )
            self._db.commit()

    def watchlist(self, include_expired: bool = False) -> list[sqlite3.Row]:
        with self._lock:
            if include_expired:
                rows = self._db.execute(
                    "SELECT * FROM watchlist ORDER BY source, ticker"
                ).fetchall()
            else:
                rows = self._db.execute(
                    "SELECT * FROM watchlist WHERE expires_ts IS NULL"
                    " OR expires_ts > ? ORDER BY source, ticker",
                    (time.time(),),
                ).fetchall()
            return list(rows)

    def tickers(self) -> set[str]:
        return {r["ticker"] for r in self.watchlist()}

    def cik_map(self) -> dict[str, str]:
        """CIK (zero-padded, 10 digits) -> ticker, for active watchlist rows."""
        out = {}
        for r in self.watchlist():
            if r["cik"]:
                out[str(r["cik"]).zfill(10)] = r["ticker"]
        return out

    def expire_scanner_tickers(self) -> list[str]:
        now = time.time()
        with self._lock:
            rows = self._db.execute(
                "SELECT ticker FROM watchlist WHERE expires_ts IS NOT NULL"
                " AND expires_ts <= ?",
                (now,),
            ).fetchall()
            if rows:
                self._db.execute(
                    "DELETE FROM watchlist WHERE expires_ts IS NOT NULL"
                    " AND expires_ts <= ?",
                    (now,),
                )
                self._db.commit()
            return [r["ticker"] for r in rows]

    def scanner_count(self) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT COUNT(*) c FROM watchlist WHERE source='scanner'"
                " AND (expires_ts IS NULL OR expires_ts > ?)",
                (time.time(),),
            ).fetchone()
            return row["c"]

    # ---------------- health ----------------

    def mark_health(self, source: str, *, ok: bool, err: str | None = None) -> None:
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO health(source, last_ok, ok_count, err_count)"
                " VALUES (?, NULL, 0, 0) ON CONFLICT(source) DO NOTHING",
                (source,),
            )
            if ok:
                self._db.execute(
                    "UPDATE health SET last_ok=?, ok_count=ok_count+1 WHERE source=?",
                    (now, source),
                )
            else:
                self._db.execute(
                    "UPDATE health SET last_err=?, last_err_ts=?,"
                    " err_count=err_count+1 WHERE source=?",
                    (redact(err) if err else err, now, source),
                )
            self._db.commit()

    def health(self) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._db.execute("SELECT * FROM health ORDER BY source"))

    # ---------------- settings ----------------

    def get(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT v FROM settings WHERE k = ?", (key,)
            ).fetchone()
            return row["v"] if row else default

    def set(self, key: str, value: str) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO settings(k, v) VALUES (?, ?)", (key, value)
            )
            self._db.commit()

    # ---------------- alert log ----------------

    def log_alert(
        self, ticker: str, severity: str, kind: str, title: str, url: str
    ) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO alert_log(ts, ticker, severity, kind, title, url)"
                " VALUES (?,?,?,?,?,?)",
                (time.time(), ticker, severity, kind, title[:500], url),
            )
            self._db.commit()

    def alert_counts_since(self, since: float) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute(
                "SELECT kind, COUNT(*) c FROM alert_log WHERE ts >= ? GROUP BY kind",
                (since,),
            ).fetchall()
            return {r["kind"]: r["c"] for r in rows}

    def recent_alerts(self, limit: int = 10) -> list[sqlite3.Row]:
        with self._lock:
            return list(
                self._db.execute(
                    "SELECT * FROM alert_log ORDER BY ts DESC LIMIT ?", (limit,)
                )
            )


store = Store(cfg.db_path)
