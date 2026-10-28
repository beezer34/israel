#!/usr/bin/env python3
"""Updater for Politics Decoded (index.html) after the 27 October 2026 election.

Two modes:
  --mode counts   Runs at 13:00, 18:00 and 23:00 Israel time. Reads the latest count and
                  rewrites ONLY the <script id="results-data"> block.
  --mode daily    Runs at 08:00 Israel time. Does the same, then asks Claude to update the coalition
                  talks (the <script id="coalition-data"> block, see coalition_update.py), pulls
                  election headlines and writes a report that the workflow posts as a GitHub issue.

Where the count comes from:
  The Central Elections Committee site (votes26.bechirot.gov.il) blocks visitors from outside
  Israel, including GitHub's servers. The Petek (thepetek.com/results) republishes the committee's
  count within minutes: share counted, valid votes, each list's vote share and the seats that share
  gives under the Bader-Ofer method. Its reuse terms allow this with credit and a link, which the
  page gives. The script reads The Petek's results page, checks the numbers hold together (seats add
  up to 120, blocs match, nothing goes backwards or jumps) and refuses to publish if they don't.

As a second check, the script works out the seats itself from the vote shares (3.25% threshold,
Bader-Ofer with the parties' surplus-vote agreements) and notes any list where it disagrees.

It never edits any other part of the page. If anything looks wrong, it leaves index.html alone
and says why in the report.

Local test (no network for the count, no Claude, no news):
  python scripts/update_site.py --index index.html --petek-file tests/petek_fixture.html --mode daily --no-news --no-claude
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

ELECTION_DAY = dt.date(2026, 10, 27)
FINAL_RESULTS_DUE = dt.date(2026, 11, 4)   # the committee publishes official results by this date
SEATS, THRESHOLD = 120, 0.0325

PETEK_URL = "https://www.thepetek.com/results"
OFFICIAL_URL = "https://votes26.bechirot.gov.il/"
SITE_URL = "https://beezer34.github.io/israel/"

UA = "politics-decoded-family-site/2.0 (+https://beezer34.github.io/israel/)"
# News sites block unknown bots, so the news fetch identifies as a normal browser.
NEWS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
# Headlines for the daily issue: Ynet's Hebrew feeds (main news and news flashes) and the Jerusalem Post.
# Kan has no public feed, and the Times of Israel feed refuses GitHub's servers; Claude searches both directly.
NEWS_FEEDS = [
    "https://www.ynet.co.il/Integration/StoryRss2.xml",
    "https://www.ynet.co.il/Integration/StoryRss1854.xml",
    "https://www.jpost.com/rss/rssfeedsisraelnews.aspx",
]

# The Petek party page slug (/party/<slug>) -> id used on the site
PETEK_IDS = {
    "likud": "likud", "yashar": "yashar", "jointlist": "jointlist", "beyachad": "together",
    "democrats": "democrats", "beiteinu": "beiteinu", "utj": "utj", "otzma": "otzma", "shas": "shas",
    "raam": "raam", "rz": "rzp", "reservists-economic": "reservists", "amcha-israel": "amcha",
    "blue-white": "bluewhite",
}
NAMES = {
    "likud": "Likud", "yashar": "Yashar", "together": "Together", "democrats": "The Democrats",
    "otzma": "Otzma Yehudit", "jointlist": "Joint List", "shas": "Shas", "utj": "UTJ",
    "beiteinu": "Yisrael Beiteinu", "raam": "Ra'am", "rzp": "Religious Zionism",
    "amcha": "Amcha Yisrael", "bluewhite": "Blue and White", "reservists": "Reservists",
}
# Surplus-vote agreements signed for 2026 (Central Elections Committee notice, 19 October 2026).
# An agreement lapses if either list misses the threshold.
SURPLUS_PAIRS = [("together", "beiteinu"), ("yashar", "democrats"), ("jointlist", "raam"),
                 ("likud", "rzp"), ("shas", "utj")]
# Must match the blocs used in index.html
BLOCS = {
    "Netanyahu bloc": ["likud", "shas", "utj", "rzp", "otzma", "amcha"],
    "Opposition": ["yashar", "together", "democrats", "beiteinu", "bluewhite", "reservists"],
    "Arab parties": ["jointlist", "raam"],
}
NEWS_WORDS = [
    "election", "knesset", "coalition", "threshold", "ballot", "vote count", "votes", "results",
    "herzog", "president", "consultations", "mandate", "recommend", "unity government", "caretaker",
    "likud", "yashar", "bennett", "lapid", "eisenkot", "netanyahu", "golan", "democrats",
    "ben-gvir", "ben gvir", "otzma", "smotrich", "religious zionism", "shas", "deri",
    "utj", "united torah", "haredi", "liberman", "lieberman", "yisrael beytenu", "yisrael beiteinu",
    "ra'am", "raam", "abbas", "joint list", "hadash", "jabareen", "gantz", "hendel", "reservists",
    "ofer winter", "amcha",
]
# Hebrew words take prefixes (ו, ה, ב, ל, ש...), so these are matched anywhere in the headline
NEWS_WORDS_HE = [
    "בחירות", "כנסת", "קואליצי", "אחוז החסימה", "ספירת", "הקולות", "קלפי", "מעטפות", "ועדת הבחירות",
    "הרצוג", "הנשיא", "התייעצויות", "מנדט", "להרכיב", "הרכבת הממשלה", "ממשלת אחדות", "ממשלת מעבר", "גוש",
    "נתניהו", "איזנקוט", "בנט", "לפיד", "גולן", "ליברמן", "בן גביר", "בן-גביר", "סמוטריץ", "דרעי",
    "עבאס", 'רע"ם', "רע״ם", "ג'בארין", "ג׳בארין", "טיבי", "הנדל", "וינטר",
    "ליכוד", "ישר!", "ביחד", "הדמוקרטים", 'ש"ס', "ש״ס", "יהדות התורה", "עוצמה יהודית", "הציונות הדתית",
    "המשותפת", "ישראל ביתנו", "המילואימניקים", "עמך ישראל",
]


class Fail(Exception):
    pass


# ---------------------------------------------------------------- fetching
def fetch(url, timeout=30, ua=UA):
    req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ---------------------------------------------------------------- parsing The Petek
class PartyRows(HTMLParser):
    """Collects the text inside every <a href="/party/<slug>"> link, in order."""

    def __init__(self):
        super().__init__()
        self.rows, self.cur, self.depth = [], None, 0

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        if self.cur is None:
            href = dict(attrs).get("href", "")
            m = re.fullmatch(r"/party/([a-z0-9-]+)", href)
            if m:
                self.cur, self.depth = {"slug": m.group(1), "bits": []}, 1
        else:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag == "a" and self.cur is not None:
            self.depth -= 1
            if self.depth == 0:
                self.rows.append(self.cur)
                self.cur = None

    def handle_data(self, data):
        if self.cur is not None and data.strip():
            self.cur["bits"].append(data.strip())


def page_text(page):
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page, flags=re.S)
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", t)).split())


def parse_petek(page):
    """Returns the count as The Petek shows it. Raises Fail if anything is missing or odd."""
    p = PartyRows()
    p.feed(page)
    lists = {}
    for r in p.rows:
        bits = r["bits"]
        # A full results row reads: letters, name, ",", leader, ":", seats or "—", "seats,", "20.7% · -5", "."
        if "seats," not in bits:
            continue
        i = bits.index("seats,")
        if i < 1 or i + 1 >= len(bits):
            continue
        seats_txt, share_txt = bits[i - 1], bits[i + 1]
        m = re.match(r"(\d+(?:\.\d+)?)%", share_txt)
        if not m:
            raise Fail(f"Couldn't read the vote share for '{r['slug']}' ('{share_txt}'). The Petek's layout may have changed.")
        seats = 0 if seats_txt in ("—", "-", "–") else int(seats_txt) if seats_txt.isdigit() else None
        if seats is None:
            raise Fail(f"Couldn't read the seats for '{r['slug']}' ('{seats_txt}').")
        name = bits[1] if len(bits) > 1 else r["slug"]
        if r["slug"] in lists:
            continue
        lists[r["slug"]] = {"name": html.unescape(name), "pct": float(m.group(1)), "seats": seats}
    if len(lists) < 10:
        raise Fail(f"Only found {len(lists)} lists on The Petek's results page. Its layout may have changed.")

    text = page_text(page)
    m = re.search(r"([\d,]+)\s+valid votes counted", text) or re.search(r"valid votes counted\s+([\d,]+)", text)
    if not m:
        raise Fail("Couldn't find the number of valid votes counted on The Petek's page.")
    valid = int(m.group(1).replace(",", ""))
    m = re.search(r"Counting\s*·\s*(\d+(?:\.\d+)?)% of ballots", text)
    reporting = float(m.group(1)) if m else None
    m = re.search(r'"dateModified":"([0-9T:\-.]+Z)"', page) or re.search(r'<time dateTime="([0-9T:\-.]+Z)"', page)
    if not m:
        raise Fail("Couldn't find when The Petek last updated its results.")
    as_of = m.group(1).replace(".000Z", "Z")
    stated = {}
    for num_, bloc in re.findall(r"(\d+)\s+seats for the\s+(coalition|opposition)", text):
        stated[bloc] = int(num_)
    return {"lists": lists, "validVotes": valid, "reporting": reporting, "asOf": as_of, "statedBlocs": stated}


# ---------------------------------------------------------------- seat calculation (second check)
def dhondt(votes, seats_have, extra):
    """Hand out `extra` seats one at a time to the highest votes / (seats + 1)."""
    seats = dict(seats_have)
    for _ in range(extra):
        best = max(votes, key=lambda k: (votes[k] / (seats[k] + 1), votes[k]))
        seats[best] += 1
    return seats


def bader_ofer(votes, pairs=SURPLUS_PAIRS):
    """votes: {list key: votes} for every list on the ballot. Returns {key: seats} for lists that got in."""
    total = sum(votes.values())
    passed = {k: v for k, v in votes.items() if v >= THRESHOLD * total}
    quota = sum(passed.values()) / SEATS
    seats = {k: int(v // quota) for k, v in passed.items()}

    # Lists with a surplus agreement compete for leftover seats as one unit
    units, unit_of = {}, {}
    for a, b in pairs:
        if a in passed and b in passed:
            units[a + "+" + b] = (a, b)
            unit_of[a] = unit_of[b] = a + "+" + b
    for k in passed:
        if k not in unit_of:
            units[k] = (k,)
    u_votes = {u: sum(passed[k] for k in members) for u, members in units.items()}
    u_seats = {u: sum(seats[k] for k in members) for u, members in units.items()}
    u_seats = dhondt(u_votes, u_seats, SEATS - sum(u_seats.values()))

    # Split each pair's seats between its two lists the same way
    for u, members in units.items():
        if len(members) == 1:
            seats[members[0]] = u_seats[u]
            continue
        sub_votes = {k: passed[k] for k in members}
        sub_quota = sum(sub_votes.values()) / u_seats[u]
        sub = {k: int(v // sub_quota) for k, v in sub_votes.items()}
        sub = dhondt(sub_votes, sub, u_seats[u] - sum(sub.values()))
        seats.update(sub)
    if sum(seats.values()) != SEATS:
        raise Fail(f"Seat calculation came to {sum(seats.values())}, not 120.")
    return seats


def build_results(parsed, today):
    lists = parsed["lists"]
    parties = {}
    for slug, l in lists.items():
        pid = PETEK_IDS.get(slug)
        if pid is None:
            if l["pct"] >= 1 or l["seats"]:
                raise Fail(f"'{l['name']}' has {l['pct']}% and {l['seats']} seats but isn't on the site. "
                           "It needs a party card before this can be published. Ask Claude to add it.")
            continue
        parties[pid] = {"pct": l["pct"], "seats": l["seats"]}
    for must in ("likud", "yashar"):
        if must not in parties:
            raise Fail(f"{NAMES[must]} is missing from The Petek's results. Its page may be mid-update.")

    total_seats = sum(l["seats"] for l in lists.values())
    if total_seats != SEATS:
        raise Fail(f"The Petek's seats add up to {total_seats}, not 120.")
    total_pct = sum(l["pct"] for l in lists.values())
    if not 98.5 <= total_pct <= 101.5:
        raise Fail(f"The Petek's vote shares add up to {total_pct:.1f}%, not about 100%.")
    for pid, p in parties.items():
        if p["seats"] and p["pct"] < 3.2:
            raise Fail(f"{NAMES[pid]} has seats with only {p['pct']}% of the vote.")
        if not p["seats"] and p["pct"] >= 3.3:
            raise Fail(f"{NAMES[pid]} has {p['pct']}% of the vote but no seats.")
    stated = parsed["statedBlocs"]
    b = blocs(parties)
    for key, ours in (("coalition", b["Netanyahu bloc"]), ("opposition", b["Opposition"])):
        if key in stated and stated[key] != ours:
            raise Fail(f"The Petek says the {key} bloc has {stated[key]} seats, but its party rows add up to {ours}.")

    reporting = parsed["reporting"]
    final = today >= FINAL_RESULTS_DUE and reporting is None
    return {
        "asOf": parsed["asOf"],
        "status": "final" if final else "counting",
        "reporting": reporting,
        "validVotes": parsed["validVotes"],
        "thresholdVotes": int(THRESHOLD * parsed["validVotes"]) + 1,
        "otherPct": round(max(0.0, 100 - sum(p["pct"] for p in parties.values())), 2),
        "source": PETEK_URL,
        "official": OFFICIAL_URL,
        "parties": dict(sorted(parties.items(), key=lambda kv: (-kv[1]["seats"], -kv[1]["pct"]))),
    }


def seat_check(parsed):
    """Work the seats out from the vote shares and list any party where The Petek differs by more than 1.
    Shares are rounded on The Petek's page, so a difference of 1 can be rounding."""
    votes = {}
    for slug, l in parsed["lists"].items():
        votes[PETEK_IDS.get(slug) or slug] = l["pct"] / 100 * parsed["validVotes"]
    ours = bader_ofer(votes)
    out = []
    for slug, l in parsed["lists"].items():
        key = PETEK_IDS.get(slug) or slug
        if abs(ours.get(key, 0) - l["seats"]) >= 1:
            out.append((key, l["seats"], ours.get(key, 0)))
    return out


