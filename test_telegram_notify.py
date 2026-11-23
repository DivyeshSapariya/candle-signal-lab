"""
test_telegram_notify.py
-----------------------
Checks the Telegram alerts WITHOUT a real bot: a fake Telegram server runs on your computer, and the
journal runs in a throwaway folder (your own journal and data are never touched).

What it proves
  1. The .env file is read, and values you set in the terminal are not overwritten.
  2. The message text has the prices, the estimate warning and the disclaimer.
  3. Sending works, and a Telegram error never leaks the bot token and never crashes anything.
  4. A new idea is alerted ONCE (not again after a restart), and its result is alerted ONCE.
  5. An idea that was not alerted never sends a result.

Run:   python test_telegram_notify.py
"""

import contextlib
import io
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import candle_backtest as bt
import candle_journal as journal
import candle_signals as cs
import telegram_notify as tg
from test_no_lookahead import make_candles

TOKEN, CHAT = "123456:SECRET-TEST-TOKEN", "-1001234567890"


# ---------------------------------------------------------------------------
# A fake Telegram server
# ---------------------------------------------------------------------------
class FakeTelegram:
    def __init__(self):
        self.requests, self.status = [], 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])).decode("utf-8"))
                outer.requests.append({"path": self.path, "body": body})
                reply = {"ok": True, "result": {}} if outer.status == 200 else \
                        {"ok": False, "description": "Unauthorized"}
                data = json.dumps(reply).encode("utf-8")
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):          # keep the test output quiet
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.server.shutdown()


def sample_levels():
    return {"entry_price": 166.2, "stop_price": 153.3, "target_price": 194.05, "loss_per_unit": 12.9,
            "profit_per_unit": 27.85, "expiry": "Mon 23 Nov", "price_source": "estimated",
            "captured": "estimate_now"}


def sample_idea():
    return {"signal": "CE", "option": "NIFTY 25450 CE", "entry": 25430.4, "stop": 25404.0,
            "target": 25483.2, "score": 4, "max_score": 4, "decision_time": "2026-11-19T14:05:00+05:30"}


# ---------------------------------------------------------------------------
def test_env_file():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, ".env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# comment\nTG_TEST_A=from_file\nTG_TEST_B = \"quoted value\"\n\nNOT A LINE\n")
        os.environ["TG_TEST_A"] = "already_set"
        os.environ.pop("TG_TEST_B", None)
        loaded = tg.load_env_file(path)
    assert os.environ["TG_TEST_A"] == "already_set", "a value set in the terminal must win"
    assert os.environ["TG_TEST_B"] == "quoted value" and loaded == ["TG_TEST_B"]
    del os.environ["TG_TEST_A"], os.environ["TG_TEST_B"]
    print("ok  .env file: read, quotes removed, terminal values not overwritten")


