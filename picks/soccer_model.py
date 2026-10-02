"""
Corners & cards model for soccer, built on free match stats from football-data.co.uk.

For each team it learns, with recent games weighted more:
  - how many corners it wins and concedes (vs. league average, home/away adjusted)
  - how many cards it picks up and provokes from opponents
  - each referee's card tendency
Then it turns expected counts into probabilities with a negative binomial
distribution (corners and cards are "lumpier" than a plain Poisson).
"""
import csv
import io
import math
import os
import time
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime

LEAGUES = {
    # football-data code: (display name, The Odds API sport key)
    "E0": ("Premier League", "soccer_epl"),
    "SP1": ("La Liga", "soccer_spain_la_liga"),
    "I1": ("Serie A", "soccer_italy_serie_a"),
    "D1": ("Bundesliga", "soccer_germany_bundesliga"),
    "F1": ("Ligue 1", "soccer_france_ligue_one"),
}

HALF_LIFE_DAYS = float(os.getenv("HALF_LIFE_DAYS", "150"))  # a game 150 days old counts half
SHRINK_GAMES = float(os.getenv("SHRINK_GAMES", "6"))        # pull small samples toward league average
REF_SHRINK_GAMES = float(os.getenv("REF_SHRINK_GAMES", "8"))
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache")
BASE = "https://www.football-data.co.uk"


# ---------------- data ----------------
@dataclass
class Match:
    league: str
    day: date
    home: str
    away: str
    hc: int
    ac: int
    hk: int   # home cards (yellow + red)
    ak: int
    ref: str

    @property
    def corners(self):
        return self.hc + self.ac

    @property
    def cards(self):
        return self.hk + self.ak


def season_code(d):
    start = d.year if d.month >= 7 else d.year - 1
    return f"{start % 100:02d}{(start + 1) % 100:02d}"


def previous_season(code):
    a = int(code[:2])
    return f"{(a - 1) % 100:02d}{a:02d}"


