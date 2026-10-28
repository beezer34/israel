"""Tests for the updater. Run from the repository root:  python -m unittest discover tests"""
import copy
import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import coalition_update as cu  # noqa: E402
import update_site as us  # noqa: E402

HERE = os.path.dirname(__file__)
TODAY = dt.date(2026, 10, 28)


def fixture():
    with open(os.path.join(HERE, "petek_fixture.html"), encoding="utf-8") as f:
        return f.read()


class Count(unittest.TestCase):
    def test_parse_petek(self):
        parsed = us.parse_petek(fixture())
        self.assertEqual(len(parsed["lists"]), 35)
        self.assertEqual(parsed["validVotes"], 4006695)
        self.assertEqual(parsed["reporting"], 76.89)
        self.assertEqual(parsed["asOf"], "2026-10-28T08:58:52Z")
        res = us.build_results(parsed, TODAY)
        self.assertEqual(res["status"], "counting")
        self.assertEqual(res["parties"]["together"], {"pct": 8.8, "seats": 11})     # slug "beyachad"
        self.assertEqual(res["parties"]["rzp"]["seats"], 6)                         # slug "rz"
        self.assertEqual(res["parties"]["reservists"]["seats"], 0)
        self.assertEqual(us.blocs(res["parties"]), {"Netanyahu bloc": 54, "Opposition": 47, "Arab parties": 19})

    def test_own_seat_calculation_agrees(self):
        self.assertEqual(us.seat_check(us.parse_petek(fixture())), [])

    def test_official_numbers_28_oct_0757(self):
        # Central Elections Committee page, 28 Oct 2026 07:57 (51% counted); news reported 55 / 49 / 16
        v = dict(likud=607519, yashar=490877, together=264631, jointlist=262714, democrats=202124, utj=200220,
                 beiteinu=178743, shas=168103, otzma=158164, rzp=144046, raam=102825, reservists=77560, amcha=48553)
        v["other"] = 2937354 - sum(v.values())
        seats = us.bader_ofer(v)
        self.assertEqual(us.blocs({k: {"seats": n} for k, n in seats.items()}),
                         {"Netanyahu bloc": 55, "Opposition": 49, "Arab parties": 16})

    def test_surplus_agreement_lapses_below_threshold(self):
        v = {"a": 500, "b": 20, "c": 480}            # b is under 3.25%, so a+b is not a unit
        self.assertEqual(us.bader_ofer(v, pairs=[("a", "b")]), {"a": 61, "c": 59})

    def test_final_only_after_publication_date(self):
        parsed = us.parse_petek(fixture())
        parsed["reporting"] = None
        self.assertEqual(us.build_results(parsed, TODAY)["status"], "counting")
        self.assertEqual(us.build_results(parsed, dt.date(2026, 11, 4))["status"], "final")

    def test_bad_pages_are_refused(self):
        page = fixture()
        with self.assertRaises(us.Fail):                              # a seat count that doesn't add up
            us.build_results(us.parse_petek(page.replace(">27<span class=\"sr-only\"> seats, ", ">29<span class=\"sr-only\"> seats, ", 1)), TODAY)
        with self.assertRaises(us.Fail):                              # an unknown list with real support
            us.build_results(us.parse_petek(page.replace('href="/party/yashar"', 'href="/party/brand-new"', 1)), TODAY)
        with self.assertRaises(us.Fail):                              # layout changed: no rows at all
            us.parse_petek(page.replace("/party/", "/list/"))
        with self.assertRaises(us.Fail):                              # no total
            us.parse_petek(page.replace("valid votes counted", "votes"))

    def test_sanity_rejects_backwards_and_jumps(self):
        res = us.build_results(us.parse_petek(fixture()), TODAY)
        fewer = copy.deepcopy(res)
        fewer["validVotes"] = int(res["validVotes"] * 0.9)
        with self.assertRaises(us.Fail):
            us.sanity_check(res, fewer)
        jump = copy.deepcopy(res)
        jump["parties"]["likud"]["pct"] += 6
        with self.assertRaises(us.Fail):
            us.sanity_check(res, jump)
        us.sanity_check(res, copy.deepcopy(res))       # identical is fine

    def test_block_roundtrip(self):
        page = '<head><script id="results-data" type="application/json">\n{}\n    </script></head>'
        out = us.write_block(page, "results-data", {"a": 1})
        self.assertEqual(us.read_block(out, "results-data"), {"a": 1})