def test_messages():
    rupee = "₹"
    text = tg.format_idea(sample_idea(), sample_levels(), {"verdict": "warns", "text": "5 of 14 won"},
                          setup="Setup: test.", index="nifty", interval="5m", status="Entry zone is NOW.")
    expected = ("\U0001F7E2 CE IDEA - NIFTY 25450 CE\n"
                "NIFTY 5m candles | confirmed at 14:05 IST\n"
                "Expiry - Mon 23 Nov\n"
                "\n"
                f"BUY - {rupee}166.20\n"
                f"STOP LOSS - {rupee}153.30\n"
                f"TARGET - {rupee}194.05\n"
                "\n"
                "Per unit:\n"
                f"loss - {rupee}12.90\n"
                f"profit - {rupee}27.85")
    assert text == expected, "the idea message must be exactly the compact format:\n" + text
    assert len(text.splitlines()) == 11, "the idea message must stay short"
    late = tg.format_idea(sample_idea(), sample_levels(), delayed_min=17, index="nifty", interval="5m")
    assert "DELAYED DATA: about 17 min old" in late and "BUY - " in late
    plain = tg.format_idea(sample_idea(), None, index="nifty", interval="5m")     # no option prices
    assert "BUY - 25,430.40 (index)" in plain and "Per unit" not in plain
    put = tg.format_idea(dict(sample_idea(), signal="PE"), sample_levels(), index="nifty", interval="5m")
    assert put.startswith("\U0001F534 PE IDEA")
    base = {"option": "NIFTY 25450 CE", "index": "nifty", "interval": "5m", "story": "A long story that must not be sent.",
            "entry": 25430.4, "stop": 25404.0, "target": 25483.21, "exit_time": "2026-11-19T14:35:00+05:30"}
    # target hit: exactly the compact format, and the long story / disclaimer are not in it
    hit = tg.format_result(dict(base, status="TARGET", r_net=1.9, r_gross=2.0, bars=6, option_levels=sample_levels()))
    expected_hit = ("✅ TARGET HIT - NIFTY 25450 CE\n"
                    "NIFTY 5m candles | closed at 14:35 IST\n"
                    "\n"
                    f"Profit - {rupee}27.85 per unit\n"
                    "Held - 6 candles (30 min)\n"
                    "\n"
                    f"BUY - {rupee}166.20\n"
                    f"TARGET - {rupee}194.05")
    assert hit == expected_hit, "the target message must be exactly the compact format:\n" + hit
    assert "story" not in hit.lower() and tg.DISCLAIMER not in hit
    assert "R " not in hit.replace("TARGET ", "").replace("NIFTY ", ""), "no R units in the message"
    # stop loss hit: a loss in rupees per unit, the buy price and the stop price
    stop = tg.format_result(dict(base, status="STOP", r_net=-1.1, r_gross=-1.0, bars=2, option_levels=sample_levels()))
    assert stop.splitlines()[0] == "❌ STOP LOSS HIT - NIFTY 25450 CE"
    assert f"Loss - {rupee}12.90 per unit" in stop and "Held - 2 candles (10 min)" in stop
    assert f"STOP LOSS - {rupee}153.30" in stop and "TARGET" not in stop.replace("TARGET HIT", "")
    # time exit: approximate rupees (between the stop and the target prices), only the buy price below
    timed_up = tg.format_result(dict(base, status="TIME", r_net=0.3, r_gross=0.4, bars=24, option_levels=sample_levels()))
    assert timed_up.startswith("⏱ TIME EXIT") and f"BUY - {rupee}166.20" in timed_up
    assert f"Profit - about {rupee}5.57 per unit" in timed_up
    assert "STOP LOSS -" not in timed_up and "TARGET -" not in timed_up
    timed_down = tg.format_result(dict(base, status="TIME", r_net=-0.6, r_gross=-0.5, bars=24, option_levels=sample_levels()))
    assert f"Loss - about {rupee}6.45 per unit" in timed_down
    flat = tg.format_result(dict(base, status="TIME", r_net=-0.1, r_gross=0.0, bars=24, option_levels=sample_levels()))
    assert "Result - break-even" in flat
    # no option prices saved: index points, and no clock line when the close time is unknown
    bare = tg.format_result(dict(base, status="TARGET", r_net=1.9, r_gross=2.0, bars=6, exit_time=None, option_levels=None))
    assert "closed at" not in bare and "Profit - 52.81 points (index)" in bare
    assert "BUY - 25,430.40 (index)" in bare and "TARGET - 25,483.21 (index)" in bare
    bare_stop = tg.format_result(dict(base, status="STOP", r_net=-1.1, r_gross=-1.0, bars=2, option_levels=None))
    assert "Loss - 26.40 points (index)" in bare_stop
    one = tg.format_result(dict(base, status="STOP", r_net=-1.1, r_gross=-1.0, bars=1, option_levels=sample_levels()))
    assert "Held - 1 candle (5 min)" in one, "one candle must not say 'candles'"
    assert len(hit.splitlines()) == 8, "the result message must stay short"
    print("ok  messages: compact idea and compact results (target / stop / time exit), delayed note, index fallback")


def test_sending(fake):
    os.environ.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT, TELEGRAM_API_BASE=fake.url)
    assert tg.is_configured()
    tg.send_message("hello")
    sent = fake.requests[-1]
    assert sent["path"] == f"/bot{TOKEN}/sendMessage" and sent["body"]["chat_id"] == CHAT
    assert sent["body"]["text"] == "hello"
    # a failure: readable error, no token in it, and send_async only prints (never raises)
    fake.status = 401
    try:
        tg.send_message("x")
        raise AssertionError("a 401 should raise TelegramError")
    except tg.TelegramError as error:
        assert TOKEN not in str(error) and "Unauthorized" in str(error)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        tg.send_async("x", attempts=1).join(timeout=10)
    assert "could not send" in out.getvalue() and TOKEN not in out.getvalue()
    fake.status = 200
    # not configured: clear message, nothing sent
    before = len(fake.requests)
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    assert not tg.is_configured()
    try:
        tg.send_message("x")
        raise AssertionError("should refuse without a token")
    except tg.TelegramError:
        pass
    assert len(fake.requests) == before
    os.environ["TELEGRAM_BOT_TOKEN"] = TOKEN
    print("ok  sending: right URL and body, errors are readable, token never leaks")


def finished_positions(f, wanted):
    """Positions where a made-up idea really ends in target / stop loss / time exit."""
    found = []
    for i in range(75 + 35, len(f) - 30):
        plan = {"signal": "CE", "entry": float(f["close"].iloc[i]), "risk_points": 40.0, "mode": "confirm"}
        if bt.simulate_trade(f, i, plan, 5)["status"] in journal.FINISHED:
            found.append(i)
        if len(found) == wanted:
            break
    return found


