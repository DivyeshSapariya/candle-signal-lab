"""
news_feed.py
------------
Market news for context, from the publishers' own public RSS feeds. It stores only what an RSS feed offers for
syndication: the headline, a short snippet and the link back to the article. Everything is saved in your own
database (market_data.db, table "news") so the dashboard never has to wait for a feed, and so history builds up
for later study.

Politeness (these are other people's servers):
  - one request per feed every 10 minutes during market hours, every hour otherwise
  - conditional requests (ETag / Last-Modified) where the feed supports them: an unchanged feed costs almost nothing
  - an honest User-Agent, a short timeout, and a failing feed is not hammered (it waits its normal interval)
  - robots.txt of each feed's site was checked when the feeds were chosen (all allow these paths)

News is CONTEXT only. No trading rule uses it, and it cannot be tested as a predictor until enough history is stored.

Run:   python news_feed.py fetch            fetch every feed that is due (the dashboard does this by itself)
       python news_feed.py fetch --force    ignore the waiting times
       python news_feed.py status           what is stored and how each feed is doing
       python news_feed.py latest --important
       python news_feed.py retag            work out the tags again after changing TAG_RULES
"""

import argparse
import email.utils
import html
import re
import sqlite3
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import candle_store

IST = ZoneInfo("Asia/Kolkata")
USER_AGENT = "candle-signal-lab/1.0 (personal research RSS reader)"

# key, publisher, feed address. Add or remove feeds here; check a publisher's terms before adding it.
FEEDS = [
    ("mint_markets", "Mint", "https://www.livemint.com/rss/markets"),
    ("mint_companies", "Mint", "https://www.livemint.com/rss/companies"),
    ("et_markets", "Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms"),
    ("et_top", "Economic Times", "https://economictimes.indiatimes.com/rssfeedstopstories.cms"),
    ("moneycontrol_top", "Moneycontrol", "https://www.moneycontrol.com/rss/MCtopnews.xml"),
    ("bs_markets", "Business Standard", "https://www.business-standard.com/rss/markets-106.rss"),
    ("businessline_markets", "BusinessLine", "https://www.thehindubusinessline.com/markets/feeder/default.rss"),
    ("rbi_press", "RBI", "https://www.rbi.org.in/pressreleases_rss.xml"),
]
PUBLISHER = {key: name for key, name, _ in FEEDS}

INTERVAL_MARKET = 600        # seconds between requests to one feed, weekdays 9:00-16:00 IST
INTERVAL_OFF = 3600          # the rest of the time
TIMEOUT = 15
MAX_BYTES = 2_000_000
SNIPPET_CHARS = 300
KEEP_DAYS = 365              # kept long on purpose: months of saved headlines are needed to test a news filter later

# --- tags: plain keyword rules, easy to read and change. "Market-moving" = one of the IMPORTANT tags. ---
TAG_RULES = {
    "index": r"\b(nifty|sensex|bank nifty)\b",
    "rbi": r"\b(rbi|reserve bank|repo rate|mpc|monetary policy)\b",
    "fed": r"\b(fed|federal reserve|fomc|powell)\b",
    "inflation": r"\b(inflation|cpi|wpi)\b",
    "gdp": r"\bgdp\b",
    "budget": r"\b(budget|fiscal deficit)\b",
    "crude": r"\b(crude|brent|oil prices?)\b",
    "rupee": r"\b(rupee|forex)\b",
    "fii": r"\b(fii|fpi|dii|foreign (institutional|portfolio) (investors?|flows?))\b",
    "geopolitics": r"\b(war|ceasefire|sanctions?|tariffs?|trump|attack|missile)\b",
    "results": r"\b(q[1-4] (results?|earnings)|quarterly (results?|earnings)|earnings)\b",
    "ipo": r"\bipo\b",
    "us_market": r"\b(wall street|dow jones|nasdaq|s&p 500)\b",
    "big_move": r"\b(crash(es|ed)?|plunge[sd]?|tumble[sd]?|surge[sd]?|soar(s|ed)?|rall(y|ies|ied)|circuit)\b",
}
IMPORTANT = {"rbi", "fed", "inflation", "gdp", "budget", "fii", "geopolitics", "big_move"}
_COMPILED = {tag: re.compile(rule, re.I) for tag, rule in TAG_RULES.items()}


