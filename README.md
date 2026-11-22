# NIFTY50 / SENSEX Candle Signal Tool

A personal tool that reads NIFTY50 and SENSEX candles, finds candle patterns, checks them with several filters, and gives a trade idea: **CE, PE or No trade**, with option buy price, stop loss and target. It also has a backtest (to measure how often the rules work) and an automatic paper-trade journal.

For personal use only. This is not financial advice. No tool can predict the market. A signal is a "chance", not a promise.

**Status: Round 1 done (free, no broker account needed).**

---

## 1. Files in this folder

| File | What it does |
|---|---|
| `candle_patterns.py` | The candle dictionary. Finds 6 patterns and explains them. |
| `candle_signals.py` | The brain. Filters + rules that decide CE / PE / No trade, with entry, stop loss, target. Used by the dashboard AND the backtest, so both follow the same rules. |
| `trade_plan.py` | Small live layer: where the price is now compared with the plan. |
| `option_pricer.py` | Turns index levels into option (CE/PE) buy price, stop loss price, target price. |
| `data_sources.py` | Gets candles from free sources (Yahoo Finance or Angel One). |
| `dashboard.py` | Live chart + analysis in your browser. |
| `candle_backtest.py` | Tests the rules on past candles. |
| `candle_journal.py` | Idea history: saves every idea, checks later how it ended (success / failed), writes the story and saves a chart. |
| `candle_memory.py` | The tool's memory: remembers how every finished idea ended, recalls similar past ideas, writes lessons. |
| `live_monitor.py` | Text-only live watcher (simple version). |
| `candle_store.py` | Your own candle database. Saves NIFTY / SENSEX candles and India VIX into `market_data.db` so you build a long history (see section 3b). |
| `candle_baseline.py` | Reality check: compares the strategy with random entries (same exits, costs and stop sizes) to see if it beats chance (see section 7b). |
| `test_no_lookahead.py` | Safety test: checks that the rules never use the future. |
| `telegram_notify.py` | Sends a short Telegram message when a new idea appears and when it ends (see section 3c). Also has the setup helpers `--check`, `--test` and `--find-chat`. |
| `test_telegram_notify.py` | Tests the Telegram alerts with a fake Telegram, so no real bot is needed. |
| `.env.example` | Template for your private Telegram settings. Copy it to `.env` and fill in your values (section 3c). |
| `requirements.txt` | The packages the project needs (`pip install -r requirements.txt`). |
| `README.md` | This document. |

Files created by the tool: `trade_journal.csv` (idea history), `trade_snapshots/` (a small chart of every finished idea), `trade_memory.csv` (the memory), `market_data.db` (your saved candles), `backtest_trades.csv` (only if you use `--save`), a `.cache` folder (only with Angel One), and `.env` (you create it yourself from `.env.example` for Telegram; it holds your private token and is never uploaded to Git). Do not delete `trade_journal.csv`, `trade_snapshots/`, `trade_memory.csv` and `market_data.db` if you want to keep your history, memory and candle data.

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
| See what the database holds | `python candle_store.py status` |
| Does the strategy beat random entries? | `python candle_baseline.py` |
| Check that the rules never use the future | `python test_no_lookahead.py` |
| Telegram: check the settings / send a test message / find the group id | `python telegram_notify.py --check` / `--test` / `--find-chat` |
| Test the Telegram alerts (no real bot needed) | `python test_telegram_notify.py` |

Stop any of them with **Ctrl + C**.

### Dashboard options
`python dashboard.py --port 5050` (other port), `--no-browser` (do not open the browser), `--no-reload` (turn off auto-reload, see below), `--no-alerts` (do not run the background Telegram alert loop, see section 3c).

**Auto-reload:** you do not need to stop and start the dashboard after changing the code. When any `.py` file in the project changes, the server restarts by itself and the page open in your browser refreshes itself a moment later (it may show "Cannot reach the dashboard server" for a second or two while the server restarts). Only `.py` files trigger it; changes to this README or to the data files (`trade_journal.csv`, `market_data.db`) do not. A dashboard that was started before auto-reload existed does not have it: stop it once with Ctrl + C and start it again. Use `--no-reload` if you want the old behaviour (the server never restarts by itself).

