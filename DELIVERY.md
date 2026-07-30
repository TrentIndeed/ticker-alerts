# Delivery guide (operator notes)

Internal runbook for standing this up for a client. The client does not touch
the server — their entire interface is Telegram.

| Who | Does what |
|---|---|
| **Client** | Creates the Telegram bot, sends you the token + their chat ID. Then manages tickers via bot commands, forever. |
| **You** | Host it, put the two values in `.env`, deploy, verify. |

---

## Phase 1 — verify locally, before you contact the client

Don't ask for credentials until you know the build is green. Nothing here needs
the client's bot.

```bash
cd ticker-alerts
cp .env.example .env
nano .env          # your own SEC_USER_AGENT is enough for this step
docker compose build
docker compose run --rm ticker-alerts python -m app.selftest
```

Expect **19 passed, 0 failed** (1 warning if Alpaca keys are unset). If
anything fails, fix it now — see [RUNBOOK.md](RUNBOOK.md). Diagnosing a broken
feed while the client is waiting on a handover call is avoidable.

**Get Alpaca keys now too.** Free, paper account, no funding. Without them news
runs on RSS at 30–60s instead of a real-time websocket, and movers fall back to
the Nasdaq screener. Two minutes for a materially better product.

---

## Phase 2 — collect credentials from the client

Send them **[CLIENT-SETUP.md](CLIENT-SETUP.md)**. Nothing else — it's written
for a non-technical reader and covers only what they need to do.

You're waiting on exactly two values:

```
TELEGRAM_BOT_TOKEN   8123456789:AAF...
TELEGRAM_CHAT_IDS    987654321
```

Three things that will otherwise cost you a support round-trip:

1. **Token as text, never a screenshot.** `0`/`O` and `1`/`l` are
   indistinguishable in most fonts, and a wrong character gives a silent
   `Unauthorized`. CLIENT-SETUP.md says this, but say it again.
2. **They must press Start on their own bot.** Telegram blocks bots from
   messaging users who haven't. Without it your send test fails with
   `chat not found`.
3. **Their bot must not have a webhook set.** A fresh bot won't. But if they
   reuse an existing bot that has one, `getUpdates` returns `409 Conflict` and
   ticker commands silently stop working while alerts keep sending — a
   confusing half-broken state. A new bot avoids this entirely.

---

## Phase 3 — deploy

### Getting the code onto the box

The repo is private, so plain `git clone` over HTTPS will prompt for auth.
Pick one:

```bash
# A. Simplest — copy from your machine, no repo auth needed
scp -r ticker-alerts root@YOUR_VPS:/opt/

# B. Deploy key (best if you'll update via git pull)
ssh root@YOUR_VPS 'ssh-keygen -t ed25519 -N "" -f ~/.ssh/ticker_alerts && cat ~/.ssh/ticker_alerts.pub'
# add that key at: github.com/TrentIndeed/ticker-alerts/settings/keys
ssh root@YOUR_VPS
git -c core.sshCommand="ssh -i ~/.ssh/ticker_alerts" clone git@github.com:TrentIndeed/ticker-alerts.git /opt/ticker-alerts

# C. gh CLI, if already authenticated on the box
gh repo clone TrentIndeed/ticker-alerts /opt/ticker-alerts
```

### Check for a port collision first

The health endpoint binds `127.0.0.1:8087`. On a shared box, confirm it's free:

```bash
ss -tlnp | grep 8087 || echo "8087 free"
```

If taken, change the left-hand number in `docker-compose.yml`. It's
localhost-only and never needs exposing through Caddy.

### Configure and start

```bash
cd /opt/ticker-alerts
cp .env.example .env
nano .env
```

Fill in:

```
TELEGRAM_BOT_TOKEN=<from client>
TELEGRAM_CHAT_IDS=<from client>
SEC_USER_AGENT=Your Name your@email.com
ALPACA_API_KEY=<yours>
ALPACA_API_SECRET=<yours>
```

Then:

```bash
chmod 600 .env
./deploy.sh
```

`deploy.sh` refuses to start if a required value is blank or the self-test
fails, so a green run means it's actually working.

### Confirm

```bash
docker compose ps                  # expect: Up (healthy)
curl -s localhost:8087             # expect: "status": "ok"
docker compose logs --tail 30
```

---

## Phase 4 — verify with the client watching

Do this live, on a call or over chat. Five minutes, and it converts "I think
it's running" into "they've seen it work."

Ask them to send, in order:

| They send | They should see |
|---|---|
| `/status` | All feeds ✅ with recent timestamps |
| `/test` | A 🚨 DILUTION RISK sample alert |
| `/add NVDA AMD` | `✅ Watching: $NVDA, $AMD` |
| `/list` | Their two names, plus auto-discovered movers |
| `/help` | Full command list |

Then set expectations explicitly, because both of these generate support
messages otherwise:

- **They'll get alerts for tickers they never added.** The scanner auto-watches
  the day's movers. It's intentional. Those clear at the end of each session.
- **Silence means nothing happened.** The system reports its own failures — a
  ⚠️ FEED PROBLEM message if a feed goes stale, and a nightly summary. Quiet is
  quiet, not broken.

---

## Phase 5 — handover message

> Ticker Alerts is live on your bot.
>
> **Managing tickers:** message the bot — `/add NVDA AMD`, `/remove NVDA`,
> `/list`. Takes effect immediately, no need to contact me.
>
> **Checking it's alive:** `/status` shows every data feed and when it last
> worked. You'll also get a short summary each evening.
>
> **Alert levels:** 🚨 is dilution — offerings, shelfs, ATMs. These reach you
> even outside market hours and even if you've muted. 🟠 is material news, 🔵
> ordinary headlines, ⚪ routine filings (silent).
>
> **Too noisy?** `/mute 60` quiets it for an hour. Dilution alerts still come
> through — that's deliberate.
>
> It also finds the day's biggest movers on its own and watches those, so
> you'll see alerts for tickers you didn't add. That's covering what's in play;
> they clear each evening.
>
> Full command list: `/help`

---

## Ongoing

**Client self-serves** (no involvement from you): adding/removing tickers,
checking status, muting, testing.

**Comes to you:** alert volume tuning (`MOVERS_MIN_PCT`, `MOVERS_MAX_TICKERS`,
`SECTOR_NEWS_ENABLED`), alert window changes, feed outages lasting more than
~30 minutes, anything in [RUNBOOK.md](RUNBOOK.md).

Since it runs on your infrastructure, **you own uptime**. Worth agreeing up
front what that means.

### Updating

```bash
cd /opt/ticker-alerts
./deploy.sh update        # git pull, rebuild, self-test, restart
```

The watchlist and alert history live in `data/alerts.db` and survive rebuilds.
Back it up before anything risky:

```bash
cp data/alerts.db ~/alerts-$(date +%F).db
```

### If you need to hand over ownership entirely

Nothing is tied to your accounts except the Alpaca keys and `SEC_USER_AGENT`.
To transfer: they create their own Alpaca account (free), put their own name
and email in `SEC_USER_AGENT`, and run `./deploy.sh` on their own box. The bot
token is already theirs.