def tag_item(title: str, summary: str = "") -> list:
    text = f"{title} {summary}"
    tags = [tag for tag, rule in _COMPILED.items() if rule.search(text)]
    if "big_move" in tags and not {"index", "us_market"} & set(tags):
        tags.remove("big_move")                      # a single stock surging is not a market-moving story
    return tags


# ---------------------------------------------------------------------------
# Reading a feed (RSS 2.0 and Atom)
# ---------------------------------------------------------------------------
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _clean(text) -> str:
    text = html.unescape(html.unescape(str(text or "")))           # some feeds escape twice
    text = re.sub(r"#(\d{2,5});", lambda m: chr(int(m.group(1))) if int(m.group(1)) < 0x3000 else m.group(0), text)   # "day#39;s": a feed that lost the "&"
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _when(text):
    """Publish time as seconds since 1970 (UTC), or None. A time without a zone is taken as Indian time."""
    text = (text or "").strip()
    if not text:
        return None
    parsed = None
    try:
        parsed = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return int(parsed.timestamp())


def parse_feed(data: bytes) -> list:
    """Items of an RSS / Atom feed: title, link, guid, published (epoch or None), summary. Raises ET.ParseError or ValueError."""
    root = ET.fromstring(data)
    if _local(root.tag) not in ("rss", "feed", "rdf"):
        raise ValueError("not an RSS or Atom feed")                  # for example an HTML page that says "blocked"
    items = []
    for node in root.iter():
        if _local(node.tag) not in ("item", "entry"):
            continue
        fields = {}
        for child in node:
            name = _local(child.tag)
            if name == "link":
                fields.setdefault("link", child.get("href") or (child.text or ""))
            elif name in ("title", "guid", "id", "description", "summary", "encoded", "content"):
                fields.setdefault(name, child.text or "")
            elif name in ("pubdate", "published", "updated", "date"):
                fields.setdefault("date", child.text or "")
        title = _clean(fields.get("title"))
        link = (fields.get("link") or "").strip()
        if not title or not link:
            continue
        summary = _clean(fields.get("description") or fields.get("summary") or fields.get("encoded") or fields.get("content"))
        items.append({
            "title": title[:400], "link": link, "guid": (fields.get("guid") or fields.get("id") or link).strip(),
            "published": _when(fields.get("date")), "summary": summary[:SNIPPET_CHARS],
        })
    return items


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def save_items(con: sqlite3.Connection, source: str, items: list, now: int) -> int:
    """Save new items (a story already stored for this feed is skipped). Returns how many were new."""
    new = 0
    with con:
        for it in items:
            tags = ",".join(tag_item(it["title"], it["summary"]))
            cur = con.execute(
                "INSERT OR IGNORE INTO news (source, guid, published, fetched, title, summary, link, tags) VALUES (?,?,?,?,?,?,?,?)",
                (source, it["guid"], it["published"] or now, now, it["title"], it["summary"], it["link"], tags))
            new += cur.rowcount
    return new


def prune(con: sqlite3.Connection, now: int, days: int = KEEP_DAYS) -> int:
    with con:
        return con.execute("DELETE FROM news WHERE published < ?", (now - days * 86400,)).rowcount


def _row(r) -> dict:
    tags = [t for t in (r[7] or "").split(",") if t]
    return {"id": r[0], "source": PUBLISHER.get(r[1], r[1]), "published": r[2], "title": r[3], "summary": r[4],
            "link": r[5], "tags": tags, "important": any(t in IMPORTANT for t in tags),
            "time": datetime.fromtimestamp(r[2], IST).strftime("%H:%M"), "date": datetime.fromtimestamp(r[2], IST).strftime("%d %b")}


