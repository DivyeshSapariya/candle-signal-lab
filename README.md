# NIFTY50 / SENSEX Candle Signal Tool

A personal tool that reads NIFTY50 and SENSEX candles, finds candle patterns, checks them with several filters, and gives a trade idea: **CE, PE or No trade**, with option buy price, stop loss and target. It also has a backtest (to measure how often the rules work) and an automatic paper-trade journal.

For personal use only. This is not financial advice. No tool can predict the market. A signal is a "chance", not a promise.

**Status: Round 1 done (free, no broker account needed). Round 2 is mostly in place: Angel One live data, one year of saved candles, real option prices and futures volume. The rules still show no proven edge, see section 7c.**

---

## 1. Files in this folder

| File | What it does |
|---|---|
| `candle_patterns.py` | The candle dictionary. Finds 6 patterns and explains them. |
| `candle_signals.py` | The brain. Filters + rules that decide CE / PE / No trade, with entry, stop loss, target. Used by the dashboard AND the backtest, so both follow the same rules. |
| `trade_plan.py` | Small live layer: where the price is now compared with the plan. |
| `option_pricer.py` | Turns index levels into option (CE/PE) buy price, stop loss price, target price. |
| `data_sources.py` | Gets candles from Yahoo Finance or Angel One (live candles, futures with volume, option prices). All Angel One requests go through one queue with pauses and retries (section 9). |
| `dashboard.py` | Live chart + analysis in your browser. |
| `candle_backtest.py` | Tests the rules on past candles. |
| `candle_journal.py` | Idea history: saves every idea, checks later how it ended (success / failed), writes the story and saves a chart. |
| `candle_memory.py` | The tool's memory: remembers how every finished idea ended, recalls similar past ideas, writes lessons. |
| `live_monitor.py` | Text-only live watcher (simple version). |
| `candle_store.py` | Your own candle database. Saves NIFTY / SENSEX candles and India VIX into `market_data.db`. Can also download a year of history, the futures volume and the option chain (open interest) from Angel One (see section 3b). |
| `candle_analysis.py` | Explains the decision for the dashboard: the four "Live analysis" steps in the decision banner and the list of recent setups that were dropped, and why (section 3). It adds no rules. |
| `candle_chain.py` | Turns option open interest into the plain facts of the dashboard's Option chain card (put/call ratio, walls, change), and builds the open-interest features for the optional `oi_*` checks (section 5). |
| `candle_research.py` | Honest checks on your saved candles: walk-forward blocks, cost stress, uncertainty range, breakdowns, extra checks (section 7c). |
| `strategy_lab.py` | Tests other strategy types (opening-range breakout, VWAP pullback, trend continuation, mean reversion) on your saved candles against a strict pass/fail gate (section 7d). Also runs the short-target experiment (section 7e). Changes no live rules. |
| `candle_baseline.py` | Reality check: compares the strategy with random entries (same exits, costs and stop sizes) to see if it beats chance (see section 7b). |
| `test_no_lookahead.py` | Safety test: checks that the rules never use the future. |
| `test_candle_analysis.py` | Checks that the Live analysis steps always agree with the real decision. |
| `test_candle_research.py` | Checks the research maths, and the VWAP / volume / gap features (no look-ahead). |
| `test_strategy_lab.py` | Checks the strategy lab: no look-ahead, same simulator and costs as the main backtest, the short-target maths and baseline, and that random data fails the gate. |
| `test_candle_chain.py` | Checks the open-interest facts (last value per day, previous session, walls, edge cases). |
| `test_oi_filters.py` | Checks the open-interest features and the three optional `oi_*` checks: right numbers, no look-ahead, off by default, no numbers on thin history. |
| `test_option_chain.py` | Checks the option-chain saving with a fake Angel One (nearest expiry, strikes, no duplicates, no running candle). |
| `test_candle_backfill.py` | Checks the Angel One downloader and the rate-limit handling with a fake Angel One (no login, no internet). |
| `telegram_notify.py` | Sends a short Telegram message when a new idea appears and when it ends (see section 3c). Also has the setup helpers `--check`, `--test` and `--find-chat`. |
| `test_telegram_notify.py` | Tests the Telegram alerts with a fake Telegram, so no real bot is needed. |
| `holidays.csv` | The exchange holidays (date, name). The tool reads market status, expiry dates and the alert loop from it. Add rows from the NSE / BSE calendar; saving the file is enough (section 3d). |
| `risk_limits.py` | Optional daily limits and the position-size helper, all off until you set values in `.env` (section 3d). |
| `session_check.py` | A read-only end-of-day check: trading day, fresh data, the day's ideas and alerts, what was saved, your settings (section 3d). |
| `test_holidays.py`, `test_risk_limits.py`, `test_session_check.py` | Tests for the three items above. |
| `test_speed.py` | Checks the first-load speed-ups with fake Angel One calls: page requests go first, slow refreshes never block the page, Refresh waits for fresh numbers, the alert loop looks once per candle. |
| `test_late_trade.py` | Checks that a late-day trade is closed at the day's last candle even without a 15:25 candle, and that a running session stays open. |
| `.env.example` | Template for your private settings (Telegram, Angel One, optional risk limits). Copy it to `.env` and fill in your values (sections 3c and 9). |
| `requirements.txt` | The packages the project needs (`pip install -r requirements.txt`). |
| `README.md` | This document. |

Files created by the tool: `trade_journal.csv` (idea history), `trade_snapshots/` (a small chart of every finished idea), `trade_memory.csv` (the memory), `market_data.db` (your saved candles), `backtest_trades.csv` (only if you use `--save`), a `.cache` folder and a `logs` folder (only with Angel One; the Angel One library writes request logs there, which can contain your API key, so both are ignored by Git), and `.env` (you create it yourself from `.env.example`; it holds your private Telegram token and Angel One values and is never uploaded to Git). Do not delete `trade_journal.csv`, `trade_snapshots/`, `trade_memory.csv` and `market_data.db` if you want to keep your history, memory and candle data.

The `.venv` folder (created in section 2) holds the installed packages for this project only. It can be deleted and rebuilt at any time.

---

## 2. Setup (one time)

You need **Python 3.9 or newer**. Check: `python --version`

1. Put all files in one folder, for example `nifty-tool`.
2. Open a terminal inside that folder.
3. (Recommended) Create a virtual environment:
   - `python -m venv venv`
   - Windows: `venv\Scripts\activate`   |   Mac / Linux: `source venv/bin/activate`
4. Install packages (inside the virtual environment, so nothing is installed globally):

```
pip install -r requirements.txt
```

Tip: you do not have to activate the environment. You can also run any command with the environment's own Python, for example `.venv\Scripts\python.exe dashboard.py` (Windows).

---

## 3. How to run

| What you want | Command |
|---|---|
| Live chart dashboard (recommended) | `python dashboard.py` then open `http://127.0.0.1:5000` (it opens by itself) |
| Backtest (how good are the rules?) | `python candle_backtest.py` |
| Backtest and give the results to the memory | `python candle_backtest.py --learn` |
| Text-only live watcher | `python live_monitor.py` |
| Quick test with sample candles | `python candle_patterns.py` |
| Save today's candles to your database | `python candle_store.py collect` |
| Download a year of history from Angel One | `python candle_store.py backfill --days 365` |
| Save futures candles (real volume) from Angel One | `python candle_store.py backfill-futures` |
| Save option-chain data (open interest, volume, price) from Angel One | `python candle_store.py backfill-options` |
| See what the database holds | `python candle_store.py status` |
| Does the strategy beat random entries? | `python candle_baseline.py` |
| Walk-forward, cost stress, uncertainty and extra checks on your saved data | `python candle_research.py` |
| Test other strategy types on your saved data (about 8 minutes) | `python strategy_lab.py` (options `--index`, `--interval`, `--strategy orb\|vwap_pull\|trend_pull\|mean_rev`, `--runs`) |
| Check that the rules never use the future | `python test_no_lookahead.py` |
| Telegram: check the settings / send a test message / find the group id | `python telegram_notify.py --check` / `--test` / `--find-chat` |
| Test the Telegram alerts (no real bot needed) | `python test_telegram_notify.py` |
| End-of-day check (read-only) | `python session_check.py` (options `--date YYYY-MM-DD`, `--no-live`) |
| Test smaller targets on the current rules' entries (section 7e) | `python strategy_lab.py --short-targets` (options `--index`, `--interval`, `--variant r0.5\|r1.0\|rs10\|rs20\|pct30\|pct50`, `--runs`) |
| Other tests | `python test_candle_analysis.py`, `python test_candle_research.py`, `python test_candle_backfill.py`, `python test_option_chain.py`, `python test_candle_chain.py`, `python test_oi_filters.py`, `python test_strategy_lab.py`, `python test_holidays.py`, `python test_risk_limits.py`, `python test_session_check.py`, `python test_late_trade.py`, `python test_speed.py` |

