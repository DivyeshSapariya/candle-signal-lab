"""
test_news_feed.py
-----------------
Checks the news reader with FAKE feeds (no internet): parsing, tags, storage without duplicates, polite fetching
(waiting times, conditional requests, failing feeds), and the "what is new since id" query.

Run:   python test_news_feed.py
"""

import os
import tempfile
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message

import candle_store
import news_feed as nf

RSS = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel><title>x</title>
<item><title><![CDATA[RBI keeps repo rate unchanged; Nifty, Sensex surge]]></title><link>https://example.com/a</link>
  <guid>g-a</guid><pubDate>Thu, 26 Nov 2026 10:05:00 +0530</pubDate>
  <description><![CDATA[<p>The <b>MPC</b> voted&nbsp;5-1 &amp; kept rates.</p>]]></description></item>
<item><title>Q2 results: Company X profit jumps</title><link>https://example.com/b</link>
  <pubDate>Thu, 26 Nov 2026 04:00:00 GMT</pubDate><description>day#39;s results</description></item>
<item><title>No date story</title><link>https://example.com/c</link></item>
<item><title></title><link>https://example.com/empty-title</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>Fed signals cuts, Wall Street rallies</title><link href="https://example.com/atom1"/>
 <id>tag:example.com,2026:1</id><updated>2026-11-26T05:00:00Z</updated><summary>Powell said inflation is cooling.</summary></entry></feed>"""


class FakeResponse:
    def __init__(self, body, etag=None, modified=None, status=200):
        self.body, self.status = body, status
        self.headers = Message()
        if etag:
            self.headers["ETag"] = etag
        if modified:
            self.headers["Last-Modified"] = modified

    def read(self, n=-1):
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def main():
    # --- parsing: RSS and Atom, CDATA, html, double-escaped text, missing dates, items without a title are dropped ---
    items = nf.parse_feed(RSS)
    assert [i["title"] for i in items] == ["RBI keeps repo rate unchanged; Nifty, Sensex surge",
                                           "Q2 results: Company X profit jumps", "No date story"], items
    assert items[0]["summary"] == "The MPC voted 5-1 & kept rates." and items[0]["guid"] == "g-a"
    ist = timezone(timedelta(hours=5, minutes=30))
    assert items[0]["published"] == int(datetime(2026, 11, 26, 10, 5, tzinfo=ist).timestamp())
    assert items[1]["published"] == int(datetime(2026, 11, 26, 4, 0, tzinfo=timezone.utc).timestamp())
    assert items[1]["summary"] == "day's results" and items[1]["guid"] == "https://example.com/b", "no guid: the link is the id"
    assert items[2]["published"] is None
    atom = nf.parse_feed(ATOM)
    assert len(atom) == 1 and atom[0]["link"] == "https://example.com/atom1" and atom[0]["guid"].startswith("tag:")
    assert atom[0]["published"] == int(datetime(2026, 11, 26, 5, 0, tzinfo=timezone.utc).timestamp())
    try:
        nf.parse_feed(b"this is not xml")
        raise AssertionError("must fail")
    except Exception as error:
        assert error.__class__.__name__ == "ParseError"
    naive = nf._when("2026-11-26T10:00:00")                                    # no zone: Indian time
    assert naive == int(datetime(2026, 11, 26, 10, 0, tzinfo=ist).timestamp())

    # --- tags ---
    assert set(nf.tag_item("RBI keeps repo rate unchanged; Nifty, Sensex surge")) >= {"rbi", "index", "big_move"}
    assert "fed" in nf.tag_item("Powell speaks") and "inflation" in nf.tag_item("CPI inflation cools")
    assert nf.tag_item("A quiet day for a small company") == []
    assert "big_move" not in nf.tag_item("Sugar stocks surge up to 14% today"), "one stock moving is not a market-moving story"
    assert "big_move" in nf.tag_item("Wall Street plunges, global selloff") and "big_move" in nf.tag_item("Nifty tumbles 500 points")
    assert "geopolitics" in nf.tag_item("New tariffs announced") and "results" in nf.tag_item("Q2 results out")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        path = os.path.join(tmp, "n.db")
        con = candle_store.connect(path)
        now = datetime(2026, 11, 26, 11, 0, tzinfo=ist)                          # a weekday, market hours: 10 minute waiting time
        stamp = int(now.timestamp())

        # --- storage: new items saved once, tags kept, dates default to the fetch time ---
        assert nf.save_items(con, "et_top", items, stamp) == 3
        assert nf.save_items(con, "et_top", items, stamp) == 0, "the same stories are not saved twice"
        assert nf.save_items(con, "mint_markets", items[:1], stamp) == 1, "the same story from another feed is its own row"
        rows = nf.latest(con, 10)
        assert rows[0]["title"].startswith("RBI keeps") or rows[0]["title"] == "No date story"
        top = [r for r in rows if r["title"].startswith("RBI keeps")][0]
        assert top["important"] and "rbi" in top["tags"] and top["source"] in ("Economic Times", "Mint") and top["time"] == "10:05"
        assert all(r["published"] for r in rows)
        only = nf.latest(con, 10, important_only=True)
        assert only and all(r["important"] for r in only)
        first_id = max(r["id"] for r in rows)
        nf.save_items(con, "et_top", [{"title": "Fresh story", "link": "https://example.com/new", "guid": "new", "published": stamp, "summary": ""}], stamp)
        newer = nf.latest(con, 10, since_id=first_id)
        assert [r["title"] for r in newer] == ["Fresh story"], "since_id gives only what is new"

        # --- polite fetching ---
        calls = []
        feed_key, _, feed_url = nf.FEEDS[0]

        def opener(request, timeout=None):
            calls.append(dict(request.header_items()))
            return FakeResponse(RSS, etag='"v1"', modified="Thu, 26 Nov 2026 05:00:00 GMT")

        assert nf.fetch_one(con, feed_key, feed_url, now, opener) == "new 2"          # g-a was saved above
        assert calls[0]["User-agent"] == nf.USER_AGENT, "an honest user agent"
        assert nf.fetch_one(con, feed_key, feed_url, now + timedelta(minutes=5), opener) == "skipped" and len(calls) == 1, "waits 10 minutes in market hours"
        # after the waiting time: a conditional request, and a 304 answer costs nothing
        def not_modified(request, timeout=None):
            calls.append(dict(request.header_items()))
            raise urllib.error.HTTPError(feed_url, 304, "Not Modified", Message(), None)
        assert nf.fetch_one(con, feed_key, feed_url, now + timedelta(minutes=11), not_modified) == "not modified"
        assert calls[1]["If-none-match"] == '"v1"' and calls[1]["If-modified-since"].startswith("Thu, 26 Nov 2026")
        # outside market hours the wait is an hour
        evening = datetime(2026, 11, 26, 20, 0, tzinfo=ist)
        assert nf.interval_for(now) == 600 and nf.interval_for(evening) == 3600
        assert nf.interval_for(datetime(2026, 11, 28, 11, 0, tzinfo=ist)) == 3600, "Saturday"
        # a failing feed is recorded, never raises, and is not asked again before its normal waiting time
        def broken(request, timeout=None):
            calls.append("x")
            raise urllib.error.URLError("no network")
        other_key, _, other_url = nf.FEEDS[1]
        assert nf.fetch_one(con, other_key, other_url, now, broken).startswith("error")
        n = len(calls)
        assert nf.fetch_one(con, other_key, other_url, now + timedelta(minutes=2), broken) == "skipped" and len(calls) == n
        def bad_xml(request, timeout=None):
            return FakeResponse(b"<html>blocked</html>")
        assert nf.fetch_one(con, nf.FEEDS[2][0], nf.FEEDS[2][2], now, bad_xml).startswith("error")
        def huge(request, timeout=None):
            return FakeResponse(b"x" * (nf.MAX_BYTES + 10))
        assert nf.fetch_one(con, nf.FEEDS[3][0], nf.FEEDS[3][2], now, huge) == "error: too large"

        # one broken feed does not stop the others
        con2 = candle_store.connect(os.path.join(tmp, "n2.db"))
        def mixed(request, timeout=None):
            if "livemint" in request.full_url:
                raise urllib.error.URLError("down")
            return FakeResponse(RSS)
        result = nf.fetch_all(con2, now, mixed, pause=0)
        assert result["fetched"] == len(nf.FEEDS) and result["errors"] == 2 and result["new"] > 0, result
        s = nf.status(con2)
        assert s["items"] > 0 and sum(1 for f in s["feeds"] if not f["ok"]) == 2

        # --- old items are removed after a year (and newer ones are kept) ---
        keep = int((now - timedelta(days=200)).timestamp())
        nf.save_items(con, "et_top", [{"title": "Half a year old", "link": "https://example.com/mid", "guid": "mid", "published": keep, "summary": ""}], stamp)
        old = int((now - timedelta(days=400)).timestamp())
        nf.save_items(con, "et_top", [{"title": "Ancient", "link": "https://example.com/old", "guid": "old", "published": old, "summary": ""}], stamp)
        assert nf.prune(con, stamp) == 1 and not [r for r in nf.latest(con, 50) if r["title"] == "Ancient"]
        con.close()
        con2.close()

    print("OK: news reader: RSS/Atom parsing, tags, no duplicates, polite waiting and conditional requests, failing feeds isolated")


if __name__ == "__main__":
    main()
