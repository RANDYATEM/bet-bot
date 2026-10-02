#!/usr/bin/env python3
"""
Value finder - compares sportsbook prices with the sharp (Pinnacle) line across
sports and markets, and logs paper bets so we can measure the edge.

  python ev.py scan     # find +EV prices, log paper bets, refresh closing lines
  python ev.py settle   # grade finished games
  python ev.py report   # scorecard -> report.md

No real money is ever placed. Standard library only.
Env: ODDS_API_KEY (required), SPORTS, MARKETS (see README for the paid-plan setup).
"""
import json
import math
import os
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ---------- settings ----------
# Free plan (500 credits/month): NHL moneyline only. $30 plan: set SPORTS/MARKETS to the full lists (README).
SPORTS = (os.getenv("SPORTS") or "icehockey_nhl").split(",")
MARKETS = (os.getenv("MARKETS") or "h2h").split(",")
REGIONS = os.getenv("REGIONS") or "us,eu"  # eu = Pinnacle. Cost per scan = sports x markets x regions
SHARP_BOOK = os.getenv("SHARP_BOOK", "pinnacle")
BET_BOOKS = (os.getenv("BET_BOOKS") or
             "draftkings,fanduel,betmgm,williamhill_us,betrivers,bet365,espnbet,fanatics").split(",")
MIN_EV = float(os.getenv("MIN_EV", "0.02"))
MIN_ODDS = float(os.getenv("MIN_ODDS", "1.40"))
MAX_ODDS = float(os.getenv("MAX_ODDS", "4.00"))
STAKE = float(os.getenv("STAKE", "3"))
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "bets.db"))
API = "https://api.the-odds-api.com/v4"

SPORT_NAMES = {
    "icehockey_nhl": "NHL", "americanfootball_nfl": "NFL", "basketball_nba": "NBA", "baseball_mlb": "MLB",
    "americanfootball_ncaaf": "NCAAF", "basketball_ncaab": "NCAAB",
    "soccer_epl": "Premier League", "soccer_spain_la_liga": "La Liga", "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga", "soccer_france_ligue_one": "Ligue 1", "soccer_usa_mls": "MLS",
}


# ---------- math ----------
def no_vig_probs(prices):
    implied = [1.0 / p for p in prices]
    total = sum(implied)
    return [x / total for x in implied]


def expected_value(fair_prob, decimal_odds):
    return fair_prob * decimal_odds - 1.0


def clv(bet_odds, closing_fair_prob):
    return bet_odds * closing_fair_prob - 1.0


# ---------- storage ----------
SCHEMA = """
CREATE TABLE IF NOT EXISTS bets (
    id INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL,
    sport TEXT NOT NULL,
    commence_time TEXT NOT NULL,
    home_team TEXT NOT NULL,
    away_team TEXT NOT NULL,
    market TEXT NOT NULL DEFAULT 'h2h',
    pick TEXT NOT NULL,          -- label, e.g. 'Leafs', 'Over 6.5', 'Leafs -1.5'
    sel_name TEXT,               -- team / Over / Under / Draw
    point REAL,                  -- line for spreads & totals
    book TEXT NOT NULL,
    odds REAL NOT NULL,
    fair_prob REAL NOT NULL,
    ev REAL NOT NULL,
    stake REAL NOT NULL,
    placed_at TEXT NOT NULL,
    close_fair_prob REAL,
    close_seen_at TEXT,
    result TEXT,
    pnl REAL,
    settled_at TEXT,
    UNIQUE(event_id, market, pick)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY,
    kind TEXT, at TEXT, events INTEGER, new_bets INTEGER,
    credits_remaining TEXT, note TEXT
);
"""


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def api_get(path, **params):
    key = os.getenv("ODDS_API_KEY")
    if not key:
        sys.exit("ODDS_API_KEY is not set. Get a free key at https://the-odds-api.com")
    params["apiKey"] = key
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        remaining = r.headers.get("x-requests-remaining", "?")
        return json.loads(r.read().decode()), remaining


# ---------- core logic (pure, testable) ----------
def label(market, name, point):
    if market == "totals":
        return f"{name} {point:g}"
    if market == "spreads":
        return f"{name} {point:+g}"
    return name


def market_groups(event, book):
    """
    Split one bookmaker's markets into complete groups of opposing outcomes:
      h2h:     one group (2 or 3 outcomes incl. Draw)
      totals:  one group per line (Over x / Under x)
      spreads: one group per line (Team A -x / Team B +x), keyed by the home team's line
    Returns {(market, group_key): {label: (price, sel_name, point)}}
    """
    out = {}
    for m in book.get("markets", []):
        mk = m["key"]
        for o in m.get("outcomes", []):
            pt = o.get("point")
            if mk == "h2h":
                gk = None
            elif mk == "totals":
                gk = pt
            elif mk == "spreads":
                gk = pt if o["name"] == event["home_team"] else (-pt if pt is not None else None)
            else:
                continue
            out.setdefault((mk, gk), {})[label(mk, o["name"], pt)] = (o["price"], o["name"], pt)
    return out


