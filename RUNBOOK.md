# Runbook — when something breaks

Written to be followed without knowing Python. Work top to bottom.

## The one command that answers most questions

```bash
cd /opt/ticker-alerts
docker compose run --rm ticker-alerts python -m app.selftest
```

This checks every upstream and prints `OK`, `WARN` or `FAIL` for each. Add
`--telegram` to also send a real test message to your phone.

Then find your `FAIL` line below.

---

## Quick triage

| Symptom | Go to |
|---|---|
| No alerts at all, ever | [A](#a-no-alerts-at-all) |
| Bot ignores my commands | [B](#b-bot-ignores-commands) |
| Got a ⚠️ FEED PROBLEM message | [C](#c-feed-problem-alert) |
| Too many alerts | [D](#d-too-much-noise) |
| Missed a filing I know happened | [E](#e-missed-a-filing) |
| Container keeps restarting | [F](#f-container-restart-loop) |
| It was working, now nothing | [G](#g-was-working-now-silent) |

---

## A. No alerts at all

**1. Is it running?**

```bash
docker compose ps
```

Expect `ticker-alerts   Up X minutes (healthy)`. If it's missing or
`Exited`, jump to [F](#f-container-restart-loop).

**2. Do you have any tickers?**

Message the bot `/list`. An empty watchlist means nothing to alert on.
Fix: `/add NVDA AMD`.

**3. Is Telegram wired up?**

```bash
docker compose run --rm ticker-alerts python -m app.selftest --telegram
```

- `Telegram configured: FAIL` → `TELEGRAM_BOT_TOKEN` or `TELEGRAM_CHAT_IDS`
  is empty in `.env`. Re-read step 1 of the README setup.
- `Telegram bot reachable: FAIL` → the token is wrong. Get a fresh one from
  @BotFather with `/mytoken`.
- `Telegram send: FAIL` with `chat not found` → you never pressed **Start** on
  your bot. Open the bot in Telegram, press Start, try again.

**4. Is it just quiet?**

Genuinely possible. `/status` shows how many alerts fired in the last 24h and
when each feed last succeeded. If feeds are green and the count is low, nothing
happened. Confirm the pipe works end to end with `/test`.

---

## B. Bot ignores commands

Almost always the chat ID.

```bash
docker compose logs --tail 50 | grep -i unauthor
```

If you see `ignoring message from unauthorized chat 987654321`, that number is
your real chat ID and it doesn't match `.env`. Fix it:

```bash
nano .env          # set TELEGRAM_CHAT_IDS=987654321
docker compose up -d
```

This is a safety feature — without it anyone who finds your bot could read your
watchlist.

---

## C. FEED PROBLEM alert

The message names the failing source and how long it's been down. Most of these
recover on their own; the alert exists so you know whether to trust the silence.

| Source | Meaning | What to do |
|---|---|---|
| `sec_feed[ALL]` | EDGAR's main feed is unreachable | Usually a slow SEC afternoon. Two other SEC paths are still running. If it persists >30 min, see below. |
| `sec_submissions` | data.sec.gov unreachable | Same — the feed paths still cover you. |
| `sec_feed[424B5]` etc. | One form feed is down | Low risk; the other two paths still catch it. |
| `telegram` | Can't reach Telegram | Check the server has internet: `curl -s https://api.telegram.org` |
| `alpaca_news` | News websocket dropped | Reconnects automatically with backoff. RSS still covers news. |
| `movers` | Screener unavailable | Cosmetic. Your own watchlist is unaffected. |

**If every SEC source is failing at once**, you have most likely been throttled
by the SEC. Check:

```bash
docker compose logs --tail 100 | grep -iE "403|429|throttl|denied"
```

A 403 from EDGAR means your `SEC_USER_AGENT` is missing or not a real
name+email. Fix it in `.env` and restart. Blocks lift on their own within about
10 minutes once you're identifying properly.

---

## D. Too much noise

In order of how much they'll quiet things down:

**Turn off sector news** — the general NASDAQ/AI headlines, not your tickers:

```bash
nano .env      # SECTOR_NEWS_ENABLED=false
docker compose up -d
```

**Make the mover scanner pickier**, or turn it off:

```
MOVERS_MIN_PCT=12.0        # was 6.0 — only bigger movers
MOVERS_MAX_TICKERS=10      # was 25
MOVERS_ENABLED=false       # or off entirely
```

**Confirm opinion filtering is on** (it drops "Should You Buy X?" articles):

```
FILTER_OPINION=true
```

**Temporarily**: `/mute 120` for two hours. Dilution filings still come
through — that's deliberate and not configurable, since it's the thing you
asked never to miss.

**Never do this**: setting `SEC_WIDE_NET=true` adds hundreds of daily alerts
from bank structured-note filings. It exists for completeness, not for use.

---

## E. Missed a filing

Take this seriously — it's the one failure that matters. Work through it in
order.

**1. Was the ticker actually being watched at the time?**

`/list` shows the watchlist *now*. An auto-discovered mover expires at 8pm, so
a filing at 9pm for a scanner-added name wouldn't have been covered. Add it
yourself with `/add TICKER` to make it permanent.

**2. Did the system see it but classify it quietly?**

```bash
docker compose logs --since 24h | grep -i TICKER
```

If you see the filing logged as `[info/filing]`, it was sent as a silent
notification rather than missed. Check your Telegram notification settings for
the chat.

**3. Was it suppressed?**

```bash
docker compose logs --since 24h | grep -i suppressed
```

`suppressed (muted)` or `suppressed (outside alert window)` explains it.
Non-critical alerts don't fire outside 4am–8pm ET.

**4. Was the ticker resolvable to a CIK?**

`/list` flags names with `⚠️ no SEC match`. Those get news but not filings —
usually a very new listing. The ticker index refreshes hourly from the SEC, so
it typically resolves itself within a day.

**5. Genuinely missed?**

Grab the accession number from the SEC filing page and check:

```bash
docker compose logs --since 48h | grep 0001234567-26-123456
```

Nothing at all means all three detection paths missed it. Capture the
accession number, form type, and `docker compose logs --since 48h > /tmp/log.txt`
before restarting anything — that's what's needed to diagnose it.

---

## F. Container restart loop

```bash
docker compose logs --tail 40
```

| Log line | Cause | Fix |
|---|---|---|
| `Missing required settings in .env` | Blank required value | The message names the setting. Fill it in. |
| `ZoneInfoNotFoundError` | Timezone data missing | `docker compose build --no-cache` |
| `no such table` / `database is locked` | Corrupted database | `mv data/alerts.db data/alerts.db.bad && docker compose up -d` — rebuilds empty; you re-add tickers with `/add`. |
| `Address already in use` | Port 8087 taken | Change the left-hand port in `docker-compose.yml`. |
| `permission denied: /app/data` | Host folder ownership | `sudo chown -R $(id -u):$(id -g) data` |

---

## G. Was working, now silent

**1. Confirm it's actually silent rather than quiet**: `/status`. Green feeds
with recent timestamps means it's alive and nothing happened.

**2. Did the server reboot?**

```bash
uptime
docker compose ps
```

`restart: unless-stopped` brings it back automatically — unless someone ran
`docker compose down`, which is sticky across reboots. `docker compose up -d`.

**3. Disk full?** SQLite fails silently-ish when it can't write.

```bash
df -h /
```

If the disk is full, the usual culprit is another service's logs. This one caps
its own at 100 MB total.

**4. Nuclear option** — safe, keeps your watchlist:

```bash
cd /opt/ticker-alerts
cp data/alerts.db ~/alerts-backup.db
docker compose down
docker compose up -d --build
```

---

## Useful commands

```bash
# Live logs
docker compose logs -f

# Only what was sent
docker compose logs --since 24h | grep "sent \["

# Only problems
docker compose logs --since 24h | grep -E "ERROR|WARN|failed"

# Health as JSON
curl -s localhost:8087

# What's on the watchlist, from the server
docker compose exec ticker-alerts \
  python -c "from app.store import store; print(sorted(store.tickers()))"

# Alert counts by type, last 24h
docker compose exec ticker-alerts \
  python -c "import time;from app.store import store; print(store.alert_counts_since(time.time()-86400))"
```

## Health endpoint

`curl -s localhost:8087` returns:

```json
{
  "status": "ok",
  "degraded": [],
  "alert_window_open": true,
  "watchlist": 12,
  "alerts_24h": 34,
  "sources": {
    "sec_feed[ALL]":   {"ok": true, "last_ok_age_sec": 6,  "errors": 0},
    "sec_submissions": {"ok": true, "last_ok_age_sec": 22, "errors": 1}
  }
}
```

`"status": "degraded"` with entries in `degraded` means those sources are
stale. It's bound to localhost, so it isn't reachable from the internet.