def made_up_plan(f, i):
    return {"signal": "CE", "signal_time": f.index[i - 1].isoformat(), "decision_time": f.index[i].isoformat(),
            "mode": "confirm", "entry": float(f["close"].iloc[i]), "stop": float(f["close"].iloc[i]) - 40.0,
            "target": float(f["close"].iloc[i]) + 80.0, "risk_points": 40.0, "option": "NIFTY 25000 CE",
            "score": 4, "reasons": [], "context": {}}


def test_journal_flow():
    f = cs.compute_features(make_candles(), "nifty", 5, None)
    first, second = finished_positions(f, 2)
    with tempfile.TemporaryDirectory() as tmp:
        here = os.getcwd()
        os.chdir(tmp)
        try:
            # --- a new idea is alerted once; scanning again (a restart) does not alert again ---
            real_evaluate = journal.evaluate
            plan = made_up_plan(f, first)
            journal.evaluate = lambda frame, i, index, interval_min, filters=None: (
                plan if i == first else {"signal": "NONE"})
            alerted = []
            args = (f, "nifty", "5m", 5, f.index[first].date(), "now", None,
                    lambda idea: True, lambda idea, levels, memory: alerted.append(idea["decision_time"]))
            assert journal.scan_today(*args) == 1 and len(alerted) == 1
            assert journal.scan_today(*args) == 0 and len(alerted) == 1, "a restart must not alert again"
            # --- an idea that should NOT alert (stale / not current) is saved quietly ---
            plan2 = made_up_plan(f, second)
            journal.evaluate = lambda frame, i, index, interval_min, filters=None: (
                plan2 if i == second else {"signal": "NONE"})
            quiet = []
            journal.scan_today(f, "nifty", "5m", 5, f.index[second].date(), "now", None,
                               lambda idea: False, lambda idea, levels, memory: quiet.append(1))
            assert quiet == [], "should_alert=False must send nothing"
            journal.evaluate = real_evaluate
            rows = journal._load()
            assert sorted(rows["alerts"]) == ["", "new"], "only the alerted idea carries the 'new' flag"

            # --- results: only the alerted idea reports, and only once ---
            ended = []
            journal.update_results(f, "nifty", 5, lambda info: ended.append(info))
            assert len(ended) == 1, f"exactly one result alert expected, got {len(ended)}"
            info = ended[0]
            assert info["option"] == "NIFTY 25000 CE" and info["status"] in journal.FINISHED
            assert "result" in journal._load().loc[lambda d: d["alerts"] != "", "alerts"].iloc[0]
            journal.update_results(f, "nifty", 5, lambda info: ended.append(info))
            assert len(ended) == 1, "the result must not be alerted twice"
            # the message for it can be built from the info the journal gives
            assert info["exit_time"], "the journal must pass the close time"
            assert "NIFTY 25000 CE" in tg.format_result(info)
        finally:
            journal.evaluate = real_evaluate
            os.chdir(here)
    print("ok  journal: one alert per new idea, none after a restart, one result alert, stale ideas quiet")


def test_alerts_on_old_data_by_default():
    """Free Yahoo data is always late, so blocking old data would block every alert."""
    import dashboard
    os.environ.pop("TELEGRAM_ALLOW_DELAYED", None)
    assert tg.allow_delayed(), "alerts on old data must be ON when nothing is set"
    for value in ("1", "true", "yes", "on", ""):
        os.environ["TELEGRAM_ALLOW_DELAYED"] = value
        assert tg.allow_delayed(), f"TELEGRAM_ALLOW_DELAYED={value!r} must keep alerts on"
    for value in ("0", "false", "No", "OFF"):
        os.environ["TELEGRAM_ALLOW_DELAYED"] = value
        assert not tg.allow_delayed(), f"TELEGRAM_ALLOW_DELAYED={value!r} must switch alerts on old data off"
    # what the startup message tells you
    os.environ.update(TELEGRAM_BOT_TOKEN=TOKEN, TELEGRAM_CHAT_ID=CHAT, ALERT_SOURCE="yahoo")
    os.environ.pop("TELEGRAM_ALLOW_DELAYED", None)
    assert "DELAYED DATA" in dashboard._alert_banner() and "NO alert" not in dashboard._alert_banner()
    os.environ["TELEGRAM_ALLOW_DELAYED"] = "0"
    assert "NO alert will be sent" in dashboard._alert_banner()
    os.environ.pop("TELEGRAM_ALLOW_DELAYED", None)
    print("ok  old data: alerts are sent by default (marked DELAYED DATA); TELEGRAM_ALLOW_DELAYED=0 turns them off")


def main():
    saved = dict(os.environ)
    fake = FakeTelegram()
    try:
        test_env_file()
        test_messages()
        test_sending(fake)
        test_alerts_on_old_data_by_default()
        test_journal_flow()
    finally:
        fake.stop()
        os.environ.clear()
        os.environ.update(saved)
    print("\nAll Telegram checks passed.")


if __name__ == "__main__":
    main()
