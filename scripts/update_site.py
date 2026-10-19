#!/usr/bin/env python3
"""Daily updater for Politics Decoded (index.html).

What it does:
  1. Reads the poll of polls from israelvote.meforum.org.
  2. Checks the numbers make sense (known parties, seats add up to 120).
  3. Rewrites ONLY the <script id="poll-data"> block in index.html.
  4. Pulls election headlines from the Times of Israel RSS feed.
  5. Writes a short report (report.md) that the workflow posts as a GitHub issue.

It never edits any other text on the site. News is only listed for a human to read.
If anything looks wrong, it leaves index.html alone and says why in the report.

Uses only the Python standard library.

Local test:
  python scripts/update_site.py --index index.html --mef-file tests/mef_fixture.html --no-news
"""
import argparse
import datetime as dt
import html
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

MEF_URL = "https://israelvote.meforum.org/"
NEWS_FEEDS = ["https://www.timesofisrael.com/feed/"]
ELECTION_DAY = dt.date(2026, 10, 27)
UA = "Mozilla/5.0 (compatible; politics-decoded-family-site/1.0; +https://beezer34.github.io/israel/)"

# MEF party page slug -> id used on the site
SLUGS = {
    "likud": "likud", "yashar": "yashar", "together": "together", "dems": "democrats",
    "otzma": "otzma", "joint-list": "jointlist", "shas": "shas", "utj": "utj",
    "yisrael-beytenu": "beiteinu", "raam": "raam", "rzp-zehut": "rzp",
    "amcha-yisrael": "amcha", "blue-white": "bluewhite", "reservists-economic": "reservists",
}
NAMES = {
    "likud": "Likud", "yashar": "Yashar", "together": "Together", "democrats": "The Democrats",
    "otzma": "Otzma Yehudit", "jointlist": "Joint List", "shas": "Shas", "utj": "UTJ",
    "beiteinu": "Yisrael Beiteinu", "raam": "Ra'am", "rzp": "Religious Zionism",
    "amcha": "Amcha Yisrael", "bluewhite": "Blue and White", "reservists": "Reservists",
}
# Must match electionKey5() in index.html
BLOCS = {
    "Netanyahu bloc": ["likud", "shas", "utj", "rzp", "otzma", "amcha"],
    "Opposition": ["yashar", "together", "democrats", "beiteinu", "bluewhite", "reservists"],
    "Arab parties": ["jointlist", "raam"],
}
NEWS_WORDS = [
    "election", "knesset", "poll", "coalition", "threshold", "ballot", "campaign",
    "likud", "yashar", "bennett", "eisenkot", "netanyahu", "golan", "democrats",
    "ben-gvir", "ben gvir", "otzma", "smotrich", "religious zionism", "shas", "deri",
    "utj", "united torah", "haredi", "liberman", "yisrael beytenu", "yisrael beiteinu",
    "ra'am", "raam", "abbas", "joint list", "hadash", "gantz", "blue and white",
    "hendel", "zelekha", "reservists", "ofer winter", "amcha", "draft", "judicial",
    "attorney general", "high court", "supreme court",
]

BLOCK_RE = re.compile(r'(<script id="poll-data" type="application/json">\s*)(.*?)(\s*</script>)', re.S)


class Fail(Exception):
    pass


# ---------------------------------------------------------------- fetching
def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- parsing MEF
class TableParser(HTMLParser):
    """Collects every <table> as a list of rows. Each cell keeps its text and any party slug."""

    def __init__(self):
        super().__init__()
        self.tables, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "table":
            self.tables.append([])
        elif tag == "tr" and self.tables:
            self.row = []
        elif tag in ("td", "th") and self.row is not None:
            self.cell = {"text": "", "slug": None}
        elif tag == "a" and self.cell is not None:
            m = re.search(r"parties/([a-z0-9-]+)/?", a.get("href", ""))
            if m:
                self.cell["slug"] = m.group(1)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.cell["text"] = " ".join(self.cell["text"].split())
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.row:
                self.tables[-1].append(self.row)
            self.row = None

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data


def num(s):
    m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", ""))
    if not m:
        raise Fail(f"Expected a number, got '{s}'")
    return float(m.group())