Stop any of them with **Ctrl + C**.

### Dashboard options
`python dashboard.py --port 5050` (other port), `--no-browser` (do not open the browser), `--no-reload` (turn off auto-reload, see below), `--no-alerts` (do not run the background Telegram alert loop, see section 3c).

**Auto-reload:** you do not need to stop and start the dashboard after changing the code. When any `.py` file in the project changes, the server restarts by itself and the page open in your browser refreshes itself a moment later (it may show "Cannot reach the dashboard server" for a second or two while the server restarts). Only `.py` files trigger it; changes to this README or to the data files (`trade_journal.csv`, `market_data.db`) do not. A dashboard that was started before auto-reload existed does not have it: stop it once with Ctrl + C and start it again. Use `--no-reload` if you want the old behaviour (the server never restarts by itself).

In the page: use the buttons at the top to pick NIFTY / SENSEX and 1 / 5 / 15 minute candles, and the drop-down to pick the data source (Angel One when its four values are in your `.env`, otherwise Yahoo; see section 9). The page remembers your choices, and the moon / sun button switches between dark and light theme. It refreshes every 15 seconds. The chart library and the fonts (Inter, JetBrains Mono) load from the internet, so you need internet; without it the page falls back to system fonts only if the chart library was already cached. If a refresh fails (for example Angel One is busy), the page keeps the last good numbers for up to 5 minutes and tries again by itself.

**Page layout (three tiers, dark colours with a light theme):**
- **Top bar:** logo and title, market and data status (open / closed, data up to date or delayed), the time of the last update, and the controls, in one row (it wraps on a narrow window).
- **Decision banner (the answer first):** a round status icon (amber for no trade, green for CE, red for PE), the title ("No trade right now", or "Buy CE (call) idea" / "Buy PE (put) idea") and one sentence. On the right are the four steps of the **Live analysis**: 1. Pattern, 2. Confirmation, 3. Filters, 4. Agreement, each a Pass / Fail card (hover for the detail) with one line saying where the decision stands and, while the market is open, when the next candle closes. When there is an idea, the BUY price, STOP LOSS and TARGET tiles show there too. A "Practice only" tag appears when the market is closed or the data is delayed, and a strip along the bottom explains why waiting is normal.
- **Tier 1, the chart** (left) with **Why this decision** (right: the checklist split into "Must pass" and "Bonus points", with the agreement meter; **Trade details** appears below it only when there is an idea: the option, the index levels, the memory note and "How to place this trade", which starts closed).
  - Above the chart: price and change, the CE / PE legend, "Reset view", a row of chart options (below) and zoom presets (last 50 candles, last 100, All).
  - When there is an idea, dashed ENTRY, STOP LOSS and TARGET lines are drawn on the chart.
  - Under the chart, one tile per fact: VWAP (and whether the price is above or below), the opening gap, volume against its average, and from the option chain the put/call OI ratio, the call wall and the put wall.
- **Tier 2, context:** **Latest finished candle** (a small drawing of the candle, open / high / low / close, colour, trend before, and the pattern with its meaning) and **Recent candle patterns**; the **Option chain (open interest)** card; and the **Paper-trade scoreboard** (ideas, win rate, net result, latest ideas).
  - Option chain: a bar per strike for calls (red) and puts (green) with the OI and its change since the previous session, the strike nearest the index highlighted, and a few plain sentences (where the index sits between the walls). With Angel One it uses the current OI (one read-only request, cached for a minute); with Yahoo, or if that fails, the last saved day (section 3b). The heading says "live" or "saved". **Facts only: no rule or alert uses it.**
- **Tier 3, track record:** **Idea history** and **What the tool learned** (tabs in one card; click a row in the history to open its story, prices and chart), and **Recent setups with no idea**: the last 8 times a pattern appeared but no idea was made, each with the first rule that said no (for example "15 min trend DOWN, 60 min trend UP - does not agree"). This shows why you see so few ideas.
- A collapsed **Quick guide** and a short safety note finish the page.

**Chart options.** A row of buttons above the chart switches each item on or off; the page remembers your choice. Indicator lines come from the server and use only finished candles.

| Option | Shows | Default |
|---|---|---|
| VWAP | The day's VWAP (dashed amber), one piece per day because it restarts every morning. Volume-weighted when futures volume is saved, otherwise the plain average price of the day, as in the rules | On |
| EMA 9, EMA 21 | The two moving averages the rules use (blue and purple) | On |
| Previous day | Yesterday's high, low and close | Off |
| Opening range | High and low of the first 30 minutes (shown once the range is final) | Off |
| Swing high / low | The recent swing levels the key-level check uses | Off |
| OI walls | The call wall and put wall as price lines (same strikes as the option chain card) | Off |
| Pattern arrows | The CE / PE arrows and pattern names | On |
| Signal candle | When an idea is active, a square on the signal candle ("signal") and a circle on the confirmation candle ("confirm") | On |
| No-trade zones | Shaded bands behind the candles where the rules say no: grey for the first 15 minutes and after 3 pm, red for the afternoon of an expiry day (from 1 pm) | On |
| Day open | A line at today's opening price, labelled with the opening gap (for example "Open (gap +0.50%)") | Off |
| Risk / reward zone | When an idea is active, a green band from entry to target and a red band from entry to stop, starting at the decision candle | On |
| Past ideas | Every journal idea for the market and candle size on screen, on its signal candle, labelled with the side and result (for example "CE +1.90R"), green for success, red for failed, amber for time exit, grey for waiting | On |

Past ideas older than the visible range appear when you zoom out (press "All").

**Panels** (a second row of buttons, all off by default). Each switches on a small chart under the main chart. It is lined up with the main chart and follows its zoom. They show what the rules look at, so you can see why a check passed or failed:

| Panel | Shows | Reference line |
|---|---|---|
| ADX panel | Trend strength | 20 (the rules need 20 or more) |
| VIX panel | India VIX of the previous close | 22 (the rules need 22 or less) |
| Volume panel | Futures volume divided by its recent 20-candle average, so only days with futures volume have bars | 1x (the optional volume check) |
| OI ratio panel | Put/call open-interest ratio | 1 |
| OI build-up panel | Put minus call OI build-up over 30 minutes, in % (green = more puts written) | 0 |

The two OI panels use the **saved** option history only (section 3b), so they show past days that were saved, not the live minute. A panel says "No data for the days on screen yet" when nothing is saved for them. None of the chart options changes a rule or an alert.

The page is made of flexible rows, so it follows the size of your window: the chart and the checklist share a row on a wide screen, and everything stacks into one column on a phone.

### Backtest options
| Option | Meaning | Example |
|---|---|---|
| `--index` | `nifty` (default) or `sensex` | `--index sensex` |
| `--interval` | `1m`, `5m` (default), `15m` | `--interval 15m` |
| `--days` | Days of history (Yahoo gives at most about 60 for 5m / 15m) | `--days 58` |
| `--csv` | Use your own candle file (columns: datetime, open, high, low, close) | `--csv my_candles.csv` |
| `--rr` | Reward : risk (default 2) | `--rr 1.5` |
| `--cost` | Cost per trade in R (default 0.10) | `--cost 0.15` |
| `--no-vix` | Do not use India VIX | `--no-vix` |
| `--save` | Save all trades to `backtest_trades.csv` | `--save` |
| `--learn` | Give the results to the tool's memory (`trade_memory.csv`) as a head start | `--learn` |

---

## 3b. Your own candle database (`candle_store.py`)

