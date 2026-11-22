"""
telegram_notify.py
------------------
Sends trade ideas (and how they ended) to a Telegram chat or group.

How it is used
  The dashboard calls this by itself. When a NEW idea appears (and, later, when that idea ends in
  target / stop loss / time exit) one short message is sent. Nothing else in the tool needs it.

Setup (once)
  1. In Telegram, talk to @BotFather, send /newbot and follow the steps. It gives you a TOKEN.
  2. Create a group (or private channel) for yourself and add your bot to it.
  3. Send any message in that group, then run:   python telegram_notify.py --find-chat
     It prints the CHAT ID of the group (a negative number for groups).
  4. Copy .env.example to .env and put the two values in it (the file .env is ignored by Git, so
     the token is never uploaded). Or set them in the terminal:
         PowerShell:  $env:TELEGRAM_BOT_TOKEN="..." ; $env:TELEGRAM_CHAT_ID="..."
  5. Check it:   python telegram_notify.py --check    then    python telegram_notify.py --test

Settings (environment variables or the .env file)
  TELEGRAM_BOT_TOKEN       required. Keep it secret. Anyone with it can send messages as your bot.
  TELEGRAM_CHAT_ID         required. Where the messages go.
  TELEGRAM_ALLOW_DELAYED   1 = also alert when the data is old (free Yahoo data is about 15 minutes
                           late). The message then says so. Default: no alert on old data.
  ALERT_MARKETS            markets the dashboard watches for alerts. Default nifty:5m,sensex:5m
  ALERT_SOURCE             yahoo (default) or angel

Notes
  - Paper trading only. A message is not financial advice. Posting ideas in a group that other
    people read may count as sharing signals (see README, safety notes). Keep the group private.
  - No extra package is needed (it uses only Python's standard library).
"""

import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

TIMEOUT_SECONDS = 10
MAX_TEXT = 4000                      # Telegram allows 4096 characters per message

DISCLAIMER = "Paper trading only. Not financial advice."


class TelegramError(Exception):
    """A readable problem. It never contains the bot token."""


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def load_env_file(path: str = None) -> list:
    """
    Read KEY=VALUE lines from a .env file next to this file and put them into the environment.
    Values that are already set in the terminal are NOT overwritten. Returns the NAMES loaded.
    """
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    loaded = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
                    loaded.append(key)
    except OSError:
        pass
    return loaded


def _setting(name: str) -> str:
    return os.environ.get(name, "").strip()


def is_configured() -> bool:
    return bool(_setting("TELEGRAM_BOT_TOKEN") and _setting("TELEGRAM_CHAT_ID"))


def allow_delayed() -> bool:
    return _setting("TELEGRAM_ALLOW_DELAYED").lower() in ("1", "true", "yes", "on")


def _api_base() -> str:
    return (_setting("TELEGRAM_API_BASE") or "https://api.telegram.org").rstrip("/")


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
def _call(method: str, payload: dict, token: str) -> dict:
    """One call to the Telegram Bot API. Raises TelegramError (without the token in the text)."""
    url = f"{_api_base()}/bot{token}/{method}"
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        reason = ""
        try:
            reason = json.loads(error.read().decode("utf-8")).get("description", "")
        except Exception:
            pass
        raise TelegramError(f"Telegram said {error.code}: {reason or error.reason}") from None
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise TelegramError(f"could not reach Telegram ({type(error).__name__})") from None
    if not body.get("ok"):
        raise TelegramError(f"Telegram refused it: {body.get('description', 'unknown reason')}")
    return body


def send_message(text: str, chat_id: str = None, token: str = None) -> None:
    """Send one message and wait for the answer. Raises TelegramError if it fails."""
    token = token or _setting("TELEGRAM_BOT_TOKEN")
    chat_id = chat_id or _setting("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise TelegramError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are not set")
    _call("sendMessage", {"chat_id": chat_id, "text": text[:MAX_TEXT],
                          "disable_web_page_preview": True}, token)


def send_async(text: str, attempts: int = 2) -> threading.Thread:
    """
    Send in the background, so a slow or broken Telegram can never slow down the dashboard.
    A failure is printed once and forgotten.
    """
    def work():
        for attempt in range(1, attempts + 1):
            try:
                send_message(text)
                return
            except TelegramError as error:
                if attempt == attempts:
                    print(f"[telegram] could not send the message: {error}")
                else:
                    time.sleep(2)
            except Exception as error:                 # never let a thread die noisily
                print(f"[telegram] unexpected problem: {type(error).__name__}")
                return

    thread = threading.Thread(target=work, name="telegram-send", daemon=True)
    thread.start()
    return thread