def find_table(tables, header_word):
    for t in tables:
        if t and any(header_word.lower() in c["text"].lower() for c in t[0]):
            return t
    raise Fail(f"Could not find the table with a '{header_word}' column. The MEF page layout may have changed.")


def col(header_row, word):
    for i, c in enumerate(header_row):
        if word.lower() in c["text"].lower():
            return i
    raise Fail(f"Column '{word}' missing. The MEF page layout may have changed.")


def parse_mef(page):
    p = TableParser()
    p.feed(page)

    main = find_table(p.tables, "MEF average")
    i_avg, i_polls = col(main[0], "MEF average"), col(main[0], "Polls")
    seats = {}
    for row in main[1:]:
        slug = row[0]["slug"]
        if slug not in SLUGS:
            raise Fail(f"Unknown party in the main table: '{row[0]['text']}' (slug '{slug}'). The site needs a new party card before this can be published.")
        v = num(row[i_avg]["text"])
        if v != int(v):
            raise Fail(f"Seat number for {row[0]['text']} is not a whole number: {v}")
        seats[SLUGS[slug]] = int(v)

    near = {}
    try:
        t2 = find_table(p.tables, "Polls under")
    except Fail:
        t2 = None
    if t2:
        h = t2[0]
        i_mean, i_share = col(h, "Mean seats"), col(h, "share")
        i_under, i_polls2 = col(h, "Polls under"), len(h) - 1
        for row in t2[1:]:
            slug = row[0]["slug"]
            if slug not in SLUGS:
                raise Fail(f"Unknown party near the threshold: '{row[0]['text']}' (slug '{slug}').")
            near[SLUGS[slug]] = {
                "meanSeats": num(row[i_mean]["text"]),
                "shareWhenUnder": num(row[i_share]["text"]),
                "pollsUnderPct": int(round(num(row[i_under]["text"]))),
                "polls": int(num(row[i_polls2]["text"])),
            }

    m = re.search(r"Last updated\s+([A-Z][a-z]+ \d{1,2}, \d{4})", html.unescape(re.sub(r"<[^>]+>", " ", page)))
    if not m:
        raise Fail("Could not find the 'Last updated' date on the MEF page.")
    as_of = dt.datetime.strptime(m.group(1), "%B %d, %Y").date()

    total = sum(seats.values())
    if total != 120:
        raise Fail(f"Main table seats add up to {total}, not 120.")
    if len(seats) < 8:
        raise Fail(f"Only {len(seats)} parties in the main table. That looks wrong.")
    overlap = set(seats) & set(near)
    if overlap:
        raise Fail(f"Party listed in both tables: {', '.join(overlap)}")

    return {"asOf": as_of.isoformat(), "source": MEF_URL, "seats": seats, "nearThreshold": near}


# ---------------------------------------------------------------- index.html
def read_block(index_html):
    m = BLOCK_RE.search(index_html)
    if not m:
        raise Fail('index.html has no <script id="poll-data"> block. Upload the new version of the site first.')
    return json.loads(m.group(2))


def write_block(index_html, data):
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return BLOCK_RE.sub(lambda m: m.group(1) + body + m.group(3), index_html, count=1)


# ---------------------------------------------------------------- report
def blocs(seats):
    return {b: sum(seats.get(p, 0) for p in ids) for b, ids in BLOCS.items()}


def compare(old, new):
    lines, flags = [], []
    os_, ns = old.get("seats", {}), new["seats"]
    for pid in sorted(set(os_) | set(ns), key=lambda k: -ns.get(k, 0)):
        a, b = os_.get(pid, 0), ns.get(pid, 0)
        if a != b:
            sign = "+" if b > a else ""
            lines.append(f"- {NAMES.get(pid, pid)}: {a} → {b} ({sign}{b - a})")
        if a == 0 and b > 0:
            flags.append(f"**{NAMES.get(pid, pid)} is now above the threshold** in the poll average ({b} seats). The site moves it into the Knesset automatically. Check its party card still reads right.")
        if a > 0 and b == 0:
            flags.append(f"**{NAMES.get(pid, pid)} has dropped below the threshold** in the poll average. The site now shows it on the threshold panel. Check its party card still reads right.")
    ob, nb = blocs(os_), blocs(ns)
    for b in nb:
        if ob[b] < 61 <= nb[b]:
            flags.append(f"**{b} reaches {nb[b]} seats, a majority on its own.** The election summary text changes automatically. Worth reading Part 5 again.")
        if ob[b] >= 61 > nb[b]:
            flags.append(f"**{b} falls back below 61** ({nb[b]}).")
    on, nn = old.get("nearThreshold", {}), new["nearThreshold"]
    for pid in set(on) | set(nn):
        a, b = on.get(pid), nn.get(pid)
        if a and b and (a["meanSeats"], a["pollsUnderPct"]) != (b["meanSeats"], b["pollsUnderPct"]):
            lines.append(f"- {NAMES[pid]} (near threshold): {a['meanSeats']} → {b['meanSeats']} seats on average, under the threshold in {b['pollsUnderPct']}% of polls")
    return lines, flags, nb