def find_value(events, now):
    bets, closes = [], {}
    for ev in events:
        if parse_time(ev["commence_time"]) <= now:
            continue  # never use in-play prices
        books = {b["key"]: b for b in ev.get("bookmakers", [])}
        if SHARP_BOOK not in books:
            continue
        sharp = market_groups(ev, books[SHARP_BOOK])
        soft = {k: market_groups(ev, books[k]) for k in BET_BOOKS if k in books}
        for (mk, gk), sels in sharp.items():
            expected = 3 if (mk == "h2h" and any(s[1] == "Draw" for s in sels.values())) else 2
            if len(sels) != expected:
                continue
            labels = list(sels)
            fair = dict(zip(labels, no_vig_probs([sels[l][0] for l in labels])))
            for lab, p in fair.items():
                closes[(ev["id"], mk, lab)] = p
            for lab in labels:
                best = None
                for bk, groups in soft.items():
                    s = groups.get((mk, gk), {}).get(lab)
                    if s and (best is None or s[0] > best[1]):
                        best = (bk, s[0])
                if not best:
                    continue
                book, price = best
                e = expected_value(fair[lab], price)
                if e >= MIN_EV and MIN_ODDS <= price <= MAX_ODDS:
                    bets.append(dict(
                        event_id=ev["id"], sport=ev.get("sport_key", ""), commence_time=ev["commence_time"],
                        home_team=ev["home_team"], away_team=ev["away_team"], market=mk, pick=lab,
                        sel_name=sels[lab][1], point=sels[lab][2], book=book, odds=price,
                        fair_prob=fair[lab], ev=e,
                    ))
    return bets, closes


def grade(market, sel_name, point, home, away, score_rows):
    """Returns 'win' / 'loss' / 'void' (push), or None if scores are missing."""
    try:
        s = {r["name"]: float(r["score"]) for r in score_rows}
        hs, as_ = s[home], s[away]
    except (TypeError, ValueError, KeyError):
        return None
    if market == "h2h":
        if sel_name == "Draw":
            return "win" if hs == as_ else "loss"
        if hs == as_:
            return "void"  # 2-way market that ended level (e.g. NFL tie)
        winner = home if hs > as_ else away
        return "win" if sel_name == winner else "loss"
    if market == "totals":
        total = hs + as_
        if total == point:
            return "void"
        return "win" if (total > point) == (sel_name == "Over") else "loss"
    if market == "spreads":
        mine, theirs = (hs, as_) if sel_name == home else (as_, hs)
        diff = mine + point - theirs
        return "void" if diff == 0 else ("win" if diff > 0 else "loss")
    return None