Yahoo only gives about 60 days of 5-minute candles and 7 days of 1-minute candles. Older candles cannot be downloaded later. So the tool saves its own copy, and the history grows every day.

- **Where:** `market_data.db` (SQLite, a normal file in this folder; no extra install).
- **What is saved:** NIFTY and SENSEX at 1m, 5m and 15m (finished candles only), plus daily India VIX.
- **Safe to repeat:** running it again never makes duplicates. A candle that Yahoo already saved is kept as it is (Yahoo never overwrites saved candles, so it cannot replace the Angel One history); Angel One downloads do update saved candles.

| What you want | Command |
|---|---|
| Save candles now | `python candle_store.py collect` |
| Only one index or candle size | `python candle_store.py collect --index nifty --interval 5m` |
| See what is stored (counts, dates, last run) | `python candle_store.py status` |
| Export to a CSV file | `python candle_store.py export --index nifty --interval 5m --out nifty_5m.csv` |
| Download up to a year of 5m and 15m candles from Angel One | `python candle_store.py backfill --days 365` (options `--index`, `--interval`) |
| Save futures candles with real volume from Angel One | `python candle_store.py backfill-futures` |
| Save option-chain data (open interest) from Angel One | `python candle_store.py backfill-options` (options `--days`, `--strikes`, `--index`) |

Use the saved history in your own code (same format as the live candles):

```python
from candle_store import load_candles, load_vix
df = load_candles("nifty", "5m")
vix = load_vix()
```

**Daily schedule:** a Windows scheduled task named `AI-Trading-Candle-Collect` runs every Monday to Friday at 4:00 pm (after the market closes). It has three steps, one after the other: `candle_store.py collect` (Yahoo candles and India VIX), `candle_store.py backfill-futures --days 5` (the futures volume) and `candle_store.py backfill-options --days 5` (the option chain, about 3 minutes); the last two need the Angel One values in `.env`. If one step fails, the other still runs. If the PC was off at that time, it runs when the PC is next on and you are logged in. Run `collect` yourself at least once a week; if the PC is off for more than 7 days, those 1-minute candles are lost for good. To remove the task: `Unregister-ScheduledTask -TaskName "AI-Trading-Candle-Collect" -Confirm:$false` (PowerShell).

The backtest still downloads from Yahoo by default. To test on your saved history, load it with `load_candles` and pass it to `compute_features` and `run_backtest`, or export a CSV and use `candle_backtest.py --csv`. `candle_baseline.py` and `candle_research.py` already read the saved history.

### Angel One history (`backfill`)

Yahoo gives about 58 days of 5-minute candles. Angel One gives much more. `python candle_store.py backfill --days 365` downloads up to a year of 5m and 15m candles for NIFTY and SENSEX (1m only when you ask with `--interval 1m`).

- It needs `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PIN` and `ANGEL_TOTP_SECRET` in your `.env` (section 9). It only reads candles; it never places an order.
- It downloads in short pieces (60 days for 5m, 100 days for 15m), newest first, with a pause between requests because Angel One refuses fast requests. Each piece is saved straight away, so a stop in the middle loses nothing. Run it again any time.
- After the first run you have about 248 trading days of 5m and 15m candles for both indexes (24 Nov 2025 to 23 Nov 2026 when it was run). Run it again from time to time to extend the history.

### Futures volume (`backfill-futures`)

The indexes themselves have no volume, so volume and a real VWAP come from the index **futures** (NIFTY and SENSEX), which Angel One serves with real volume.

- `python candle_store.py backfill-futures` saves them as `NIFTY_FUT` and `SENSEX_FUT`. Each contract is saved only for the time it was the **nearest** one (from the day after the previous monthly expiry until its own expiry), because far-month contracts have very little volume and would give misleading numbers.
- **Expired contracts cannot be downloaded.** So futures volume history cannot go back a year: it starts from the oldest contract that is still listed (about 17 days of SENSEX at the first run; NIFTY starts when its December contract became the nearest one). It then grows by itself through the daily schedule above. If you do not use the schedule, run `backfill-futures` at least once a month, before the contract expires, or that month's volume is lost for good.
- The dashboard also fetches the nearest future live (cached for a minute) when the data source is Angel One, so VWAP and volume are real there.

### Option chain (`backfill-options`)

Saves what other traders are holding: for the **nearest expiry**, the at-the-money strike plus 5 strikes each side (calls and puts, 22 contracts per index), the 5-minute price, volume and **open interest (OI)**. They go into the `option_oi` table in `market_data.db`. Read-only, no orders.

- The current index level comes from your saved 5m candles, so run `collect` first on a new database.
- Safe to repeat: running it again updates rows, never duplicates them. The running candle is never saved, and one failing contract does not stop the others. A contract that expires today is still saved after the close.
- **Expired contracts cannot be downloaded**, so OI history cannot go back in time: it grows by one trading day per day through the daily schedule (the first run saved 19 to 23 Nov 2026). The strikes saved follow the index level on the day it is run.
- Read it in your own code with `load_option_chain("nifty")` (columns: expiry, strike, side, close, volume, oi).
- **Shown, not used for decisions:** the dashboard's Option chain card shows OI as plain facts (section 3). No rule or alert uses it. The three optional `oi_*` checks and their research section are built but off (section 5); they are judged with `candle_research.py` (section 6) once 60 days of history exist.

---

## 3c. Telegram alerts (optional)

The dashboard can send a short message to a Telegram chat or group when a new trade idea appears, and another one when that idea ends. You do not have to watch the screen.

**A new idea** (🟢 for CE, 🔴 for PE):

```
🟢 CE IDEA - NIFTY 25450 CE
NIFTY 5m candles | confirmed at 14:05 IST
Expiry - Mon 23 Nov

BUY - ₹184.70
STOP LOSS - ₹171.70
TARGET - ₹212.40

Per unit:
loss - ₹13.00
profit - ₹27.70
```

**When it ends** (✅ target hit, ❌ stop loss hit, ⏱ time exit):

```
✅ TARGET HIT - NIFTY 25450 CE
NIFTY 5m candles | closed at 14:35 IST

Profit - ₹27.70 per unit
Held - 6 candles (30 min)

BUY - ₹184.70
TARGET - ₹212.40
```

How to read them:
- **BUY, STOP LOSS and TARGET are option prices** (what you see in Groww), per unit. Multiply by the lot size for the total. They are ESTIMATES (or live prices with Angel One), so the real price can differ by 10 to 20%. Always check the real price in Groww before buying.
- **`Profit - ₹27.70 per unit`** is the buy price to the target price. **`Loss - ₹13.00 per unit`** is the buy price to the stop loss price. Brokerage and taxes are not included.
- **A time exit (⏱)** means neither the target nor the stop loss was reached and the trade was closed. Its figure says `about`, because it ended somewhere between the two prices (or `Result - break-even`).
- **`(index)`** after a price means the option prices could not be worked out, so the index levels are shown instead.
- **A `DELAYED DATA` line** appears when you allowed alerts on old data (see below).

### When an alert is sent

- Only for the idea that is on the screen **right now**, while the market is open. An idea found after the fact (it appeared while nobody was looking) is saved in the history but not sent, because it would be stale.
- **Free Yahoo data is always about 15 minutes late.** Alerts are sent anyway, and each one starts with a `DELAYED DATA: about N min old` line, because the entry moment may already have passed. To send nothing on old data, set `TELEGRAM_ALLOW_DELAYED=0`. Or use Angel One for live data.
- **Each alert is sent exactly once**, also after a restart or an auto-reload (the journal remembers it in its `alerts` column). The result message is only sent for ideas that were alerted.
- **A browser tab is not needed.** While `python dashboard.py` runs (keep the terminal open), a background loop checks NIFTY and SENSEX (5 minute candles) every 30 seconds on weekdays from 9:10 to 16:00 IST. Use `--no-alerts` to turn the loop off.
- If Telegram is down or the token is wrong, a short note is printed in the terminal. The dashboard keeps working.

### Setup (once)

