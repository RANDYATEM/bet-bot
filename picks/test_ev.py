"""Offline tests for the value finder. Run: python test_ev.py"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
import ev  # noqa: E402

NOW = datetime(2026, 10, 10, 15, 0, tzinfo=timezone.utc)


def mk(key, outcomes):
    return {"key": key, "outcomes": [dict(zip(("name", "price", "point"), o)) if len(o) == 3
                                     else {"name": o[0], "price": o[1]} for o in outcomes]}


def game(gid, start, sharp_markets, soft_markets, soft_book="draftkings", home="Leafs", away="Habs"):
    return {"id": gid, "sport_key": "icehockey_nhl", "commence_time": ev.iso(start),
            "home_team": home, "away_team": away,
            "bookmakers": [{"key": "pinnacle", "markets": sharp_markets},
                           {"key": soft_book, "markets": soft_markets}]}


SOON = NOW + timedelta(hours=5)


class Math(unittest.TestCase):
    def test_no_vig(self):
        self.assertAlmostEqual(ev.no_vig_probs([1.91, 1.91])[0], 0.5)
        self.assertAlmostEqual(sum(ev.no_vig_probs([2.1, 3.4, 3.6])), 1.0)

    def test_ev_and_clv(self):
        self.assertAlmostEqual(ev.expected_value(0.5, 2.10), 0.05)
        self.assertAlmostEqual(ev.clv(2.10, 0.5), 0.05)


class FindValue(unittest.TestCase):
    def test_moneyline(self):
        g = game("g1", SOON, [mk("h2h", [("Leafs", 1.91), ("Habs", 1.91)])],
                 [mk("h2h", [("Leafs", 2.10), ("Habs", 1.75)])])
        bets, closes = ev.find_value([g], NOW)
        self.assertEqual([(b["market"], b["pick"]) for b in bets], [("h2h", "Leafs")])
        self.assertIn(("g1", "h2h", "Habs"), closes)

    def test_totals_only_same_line(self):
        sharp = [mk("totals", [("Over", 1.91, 6.5), ("Under", 1.91, 6.5)])]
        same = [mk("totals", [("Over", 2.10, 6.5), ("Under", 1.75, 6.5)])]
        other = [mk("totals", [("Over", 2.40, 5.5), ("Under", 1.55, 5.5)])]
        self.assertEqual([b["pick"] for b in ev.find_value([game("t1", SOON, sharp, same)], NOW)[0]], ["Over 6.5"])
        self.assertEqual(ev.find_value([game("t2", SOON, sharp, other)], NOW)[0], [])  # different line: skip

    def test_spreads(self):
        sharp = [mk("spreads", [("Leafs", 2.30, -1.5), ("Habs", 1.65, 1.5)])]
        soft = [mk("spreads", [("Leafs", 2.55, -1.5), ("Habs", 1.55, 1.5)])]
        bets = ev.find_value([game("s1", SOON, sharp, soft)], NOW)[0]
        self.assertEqual([b["pick"] for b in bets], ["Leafs -1.5"])
        self.assertEqual(bets[0]["point"], -1.5)

    def test_three_way_soccer(self):
        sharp = [mk("h2h", [("Arsenal", 2.0), ("Draw", 3.6), ("Chelsea", 4.2)])]
        soft = [mk("h2h", [("Arsenal", 2.25), ("Draw", 3.4), ("Chelsea", 3.8)])]
        bets = ev.find_value([game("s2", SOON, sharp, soft, home="Arsenal", away="Chelsea")], NOW)[0]
        self.assertEqual([b["pick"] for b in bets], ["Arsenal"])

    def test_ignores_started_and_unlisted_books(self):
        m = [mk("h2h", [("Leafs", 1.91), ("Habs", 1.91)])]
        soft = [mk("h2h", [("Leafs", 2.50), ("Habs", 1.60)])]
        self.assertEqual(ev.find_value([game("g", NOW - timedelta(minutes=1), m, soft)], NOW), ([], {}))
        self.assertEqual(ev.find_value([game("g", SOON, m, soft, soft_book="offshore")], NOW)[0], [])


class Grading(unittest.TestCase):
    S = [{"name": "Leafs", "score": "4"}, {"name": "Habs", "score": "2"}]

    def test_moneyline(self):
        self.assertEqual(ev.grade("h2h", "Leafs", None, "Leafs", "Habs", self.S), "win")
        self.assertEqual(ev.grade("h2h", "Habs", None, "Leafs", "Habs", self.S), "loss")
        level = [{"name": "Leafs", "score": "1"}, {"name": "Habs", "score": "1"}]
        self.assertEqual(ev.grade("h2h", "Draw", None, "Leafs", "Habs", level), "win")

    def test_totals(self):
        self.assertEqual(ev.grade("totals", "Over", 5.5, "Leafs", "Habs", self.S), "win")
        self.assertEqual(ev.grade("totals", "Under", 5.5, "Leafs", "Habs", self.S), "loss")
        self.assertEqual(ev.grade("totals", "Over", 6.0, "Leafs", "Habs", self.S), "void")

    def test_spreads(self):
        self.assertEqual(ev.grade("spreads", "Leafs", -1.5, "Leafs", "Habs", self.S), "win")
        self.assertEqual(ev.grade("spreads", "Habs", 1.5, "Leafs", "Habs", self.S), "loss")
        self.assertEqual(ev.grade("spreads", "Leafs", -2.0, "Leafs", "Habs", self.S), "void")

    def test_missing(self):
        self.assertIsNone(ev.grade("h2h", "Leafs", None, "Leafs", "Habs", None))


class Report(unittest.TestCase):
    def test_report(self):
        con = ev.db()
        self.assertIn("Too early", ev.build_report(con))
        past = ev.iso(NOW - timedelta(days=1))
        con.execute("""INSERT INTO bets (event_id, sport, commence_time, home_team, away_team, market, pick,
            book, odds, fair_prob, ev, stake, placed_at, close_fair_prob, result, pnl)
            VALUES ('x','icehockey_nhl',?,'Leafs','Habs','h2h','Leafs','bet365',2.1,0.5,0.05,3,?,0.52,'win',3.3)""",
                    (past, past))
        out = ev.build_report(con)
        self.assertIn("bet365", out)
        self.assertIn("NHL", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