def sanity_check(old, new):
    """Refuse updates that go backwards or jump in a way a real count doesn't."""
    if not old or not old.get("parties"):
        return
    if old.get("validVotes") and new["validVotes"] < old["validVotes"] * 0.98:
        raise Fail(f"Counted votes went down ({old['validVotes']:,} → {new['validVotes']:,}). Trying again next run.")
    ro, rn = old.get("reporting"), new.get("reporting")
    if ro is not None and rn is not None and rn < ro - 1:
        raise Fail(f"Share counted went down ({ro}% → {rn}%). Trying again next run.")
    grew = new["validVotes"] / max(old.get("validVotes") or 1, 1) - 1
    for pid, p in new["parties"].items():
        before = old["parties"].get(pid)
        if before and abs(p["pct"] - before["pct"]) > 4 and grew < 0.25:
            raise Fail(f"{NAMES.get(pid, pid)} jumped from {before['pct']}% to {p['pct']}% while only "
                       f"{grew:.0%} more votes were counted. That looks wrong, so nothing was published.")


# ---------------------------------------------------------------- index.html blocks
def block_re(name):
    return re.compile(r'(<script id="' + re.escape(name) + r'" type="application/json">\s*)(.*?)(\s*</script>)', re.S)


def read_block(index_html, name):
    m = block_re(name).search(index_html)
    if not m:
        raise Fail(f'index.html has no <script id="{name}"> block. Upload the new version of the site first.')
    return json.loads(m.group(2))