# ---------------------------------------------------------------------------
# Message text
# ---------------------------------------------------------------------------
def _rs(value) -> str:
    return "₹" + f"{float(value):,.2f}"


def _num(value) -> str:
    return f"{float(value):,.2f}"


def _clock(iso_text) -> str:
    try:
        return datetime.fromisoformat(str(iso_text)).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def format_idea(idea: dict, option_levels: dict = None, memory: dict = None, setup: str = "",
                index: str = "", interval: str = "", status: str = "", delayed_min=None) -> str:
    """
    The message for a NEW trade idea, kept short: the option, its expiry, the buy / stop loss /
    target prices and the loss and profit per unit. idea = the plan from candle_signals.evaluate().
    (memory, setup and status are accepted but not shown: the dashboard has them.)
    """
    side = idea.get("signal", "")
    dot = "🟢" if side == "CE" else "🔴"
    lines = [f"{dot} {side} IDEA - {idea.get('option', '')}"]
    when = _clock(idea.get("decision_time"))
    head = f"{str(index).upper()} {interval} candles".strip()
    if when:
        head += f" | confirmed at {when} IST"
    lines.append(head)
    if option_levels and option_levels.get("expiry"):
        lines.append(f"Expiry - {option_levels['expiry']}")
    if delayed_min is not None:
        lines.append(f"DELAYED DATA: about {delayed_min} min old, the price may have moved.")
    lines.append("")

    if option_levels:
        lines += [f"BUY - {_rs(option_levels['entry_price'])}",
                  f"STOP LOSS - {_rs(option_levels['stop_price'])}",
                  f"TARGET - {_rs(option_levels['target_price'])}",
                  "",
                  "Per unit:",
                  f"loss - {_rs(option_levels['loss_per_unit'])}",
                  f"profit - {_rs(option_levels['profit_per_unit'])}"]
    else:                      # no option prices could be worked out: show the index levels instead
        lines += [f"BUY - {_num(idea['entry'])} (index)",
                  f"STOP LOSS - {_num(idea['stop'])} (index)",
                  f"TARGET - {_num(idea['target'])} (index)"]
    return "\n".join(lines)


_RESULT_TITLE = {"TARGET": ("✅", "TARGET HIT"), "STOP": ("❌", "STOP LOSS HIT"),
                 "TIME": ("⏱", "TIME EXIT")}


def _result_line(info: dict) -> list:
    """
    The result in plain rupees: "Profit - ₹27.70 per unit" or "Loss - ₹13.00 per unit".
    Target and stop loss use the saved option prices. A time exit ended between the two, so its
    figure is an approximation ("about"). Without option prices it falls back to index points.
    """
    status, levels, gross = info.get("status"), info.get("option_levels"), info.get("r_gross")
    value, approx, unit, money = None, False, "", _rs
    if levels:
        unit = " per unit"
        if status == "TARGET":
            value = float(levels["profit_per_unit"])
        elif status == "STOP":
            value = -float(levels["loss_per_unit"])
        elif gross is not None:
            value = (gross * float(levels["loss_per_unit"]) if gross < 0
                     else gross * float(levels["profit_per_unit"]) / 2.0)
            approx = True
    elif info.get("entry") is not None and info.get("stop") is not None:
        risk = abs(float(info["entry"]) - float(info["stop"]))
        unit, money = " points (index)", _num
        if status == "TARGET" and info.get("target") is not None:
            value = abs(float(info["target"]) - float(info["entry"]))
        elif status == "STOP":
            value = -risk
        elif gross is not None:
            value, approx = gross * risk, True
    if value is None:
        return []
    if abs(value) < 0.005:
        return ["Result - break-even"]
    return [f"{'Profit' if value > 0 else 'Loss'} - {'about ' if approx else ''}{money(abs(value))}{unit}"]