def good_raw():
    return {
        "stage": "consultations",
        "headline_en": "President Herzog began consultations with the parties.",
        "headline_he": "הנשיא הרצוג החל בהתייעצויות עם המפלגות.",
        "mandate_holder": "", "mandate_given": "", "mandate_deadline": "",
        "recommendations": [{"party": "likud", "recommends": "netanyahu"}, {"party": "raam", "recommends": "none"}],
        "key_dates": [{"date": "2026-11-04", "label_en": "Official results", "label_he": "התוצאות הרשמיות"}],
        "new_updates": [{
            "date": "2026-10-28", "title_en": "Talks begin", "title_he": "המגעים מתחילים",
            "text_en": "Parties started talking — informally.", "text_he": "המפלגות החלו לדבר.",
            "sources": [{"name": "Times of Israel", "url": "https://www.timesofisrael.com/x/"}],
        }],
        "editor_notes": "",
    }


class CoalitionChecks(unittest.TestCase):
    def test_good_update_passes_and_is_cleaned(self):
        state, updates, notes = cu.clean(good_raw(), TODAY)
        self.assertEqual(state["recommendations"], {"likud": "netanyahu", "raam": "none"})
        self.assertNotIn("—", updates[0]["text"]["en"])           # no em dashes on the site
        self.assertEqual(notes, "")

    def test_rejects_bad_fields(self):
        cases = [
            ("stage", "victory"),
            ("headline_he", "English only"),
            ("mandate_holder", "trump"),
        ]
        for key, val in cases:
            raw = good_raw()
            raw[key] = val
            with self.assertRaises(cu.Fail, msg=key):
                cu.clean(raw, TODAY)
        raw = good_raw()
        raw["new_updates"][0]["sources"][0]["url"] = "javascript:alert(1)"
        with self.assertRaises(cu.Fail):
            cu.clean(raw, TODAY)
        raw = good_raw()
        raw["new_updates"][0]["date"] = "2026-12-01"                  # in the future
        with self.assertRaises(cu.Fail):
            cu.clean(raw, TODAY)
        raw = good_raw()
        raw["new_updates"][0]["sources"] = []
        with self.assertRaises(cu.Fail):
            cu.clean(raw, TODAY)

    def test_html_is_stripped(self):
        raw = good_raw()
        raw["new_updates"][0]["text_en"] = "Hello <script>x</script> world"
        _, updates, _ = cu.clean(raw, TODAY)
        self.assertNotIn("<", updates[0]["text"]["en"])

    def test_mandate_needs_dates(self):
        raw = good_raw()
        raw.update(stage="mandate", mandate_holder="netanyahu", mandate_given="2026-10-28", mandate_deadline="2026-11-25")
        state, _, _ = cu.clean(raw, TODAY)
        self.assertEqual(state["mandate"]["holder"], "netanyahu")
        raw["mandate_given"] = ""
        with self.assertRaises(cu.Fail):
            cu.clean(raw, TODAY)

    def test_merge_without_calling_claude(self):
        with open(os.path.join(HERE, "..", "index.html"), encoding="utf-8") as f:
            page = f.read()
        cu.call_claude = lambda system, user: (good_raw(), {"input": 1000, "output": 100, "searches": 2, "calls": 1})
        out, lines = cu.update_coalition(page, None, [], TODAY)
        data = us.read_block(out, "coalition-data")
        self.assertEqual(data["stage"], "consultations")
        self.assertEqual(data["updates"][0]["title"]["en"], "Talks begin")
        self.assertTrue(any("Claude updated" in l for l in lines))
        # Running again with the same answer adds nothing new
        out2, _ = cu.update_coalition(out, None, [], TODAY)
        self.assertEqual(len(us.read_block(out2, "coalition-data")["updates"]), len(data["updates"]))

    def test_tool_schema_is_strict_ready(self):
        def walk(node):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for v in node["properties"].values():
                    walk(v)
            if node.get("type") == "array":
                walk(node["items"])
        walk(cu.TOOL["input_schema"])


if __name__ == "__main__":
    unittest.main()
