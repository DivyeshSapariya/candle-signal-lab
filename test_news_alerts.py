"""
test_news_alerts.py
-------------------
Checks the Telegram alerts for market-moving news with a temporary database (no internet, nothing is sent):
the first call only marks old stories, only market-moving and recent ones go out, a story never twice, the same
headline from two publishers once, at most 4 per message, and the message text.

Run:   python test_news_alerts.py
"""

import os
import tempfile

import candle_store
import news_feed as nf
import telegram_notify as tg

NOW = 1_800_000_000


def story(key, title, minutes_ago, source="mint_markets"):
    return {"guid": key, "title": title, "summary": "", "link": f"https://example.com/{key}",
            "published": NOW - minutes_ago * 60}


def main():
    with tempfile.TemporaryDirectory() as tmp:
        con = candle_store.connect(os.path.join(tmp, "t.db"))
        nf.save_items(con, "mint_markets", [story("old", "RBI cuts repo rate by 25 bps", 30)], NOW - 3600)

        assert nf.pending_alerts(con, NOW) == [], "the first call only marks what is already stored"
        assert nf.pending_alerts(con, NOW) == [], "...and the old story is never sent later"
        assert all(f["source"] != "_alerts" for f in nf.status(con)["feeds"]), "the marker is not shown as a feed"

        nf.save_items(con, "mint_markets", [
            story("a", "RBI keeps repo rate unchanged", 10),
            story("b", "Q2 results: Company X profit jumps", 5),                  # not market-moving
            story("c", "Fed signals rate cuts, Wall Street rallies", 20),
            story("d", "Budget 2027: finance minister meets industry", 4 * 60),    # too old
        ], NOW)
        nf.save_items(con, "et_markets", [story("a2", "RBI keeps repo rate unchanged!", 9)], NOW)   # same headline, other publisher
        got = nf.pending_alerts(con, NOW)
        titles = [g["title"] for g in got]
        assert len(got) == 2, titles
        assert titles[0].startswith("RBI") and titles[1].startswith("Fed"), "newest first, one copy of the RBI headline"
        assert nf.pending_alerts(con, NOW) == [], "a story is never sent twice"

        # a later copy of an already-sent headline is skipped too
        nf.save_items(con, "bs_markets", [story("a3", "RBI keeps repo rate unchanged", 1)], NOW)
        assert nf.pending_alerts(con, NOW) == []

        # a flood: only 4 in the message, the rest are dropped (not queued for later)
        nf.save_items(con, "mint_markets", [story(f"f{i}", f"FII selling number {i} hits market", i + 1) for i in range(7)], NOW)
        got = nf.pending_alerts(con, NOW)
        assert len(got) == nf.ALERT_MAX_PER_MESSAGE
        assert nf.pending_alerts(con, NOW) == []
        con.close()

    text = tg.format_news([{"time": "10:05", "title": "RBI keeps repo rate unchanged", "source": "Mint",
                            "tags": ["index", "rbi"], "link": "https://example.com/a"}])
    assert "MARKET NEWS" in text and "10:05 - RBI keeps repo rate unchanged" in text and "rbi" in text
    assert "https://example.com/a" in text and "For information only" in text
    assert "(2)" in tg.format_news([{"time": "1", "title": "a", "source": "x", "tags": []}] * 2)

    print("OK: news alerts: first call marks old, only market-moving and recent, never twice, same headline once, at most 4, text")


if __name__ == "__main__":
    main()