def write_block(index_html, name, data):
    body = json.dumps(data, indent=2, ensure_ascii=False)
    return block_re(name).sub(lambda m: m.group(1) + body + m.group(3), index_html, count=1)


# ---------------------------------------------------------------- report helpers
def blocs(parties):
    return {b: sum(parties.get(p, {}).get("seats", 0) for p in ids) for b, ids in BLOCS.items()}


def compare(old, new):
    lines, flags = [], []
    op, np_ = (old or {}).get("parties", {}), new["parties"]
    for pid, p in np_.items():
        a = op.get(pid, {}).get("seats", 0)
        b = p["seats"]
        if a != b:
            lines.append(f"- {NAMES.get(pid, pid)}: {a} → {b} seats ({'+' if b > a else ''}{b - a})")
        if a == 0 and b > 0 and op:
            flags.append(f"**{NAMES.get(pid, pid)} is now above the threshold** ({p['pct']}%).")
        if a > 0 and b == 0:
            flags.append(f"**{NAMES.get(pid, pid)} has dropped below the threshold** ({p['pct']}%).")
    ob, nb = blocs(op), blocs(np_)
    for b in nb:
        if ob[b] < 61 <= nb[b]:
            flags.append(f"**{b} reaches {nb[b]} seats, a majority on its own.**")
        if ob[b] >= 61 > nb[b] and op:
            flags.append(f"**{b} falls back below 61** ({nb[b]}).")
    return lines, flags, nb