In the page: use the buttons at the top to pick NIFTY / SENSEX and 1 / 5 / 15 minute candles, and the drop-down to pick the data source. The page remembers your choices, and the moon / sun button switches between dark and light theme. It refreshes every 15 seconds. The chart library loads from the internet, so you need internet.

**Page layout (compact, made to fit one screen):**
- **Top bar:** title, market and data status (open / closed, data up to date or delayed), the time of the last update, and the controls, all in one row (two rows on a narrow window).
- **Decision banner:** the answer first. It says "No trade right now", or "Buy CE (call) idea" / "Buy PE (put) idea" with three tiles for BUY price, STOP LOSS and TARGET. A small "Practice only" tag appears when the market is closed or the data is delayed.
- **Chart:** price and change, the CE / PE legend and a "Reset view" button share one line above the chart. When there is an idea, dashed ENTRY, STOP LOSS and TARGET lines are drawn on it.
- **Why this decision:** the checklist, split into "Must pass" and "Bonus points", with the agreement meter.
- **Trade details** (only when there is an idea): the option, the index levels, the memory note and "How to place this trade". That last box starts closed; click it to open the steps.
- **Latest finished candle** and **Recent candle patterns:** the open / high / low / close, the pattern and what it means, and a short list of recent patterns.
- **Paper-trade scoreboard:** ideas, win rate and net result, plus the latest ideas.
- **Idea history** and **What the tool learned** are below. Click a row in the history to open its story, prices and chart.

The columns follow the size of your window. On a wide screen (about 1500 px and more) it is three columns in one row (chart, checklist and trade, candle and scoreboard) and the chart is as tall as the window. On a medium window the chart and candle row are on the left and the checklist and scoreboard on the right. On a narrow window or a phone it is one column. When an idea is showing, the right-hand column is longer than one screen, so scroll a little to see the scoreboard.

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
- **Safe to repeat:** running it again updates candles that already exist and never makes duplicates.

| What you want | Command |
|---|---|
| Save candles now | `python candle_store.py collect` |
| Only one index or candle size | `python candle_store.py collect --index nifty --interval 5m` |
| See what is stored (counts, dates, last run) | `python candle_store.py status` |
| Export to a CSV file | `python candle_store.py export --index nifty --interval 5m --out nifty_5m.csv` |

Use the saved history in your own code (same format as the live candles):

```python
from candle_store import load_candles, load_vix
df = load_candles("nifty", "5m")
vix = load_vix()
```

**Daily schedule:** a Windows scheduled task named `AI-Trading-Candle-Collect` runs `candle_store.py collect` every Monday to Friday at 4:00 pm (after the market closes). If the PC was off at that time, it runs when the PC is next on and you are logged in. Run `collect` yourself at least once a week; if the PC is off for more than 7 days, those 1-minute candles are lost for good. To remove the task: `Unregister-ScheduledTask -TaskName "AI-Trading-Candle-Collect" -Confirm:$false` (PowerShell).

The backtest still downloads from Yahoo by default. To test on your saved history, load it with `load_candles` and pass it to `compute_features` and `run_backtest`, or export a CSV and use `candle_backtest.py --csv`.

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
- **Free Yahoo data is about 15 minutes late**, so by default NO alert is sent on old data: the entry moment has usually passed. To get alerts anyway, set `TELEGRAM_ALLOW_DELAYED=1`. Or use Angel One for live data.
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

   For free Yahoo data, also remove the `#` from the line `TELEGRAM_ALLOW_DELAYED=1`. Put real values only in `.env`, never in `.env.example`.