1. **Create the bot.** In Telegram, open **@BotFather** (the one with the blue tick), send `/newbot`, choose a name and a username that ends in `bot`. BotFather gives you a **token** like `123456789:ABC...`. Keep it secret.
2. **Add the bot to your group.** Create a group (only you in it is best, see the cautions below), then open the group menu (⋮), choose **Add members** and add your bot.
3. **Let the bot see the group.** In the group, send `/start@yourbotname` (use your bot's real username). Bots do not see normal messages in groups, but they do see messages that mention them.
4. **Find the chat id.** Put the token in `.env` first (step 5), or set it in the terminal, then run `python telegram_notify.py --find-chat`. It lists the chats the bot can see. A group id is a negative number.
5. **Create your `.env` file.** Copy `.env.example` to `.env` (in the project folder) and fill in the two values:

   ```
   TELEGRAM_BOT_TOKEN=paste-your-token-here
   TELEGRAM_CHAT_ID=-1001234567890
   ```

   Put real values only in `.env`, never in `.env.example`.
6. **Test it.** Run `python telegram_notify.py --check` (it checks the token and shows the bot's name; the token itself is never shown), then `python telegram_notify.py --test` (it sends a test message to the chat).
7. **Start the dashboard** with `python dashboard.py`. The first lines say `Telegram alerts: ON` (or `OFF` and what is missing).

### Settings

Put them in `.env` or set them in the terminal (a value set in the terminal wins over `.env`):

| Setting | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Required. From @BotFather. Anyone who has it can send messages as your bot. |
| `TELEGRAM_CHAT_ID` | Required. The group or chat that receives the messages. |
| `TELEGRAM_ALLOW_DELAYED` | Alerts are also sent when the data is old (free Yahoo always is); the message then starts with a `DELAYED DATA` line. Set it to `0` to send nothing on old data. Default: alerts are sent. |
| `ALERT_MARKETS` | The markets the loop watches. Default `nifty:5m,sensex:5m`. |
| `ALERT_SOURCE` | `yahoo` or `angel`. Default: `angel` when the four `ANGEL_*` values are in `.env`, otherwise `yahoo`. |

### Cautions

- **Keep the token private.** It belongs only in `.env`, which Git ignores. Do not put it in `.env.example`, in the code, in a screenshot or in a chat. If it leaks, open @BotFather, send `/revoke`, pick your bot and put the new token in `.env`.
- **Keep the group private.** Sharing signals with other people may need SEBI registration (see section 13), and the backtest tests so far show no clear edge over simple trend-following. A group where only you read the messages avoids the problem.
- If the group is turned into a "supergroup" (for example when many members join or it becomes public), Telegram gives it a new id that starts with `-100`. Run `--find-chat` again if the alerts stop.
- `python test_telegram_notify.py` tests all of this with a fake Telegram on your computer. It never uses your real token or group.

---

## 3d. Holidays, risk limits and the end-of-day check

**Holidays (`holidays.csv`).** One holiday per line as `date,name` (for example `2027-01-26,Republic Day`); lines starting with `#` are ignored. The tool uses it for "Market closed", for the weekly expiry date (an expiry on a holiday moves to the previous trading day) and to keep the Telegram alert loop quiet. **Saving the file is enough**: it is re-read when it changes, no restart needed. A bad line is reported in the console and skipped, and if the file is missing the two built-in dates (24 Nov and 25 Dec 2026) are used. **Only those two dates are in it so far; add the other NSE / BSE holidays from the official calendar.**

**Risk limits (optional, off while empty).** Put values in your private `.env` (see `.env.example`):

| Setting | What it does |
|---|---|
| `RISK_MAX_LOSS_R=3` | After the day's finished ideas lose this many R (net, after cost), **new** ideas are saved as SKIPPED and no alert is sent |
| `RISK_MAX_IDEAS_PER_DAY=4` | After this many ideas in a day (both markets together), new ideas are skipped the same way |
| `RISK_CAPITAL=200000` and `RISK_PER_TRADE_PCT=1` | Turn on the **position-size helper**: in Trade details it shows your risk budget, the lot size (from Angel One's public instrument list), the loss per lot at the stop and how many lots fit. It only shows numbers |

- A skipped idea says "Daily limit ..." in its note, never counts as an idea made, is never traded and does not block the next idea. Ideas saved earlier are not changed.
- The scoreboard card shows "Today: N idea(s), X R, limits ..." and a warning when a limit is reached; the banner tag says "Daily limit reached".
- The limits are about discipline, not prediction. They change no rule and place no order.

**End-of-day check (`python session_check.py`).** Read-only: it writes nothing. After the close it prints OK / CHECK / INFO lines for: whether it was a trading day; whether NIFTY and SENSEX live candles are fresh (it asks the data source, skip with `--no-live`); the day's ideas, net result, alerts recorded and ideas skipped by a limit; what your database saved that day (candles, futures volume, option open interest) and the last collection runs, with failed runs flagged; and whether alerts, Angel One values, limits and the helper are set. Use `--date` to look at an earlier day. It cannot see the dashboard's console messages, so it reports only what was recorded.

---

## 4. The candle patterns

| Pattern | Simple meaning | Leans towards |
|---|---|---|
| Hammer | Sellers pushed price down, buyers brought it back up. Counts only after a fall. | CE |
| Shooting star | Buyers pushed price up, sellers brought it back down. Counts only after a rise. | PE |
| Bullish engulfing | Big green candle fully covers the previous red candle. | CE |
| Bearish engulfing | Big red candle fully covers the previous green candle. | PE |
| Doji | Open and close almost same. Market is confused. | Wait |
| Bullish marubozu | Big green candle, almost no wick. Buyers in full control. | CE |
| Bearish marubozu | Big red candle, almost no wick. Sellers in full control. | PE |

One candle alone is weak. That is why the filters below exist.

---

## 5. How the trade idea is decided

Think of a doctor: he does not diagnose from one symptom. He checks several tests. The tool does the same.

**Step 1.** The **signal candle** must show a pattern that leans CE or PE.

**Step 2. Required checks.** If any one fails, the answer is "No trade now".

| Check | What it means | Why |
|---|---|---|
| Confirmation candle | The next candle must close beyond the signal candle (above its high for CE, below its low for PE) | Proof, not hope |
| Bigger-timeframe trend | 15-minute AND 60-minute trend must agree (EMA 9 vs EMA 21 on bigger candles) | Do not swim against the river current |
| ADX trend strength | ADX must be at least 20 | Most false signals come in sideways markets |
| Time of day | Signal candle starts after 9:30, and the trade starts before 3:00 pm | First minutes are noisy, late trades have no time |
| Expiry afternoon | No new trades on expiry day after 1:00 pm | Options become wild |
| India VIX | Skip when previous-day VIX is above 22 | Too wild, options too expensive |
| Lunch time (off by default) | Skip 12:00 to 1:30 pm | You can switch it on and let the backtest judge |

**Extra checks (VWAP is on; volume and gap are off).**

| Check | What it means | State |
|---|---|---|
| VWAP | CE only when the price is above today's VWAP, PE only when it is below. VWAP = today's average price weighted by the **futures** volume. On a day with no volume data it is the plain average price of the day, and the checklist says "average price of the day" instead of VWAP. | **On** |
| Volume | The candle's futures volume must be at least its recent 20-candle average. | Off. Not enough futures history yet to judge it. |
| Opening gap | Not trading against a gap bigger than 0.3% (for example a CE after a gap down). | Off. Helped some markets and hurt others in the tests. |
| `oi_pcr` | Put/call open-interest ratio above 1.0 for a CE, below 1.0 for a PE. | Off. Research only: not enough OI history. |
| `oi_wall` | No CE when the price is within 1 ATR under the call wall (most call OI); no PE within 1 ATR over the put wall. | Off. Research only. |
| `oi_flow` | In the last 30 minutes put OI built up more than call OI for a CE, less for a PE. | Off. Research only. |

The three `oi_*` checks use the saved option-chain history (section 3b), with fixed untuned thresholds. A candle uses only OI bars that had finished before it closed, and a candle with no recent OI (older than 20 minutes, or fewer than 6 strikes per side) simply skips the check. **They do nothing in the live dashboard**, which does not compute these features: switching one on in `DEFAULT_FILTERS` only affects research and backtests that pass the saved option table to `compute_features(..., chain=load_option_chain(index))`.

Switch them in `DEFAULT_FILTERS` and let `candle_research.py` (section 7c) judge the change. Volume and gap are also shown as plain fact tiles under the chart whether or not the check is on.

**Step 3. Points (need 3 of 4).**
- +2 clear pattern
- +1 short-term trend agrees (EMA 9 vs EMA 21)
- +1 signal candle is at a **key level**: previous day high / low / close, opening range (first 30 minutes) high / low, or recent swing high / low (touching it, or breaking through it)

**Step 4. Levels (index).**

| | CE idea | PE idea |
|---|---|---|
| Entry | Close of the confirmation candle (buy now) | Close of the confirmation candle (sell side, buy PE now) |
| Stop loss | Low of the signal candle | High of the signal candle |
| Target | Entry + 2 x risk | Entry - 2 x risk |

Stop loss is kept between 0.25 and 1.5 x ATR (average candle size). Risk : reward is always 1 : 2. Strike is the nearest ATM strike (NIFTY step 50, SENSEX step 100).

You can switch filters on or off in `DEFAULT_FILTERS` at the top of `candle_signals.py`.

**No cheating:** when the tool judges a candle, it only uses candles up to that moment. Never the future. This was tested.

---

## 6. Option prices: what you actually buy and sell

The index levels are the trigger. You buy an **option**, so the dashboard also shows option prices (per unit):

| Row on the card | Meaning |
|---|---|
| Option | Example: `NIFTY 25250 PE` with its expiry date |
| Price now | Current option price (estimated, or live from Angel One) |
| BUY price | Option price at the entry level |
| STOP LOSS price | Option price when the index reaches the stop loss level |
| TARGET price | Option price when the index reaches the target level |
| Loss / Profit | Per unit. Multiply by the lot size shown in Groww. |

- **With Angel One (live):** it takes the real option price and works backwards from it. Most accurate.
- **With Yahoo (free):** it uses India VIX to estimate. The real price in Groww can differ by 10 to 20% or more. Check the real price before buying. If it is very different, do not trade.
- Weekly expiry: NIFTY on Tuesday, SENSEX on Thursday. If that day is a holiday, expiry moves to the previous trading day. Keep `holidays.csv` updated (section 3d).
- On expiry day the card shows a warning.

**How to use it in Groww:** check the real option price, buy, then place a stop-loss order at the STOP LOSS option price and a sell order at the TARGET option price. Then you do not need to watch the screen all the time. If you miss the entry candle, skip the idea.

---

## 7. Backtest: does the idea really work?

Run `python candle_backtest.py`. It shows:

1. **Result with all filters on:** number of trades, win rate, average result, profit factor, worst drawdown.
2. **Which filters help:** the same test with each filter removed. A filter that does not improve results should be removed.
3. **First part vs last part of the data:** a rule that works in only one part is probably luck.

How a trade is simulated (honest and slightly pessimistic):
- Filled at the NEXT candle's open after the confirmation candle.
- Stop loss = -1R, target = +2R (R = the amount you risk).
- If one candle touches both stop and target, it counts as a LOSS.
- If price gaps past the stop, the loss is bigger than 1R.
- Trade closes at the end of the day, or after 2 hours (time decay).
- A cost of 0.10R per trade is subtracted (rough guess for brokerage, taxes, slippage).
- One trade at a time.

How to read the numbers:
- With 1 : 2 risk-reward and costs, you need about **37% wins just to break even**.
- **Profit factor** above 1.0 means money is made. Below 1.0 means money is lost.
- Fewer than 30 trades = too small to trust.
- Do not keep changing rules until the backtest looks great. That is "overfitting" and it fails live. Check that both parts of the data look fine.

We tested the code on pure random data (where no real edge exists). It showed about -0.1R per trade, as it should. So the code has no hidden advantage.

---

## 7b. Is it better than luck? (`candle_baseline.py`)

A positive backtest can be luck. This script asks: "would random entries do as well?" It reads your saved candles (`market_data.db`), writes nothing, and uses the same trade simulation, costs, trading hours and stop-loss sizes as the strategy, with the same number of trades.

| Baseline | What it does |
|---|---|
| **A: pure random** | Random candle, random side (CE or PE), inside the allowed trading hours. |
| **B: trend + ADX only** | Random candle, but only in the direction the 15 min and 60 min trends agree on, and only when ADX is 20 or more. No candle pattern, no confirmation candle. |

Run `python candle_baseline.py` (options: `--index nifty|sensex`, `--interval 5m|15m`, `--runs 1000`, default 400). More runs give a more precise "chance" range but take longer: `--runs 2000` takes about 4 minutes for all four sets.

**Pooled summary.** When two or more sets are run (the default: NIFTY and SENSEX, 5m and 15m), the script ends with a **POOLED** block: all the strategy trades together, compared with all the random runs together (weighted by trade count), against baseline A and baseline B. It is the biggest sample, so it is the best single answer. But the sets overlap: NIFTY and SENSEX move together, and 5m and 15m use the same days. So the real number of independent trades is much smaller than the total shown, and a pooled p-value looks better than the evidence really is. A single run (for example `--index nifty --interval 5m`) prints no pooled block.

How to read it:
- **p-value** = share of random runs that did as well as or better than the strategy. Below 0.05 means the strategy beat chance. Above 0.20 means no clear edge.
- **Beating A but not B** means the trend and ADX filters do the work and the candle patterns add nothing. The strategy should beat B to show the patterns help.
- With fewer than 30 strategy trades, treat the answer as a rough hint. When you test several markets and candle sizes, one "win" can appear by luck; a result needs a much smaller p-value to count.
- Results are on the index in R, not rupees.

Result on the first 58 days of saved data (27 Aug to 20 Nov 2026): the strategy did not clearly beat chance. Only SENSEX 15m beat both baselines, on 29 trades, which does not hold up after allowing for the number of tests. Random trend-and-ADX entries already made a small profit, so the filters carry most of the edge. Pooled over all four sets (187 trades, +0.135R per trade, 2,000 runs): it beat pure random entries (p = 0.021) but not trend-and-ADX-only entries (p = 0.23). Because the sets overlap, even the 0.021 is weaker than it looks. Run it again as `market_data.db` grows (aim for 200 or more strategy trades).

---

## 7c. Honest checks on your saved data (`candle_research.py`)

Run `python candle_research.py` (options `--index nifty|sensex`, `--interval 5m|15m`). It reads `market_data.db`, writes nothing and changes no rules. For each market it prints:

1. **Walk-forward:** the same rules on 4 separate blocks of days. A real edge shows up in most blocks.
2. **Cost stress:** the result when each trade costs 0.0, 0.1, 0.2 or 0.3 R (slippage, spread, bad fills).
3. **Uncertainty:** a 95% range for the average R per trade (bootstrap). If the range includes 0, the result could easily be luck.
4. **Breakdown** by hour, pattern and side. For finding ideas only: small groups look good or bad by chance. Groups under 8 trades are hidden.
5. **Extra checks:** the default rules plus one extra check at a time (volume, opening gap).
6. **Open-interest checks:** the default rules plus each `oi_*` check, only on the days that have saved OI. It shows **no numbers until 60 trading days of OI exist** (about late February 2027; it prints how many days there are so far), because a few days would only show luck. A check counts as useful only if it also passes the strategy-lab gate (section 7d).

**Result on the first full year (248 trading days, 24 Nov 2025 to 23 Nov 2026, VWAP on):**

| Market | Trades | Average R per trade (after 0.1 R cost) | 95% range | Profitable blocks |
|---|---|---|---|---|
| NIFTY 5m | 184 | -0.12 | -0.30 to +0.07 | 1 of 4 |
| NIFTY 15m | 79 | -0.02 | -0.24 to +0.22 | 2 of 4 |
| SENSEX 5m | 195 | -0.06 | -0.24 to +0.12 | 1 of 4 |
| SENSEX 15m | 84 | +0.07 | -0.15 to +0.29 | 2 of 4 |

**What it means:** on a full year the rules show **no proven edge**. The good-looking results on the earlier 58 days did not hold up, the win rate on 5-minute candles is about 24-26% against the roughly 35% a 1:2 setup needs, and every 95% range includes zero. At a 0.3 R cost per trade even the best markets lose money. Do not trade real money on these rules. Use the year of data to look for what does work (per-hour and per-pattern results, other strategy types) and test each idea with the same checks before it goes live.

---

## 7d. Other strategy types (`strategy_lab.py`)

Run `python strategy_lab.py` (about 8 minutes; options `--index`, `--interval`, `--strategy`, `--runs`). It reads `market_data.db`, writes nothing and changes no live rules. Four fixed strategies were written down before testing and not tuned afterwards:

| Strategy | Idea |
|---|---|
| `orb` | Opening-range breakout: trade the break of the first range of the day. |
| `vwap_pull` | In a trend, buy the pullback to VWAP (sell the bounce for a down-trend). |
| `trend_pull` | Trend continuation after a pullback. |
| `mean_rev` | Quiet days only (ADX below 20): fade moves away from the day's average, 1:1 reward. |

They use the same trade simulation, 0.10 R cost and trading hours as the main backtest, and never look at later candles. A strategy **passes the gate** only if all of these hold: at least 60 trades, average R above 0 after costs, positive in at least 3 of 4 time blocks, a 95% range above zero, and better than random entries at p below 0.01 (strict, because many tests make lucky wins likely).

**Result on the first full year (16 tests, average R per trade after 0.1 R cost): 0 passed the gate.**

| Strategy | NIFTY 5m | NIFTY 15m | SENSEX 5m | SENSEX 15m |
|---|---|---|---|---|
| Opening-range breakout | -0.11 | -0.05 | -0.19 | -0.04 |
| VWAP pullback | -0.02 | +0.06 | -0.04 | -0.04 |
| Trend continuation | -0.04 | -0.05 | -0.10 | -0.05 |
| Mean reversion | -0.19 | -0.11 | -0.21 | -0.21 |
| Current rules (for comparison) | -0.12 | -0.02 | -0.05 | +0.07 |

The closest was the VWAP pullback on NIFTY 15m (+0.06 R, 3 of 4 blocks positive, beat random at p = 0.003), but its 95% range (-0.09 to +0.21) includes zero and it turns negative at a 0.2 R cost. With 16 tests, one lucky-looking result is expected. Limits: VWAP is the plain average price for most days (futures volume is short), only one fixed version of each strategy was tested, and one year is short. Re-run it as futures volume and history grow.

---

## 7e. Short-target experiment (`python strategy_lab.py --short-targets`)

Question: would a smaller target (a quick small gain per share instead of 2 times the stop) make the current rules profitable? Same entries and stops as the current candle-pattern rules; only the target changes. Six variants were written down before looking at any result and are never tuned:

| Variant | The target is |
|---|---|
| `r0.5`, `r1.0` | 0.5 / 1.0 times the stop distance on the index (today: 2.0) |
| `rs10`, `rs20` | where an at-the-money option has gained Rs 10 / Rs 20 per share |
| `pct30`, `pct50` | where an at-the-money option has gained 30% / 50% of its price |

The option variants turn the gain into an index distance with Black-Scholes (the option's price at entry from the previous day's India VIX and the time to the nearest weekly expiry); that distance divided by the stop distance is that trade's reward : risk (kept between 0.1 and 6). Time decay after the entry is not included, so these variants are a little easier than reality; with no VIX value no trade is made. The same gate as in section 7d applies, and the random-entry comparison copies the stop size and the reward : risk of the real trades. It writes nothing and changes no live rule. **Result on the one-year data (249 trading days, 24 tests): 0 passed the gate.** Average net result per trade in R, after the 0.1 R cost:

| Variant | NIFTY 5m | NIFTY 15m | SENSEX 5m | SENSEX 15m |
|---|---|---|---|---|
| `r0.5` | -0.13 | -0.04 | -0.09 | -0.09 |
| `r1.0` | -0.13 | +0.05 | -0.06 | +0.03 |
| `rs10` | -0.12 | -0.03 | -0.14 | -0.10 |
| `rs20` | -0.22 | +0.04 | -0.15 | -0.09 |
| `pct30` | -0.11 | +0.04 | -0.09 | -0.03 |
| `pct50` | -0.05 | +0.01 | -0.07 | +0.03 |
| Current rules (target 2.0) | -0.12 | -0.02 | -0.05 | +0.07 |

What it shows:
- **Smaller targets are hit much more often** (0.5 R: 60 to 68% of trades; Rs 10 on SENSEX: 78 to 87%), but the payoff shrinks by the same amount, so the average stays at or below zero. The hit rate rises exactly as fast as the break-even rate does. The 5m results are negative for every variant, and almost all are negative at a 0.2 R cost.
- **A rupee target means a different size on each market.** The average Rs 10 target is about 0.78 R on NIFTY 5m but only 0.14 to 0.24 R on SENSEX, because the same Rs 10 needs a much smaller index move relative to the stop. A 0.14 R target hit 87% of the time still loses 0.10 R, because the 0.1 R cost is bigger than the average gain. Very small targets cannot pay their costs unless nearly every trade wins.
- **The best-looking cells are the 15m results around zero** (`r1.0`: +0.05 NIFTY, +0.03 SENSEX; `rs20` and `pct30` on NIFTY: +0.04). Their 95% ranges include zero, they do not beat random entries at p < 0.01 (the closest are p = 0.06 and 0.057), and there are 24 tests, so they are noise until proven otherwise.
- **Conclusion:** a smaller target on these entries does not create an edge. What is missing is the prediction, not the target size. The option variants are also a little optimistic (no time decay after the entry). Re-run it as the data grows: `python strategy_lab.py --short-targets`.

---

## 8. Idea history (success or failed)

The "Trade idea" card clears when the next candle comes. The idea is NOT lost. Every idea is saved automatically in **Idea history** (the section below the chart) with:

- **Result:** SUCCESS (target hit), FAILED (stop loss hit), TIME EXIT (closed at end of day or after 2 hours), WAITING / RUNNING, or SKIPPED (price ran away before entry). Result is in R, after costs.
- **A story in simple words** of what happened, for example: "Price first moved 1.2R in favour, then reversed and hit the stop loss. A near miss." or "Stop loss hit within 2 candles, so this was probably a false signal."
- **The setup:** pattern, key level, ADX, 15 / 60 minute trend, India VIX.
- **Option prices:** the buy, stop loss and target price of the option (for example `NIFTY 25450 CE`: buy 166, stop loss 153, target 194, per unit), plus the loss and profit per unit. See "Saved option prices" below.
- **The checklist** that was shown at that time (what agreed, what did not).
- **A small chart** with entry, stop loss, target and where the idea ended (click a row to open it).
- **What the memory said** about similar past ideas at that moment.

Use the buttons (All / Success / Failed / Time exit / Waiting) to filter.

**Saved option prices.** The real option price cannot be found out afterwards, so the tool saves it with the idea at the moment the idea is made (in the `option_levels` column of `trade_journal.csv`). The history says where each price came from:

| Label in the history | What it means |
|---|---|
| **LIVE option price, saved at the moment this idea appeared** | Real price from Angel One. Only when you use the Angel One source, the market is open and the data is current. |
| **ESTIMATE, saved when this idea appeared** | No live option price was available (for example with Yahoo), so the price was worked out from the index level and India VIX at that moment. |
| **ESTIMATE. This idea has no saved option price** | An older idea, saved before this feature existed, or one that could not be priced. The price is worked out again from the index level and the India VIX saved with the idea. |

Ideas the dashboard finds after the fact (they appeared while you were not looking) get an estimate for their decision time, because no live price exists for that moment. An estimate can differ from the real price in Groww by 10 to 20% or more, so use it to understand the size of the trade, not as a record of what you would have paid. If an option price cannot be worked out, the history shows the index levels instead. Ideas saved before this feature keep working: their journal rows simply have an empty `option_levels` field.

How it works:
- The dashboard scans today's candles every time, so an idea is saved even if it appeared and vanished while you were not looking (for example, you were away or the page was in the background). Only today's session is scanned.
- Only one idea at a time, the same rule as the backtest.
- A finished idea is checked against the next candles, using the same trade simulation as the backtest.
- With delayed (Yahoo) data, ideas are seen late. Treat the history as "if I had acted on time".
- After 30 or more finished ideas, compare the history win rate with the backtest. If both look similar and good, only then think about real money (small size first).

---

## 8b. The tool's memory (learning from experience)

Think of a person who keeps a trading diary and reads it before every trade. The tool does the same.

**What it remembers:** for every finished idea, the facts of that moment (pattern, CE / PE, time of day, trend strength, key level, VIX, expiry day) and how it ended.

**How it uses the memory:**
1. **Recall.** When a new idea appears, the Trade idea card adds a line such as: "Similar past ideas (same pattern, same side and same time of day): 5 of 14 hit the target (36%), average -0.08R. The memory WARNS: this kind of setup has not worked well so far." It looks for the most specific match first, and falls back to wider matches.
2. **Lessons.** The tab **What the tool learned** shows what worked best and what failed most (by pattern, direction, time of day, trend strength, key level, VIX, day type).

**Where the experience comes from:**
- **Live history:** every finished idea is added automatically.
- **Backtest:** run `python candle_backtest.py --learn` to add past trades as a head start. Running it again replaces the older backtest memory of the same index and candle size.

**Honest limits:**
- This is statistics from remembered results. It is not human understanding of charts.
- A lesson or recall needs **at least 8 similar ideas**. With fewer, the memory says "not enough experience", because small numbers are mostly luck.
- Backtest memory comes from the same past data the rules were designed on, so it can look better than the future. The live history is the more honest teacher.
- Markets change. An old lesson can stop working.
- By default the memory only **advises**. It never blocks an idea. If you want it to block setups with a bad record, set `BLOCK_BAD_SETUPS = True` in `candle_memory.py` (it still needs 20 or more similar ideas to block).
- The memory shows lessons. Changing the rules is still your decision (for example, switch a filter on or off in `DEFAULT_FILTERS`), and then run the backtest again to check.

---

## 9. Data sources

| | Yahoo Finance | Angel One SmartAPI |
|---|---|---|
| Cost | Free | Free |
| Account needed | No | Yes (Angel One account and a SmartAPI app) |
| Live quality | Delayed (usually about 15 minutes) | Near real time (finished candles only) |
| History | About 58 days of 5m / 15m, 7 days of 1m | A year or more (via `backfill`, section 3b) |
| Volume | None for the indexes | Real volume from the index futures |
| Option (CE/PE) prices | Estimated only | Real (live price) |

**The default source:** the dashboard and the Telegram alert loop use **Angel One when all four `ANGEL_*` values are in your `.env`**, otherwise Yahoo. You can still switch in the drop-down, or set `ALERT_SOURCE` for the alerts. With Yahoo the dashboard shows a warning when the data is old; use it for learning only.

### Setting up Angel One

1. Open an Angel One account and create an app at `smartapi.angelone.in` ("Add App"). Name: anything. Redirect URL: any normal `https://` address (localhost is refused, and it is never used because this project logs in with client ID, PIN and TOTP). Post back URL: leave empty. Primary static IP: your public IP (find it at `https://api.ipify.org`). The portal lets you change the IPs only once a week, and a home connection's IP can change, so keep it up to date.
2. Enable TOTP on the SmartAPI page and keep the TOTP **secret text** (the long text, not the 6-digit code).
3. The libraries are in `requirements.txt` (`pip install -r requirements.txt`).
4. Put the four values in your private `.env` (never in the code, never in chat, never in `.env.example`):

```
ANGEL_API_KEY=
ANGEL_CLIENT_ID=
ANGEL_PIN=
ANGEL_TOTP_SECRET=
```

5. Run `python dashboard.py` (it reads `.env` by itself). `python live_monitor.py --source angel` does not read `.env`, so set the four values in the terminal first (PowerShell: `$env:ANGEL_API_KEY="your_value"`).

### Good to know

- **Read-only use.** This project only reads candles and prices. It has no code to place, change or cancel an order. The login still needs your PIN and TOTP secret, and there is no view-only login at Angel One, so keep `.env` private. If you suspect a leak: change your Angel One PIN, enable TOTP again and delete the app in the SmartAPI portal.
- **Rate limits.** Angel One refuses requests that come too fast ("exceeding access rate" / "Too many requests"). All Angel One requests therefore go through one queue: one at a time, 2 seconds apart, with up to 3 retries. A first load of one market can take about 5 seconds; after that the answer is cached for 10 seconds. Each market is built once at a time, so several browser tabs and the alert loop never repeat the same calls.
- **Why the first load can be slow, and what was done about it.** On a live market day about half of the Angel One calls from this PC were refused with "exceeding access rate", even with a 2 second pause and a single queue (it looks like a limit on the connection, not on how fast this tool asks). Every refused call costs a 3 to 12 second wait, and a page needs two to three calls, so an uncached page could take 12 to 45 seconds. The dashboard now avoids making the page wait:
  - **Last copy first.** A routine refresh waits at most 4 seconds for fresh numbers; if they are not ready it shows the last copy at once (up to 10 minutes old) with "refreshing" next to the update time, and the next refresh (15 seconds later) brings the new one. The Refresh button and a change of market, candle size or source always wait for fresh numbers (`/api/data?...&fresh=1`).
  - **Futures volume and the option chain no longer block.** After the first fetch, an older copy is used at once while a new one is fetched in the background. The very first option chain shows the last saved day and switches to live on the next refresh. The first futures fetch still waits, because it decides the VWAP and so the ideas.
  - **Page requests go first.** Requests for the page you are looking at go before background ones (the alert loop and cache top-ups). A background request waits for a queued page request (at most 10 seconds) and retries only once.
  - **The alert loop looks once per candle** instead of every 30 seconds (an idea can only appear when a candle closes); if the data is late with the new candle it looks again at the next tick. This cuts its Angel One calls by about three quarters. The option chain is refreshed at most every 2 minutes.
  - **Diagnosis.** The page data has a `timing` block (seconds for candles, futures, VIX, option chain and the whole build, plus counters of Angel One calls, refused calls and the last 14 calls). Open `http://127.0.0.1:5000/api/data?source=angel&index=nifty&interval=5m` to see it.
  - **What is not solved:** a market you have never opened since the dashboard started (for example 1 minute candles) still needs its first build, which waits for the candles and the futures; if Angel One refuses calls at that moment it takes a while. A page that was open before will show the last copy at once.
- **Logs.** When a call fails, the Angel One library prints and logs the request headers, which include your API key. The project switches the library's console log off, and the `logs/` folder is ignored by Git. Do not paste its content anywhere.
- **Tested** with a real account: login, 5m and 15m candles for both indexes, the year of history, futures candles and option prices (outside market hours). Watch the first live market session: the live alerts on Angel One data have not run during market hours yet.
- **Key type:** the app was created as a "Trading" API key, and it worked for candles, futures and option prices.

---

## 10. Common problems

| Problem | What to do |
|---|---|
| `ModuleNotFoundError: yfinance` or `flask` | Run `pip install pandas yfinance flask` (activate your virtual environment first). |
| "Yahoo returned no data" | Check internet. Market may be closed. Try again in a few minutes. |
| Dashboard opens but chart is empty | Check internet (chart library loads online). Read the red message at the top and the terminal. |
| Latest candle looks old or flat | Yahoo free data is delayed. Use Angel One for live data. |
| History is empty | Ideas are saved while the dashboard is open and during today's session. Leave `python dashboard.py` running in market hours. |
| History shows WAITING for a long time | The result needs the next candles to finish. Keep the dashboard open or open it again later today. |
| Memory says "not enough experience" | Normal at the start. Run `python candle_backtest.py --learn` for a head start, and let the live history grow. |
| Always "No trade now" | Normal. The filters are strict on purpose. Most of the time there is no good setup. |
| Backtest says fewer than 30 trades | Use `--interval 5m --days 58`, run both NIFTY and SENSEX, or relax one filter in `DEFAULT_FILTERS` and compare. |
| VIX filter skipped | India VIX could not be downloaded. The rest still works. |
| Option price says ESTIMATE with Angel One | Live option price could not be fetched. Read the `[option price] ...` message in the terminal. |
| Angel One login failed | Check the four `ANGEL_*` values in `.env` (or in the same terminal for `live_monitor.py`) and that the TOTP value is the long secret text, not the 6-digit code. |
| "Couldn't parse the JSON response ... exceeding access rate" or "Angel One is busy" | Angel One refused requests that came too fast. The tool pauses and retries by itself and keeps the last good data for up to 5 minutes. If it keeps happening, close extra dashboard tabs and wait a minute. |
| The update time says "refreshing" | You are looking at the last copy (up to 10 minutes old) while fresh numbers are fetched in the background. It normally clears within a refresh or two; if it stays, Angel One is refusing calls (see the `timing` block, section 9). |
| `ModuleNotFoundError: SmartApi`, `pyotp`, `logzero` or `websocket` | Run `pip install -r requirements.txt` in the project's environment. |
| Dashboard says "Market open" on a holiday | `holidays.csv` has only the dates entered so far. Add the missing exchange holidays from the NSE calendar (section 3d). |
| Volume says "not available for this data" | You are on Yahoo, or the futures data could not be fetched. VWAP then uses the plain average price of the day. |
| `backfill-futures` saves nothing for a market | The nearest futures contract only just started (for example right after an expiry). Run it again after the next trading day. |
| No Telegram alert arrives | Check that the first lines of `python dashboard.py` say `Telegram alerts: ON`. Make sure `TELEGRAM_ALLOW_DELAYED=0` is not set in `.env` (that blocks alerts on old data, which free Yahoo always is). Alerts are only for the idea on screen right now, while the market is open. Run `python telegram_notify.py --check` and `--test` (section 3c). |
| `--find-chat` shows no chat | Add the bot to the group, send `/start@yourbotname` there, then run it again. |
| Telegram says "chat not found" | The chat id is wrong, or the bot is not in the group. Run `--find-chat` again. |

---

## 11. Known limits

- The rules and tests were first checked offline with fake candles. Angel One was tested with a real account outside market hours; a full live market session on Angel One data (including alerts) still needs to be watched.
- **The end of the session in the index candles is not real trading.** Angel One's NIFTY / SENSEX feed has no 15:15 and 15:20 candle (a full day has 73 candles, not 75), and its 15:25 candle is a single-price closing print (open = high = low = close) that the dashboard drops like an unfinished candle, so NIFTY's last live candle after the close is 15:10. In your saved database about a quarter of the days have flat 15:15 / 15:20 / 15:25 candles (from the Yahoo collection). Ideas are never made after 3 pm, so decisions are not affected, and `session_check.py` treats 15:10 as the last real candle. **Measured effect on the ATR: none.** I recomputed the ATR without the flat late candles and compared (all four markets, one year): the ATR of the first six candles of the day is 0.997 to 1.002 times the old value on average (median 1.000), the rules give exactly the same signals, the stop distance changes on only 6 to 9 signals (at most 13%), and the backtest result is the same to three decimals. **One real weakness was found and fixed:** a trade used to be closed only at the day's last candle (15:25). When that candle was missing, a late-day trade that had not hit its stop or target (an idea entered after about 1:10 pm) stayed OPEN and was never closed; in the live feed the 15:25 closing print is often flat and dropped, so such an idea would have stayed "waiting" in the journal (not counted in the daily limit, no result alert). Now the newest candle of a day closes the trade as soon as a candle of the next day exists, and a session that is still running stays open. The saved-data backtests are unchanged (all four markets give exactly the same trades as before), because the database has a 15:25 candle on 247 of 248 days. A late-day idea on the live feed is therefore closed when the next morning's first candle arrives, at the 15:10 close. After the close the page no longer says "candle running".
- Yahoo gives only about 60 days of 5-minute candles. With Angel One you can save a year (section 3b); the one-year result is in section 7c and shows no proven edge.
- Option-chain (OI) history is short too: it starts on 19 Nov 2026 and only grows daily; it covers only the nearest expiry and the strikes around the index level of each day.
- Futures volume history is short: expired contracts cannot be downloaded, so it only grows from when it was first saved. The index candles themselves have no volume, so in the long backtests VWAP is the plain average price for most days.
- With Yahoo, option prices are **estimates** (live option prices come only from Angel One). A real exit can also be worse than the stop price if the market moves very fast.
- Market holidays are only as complete as `holidays.csv`, which has only 24 Nov 2026 and 25 Dec 2026 so far. Add the other NSE / BSE holidays yourself (section 3d).
- Backtest results are on the INDEX in R, not in rupees.
- The rules are a starting idea. Only your backtest and history can tell if they work.
- The memory needs many examples before it is useful, and it can be fooled by luck or by a changed market.

---

## 12. Plan

1. **Round 1 (done):** filters, backtest, journal. Run the backtest, then watch the journal.
2. **Round 2 (mostly done):** Angel One live data, a year of saved candles, real option prices and futures volume are in. Option-chain OI is saved daily (section 3b) and shown in the dashboard as facts, but not used in any rule. The `oi_*` checks and their research section are built but off; still to do: judge them once 60 trading days of OI exist (about late February 2027), and build a longer futures volume history.
   - **Round 2b (tested, none passed):** other strategy types were tested on the year of data (section 7d: 0 of 16 passed). A strategy goes live only if it is positive in at least 3 of 4 walk-forward blocks, beats the random baseline, and its 95% range after costs is above zero. Risk limits and the position-size helper are built (section 3d, off until you set values). Still to do: one quality score to rank ideas.
3. **Round 3:** Claude explanation layer (explains the idea in simple words, does not calculate prices), then tune rules using backtest and journal results.
4. Only after a good backtest AND a good journal: think about small real trades. Auto-orders last.

---

## 13. Safety notes

- Option buying loses money quickly because of time decay. Backtest and paper trade first.
- Decide stop loss and target BEFORE buying. Without them it is gambling.
- Never share your API key, PIN or TOTP secret. Never put them in the code or upload them to GitHub. The same goes for your Telegram bot token (keep it only in `.env`).
- For your own use only. Sharing signals with others or charging for them may need SEBI registration.
- This is not financial advice. Any signal is for your own judgment, not a guaranteed result.

---

## 14. Working with Git (the `development` branch)

The code is on GitHub: `https://github.com/DivyeshSapariya/candle-signal-lab`

| Branch | What it is for |
|---|---|
| `main` | The stable version. Only changes that you have tested are merged into it. Do not work on it directly. |
| `development` | Where all new work happens. Changes here do not touch `main` until you merge them. |

### Daily workflow

1. **Check you are on `development`:** `git branch --show-current` should print `development`. If not, run `git switch development`.
2. **Change the code.** The dashboard reloads by itself (section 3), so you can see each change at once.
3. **Test it:** `python test_no_lookahead.py` (the rules must never use the future). If you changed the rules, also run `python candle_backtest.py` and `python candle_baseline.py` and compare with before.
4. **Save your work:**
   ```
   git add .
   git commit -m "short description of the change"
   git push
   ```
   `git push` sends `development` to GitHub, not `main`.
5. **Release to `main`** when a change is tested and you are happy with it. Either:
   - open a pull request from `development` into `main` on GitHub (`https://github.com/DivyeshSapariya/candle-signal-lab/pull/new/development`), check it and click Merge; or
   - do it on your computer: `git switch main`, `git merge development`, `git push`, then `git switch development` to carry on working.
6. **If `main` ever gets a change that `development` does not have,** bring it over with `git switch development` and `git merge main`.

Before switching branches, commit your work first (or run `git stash`), so unfinished changes do not get mixed up.

### What is in Git and what is not

The file `.gitignore` keeps your personal data out of GitHub: `market_data.db` (saved candles), `trade_journal.csv`, `trade_memory.csv`, `trade_snapshots/`, `.venv/`, `__pycache__/`, `.cache/` and `.env` files (your private Telegram settings). `.env.example` is only a template with empty values, so it is committed. These files exist only on your computer, so back them up yourself if you want to keep them. Never put API keys, PINs or TOTP secrets in a file that is committed.

To protect `main` from direct pushes, you can turn on branch protection in the GitHub repo under Settings, then Branches. This is optional.

### Setting up on another computer

```
git clone https://github.com/DivyeshSapariya/candle-signal-lab.git
cd candle-signal-lab
git switch development
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe candle_store.py collect
```

The last command starts a new candle database. Your journal, memory and older candles do not come with the clone, and candles older than what Yahoo still offers (about 58 days of 5-minute candles) cannot be downloaded again. The daily scheduled collection (section 3b) also has to be set up again on that computer, and it must point at the folder you cloned into.