def http_get(url, max_age_hours=6):
    """Download with a small on-disk cache. Old completed seasons are cached for good."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = os.path.join(CACHE_DIR, url.replace("://", "_").replace("/", "_"))
    if os.path.exists(path) and (max_age_hours is None or
                                 time.time() - os.path.getmtime(path) < max_age_hours * 3600):
        with open(path, "rb") as f:
            return f.read()
    req = urllib.request.Request(url, headers={"User-Agent": "ev-pilot/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    with open(path, "wb") as f:
        f.write(data)
    return data


def decode(raw):
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_date(s):
    for fmt in ("%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def parse_matches(text, league):
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        try:
            d = parse_date(row.get("Date", ""))
            m = Match(
                league=league, day=d, home=row["HomeTeam"].strip(), away=row["AwayTeam"].strip(),
                hc=int(row["HC"]), ac=int(row["AC"]),
                hk=int(row["HY"]) + int(row["HR"]), ak=int(row["AY"]) + int(row["AR"]),
                ref=(row.get("Referee") or "").strip(),
            )
        except (KeyError, ValueError, TypeError):
            continue  # unplayed or incomplete row
        if d:
            out.append(m)
    return out


def load_league(league, seasons):
    matches = []
    for s in seasons:
        url = f"{BASE}/mmz4281/{s}/{league}.csv"
        try:
            # completed seasons never change; current one refreshes every 6h
            raw = http_get(url, max_age_hours=6 if s == season_code(date.today()) else None)
        except Exception as e:  # season file not published yet, network blip...
            print(f"  warning: could not load {url}: {e}")
            continue
        matches += parse_matches(decode(raw), league)
    matches.sort(key=lambda m: m.day)
    return matches


# ---------------- distributions ----------------
def nb_pmf(k, mu, phi):
    """Negative binomial with mean mu and variance phi*mu (phi=1 -> Poisson)."""
    if mu <= 0:
        return 1.0 if k == 0 else 0.0
    if phi <= 1.0001:
        return math.exp(-mu + k * math.log(mu) - math.lgamma(k + 1))
    r = mu / (phi - 1)
    p = r / (r + mu)
    return math.exp(math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                    + r * math.log(p) + k * math.log(1 - p))


def prob_over(line, mu, phi):
    """P(count > line) for a half line like 9.5."""
    cdf = sum(nb_pmf(k, mu, phi) for k in range(int(math.floor(line)) + 1))
    return max(0.0, min(1.0, 1.0 - cdf))


def main_line(mu, phi, lo, hi):
    """Half line where over/under is closest to 50/50 - usually what the bookmaker posts."""
    lines = [x + 0.5 for x in range(lo, hi)]
    return min(lines, key=lambda L: abs(prob_over(L, mu, phi) - 0.5))


# ---------------- model ----------------
class LeagueModel:
    def __init__(self, league, matches, as_of, neutral=False):
        """neutral=True ignores team/referee effects (baseline for the backtest)."""
        self.league = league
        self.as_of = as_of
        self.neutral = neutral
        self.fit([m for m in matches if m.day < as_of])

    def _w(self, m):
        return 0.5 ** ((self.as_of - m.day).days / HALF_LIFE_DAYS)

    def fit(self, ms):
        self.n = len(ms)
        if not ms:
            raise ValueError(f"no data for {self.league}")
        W = sum(self._w(m) for m in ms)
        avg = lambda f: sum(self._w(m) * f(m) for m in ms) / W
        self.H_c, self.A_c = avg(lambda m: m.hc), avg(lambda m: m.ac)
        self.H_k, self.A_k = avg(lambda m: m.hk), avg(lambda m: m.ak)

        acc = defaultdict(lambda: [0.0] * 8)  # att num/den, def num/den, cfor num/den, cprov num/den
        ref = defaultdict(lambda: [0.0, 0.0])
        games = defaultdict(float)
        for m in ms:
            w = self._w(m)
            for team, f_c, c_c, avg_f, avg_c, f_k, c_k, avg_fk, avg_ck in (
                (m.home, m.hc, m.ac, self.H_c, self.A_c, m.hk, m.ak, self.H_k, self.A_k),
                (m.away, m.ac, m.hc, self.A_c, self.H_c, m.ak, m.hk, self.A_k, self.H_k),
            ):
                a = acc[team]
                a[0] += w * f_c; a[1] += w * avg_f
                a[2] += w * c_c; a[3] += w * avg_c
                a[4] += w * f_k; a[5] += w * avg_fk
                a[6] += w * c_k; a[7] += w * avg_ck
                games[team] += w
            if m.ref:
                ref[m.ref][0] += w * m.cards
                ref[m.ref][1] += w * (self.H_k + self.A_k)

        kc = SHRINK_GAMES * (self.H_c + self.A_c) / 2
        kk = SHRINK_GAMES * (self.H_k + self.A_k) / 2
        kr = REF_SHRINK_GAMES * (self.H_k + self.A_k)
        self.att, self.dfn, self.cfor, self.cprov = {}, {}, {}, {}
        for t, a in acc.items():
            self.att[t] = (a[0] + kc) / (a[1] + kc)
            self.dfn[t] = (a[2] + kc) / (a[3] + kc)
            self.cfor[t] = (a[4] + kk) / (a[5] + kk)
            self.cprov[t] = (a[6] + kk) / (a[7] + kk)
        self.ref = {r: (v[0] + kr) / (v[1] + kr) for r, v in ref.items()}
        self.games = dict(games)

        # dispersion: how much more spread out real counts are than Poisson
        def phi(obs_mu):
            num = sum(w * (y - mu) ** 2 for w, y, mu in obs_mu)
            den = sum(w * mu for w, y, mu in obs_mu)
            return max(1.0, min(3.0, num / den)) if den else 1.0

        tc, tk, team_c = [], [], []
        for m in ms:
            w = self._w(m)
            p = self.expect(m.home, m.away, m.ref)
            tc.append((w, m.corners, p["corners"]))
            tk.append((w, m.cards, p["cards"]))
            team_c.append((w, m.hc, p["home_corners"]))
            team_c.append((w, m.ac, p["away_corners"]))
        self.phi_c, self.phi_k, self.phi_tc = phi(tc), phi(tk), phi(team_c)

    def knows(self, team):
        return team in self.att

    def ref_factor(self, ref):
        if self.neutral or not ref:
            return 1.0
        return self.ref.get(ref, 1.0)

    def expect(self, home, away, ref=""):
        g = (lambda d, t: 1.0) if self.neutral else (lambda d, t: d.get(t, 1.0))
        hc = self.H_c * g(self.att, home) * g(self.dfn, away)
        ac = self.A_c * g(self.att, away) * g(self.dfn, home)
        hk = self.H_k * g(self.cfor, home) * g(self.cprov, away)
        ak = self.A_k * g(self.cfor, away) * g(self.cprov, home)
        rf = self.ref_factor(ref)
        return {"home_corners": hc, "away_corners": ac, "corners": hc + ac,
                "home_cards": hk * rf, "away_cards": ak * rf, "cards": (hk + ak) * rf,
                "ref_factor": rf}

    def markets(self, home, away, ref=""):
        """Probabilities for the bookmaker-style main lines, plus neighbouring lines."""
        e = self.expect(home, away, ref)
        out = {"expect": e, "lines": {}}
        for name, mu, phi, lo, hi in (
            ("Total corners", e["corners"], self.phi_c, 6, 14),
            (f"{home} corners", e["home_corners"], self.phi_tc, 2, 9),
            (f"{away} corners", e["away_corners"], self.phi_tc, 1, 8),
            ("Total cards", e["cards"], self.phi_k, 1, 8),
        ):
            L = main_line(mu, phi, lo, hi)
            out["lines"][name] = {
                "mu": mu, "main": L,
                "over": {l: prob_over(l, mu, phi) for l in (L - 1, L, L + 1) if l > 0},
            }
        return out


# ---------------- backtest ----------------
def backtest(league, test_season, warmup_games=80, corner_line=9.5, card_line=4.5, data=None):
    """
    Walk-forward test: predict every game of a past season using only games
    played before it, then compare with a 'league average only' baseline.
    """
    if data is None:
        data = load_league(league, [previous_season(test_season), test_season])
    test = [m for m in data if season_code(m.day) == test_season]
    if len(test) < warmup_games + 50:
        return None
    test = test[warmup_games:]
    res = {"n": 0, "brier_c": 0, "brier_c0": 0, "brier_k": 0, "brier_k0": 0,
           "calib": defaultdict(lambda: [0, 0.0, 0.0])}  # bucket -> [count, hits, sum of predicted]
    for d in sorted({m.day for m in test}):
        model = LeagueModel(league, data, d)
        base = LeagueModel(league, data, d, neutral=True)
        for m in (x for x in test if x.day == d):
            pc = prob_over(corner_line, model.expect(m.home, m.away, m.ref)["corners"], model.phi_c)
            pc0 = prob_over(corner_line, base.expect(m.home, m.away)["corners"], base.phi_c)
            pk = prob_over(card_line, model.expect(m.home, m.away, m.ref)["cards"], model.phi_k)
            pk0 = prob_over(card_line, base.expect(m.home, m.away)["cards"], base.phi_k)
            yc, yk = float(m.corners > corner_line), float(m.cards > card_line)
            res["brier_c"] += (pc - yc) ** 2; res["brier_c0"] += (pc0 - yc) ** 2
            res["brier_k"] += (pk - yk) ** 2; res["brier_k0"] += (pk0 - yk) ** 2
            for p, y in ((pc, yc), (pk, yk)):
                b = min(int(p * 10), 9)
                res["calib"][b][0] += 1
                res["calib"][b][1] += y
                res["calib"][b][2] += p
            res["n"] += 1
    return res