def latest(con: sqlite3.Connection, limit: int = 15, important_only: bool = False, since_id: int = None,
           since_ts: int = None) -> list:
    """Newest first. since_id = only items stored after that id (the way to ask for 'what is new')."""
    where, args = [], []
    if since_id is not None:
        where.append("id > ?")
        args.append(since_id)
    if since_ts is not None:
        where.append("published >= ?")
        args.append(since_ts)
    rows = con.execute(
        "SELECT id, source, published, title, summary, link, guid, tags FROM news"
        + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY published DESC, id DESC LIMIT ?",
        (*args, limit * 6 if important_only else limit)).fetchall()
    items = [_row(r) for r in rows]
    if important_only:
        items = [i for i in items if i["important"]]
    return items[:limit]


ALERT_MAX_AGE = 3 * 3600          # a story published more than 3 hours ago is never sent as an alert
ALERT_MAX_PER_MESSAGE = 4         # the most headlines in one Telegram message


def _title_key(title: str) -> str:
    """The same story comes from two publishers with small differences: compare only letters and digits, first 60."""
    return "".join(ch for ch in (title or "").lower() if ch.isalnum())[:60]


def pending_alerts(con: sqlite3.Connection, now: int) -> list:
    """
    The market-moving stories that should go out as ONE Telegram message now, newest first, and mark them (and any
    copy of the same headline) as handled BEFORE returning, so a story is never sent twice.
    - the very first call only marks everything already stored (no flood of old news);
    - only stories published in the last 3 hours;
    - a headline already sent (any publisher) is skipped;
    - at most 4 headlines per message; older ones beyond that are marked and dropped, not queued.
    """
    first = con.execute("SELECT 1 FROM feed_state WHERE source='_alerts'").fetchone() is None
    with con:
        if first:
            con.execute("INSERT OR IGNORE INTO news_alerted (news_id, title_key, at) SELECT id, '', ? FROM news", (now,))
            con.execute("INSERT OR REPLACE INTO feed_state (source, checked, ok, note) VALUES ('_alerts', ?, 1, 'alerts switched on')", (now,))
            return []
    done = {r[0] for r in con.execute("SELECT title_key FROM news_alerted WHERE title_key != ''")}
    rows = con.execute(
        "SELECT id, source, published, title, summary, link, guid, tags FROM news "
        "WHERE published >= ? AND id NOT IN (SELECT news_id FROM news_alerted) ORDER BY published DESC, id DESC LIMIT 200",
        (now - ALERT_MAX_AGE,)).fetchall()
    send, seen = [], set(done)
    with con:
        for r in rows:
            item = _row(r)
            if not item["important"]:
                continue
            key = _title_key(item["title"])
            con.execute("INSERT OR IGNORE INTO news_alerted (news_id, title_key, at) VALUES (?,?,?)", (item["id"], key, now))
            if key in seen:
                continue
            seen.add(key)
            if len(send) < ALERT_MAX_PER_MESSAGE:
                send.append(item)
    return send


def retag(con: sqlite3.Connection) -> int:
    """Work out the tags of every stored story again (after changing TAG_RULES). Returns how many changed."""
    changed = 0
    with con:
        for rid, title, summary, tags in con.execute("SELECT id, title, summary, tags FROM news").fetchall():
            new = ",".join(tag_item(title or "", summary or ""))
            if new != (tags or ""):
                con.execute("UPDATE news SET tags=? WHERE id=?", (new, rid))
                changed += 1
    return changed


def status(con: sqlite3.Connection) -> dict:
    states = con.execute("SELECT source, checked, ok, note FROM feed_state WHERE source != '_alerts'").fetchall()
    total = con.execute("SELECT COUNT(*), MIN(published), MAX(published) FROM news").fetchone()
    return {"items": total[0], "oldest": total[1], "newest": total[2],
            "feeds": [{"source": s, "checked": c, "ok": bool(ok), "note": n} for s, c, ok, n in states]}


# ---------------------------------------------------------------------------
# Fetching (polite)
# ---------------------------------------------------------------------------
def interval_for(now: datetime) -> int:
    """Seconds between requests to one feed: shorter during market hours (weekdays 9:00 to 16:00 IST)."""
    t = now.astimezone(IST)
    return INTERVAL_MARKET if t.weekday() < 5 and 9 * 60 <= t.hour * 60 + t.minute <= 16 * 60 else INTERVAL_OFF


