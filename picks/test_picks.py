"""Offline tests for the soccer model and picks. Run: python test_picks.py"""
import math
import os
import random
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import soccer_model as sm  # noqa: E402
import picks  # noqa: E402


def fake_league(seed=1):
    """Two seasons of a league where we know each team's true corner strength."""
    rnd = random.Random(seed)

    def pois(lam):
        L, k, p = math.exp(-lam), 0, 1.0
        while True:
            p *= rnd.random()
            if p < L:
                return k
            k += 1

    teams = [f"T{i}" for i in range(20)]
    att = {t: rnd.uniform(0.7, 1.35) for t in teams}
    ms, d = [], date(2024, 8, 17)
    for _ in range(76):
        order = teams[:]
        rnd.shuffle(order)
        for i in range(10):
            h, a = order[2 * i], order[2 * i + 1]
            ms.append(sm.Match("E0", d, h, a, pois(5.4 * att[h]), pois(4.4 * att[a]), pois(2), pois(2.2), "R1"))
        d += timedelta(days=7)
    return ms, att


class Distributions(unittest.TestCase):
    def test_poisson_case(self):
        # Poisson(10): P(X > 9.5) = P(X >= 10) ~ 0.542
        self.assertAlmostEqual(sm.prob_over(9.5, 10, 1.0), 0.542, places=3)

    def test_overdispersion_widens(self):
        lo = sm.prob_over(13.5, 10, 1.0)
        hi = sm.prob_over(13.5, 10, 1.6)
        self.assertGreater(hi, lo)  # fatter tails with more dispersion

    def test_pmf_sums_to_one(self):
        self.assertAlmostEqual(sum(sm.nb_pmf(k, 9.7, 1.4) for k in range(80)), 1.0, places=6)

    def test_main_line_near_half(self):
        L = sm.main_line(10.1, 1.3, 6, 14)
        self.assertIn(L, (9.5, 10.5))


class Model(unittest.TestCase):
    def test_learns_team_strength(self):
        ms, att = fake_league()
        m = sm.LeagueModel("E0", ms, ms[-1].day + timedelta(days=1))
        strong = max(att, key=att.get)
        weak = min(att, key=att.get)
        self.assertGreater(m.att[strong], m.att[weak])
        self.assertGreater(m.expect(strong, weak)["corners"], m.expect(weak, strong)["corners"])

    def test_beats_baseline_in_backtest(self):
        ms, _ = fake_league()
        r = sm.backtest("E0", "2526", data=ms)
        self.assertLess(r["brier_c"], r["brier_c0"])

    def test_unknown_team_is_league_average(self):
        ms, _ = fake_league()
        m = sm.LeagueModel("E0", ms, ms[-1].day + timedelta(days=1))
        e = m.expect("Newly Promoted", "Also New")
        self.assertAlmostEqual(e["corners"], m.H_c + m.A_c)

    def test_season_codes(self):
        self.assertEqual(sm.season_code(date(2026, 10, 2)), "2627")
        self.assertEqual(sm.season_code(date(2027, 3, 1)), "2627")
        self.assertEqual(sm.previous_season("2627"), "2526")


class Picks(unittest.TestCase):
    def test_team_mapping(self):
        known = {"Man United", "Nott'm Forest", "Wolves", "Ath Madrid", "Bayern Munich", "Brighton", "Paris SG"}
        self.assertEqual(picks.map_team("Manchester United", known), "Man United")
        self.assertEqual(picks.map_team("Nottingham Forest", known), "Nott'm Forest")
        self.assertEqual(picks.map_team("Atlético Madrid", known), "Ath Madrid")
        self.assertEqual(picks.map_team("Bayern München", known), "Bayern Munich")
        self.assertEqual(picks.map_team("Brighton & Hove Albion", known), "Brighton")
        self.assertIsNone(picks.map_team("Totally Different FC", known))

    def test_fair_probs(self):
        p = picks.fair_probs([2.0, 4.0, 4.0])
        self.assertAlmostEqual(sum(p), 1.0)
        self.assertIsNone(picks.fair_probs([2.0, None, 4.0]))

    def test_value_detection(self):
        row = {"BFEH": "2.02", "BFED": "3.6", "BFEA": "4.1", "B365H": "2.20", "B365D": "3.4", "B365A": "3.3",
               "BFE>2.5": "1.95", "BFE<2.5": "2.05", "B365>2.5": "1.80", "B365<2.5": "2.05"}
        f = {"row": row, "home": "Arsenal", "away": "Chelsea"}
        summary, value = picks.main_market_value(f)
        labels = [v[0] for v in value]
        self.assertIn("Arsenal to win", labels)
        self.assertNotIn("Draw", labels)
        self.assertAlmostEqual(sum(summary["win"]), 1.0)

    def test_bet_if(self):
        self.assertAlmostEqual(picks.bet_if(0.5, 0.08), 2.16)


if __name__ == "__main__":
    unittest.main(verbosity=2)
