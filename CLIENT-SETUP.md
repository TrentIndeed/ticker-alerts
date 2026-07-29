# Setup — what I need from you

Two things, about 5 minutes. No technical knowledge needed, all inside Telegram.

---

## 1. Create the bot

1. Open Telegram and search for **@BotFather** (blue checkmark). Start a chat.
2. Send: `/newbot`
3. It asks for a **name** — anything you like, e.g. `Stock Alerts`
4. It asks for a **username** — must end in `bot`, e.g. `mystockalerts_bot`
   (if it's taken, just try another)
5. It replies with a long token that looks like this:

   ```
   1234567890:AAHxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
   ```

**Send me that token.**

> ⚠️ Copy the text, don't screenshot it. Screenshots make `0` and `O` (and
> `1`/`l`) impossible to tell apart, and the token won't work. Long-press →
> Copy, then paste.

---

## 2. Get your ID

1. Search for **@userinfobot** in Telegram and send it any message
2. It replies with something like `Id: 987654321`

**Send me that number too.**

This is what tells the system who to alert. It also locks the bot to you — no
one else can read your watchlist or change your tickers, even if they find it.

---

## 3. Press Start

Find the bot you just made (search its username), open it, and press **Start**.

Telegram blocks bots from messaging people who haven't done this, so if you
skip it you'll never get alerts.

---

## That's it

Send me those two things:

```
Token:  1234567890:AAHxxxxxxxxx...
ID:     987654321
```

I'll put them into the server config and you'll get a "Ticker Alerts started"
message confirming it's live.

---

## Once it's running

Message your bot to control it — no need to contact me for any of this:

| Send this | What happens |
|---|---|
| `/add NVDA AMD SMCI` | Start watching those tickers |
| `/remove NVDA` | Stop watching one |
| `/list` | See everything being watched |
| `/status` | Check everything's alive |
| `/test` | Send yourself a sample alert |
| `/mute 60` | Quiet for an hour (dilution alerts still come through) |
| `/help` | Full list |

You never need to edit a file or ask me to redeploy to change tickers.

**A note on what you'll receive:** the system also finds the day's biggest
movers on its own and watches those too, so you'll get alerts for tickers you
never added. That's intentional — it's covering what's in play today. They
clear automatically at the end of each session.