def fetch_one(con, key: str, url: str, now: datetime, opener=None, force: bool = False) -> str:
    """Fetch one feed if it is due. Returns 'skipped', 'not modified', 'new N', or 'error: ...'. Never raises."""
    opener = opener or urllib.request.urlopen
    stamp = int(now.timestamp())
    row = con.execute("SELECT etag, last_modified, checked FROM feed_state WHERE source=?", (key,)).fetchone()
    etag, modified, checked = row if row else (None, None, 0)
    if not force and checked and stamp - checked < interval_for(now):
        return "skipped"
    headers = {"User-Agent": USER_AGENT, "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    def remember(ok: bool, note: str, new_etag=etag, new_modified=modified):
        with con:
            con.execute("INSERT OR REPLACE INTO feed_state (source, etag, last_modified, checked, ok, note) VALUES (?,?,?,?,?,?)",
                        (key, new_etag, new_modified, stamp, int(ok), note[:200]))

    try:
        with opener(urllib.request.Request(url, headers=headers), timeout=TIMEOUT) as response:
            body = response.read(MAX_BYTES + 1)
            new_etag, new_modified = response.headers.get("ETag"), response.headers.get("Last-Modified")
        if len(body) > MAX_BYTES:
            remember(False, "error: feed larger than 2 MB, ignored")
            return "error: too large"
        items = parse_feed(body)
        new = save_items(con, key, items, stamp)
        remember(True, f"ok, {len(items)} items, {new} new", new_etag, new_modified)
        return f"new {new}"
    except urllib.error.HTTPError as error:
        if error.code == 304:
            remember(True, "not modified")
            return "not modified"
        remember(False, f"error: HTTP {error.code}")
        return f"error: HTTP {error.code}"
    except Exception as error:                                   # a broken feed must never stop the others
        remember(False, f"error: {str(error)[:120]}")
        return f"error: {str(error)[:60]}"


def fetch_all(con, now: datetime = None, opener=None, force: bool = False, pause: float = 0.5) -> dict:
    """Fetch every feed that is due, one after the other, with a short pause between requests."""
    now = now or datetime.now(timezone.utc)
    result = {"new": 0, "errors": 0, "fetched": 0}
    for key, _, url in FEEDS:
        outcome = fetch_one(con, key, url, now, opener, force)
        if outcome == "skipped":
            continue
        result["fetched"] += 1
        if outcome.startswith("error"):
            result["errors"] += 1
        elif outcome.startswith("new"):
            result["new"] += int(outcome.split()[1])
        if pause and opener is None:
            time.sleep(pause)
    prune(con, int(now.timestamp()))
    return result


def run_once(db: str = candle_store.DB_FILE, force: bool = False) -> dict:
    """Open the database, fetch what is due, close it (used by the dashboard's background loop and the command line)."""
    con = candle_store.connect(db)
    try:
        return fetch_all(con, force=force)
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(description="Market news from the publishers' public RSS feeds.")
    ap.add_argument("action", choices=["fetch", "status", "latest", "retag"])
    ap.add_argument("--force", action="store_true", help="fetch: ignore the waiting times")
    ap.add_argument("--important", action="store_true", help="latest: only market-moving tags")
    ap.add_argument("--n", type=int, default=12)
    ap.add_argument("--db", default=candle_store.DB_FILE)
    a = ap.parse_args()
    con = candle_store.connect(a.db)
    try:
        if a.action == "fetch":
            r = fetch_all(con, force=a.force)
            print(f"{r['fetched']} feeds asked, {r['new']} new items, {r['errors']} errors")
        elif a.action == "retag":
            print(f"{retag(con)} stories re-tagged")
        elif a.action == "status":
            s = status(con)
            print(f"{s['items']} items stored")
            for f in s["feeds"]:
                when = datetime.fromtimestamp(f["checked"], IST).strftime("%d %b %H:%M")
                print(f"  {PUBLISHER.get(f['source'], f['source']):<18} {f['source']:<22} {'ok ' if f['ok'] else 'BAD'} {when}  {f['note']}")
        else:
            for it in latest(con, a.n, important_only=a.important):
                print(f"{it['date']} {it['time']} [{it['source']}] {it['title']}  ({','.join(it['tags'])})")
    finally:
        con.close()


if __name__ == "__main__":
    main()