def format_result(info: dict) -> str:
    """
    The message when an idea that was alerted has ended, kept short and in the same style as the
    idea message: the profit or loss per unit, how long it lasted, and the buy price with the
    price it ended at.
    """
    status = info.get("status")
    icon, title = _RESULT_TITLE.get(status, ("ℹ", str(status)))
    lines = [f"{icon} {title} - {info.get('option', '')}"]
    head = f"{str(info.get('index', '')).upper()} {info.get('interval', '')} candles".strip()
    closed = _clock(info.get("exit_time"))
    if closed:
        head += f" | closed at {closed} IST"
    lines.append(head)
    lines.append("")

    lines.extend(_result_line(info))
    if info.get("bars"):
        bars = int(info["bars"])
        lines.append(f"Held - {bars} candle{'' if bars == 1 else 's'} ({bars * _minutes(info.get('interval'))} min)")
    lines.append("")

    levels = info.get("option_levels")
    if levels:
        lines.append(f"BUY - {_rs(levels['entry_price'])}")
        if status == "TARGET":
            lines.append(f"TARGET - {_rs(levels['target_price'])}")
        elif status == "STOP":
            lines.append(f"STOP LOSS - {_rs(levels['stop_price'])}")
    elif info.get("entry") is not None:                  # no option prices were saved: index levels
        lines.append(f"BUY - {_num(info['entry'])} (index)")
        if status == "TARGET" and info.get("target") is not None:
            lines.append(f"TARGET - {_num(info['target'])} (index)")
        elif status == "STOP" and info.get("stop") is not None:
            lines.append(f"STOP LOSS - {_num(info['stop'])} (index)")
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def _minutes(interval) -> int:
    try:
        return int(str(interval).rstrip("m"))
    except (TypeError, ValueError):
        return 5


# ---------------------------------------------------------------------------
# Helpers you run by hand:  --check   --test   --find-chat
# ---------------------------------------------------------------------------
def _cli_check() -> int:
    token, chat = _setting("TELEGRAM_BOT_TOKEN"), _setting("TELEGRAM_CHAT_ID")
    print(f"TELEGRAM_BOT_TOKEN : {'set (' + str(len(token)) + ' characters, hidden)' if token else 'NOT set'}")
    print(f"TELEGRAM_CHAT_ID   : {chat or 'NOT set'}")
    print(f"Alerts on old data : {'yes (TELEGRAM_ALLOW_DELAYED)' if allow_delayed() else 'no (default)'}")
    print(f"Markets watched    : {_setting('ALERT_MARKETS') or 'nifty:5m,sensex:5m (default)'}")
    print(f"Data source        : {_setting('ALERT_SOURCE') or 'yahoo (default)'}")
    if not token:
        print("\nSet the token first (see the top of telegram_notify.py).")
        return 1
    try:
        me = _call("getMe", {}, token)["result"]
        print(f"\nBot found: @{me.get('username')}  ({me.get('first_name')})")
    except TelegramError as error:
        print(f"\nThe token does not work: {error}")
        return 1
    if not chat:
        print("No chat id yet. Add the bot to your group, send a message there, then run --find-chat.")
        return 1
    return 0


def _cli_test() -> int:
    try:
        send_message("Test message from candle-signal-lab. If you can read this, alerts will reach "
                     f"this chat.\n{DISCLAIMER}")
    except TelegramError as error:
        print(f"Could not send: {error}")
        return 1
    print("Sent. Look in your Telegram chat.")
    return 0


def _cli_find_chat() -> int:
    token = _setting("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Set TELEGRAM_BOT_TOKEN first.")
        return 1
    try:
        updates = _call("getUpdates", {"timeout": 0}, token).get("result", [])
    except TelegramError as error:
        print(f"Could not read updates: {error}")
        return 1
    chats = {}
    for update in updates:
        for key in ("message", "channel_post", "my_chat_member", "edited_message"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                chats[chat["id"]] = chat
    if not chats:
        print("No chats found yet. Add the bot to your group, send a message there (for example "
              "'hello'), then run this again.")
        return 1
    print("Chats this bot can see (use the id as TELEGRAM_CHAT_ID):")
    for chat_id, chat in chats.items():
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or ""
        print(f"  {chat_id:>16}   {chat.get('type', ''):<11} {name}")
    return 0


def main() -> int:
    load_env_file()
    args = set(sys.argv[1:])
    if "--find-chat" in args:
        return _cli_find_chat()
    if "--test" in args:
        return _cli_test()
    if "--check" in args:
        return _cli_check()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