# ---------- commands ----------
def cmd_scan():
    con = db()
    now = now_utc()
    total_events, new, remaining = 0, 0, "?"
    for sport in SPORTS:
        try:
            events, remaining = api_get(f"/sports/{sport}/odds", regions=REGIONS, markets=",".join(MARKETS),
                                        oddsFormat="decimal", dateFormat="iso")
        except Exception as e:
            print(f"  {sport}: skipped ({e})")
            continue
        total_events += len(events)
        bets, closes = find_value(events, now)
        for b in bets:
            cur = con.execute(
                """INSERT OR IGNORE INTO bets (event_id, sport, commence_time, home_team, away_team, market,
                   pick, sel_name, point, book, odds, fair_prob, ev, stake, placed_at,
                   close_fair_prob, close_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (b["event_id"], b["sport"], b["commence_time"], b["home_team"], b["away_team"], b["market"],
                 b["pick"], b["sel_name"], b["point"], b["book"], b["odds"], b["fair_prob"], b["ev"], STAKE,
                 iso(now), b["fair_prob"], iso(now)))
            new += cur.rowcount
        for (eid, mk, lab), p in closes.items():
            con.execute("""UPDATE bets SET close_fair_prob=?, close_seen_at=?
                           WHERE event_id=? AND market=? AND pick=? AND result IS NULL""",
                        (p, iso(now), eid, mk, lab))
        print(f"  {SPORT_NAMES.get(sport, sport)}: {len(events)} games, {len(bets)} value prices")
    con.execute("INSERT INTO runs (kind, at, events, new_bets, credits_remaining) VALUES (?,?,?,?,?)",
                ("scan", iso(now), total_events, new, remaining))
    con.commit()
    print(f"Scanned {total_events} games, {new} new paper bets. API credits left: {remaining}")


def cmd_settle():
    con = db()
    pending = con.execute("SELECT * FROM bets WHERE result IS NULL AND commence_time < ?",
                          (iso(now_utc()),)).fetchall()
    if not pending:
        print("Nothing to settle.")
        return
    settled, remaining = 0, "?"
    for sport in sorted({b["sport"] for b in pending}):
        try:
            games, remaining = api_get(f"/sports/{sport}/scores", daysFrom=3, dateFormat="iso")
        except Exception as e:
            print(f"  {sport}: scores unavailable ({e})")
            continue
        done = {g["id"]: g for g in games if g.get("completed")}
        for b in (x for x in pending if x["sport"] == sport):
            g = done.get(b["event_id"])
            if not g:
                continue
            res = grade(b["market"], b["sel_name"] or b["pick"], b["point"], b["home_team"], b["away_team"],
                        g.get("scores") or [])
            if not res:
                continue
            pnl = {"win": b["stake"] * (b["odds"] - 1), "loss": -b["stake"], "void": 0.0}[res]
            con.execute("UPDATE bets SET result=?, pnl=?, settled_at=? WHERE id=?",
                        (res, pnl, iso(now_utc()), b["id"]))
            settled += 1
    con.execute("INSERT INTO runs (kind, at, events, new_bets, credits_remaining) VALUES (?,?,?,?,?)",
                ("settle", iso(now_utc()), len(pending), settled, remaining))
    con.commit()
    print(f"Settled {settled} bets. API credits left: {remaining}")


def build_report(con):
    rows = con.execute("SELECT * FROM bets ORDER BY placed_at").fetchall()
    started = [r for r in rows if parse_time(r["commence_time"]) <= now_utc() and r["close_fair_prob"]]
    done = [r for r in rows if r["result"] in ("win", "loss")]
    pending = [r for r in rows if r["result"] is None]
    clvs = [clv(r["odds"], r["close_fair_prob"]) for r in started]

    staked = sum(r["stake"] for r in done)
    pnl = sum(r["pnl"] for r in done)
    wins = sum(1 for r in done if r["result"] == "win")
    exp_pnl = sum(r["stake"] * r["ev"] for r in done)

    def mean_se(xs):
        if len(xs) < 2:
            return (xs[0] if xs else 0.0), float("nan")
        m = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))
        return m, sd / math.sqrt(len(xs))

    clv_m, clv_se = mean_se(clvs)
    beat = sum(1 for c in clvs if c > 0)
    if len(clvs) < 100:
        verdict = f"Too early. {len(clvs)} bets with a closing line so far - need 200+ before judging."
    elif clv_m - 2 * clv_se > 0:
        verdict = "EDGE LOOKS REAL: average CLV is positive and clearly above zero."
    elif clv_m > 0:
        verdict = "Leaning positive, not proven yet. Keep paper betting."
    else:
        verdict = "NO EDGE: not beating the closing line. Tune settings or stop."

    lines = [
        "# Value finder scorecard (paper bets only)", f"_Generated {iso(now_utc())}_", "",
        f"**Verdict:** {verdict}", "",
        "| Metric | Value |", "|---|---|",
        f"| Paper bets logged | {len(rows)} ({len(pending)} pending) |",
        f"| Settled | {len(done)} ({wins} wins) |",
        f"| Avg edge when found | {(sum(r['ev'] for r in rows) / len(rows) * 100) if rows else 0:.2f}% |",
        f"| **Avg CLV (the number that matters)** | {clv_m * 100:+.2f}% "
        f"(±{(clv_se * 200) if not math.isnan(clv_se) else 0:.2f}%) |",
        f"| Bets that beat the close | {beat}/{len(clvs)} |",
        f"| Paper staked | ${staked:,.2f} |",
        f"| Paper profit/loss | ${pnl:+,.2f} (ROI {(pnl / staked * 100) if staked else 0:+.1f}%) |",
        f"| Expected profit (from edge) | ${exp_pnl:+,.2f} |", "",
        "Profit/loss swings with luck. CLV is the honest signal: positive CLV over a few hundred bets = real edge.",
    ]
    for title, keyf in (("By sport", lambda r: SPORT_NAMES.get(r["sport"], r["sport"])),
                        ("By market", lambda r: r["market"]), ("By book", lambda r: r["book"])):
        lines += ["", f"## {title}", "| | Bets | Avg edge | Avg CLV | Paper P/L |", "|---|---|---|---|---|"]
        for k in sorted({keyf(r) for r in rows}):
            br = [r for r in rows if keyf(r) == k]
            bc = [clv(r["odds"], r["close_fair_prob"]) for r in br if r in started]
            lines.append(f"| {k} | {len(br)} | {sum(r['ev'] for r in br) / len(br) * 100:.2f}% | "
                         f"{(sum(bc) / len(bc) * 100) if bc else 0:+.2f}% | "
                         f"${sum(r['pnl'] or 0 for r in br):+.2f} |")
    lines += ["", "## Last 20 bets", "| Game | Pick | Book | Odds | Edge | Result |", "|---|---|---|---|---|---|"]
    for r in rows[-20:][::-1]:
        lines.append(f"| {r['away_team']} @ {r['home_team']} | {r['pick']} | {r['book']} | "
                     f"{r['odds']:.2f} | {r['ev'] * 100:.1f}% | {r['result'] or 'pending'} |")
    return "\n".join(lines) + "\n"


def cmd_report():
    text = build_report(db())
    with open(os.path.join(os.path.dirname(DB_PATH), "report.md"), "w") as f:
        f.write(text)
    print(text)


if __name__ == "__main__":
    cmds = {"scan": cmd_scan, "settle": cmd_settle, "report": cmd_report}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]]()