def news(feed_urls, limit=10):
    items, errors = [], []
    for url in feed_urls:
        try:
            root = ET.fromstring(fetch(url, ua=NEWS_UA).encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url.split('/')[2]}: {e}")
            continue
        for it in root.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            low = title.lower()
            if any(w in title for w in NEWS_WORDS_HE) or any(re.search(r"\b" + re.escape(w) + r"\b", low) for w in NEWS_WORDS):
                items.append((title, link))
    # Up to `limit` headlines from each outlet, so one busy feed doesn't crowd out the others
    seen, out, per_host = set(), [], {}
    for t, l in items:
        host = l.split("/")[2] if l.count("/") >= 2 else ""
        if t not in seen and per_host.get(host, 0) < limit:
            seen.add(t)
            per_host[host] = per_host.get(host, 0) + 1
            out.append((t, l))
    return out, errors


def missing_claude_settings():
    """The Claude API sign-in uses workload identity federation (GitHub identity token → Anthropic).
    Returns the settings that are missing; empty when the coalition update can run."""
    needed = ["ANTHROPIC_FEDERATION_RULE_ID", "ANTHROPIC_ORGANIZATION_ID", "ANTHROPIC_SERVICE_ACCOUNT_ID",
              "ANTHROPIC_IDENTITY_TOKEN_FILE"]
    return [k for k in needed if not os.environ.get(k)]


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="index.html")
    ap.add_argument("--report", default="report.md")
    ap.add_argument("--mode", choices=["counts", "daily"], default="daily")
    ap.add_argument("--petek-file", help="read The Petek's results page from a file instead of the web (for testing)")
    ap.add_argument("--no-news", action="store_true")
    ap.add_argument("--no-claude", action="store_true", help="skip the coalition update")
    ap.add_argument("--today", help="YYYY-MM-DD, for testing")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # Windows consoles default to cp1252

    today = dt.date.fromisoformat(args.today) if args.today else dt.datetime.now(dt.timezone.utc).date()
    out = {"changed": "false", "status": "ok", "post_issue": "true" if args.mode == "daily" else "false"}
    report = [f"Automatic daily check for {SITE_URL} ({today.isoformat()})", ""]
    index_html = open(args.index, encoding="utf-8").read()
    original = index_html
    results = None

    # ---- 1. The count
    try:
        old = read_block(index_html, "results-data")
        page = open(args.petek_file, encoding="utf-8").read() if args.petek_file else fetch(PETEK_URL)
        parsed = parse_petek(page)
        new = build_results(parsed, today)
        if old.get("asOf") and new["asOf"] < old["asOf"]:
            raise Fail(f"The Petek's update ({new['asOf']}) is older than the site's ({old['asOf']}).")
        sanity_check(old, new)
        lines, flags, nb = compare(old, new)
        differ = seat_check(parsed)
        data_changed = {k: v for k, v in new.items() if k != "asOf"} != {k: v for k, v in (old or {}).items() if k != "asOf"}
        if data_changed:
            index_html = write_block(index_html, "results-data", new)
        results = new

        counted = f"{new['reporting']}% of ballots counted" if new["reporting"] is not None else "count complete"
        report.append("## Vote count")
        report.append(f"{'**Final official results.**' if new['status'] == 'final' else 'Still counting'}: {counted}, "
                      f"{new['validVotes']:,} valid votes ([The Petek]({PETEK_URL}), from the "
                      f"[Central Elections Committee]({OFFICIAL_URL})).")
        if data_changed:
            report.append("The site has been updated." + (" Seat changes since the last update:" if lines else " No seat changes."))
            report.extend(lines)
        else:
            report.append("No change since the last update. The site was not touched.")
        report.append("")
        report.append("Blocs now: " + " · ".join(f"{b} {s}" for b, s in nb.items()))
        if differ:
            flags.append("Working the seats out from The Petek's rounded vote shares gives a different answer for: "
                         + "; ".join(f"{NAMES.get(k, k)} (The Petek {a}, check {b})" for k, a, b in differ)
                         + ". One seat apart is usually rounding; more is worth a look.")
            if any(abs(a - b) > 1 for _, a, b in differ):
                raise Fail("The Petek's seats differ from the check calculation by more than one seat. Not published.")
        if flags:
            report += ["", "## Worth a look"] + [f"- {f}" for f in flags]
    except Exception as e:  # noqa: BLE001
        out["status"] = "error"
        index_html = original
        report += ["## Vote count", "**The count update failed, so the results on the site were not changed.**",
                   "", f"Reason: {e}", "", "If this repeats, ask Claude to look at scripts/update_site.py."]
        try:
            results = read_block(index_html, "results-data")
        except Exception:  # noqa: BLE001
            results = None

    # ---- 2. Headlines (daily only)
    headlines, news_errors = ([], [])
    if args.mode == "daily" and not args.no_news:
        headlines, news_errors = news(NEWS_FEEDS)

    # ---- 3. Coalition talks, written by Claude (daily only)
    if args.mode == "daily":
        report += ["", "## Coalition talks"]
        if args.no_claude:
            report.append("Skipped (--no-claude).")
        elif [k for k in missing_claude_settings() if k != "ANTHROPIC_IDENTITY_TOKEN_FILE"]:
            report.append("**Not updated: the Claude API sign-in isn't set up yet** (missing: "
                          + ", ".join(k for k in missing_claude_settings() if k != "ANTHROPIC_IDENTITY_TOKEN_FILE")
                          + "). Add them as repository variables under Settings → Secrets and variables → Actions → Variables.")
        elif missing_claude_settings():
            out["status"] = "error"
            report.append("**Not updated: the run couldn't get an identity token from GitHub** to sign in to the "
                          "Claude API. Check the 'Get an identity token' step in the run log.")
        else:
            try:
                from coalition_update import update_coalition  # noqa: PLC0415
                index_html, lines = update_coalition(index_html, results, headlines, today)
                report += lines
            except Exception as e:  # noqa: BLE001
                out["status"] = "error"
                report += ["**The coalition update failed, so that part of the site was not changed.**", "",
                           f"Reason: {e}"]

    # ---- 4. Headlines list for a human to skim
    if args.mode == "daily" and not args.no_news:
        report += ["", "## Election headlines (Ynet in Hebrew, and the Jerusalem Post)"]
        report += [f"- [{t}]({l})" for t, l in headlines] or ["No matching headlines today."]
        if news_errors:
            report.append(f"(Couldn't read: {'; '.join(news_errors)})")

    if index_html != original:
        with open(args.index, "w", encoding="utf-8") as f:
            f.write(index_html)
        out["changed"] = "true"
    if out["status"] == "error" and args.mode == "counts":
        out["post_issue"] = "false"   # the morning run will report it

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