6. **Test it.** Run `python telegram_notify.py --check` (it checks the token and shows the bot's name; the token itself is never shown), then `python telegram_notify.py --test` (it sends a test message to the chat).
7. **Start the dashboard** with `python dashboard.py`. The first lines say `Telegram alerts: ON` (or `OFF` and what is missing).

### Settings

Put them in `.env` or set them in the terminal (a value set in the terminal wins over `.env`):

| Setting | Meaning |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Required. From @BotFather. Anyone who has it can send messages as your bot. |
| `TELEGRAM_CHAT_ID` | Required. The group or chat that receives the messages. |
| `TELEGRAM_ALLOW_DELAYED` | `1` = also send alerts when the data is old. The message then starts with a `DELAYED DATA` line. Default: off. |
| `ALERT_MARKETS` | The markets the loop watches. Default `nifty:5m,sensex:5m`. |
| `ALERT_SOURCE` | `yahoo` (default) or `angel`. |

### Cautions

- **Keep the token private.** It belongs only in `.env`, which Git ignores. Do not put it in `.env.example`, in the code, in a screenshot or in a chat. If it leaks, open @BotFather, send `/revoke`, pick your bot and put the new token in `.env`.
- **Keep the group private.** Sharing signals with other people may need SEBI registration (see section 13), and the backtest tests so far show no clear edge over simple trend-following. A group where only you read the messages avoids the problem.
- If the group is turned into a "supergroup" (for example when many members join or it becomes public), Telegram gives it a new id that starts with `-100`. Run `--find-chat` again if the alerts stop.
- `python test_telegram_notify.py` tests all of this with a fake Telegram on your computer. It never uses your real token or group.

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
- Weekly expiry: NIFTY on Tuesday, SENSEX on Thursday. If that day is a holiday, expiry moves to the previous trading day. Keep the `HOLIDAYS` list in `option_pricer.py` updated.
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

## 9. Free data sources

| | Yahoo Finance | Angel One SmartAPI |
|---|---|---|
| Cost | Free | Free |
| Account needed | No | Yes (Angel One account) |
| Live quality | Delayed (usually about 15 minutes) | Better for live use |
| Option (CE/PE) prices | Estimated only | Real |

Start with Yahoo. The dashboard shows a warning when the data is old. In that case use it for learning only.

### Using Angel One (optional, later)
1. Open an Angel One account and create an app at `smartapi.angelone.in` to get your API key. Enable TOTP and keep the TOTP secret.
2. `pip install smartapi-python pyotp logzero`
3. Set 4 environment variables in the same terminal (never write them inside the code, never share them): `ANGEL_API_KEY`, `ANGEL_CLIENT_ID`, `ANGEL_PIN`, `ANGEL_TOTP_SECRET`
   - Windows (Command Prompt): `set ANGEL_API_KEY=your_value`
   - Windows (PowerShell): `$env:ANGEL_API_KEY="your_value"`
   - Mac / Linux: `export ANGEL_API_KEY=your_value`
4. Run `python dashboard.py` and choose "Angel One (free)" in the drop-down, or `python live_monitor.py --source angel`.

The Angel One code is written but not tested on a live account yet.

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
| Angel One login failed | Check the 4 environment variables in the same terminal and the TOTP secret. |
| No Telegram alert arrives | Check that the first lines of `python dashboard.py` say `Telegram alerts: ON`. With free Yahoo data set `TELEGRAM_ALLOW_DELAYED=1` (old data sends nothing by default). Alerts are only for the idea on screen right now, while the market is open. Run `python telegram_notify.py --check` and `--test` (section 3c). |
| `--find-chat` shows no chat | Add the bot to the group, send `/start@yourbotname` there, then run it again. |
| Telegram says "chat not found" | The chat id is wrong, or the bot is not in the group. Run `--find-chat` again. |

---

## 11. Known limits

- Everything was tested offline with fake candles. Live Yahoo and Angel One runs, and the chart in a real browser, still need to be confirmed on your machine.
- Yahoo gives only about 60 days of 5-minute candles, so the first backtest is a small sample.
- With Yahoo, option prices are **estimates** (live option prices are saved only with Angel One, and that code is not yet tested on a live account). A real exit can also be worse than the stop price if the market moves very fast.
- Market holidays are only partly handled (the `HOLIDAYS` list in `option_pricer.py`).
- Backtest results are on the INDEX in R, not in rupees.
- The rules are a starting idea. Only your backtest and history can tell if they work.
- The memory needs many examples before it is useful, and it can be fooled by luck or by a changed market.

---

## 12. Plan

1. **Round 1 (done):** filters, backtest, journal. Run the backtest, then watch the journal.
2. **Round 2 (needs Angel One account):** live data, real option prices, option chain (open interest) and futures volume.
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