def news(feed_urls, limit=12):
    items, errors = [], []
    for url in feed_urls:
        try:
            root = ET.fromstring(fetch(url).encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            low = title.lower()
            if any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in NEWS_WORDS):
                items.append((title, link))
    seen, out = set(), []
    for t, l in items:
        if t not in seen:
            seen.add(t)
            out.append((t, l))
    return out[:limit], errors


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index.html")
    ap.add_argument("--report", default="report.md")
    ap.add_argument("--mef-file", help="read MEF HTML from a file instead of the web (for testing)")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--today", help="YYYY-MM-DD, for testing")
    args = ap.parse_args()

    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.utcnow().date()
    out = {"changed": "false", "status": "ok", "skip": "false"}
    report = [f"Automatic daily check for https://beezer34.github.io/israel/ ({today.isoformat()})", ""]

    if today > ELECTION_DAY:
        out["skip"] = "true"
        report.append("Election day has passed, so the poll updater has stopped. You can turn the workflow off in the Actions tab.")
    else:
        try:
            index_html = open(args.index, encoding="utf-8").read()
            old = read_block(index_html)
            page = open(args.mef_file, encoding="utf-8").read() if args.mef_file else fetch(MEF_URL)
            new = parse_mef(page)

            if new["asOf"] < old.get("asOf", ""):
                raise Fail(f"MEF's date ({new['asOf']}) is older than the site's ({old['asOf']}). Not going backwards.")

            lines, flags, nb = compare(old, new)
            data_changed = {k: v for k, v in new.items() if k != "asOf"} != {k: v for k, v in old.items() if k != "asOf"}

            if new != old:
                with open(args.index, "w", encoding="utf-8") as f:
                    f.write(write_block(index_html, new))
                out["changed"] = "true"

            report.append("## Polls")
            report.append(f"Poll of polls dated **{new['asOf']}** ([source]({MEF_URL})).")
            if data_changed:
                report.append("The site has been updated with these changes:")
                report.extend(lines)
            elif out["changed"] == "true":
                report.append("No seat changes. Only the date on the site was updated.")
            else:
                report.append("No change since the last check. The site was not touched.")
            report.append("")
            report.append("Blocs now: " + " · ".join(f"{b} {s}" for b, s in nb.items()))
            stale = (today - dt.date.fromisoformat(new["asOf"])).days
            if stale >= 3:
                flags.append(f"MEF hasn't updated its average for {stale} days.")
            if flags:
                report += ["", "## Worth a look"] + [f"- {f}" for f in flags]
        except Exception as e:  # noqa: BLE001
            out["status"] = "error"
            report += ["## Polls", f"**The poll update failed, so the site was not changed.**", "", f"Reason: {e}",
                       "", "If this repeats, the MEF page may have changed. Ask Claude to fix scripts/update_site.py."]

    if not args.no_news and out["skip"] == "false":
        items, errs = news(NEWS_FEEDS)
        report += ["", "## Election headlines (Times of Israel)"]
        if items:
            report += [f"- [{t}]({l})" for t, l in items]
        else:
            report.append("No matching headlines today.")
        if errs:
            report.append(f"(Couldn't read: {'; '.join(errs)})")
        report += ["", "The site's text is **not** changed from news automatically. If something here means a party card, "
                   "red line or explanation is out of date, paste the headline to Claude and ask for the edit."]

    with open(args.report, "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    gh = os.environ.get("GITHUB_OUTPUT")
    if gh:
        with open(gh, "a") as f:
            for k, v in out.items():
                f.write(f"{k}={v}\n")
    print("\n".join(report))
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
