#!/usr/bin/env python3
"""
Picks service: weekly report + twice-daily change alerts, sent to Telegram.

  python picks.py weekly        # full 7-day report (Mondays)
  python picks.py daily         # only sends a message if something changed
  python picks.py backtest      # test the corners/cards model on last season
  python picks.py telegram-id   # setup helper: find your Telegram chat id
  python picks.py telegram-test # setup helper: send a test message

You place bets yourself. Nothing here bets for you.
Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, ODDS_API_KEY (optional, for full-week fixtures + NHL)
"""
import csv
import difflib
import html
import io
import json
import os
import sqlite3
import sys
import unicodedata
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

import ev
import soccer_model as sm

try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo(os.getenv("LOCAL_TZ", "America/Toronto"))
    UK = ZoneInfo("Europe/London")
except Exception:  # very old Python / no tz database
    LOCAL = UK = timezone.utc

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(HERE, "state.json")
EV_DB = os.path.join(HERE, "bets.db")
MODEL_MARGIN = float(os.getenv("MODEL_MARGIN", "0.08"))   # cushion for stats-model picks
MARKET_EDGE = float(os.getenv("MARKET_EDGE", "0.02"))     # min edge vs exchange for main markets
CHANGE_PTS = float(os.getenv("CHANGE_PTS", "0.05"))       # alert if a probability moves 5+ points
STAT_MIN_Z = float(os.getenv("STAT_MIN_Z", "0.8"))        # how unusual a game must be for a stat pick
STAT_MIN_P = float(os.getenv("STAT_MIN_P", "0.60"))
STAT_MAX_P = float(os.getenv("STAT_MAX_P", "0.82"))
TOP_VALUE = int(os.getenv("TOP_VALUE", "8"))              # max price-value picks per message
TOP_STAT = int(os.getenv("TOP_STAT", "5"))                # max stats-model picks per message
PARLAY_LEGS = int(os.getenv("PARLAY_LEGS", "3"))
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
LEAGUES = [x for x in os.getenv("LEAGUES", ",".join(sm.LEAGUES)).split(",") if x in sm.LEAGUES]

ALIASES = {  # The Odds API name -> football-data.co.uk name (fuzzy matching covers the rest)
    "Manchester United": "Man United", "Manchester City": "Man City", "Tottenham Hotspur": "Tottenham",
    "Wolverhampton Wanderers": "Wolves", "Nottingham Forest": "Nott'm Forest", "Newcastle United": "Newcastle",
    "West Ham United": "West Ham", "Brighton and Hove Albion": "Brighton", "Leeds United": "Leeds",
    "Sheffield United": "Sheffield United", "AFC Bournemouth": "Bournemouth",
    "Atlético Madrid": "Ath Madrid", "Atletico Madrid": "Ath Madrid", "Athletic Bilbao": "Ath Bilbao",
    "Real Betis": "Betis", "Celta Vigo": "Celta", "Rayo Vallecano": "Vallecano", "Real Sociedad": "Sociedad",
    "Espanyol": "Espanol", "Alavés": "Alaves", "CA Osasuna": "Osasuna", "Real Oviedo": "Oviedo",
    "Inter Milan": "Inter", "AC Milan": "Milan", "AS Roma": "Roma", "Hellas Verona": "Verona",
    "Bayern Munich": "Bayern Munich", "Borussia Dortmund": "Dortmund", "Borussia Monchengladbach": "M'gladbach",
    "Bayer Leverkusen": "Leverkusen", "Eintracht Frankfurt": "Ein Frankfurt", "VfB Stuttgart": "Stuttgart",
    "1. FC Köln": "FC Koln", "FC St. Pauli": "St Pauli", "TSG Hoffenheim": "Hoffenheim",
    "1. FSV Mainz 05": "Mainz", "FSV Mainz 05": "Mainz", "FC Augsburg": "Augsburg", "VfL Wolfsburg": "Wolfsburg",
    "SC Freiburg": "Freiburg", "1. FC Heidenheim": "Heidenheim", "Hamburger SV": "Hamburg",
    "Paris Saint Germain": "Paris SG", "Saint Etienne": "St Etienne", "Olympique Lyonnais": "Lyon",
    "Marseille": "Marseille", "RC Lens": "Lens", "Stade Rennais": "Rennes", "Stade Brestois 29": "Brest",
    "AS Monaco": "Monaco", "OGC Nice": "Nice", "RC Strasbourg": "Strasbourg", "Paris FC": "Paris FC",
}


# ---------------- helpers ----------------
def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ev-pilot/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def norm(s):
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    for junk in (" fc", "fc ", " afc", "afc ", " cf", " sc", ".", "'"):
        s = s.replace(junk, " ")
    return " ".join(s.split())


def map_team(name, known):
    if name in known:
        return name
    if ALIASES.get(name) in known:
        return ALIASES[name]
    by_norm = {norm(k): k for k in known}
    n = norm(name)
    # "Brighton & Hove Albion" -> "Brighton": one name starts with the other
    starts = [k for nk, k in by_norm.items() if nk and (n.startswith(nk + " ") or nk.startswith(n + " "))]
    if len(starts) == 1:
        return starts[0]
    hit = difflib.get_close_matches(n, list(by_norm), n=1, cutoff=0.6)
    return by_norm[hit[0]] if hit else None


def fair_probs(odds):
    """Remove the margin from a set of decimal odds. None if any are missing."""
    if not odds or any(o is None or o <= 1 for o in odds):
        return None
    inv = [1 / o for o in odds]
    return [x / sum(inv) for x in inv]


def fnum(row, key):
    try:
        v = float(row.get(key, "") or "nan")
        return v if v == v else None
    except ValueError:
        return None


def bet_if(p, margin):
    return (1 / p) * (1 + margin) if p > 0 else None


def local(dt):
    return dt.astimezone(LOCAL)


def esc(s):
    return html.escape(str(s), quote=False)


# ---------------- fixtures ----------------
def load_fixture_csv():
    """football-data fixtures.csv: next few days, same team names as the stats, plus odds & referee."""
    try:
        raw = sm.http_get(f"{sm.BASE}/fixtures.csv", max_age_hours=3)
    except Exception as e:
        print(f"  warning: fixtures.csv unavailable: {e}")
        return []
    out = []
    for row in csv.DictReader(io.StringIO(sm.decode(raw))):
        if row.get("Div") not in LEAGUES:
            continue
        d = sm.parse_date(row.get("Date", ""))
        if not d:
            continue
        try:
            hh, mm = (row.get("Time") or "15:00").split(":")
            ko = datetime(d.year, d.month, d.day, int(hh), int(mm), tzinfo=UK).astimezone(timezone.utc)
        except ValueError:
            ko = datetime(d.year, d.month, d.day, 14, 0, tzinfo=timezone.utc)
        out.append({"league": row["Div"], "kickoff": ko, "home": row["HomeTeam"].strip(),
                    "away": row["AwayTeam"].strip(), "ref": (row.get("Referee") or "").strip(), "row": row})
    return out


def load_odds_api_events(league, known, now):
    key = os.getenv("ODDS_API_KEY")
    if not key:
        return [], []
    sport = sm.LEAGUES[league][1]
    q = urllib.parse.urlencode({
        "apiKey": key, "dateFormat": "iso",
        "commenceTimeFrom": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": (now + timedelta(days=DAYS_AHEAD)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    try:  # the /events endpoint is free - it does not use API credits
        events = get_json(f"https://api.the-odds-api.com/v4/sports/{sport}/events?{q}")
    except Exception as e:
        print(f"  warning: events for {league} unavailable: {e}")
        return [], []
    out, unmatched = [], []
    for e in events:
        h, a = map_team(e["home_team"], known), map_team(e["away_team"], known)
        if not h or not a:
            unmatched.append(f"{e['home_team']} v {e['away_team']}")
            continue
        ko = datetime.strptime(e["commence_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        out.append({"league": league, "kickoff": ko, "home": h, "away": a, "ref": "", "row": None})
    return out, unmatched


def gather_fixtures(models, now):
    end = now + timedelta(days=DAYS_AHEAD)
    csv_fx = [f for f in load_fixture_csv() if now < f["kickoff"] <= end]
    fixtures = {(f["league"], f["home"], f["away"]): f for f in csv_fx}
    unmatched = []
    for lg, model in models.items():
        evs, miss = load_odds_api_events(lg, set(model.att), now)
        unmatched += miss
        for f in evs:
            fixtures.setdefault((lg, f["home"], f["away"]), f)  # csv version wins (has odds + referee)
    return sorted(fixtures.values(), key=lambda f: f["kickoff"]), unmatched


# ---------------- analysis ----------------
def main_market_value(f):
    """1X2 and over/under 2.5 goals: fair odds from the Betfair exchange vs bet365 (UK) prices."""
    row = f["row"]
    if not row:
        return None, []
    picks = []
    src = "exchange"
    fair = fair_probs([fnum(row, "BFEH"), fnum(row, "BFED"), fnum(row, "BFEA")])
    if not fair:
        fair, src = fair_probs([fnum(row, "AvgH"), fnum(row, "AvgD"), fnum(row, "AvgA")]), "avg"
    fair_ou = fair_probs([fnum(row, "BFE>2.5"), fnum(row, "BFE<2.5")]) or \
        fair_probs([fnum(row, "Avg>2.5"), fnum(row, "Avg<2.5")])
    summary = {"win": fair, "o25": fair_ou[0] if fair_ou else None, "src": src}
    if src == "exchange":
        for label, p, price in ((f"{f['home']} to win", fair[0], fnum(row, "B365H")),
                                ("Draw", fair[1], fnum(row, "B365D")),
                                (f"{f['away']} to win", fair[2], fnum(row, "B365A"))):
            if price and p * price - 1 >= MARKET_EDGE:
                picks.append((label, p, price, p * price - 1))
    if fair_ou and fnum(row, "BFE>2.5"):
        for label, p, price in (("Over 2.5 goals", fair_ou[0], fnum(row, "B365>2.5")),
                                ("Under 2.5 goals", fair_ou[1], fnum(row, "B365<2.5"))):
            if price and p * price - 1 >= MARKET_EDGE:
                picks.append((label, p, price, p * price - 1))
    return summary, picks


def stat_candidates(model, a):
    """
    Corners/cards picks from the stats model. A match only qualifies when it is clearly
    different from a normal game (that's where team/referee stats add information the
    bookmaker's standard line may miss). We then choose the side and line closest to ~68%.
    """
    if a["thin"]:
        return []
    out = []
    specs = (("Total corners", "corners", model.H_c + model.A_c, model.phi_c),
             (f"{a['home']} corners", "home_corners", model.H_c, model.phi_tc),
             (f"{a['away']} corners", "away_corners", model.A_c, model.phi_tc),
             ("Total cards", "cards", model.H_k + model.A_k, model.phi_k))
    for name, ekey, base, phi in specs:
        ln = a["lines"][name]
        mu = ln["mu"]
        z = (mu - base) / ((phi * base) ** 0.5)
        if abs(z) < STAT_MIN_Z:
            continue
        side = "Over" if z > 0 else "Under"
        options = [(l, p if side == "Over" else 1 - p) for l, p in ln["over"].items()]
        options = [o for o in options if STAT_MIN_P <= o[1] <= STAT_MAX_P]
        if not options:
            continue
        line, p = min(options, key=lambda o: abs(o[1] - 0.68))
        if name.startswith("Total"):
            lab = f"{side} {line:g} {name.split()[1]}"
        else:
            lab = f"{name.rsplit(' ', 1)[0]} {side} {line:g} corners"
        out.append({"kind": "stat", "z": abs(z), "p": p, "label": lab, "mu": mu})
    return out


def analyse(models, fixtures):
    out = []
    for f in fixtures:
        model = models[f["league"]]
        mk = model.markets(f["home"], f["away"], f["ref"])
        summary, value = main_market_value(f)
        thin = [t for t in (f["home"], f["away"]) if model.games.get(t, 0) < 5]
        a = {
            "key": f"{f['league']}|{f['kickoff'].date()}|{f['home']}|{f['away']}",
            "league": f["league"], "kickoff": f["kickoff"], "home": f["home"], "away": f["away"],
            "ref": f["ref"], "ref_factor": mk["expect"]["ref_factor"], "lines": mk["lines"],
            "main": summary, "value": value, "thin": thin,
        }
        a["stat"] = stat_candidates(model, a)
        out.append(a)
    return out


# ---------------- best picks ----------------
SPORT_ICON = {"icehockey_nhl": "🏒", "americanfootball_nfl": "🏈", "basketball_nba": "🏀", "baseball_mlb": "⚾",
              "americanfootball_ncaaf": "🏈", "basketball_ncaab": "🏀"}


def isoz(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def sharp_value_picks(now):
    """Value prices found by ev.py (every sport/market it scans) for games in the coming days."""
    if not os.path.exists(EV_DB):
        return []
    con = sqlite3.connect(EV_DB)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT * FROM bets WHERE result IS NULL AND commence_time > ? AND commence_time <= ?",
                           (isoz(now), isoz(now + timedelta(days=DAYS_AHEAD)))).fetchall()
    except sqlite3.Error:
        return []
    out = []
    for r in rows:
        p = r["close_fair_prob"] or r["fair_prob"]  # latest sharp view of the chance
        edge = p * r["odds"] - 1
        if edge < MARKET_EDGE / 2:
            continue  # the line moved since we found it; value is gone
        sport = r["sport"]
        if r["market"] == "h2h":
            lab = "Draw" if r["pick"] == "Draw" else f"{r['pick']} to win"
        elif r["market"] == "totals":
            lab = f"{r['pick']} total"
        else:
            lab = r["pick"]
        out.append({
            "id": f"ev|{r['event_id']}|{r['market']}|{r['pick']}", "event": r["event_id"], "kind": "value",
            "icon": SPORT_ICON.get(sport, "⚽" if sport.startswith("soccer") else "🎯"),
            "sport": ev.SPORT_NAMES.get(sport, sport), "game": f"{r['away_team']} @ {r['home_team']}",
            "kickoff": datetime.strptime(r["commence_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc),
            "label": lab, "p": p, "bet_if": bet_if(p, MARKET_EDGE),
            "seen": f"{r['odds']:.2f} at {r['book']}", "score": edge,
        })
    return out


def soccer_picks(analysis):
    out = []
    for a in analysis:
        base = {"event": a["key"], "icon": "⚽", "sport": sm.LEAGUES[a["league"]][0],
                "game": f"{a['home']} – {a['away']}", "kickoff": a["kickoff"]}
        for lab, p, price, edge in a["value"]:
            out.append({**base, "id": f"fx|{a['key']}|{lab}", "kind": "value", "label": lab, "p": p,
                        "bet_if": bet_if(p, MARKET_EDGE), "seen": f"{price:.2f} at bet365 (UK)", "score": edge})
        for s in a["stat"]:
            extra = f"ref {a['ref']}" if a["ref"] and "cards" in s["label"] else ""
            out.append({**base, "id": f"st|{a['key']}|{s['label']}", "kind": "stat", "label": s["label"],
                        "p": s["p"], "bet_if": bet_if(s["p"], MODEL_MARGIN), "seen": None, "score": s["z"],
                        "note": f"expected ≈{s['mu']:.1f}" + (f", {extra}" if extra else "")})
    return out


def best_picks(analysis, now):
    allp = sharp_value_picks(now) + soccer_picks(analysis)
    value = sorted((p for p in allp if p["kind"] == "value"), key=lambda p: -p["score"])[:TOP_VALUE]
    stat = sorted((p for p in allp if p["kind"] == "stat"), key=lambda p: -p["score"])[:TOP_STAT]
    return value, stat


def build_parlay(value, stat):
    """Strongest legs from different games. Prefers likely outcomes so the parlay isn't a lottery ticket."""
    legs, used = [], set()
    for p in sorted(value + stat, key=lambda p: (-p["p"] * (1 + p["score"]))):
        if p["event"] in used or p["p"] < 0.55:
            continue
        legs.append(p)
        used.add(p["event"])
        if len(legs) == PARLAY_LEGS:
            break
    if len(legs) < 2:
        return None
    prob, min_odds = 1.0, 1.0
    for l in legs:
        prob *= l["p"]
        min_odds *= l["bet_if"]
    return {"legs": legs, "p": prob, "min_odds": min_odds}


def pick_line(p, prefix=""):
    ko = local(p["kickoff"])
    head = f"{prefix}{p['icon']} {ko:%a %-I:%M%p} · {esc(p['game'])} <i>({esc(p['sport'])})</i>"
    body = f"   <b>{esc(p['label'])}</b> — {p['p'] * 100:.0f}% · bet if ≥ <b>{p['bet_if']:.2f}</b>"
    if p["seen"]:
        body += f"\n   seen {esc(p['seen'])} (+{p['score'] * 100:.1f}% edge)"
    else:
        body += f"\n   📊 stats model, {esc(p.get('note', ''))}"
    return head + "\n" + body


def parlay_text(par):
    if not par:
        return ""
    n = len(par["legs"])
    L = [f"🎯 <b>Parlay idea</b> ({n} legs, different games)"]
    for i, l in enumerate(par["legs"], 1):
        L.append(f" {i}. {esc(l['label'])} — {esc(l['game'])} — {l['p'] * 100:.0f}%")
    L.append(f" All {n} hit: <b>{par['p'] * 100:.0f}%</b> → worth it only if the parlay pays ≥ "
             f"<b>{par['min_odds']:.2f}</b>")
    L.append(" <i>Parlays lose more often than singles. Keep the stake small.</i>")
    return "\n".join(L)


def best_message(title, value, stat, par):
    L = [title, "<i>Bet only if bet365's price is at or above the 'bet if' number. You place every bet.</i>"]
    if value:
        L.append("\n💰 <b>Price beats the sharp market</b>")
        L += [pick_line(p) for p in value]
    if stat:
        L.append("\n📊 <b>Strongest stats picks</b>")
        L += [pick_line(p) for p in stat]
    if par:
        L.append("\n" + parlay_text(par))
    if not value and not stat:
        L.append("\nNothing passes the filters right now. That's normal — no bet beats a bad bet.")
    return "\n".join(L)


# ---------------- formatting ----------------
CHEAT = ("<b>How to use:</b> a % is the chance it happens. Bet a side only if bet365's odds are at least:\n"
         + "  ".join(f"{p}%→{bet_if(p / 100, MODEL_MARGIN):.2f}" for p in (35, 40, 45, 50, 55, 60, 65, 70, 75))
         + "\nUnder = 100% minus Over. 💰 = bet365 price already beats the sharp market.")


def match_block(a):
    ko = local(a["kickoff"])
    L = [f"<b>{ko:%a %-I:%M%p}  {esc(a['home'])} – {esc(a['away'])}</b>"]
    m = a["main"]
    if m and m["win"]:
        w = m["win"]
        s = f"   {esc(a['home'])} {w[0] * 100:.0f}% · Draw {w[1] * 100:.0f}% · {esc(a['away'])} {w[2] * 100:.0f}%"
        if m["o25"]:
            s += f" · Goals O2.5 {m['o25'] * 100:.0f}%"
        L.append(s)
    for name, emoji in (("Total corners", "⛳"), ("Total cards", "🟨")):
        ln = a["lines"][name]
        parts = " · ".join(f"O{l:g} {p * 100:.0f}%" for l, p in sorted(ln["over"].items()))
        L.append(f"   {emoji} {name.split()[1].title()} (≈{ln['mu']:.1f}): {parts}")
    if a["ref"]:
        tag = "strict" if a["ref_factor"] > 1.1 else "lenient" if a["ref_factor"] < 0.9 else "average"
        L[-1] += f"  ref {esc(a['ref'])} ({tag})"
    for label, p, price, edge in a["value"]:
        L.append(f"   💰 <b>{esc(label)}</b> @ {price:.2f} — fair {p * 100:.0f}%, edge +{edge * 100:.1f}%")
    if a["thin"]:
        L.append(f"   ⚠️ little data on {esc(', '.join(a['thin']))} — trust less")
    return "\n".join(L)


def full_report_messages(analysis, unmatched, model_check):
    msgs = []
    if model_check:
        msgs.append(f"<i>{esc(model_check)}</i>")
    msgs.append(CHEAT)
    for lg in LEAGUES:
        games = [a for a in analysis if a["league"] == lg]
        if games:
            msgs.append(f"⚽ <b>{sm.LEAGUES[lg][0]}</b> ({len(games)} games)\n\n" +
                        "\n\n".join(match_block(a) for a in games))
    if unmatched:
        msgs.append("Couldn't match these team names (tell Claude to add them):\n" + esc("\n".join(unmatched)))
    return msgs


# ---------------- state & telegram ----------------
def load_state():
    try:
        with open(STATE_PATH) as f:
            st = json.load(f)
        return st if "sent" in st else {"sent": {}, "parlay": []}
    except (OSError, ValueError):
        return {"sent": {}, "parlay": []}


def save_state(value, stat, par):
    st = {"sent": {p["id"]: {"p": round(p["p"], 3), "label": p["label"], "game": p["game"],
                             "kickoff": isoz(p["kickoff"])} for p in value + stat},
          "parlay": [l["id"] for l in par["legs"]] if par else []}
    with open(STATE_PATH, "w") as f:
        json.dump(st, f, indent=1, sort_keys=True)


def telegram(texts):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    for text in texts:
        chunks, cur = [], ""
        for line in text.split("\n"):
            if len(cur) + len(line) > 3800:
                chunks.append(cur)
                cur = ""
            cur += line + "\n"
        chunks.append(cur)
        for c in chunks:
            if not (token and chat):
                print(c)
                continue
            data = urllib.parse.urlencode({"chat_id": chat, "text": c, "parse_mode": "HTML",
                                           "disable_web_page_preview": "true"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", data, timeout=30)
    if not (token and chat):
        print("(Telegram not configured - printed above instead)")


# ---------------- commands ----------------
def build(now):
    today = now.date()
    seasons = [sm.previous_season(sm.season_code(today)), sm.season_code(today)]
    models = {}
    for lg in LEAGUES:
        data = sm.load_league(lg, seasons)
        if data:
            models[lg] = sm.LeagueModel(lg, data, today + timedelta(days=1))
    fixtures, unmatched = gather_fixtures(models, now)
    fixtures = [f for f in fixtures if f["league"] in models]
    return analyse(models, fixtures), unmatched


def model_check_line():
    try:
        with open(os.path.join(HERE, "backtest.json")) as f:
            b = json.load(f)
        return (f"Model check (last season, {b['n']} games): corners {b['corners_gain']:+.1f}% and cards "
                f"{b['cards_gain']:+.1f}% more accurate than league averages.")
    except (OSError, ValueError, KeyError):
        return ""


def write_report_file(best, msgs):
    import re
    text = "\n\n---\n\n".join([best] + msgs)
    with open(os.path.join(HERE, "weekly_report.md"), "w") as f:
        f.write(re.sub(r"</?[bi]>", "**", text))


def cmd_weekly():
    now = datetime.now(timezone.utc)
    analysis, unmatched = build(now)
    value, stat = best_picks(analysis, now)
    par = build_parlay(value, stat)
    title = f"📅 <b>Best picks this week</b> — {local(now):%a %b %-d}"
    check = model_check_line()
    best = best_message(title, value, stat, par) + (f"\n\n<i>{esc(check)}</i>" if check else "")
    telegram([best + "\n\nFull game-by-game breakdown: weekly_report.md in your GitHub repo."])
    write_report_file(best, full_report_messages(analysis, unmatched, check))
    save_state(value, stat, par)
    print(f"Weekly: {len(value)} value + {len(stat)} stats picks from {len(analysis)} soccer games.")


def cmd_daily():
    now = datetime.now(timezone.utc)
    old = load_state()
    analysis, _ = build(now)
    value, stat = best_picks(analysis, now)
    par = build_parlay(value, stat)
    cur = {p["id"]: p for p in value + stat}
    new = [p for i, p in cur.items() if i not in old["sent"]]
    moved = [(p, old["sent"][i]["p"]) for i, p in cur.items()
             if i in old["sent"] and abs(p["p"] - old["sent"][i]["p"]) >= CHANGE_PTS]
    gone = [s for i, s in old["sent"].items()
            if i not in cur and s["kickoff"] > isoz(now)]  # still upcoming but no longer passes
    par_changed = par and [l["id"] for l in par["legs"]] != old.get("parlay", [])

    L = []
    if new:
        L.append("🆕 <b>New picks</b>")
        L += [pick_line(p) for p in new]
    if moved:
        L.append("\n🔄 <b>Changed</b>")
        for p, was in moved:
            L.append(f"{esc(p['label'])} ({esc(p['game'])}): {was * 100:.0f}% → <b>{p['p'] * 100:.0f}%</b>, "
                     f"bet if ≥ {p['bet_if']:.2f}")
    if gone:
        L.append("\n❌ <b>No longer passes — skip if you haven't bet</b>")
        L += [f"{esc(s['label'])} ({esc(s['game'])})" for s in gone]
    if par_changed:
        L.append("\n" + parlay_text(par))
    save_state(value, stat, par)
    if L:
        part = "Morning" if local(now).hour < 12 else "Evening"
        telegram([f"🔔 <b>{part} update</b>\n\n" + "\n".join(L).lstrip()])
        print(f"Sent update: {len(new)} new, {len(moved)} changed, {len(gone)} dropped.")
    else:
        print("No changes - nothing sent.")


def cmd_backtest():
    today = date.today()
    test = sm.previous_season(sm.season_code(today))
    tot = {"n": 0, "brier_c": 0, "brier_c0": 0, "brier_k": 0, "brier_k0": 0}
    calib = {}
    rows = []
    for lg in LEAGUES:
        r = sm.backtest(lg, test)
        if not r:
            continue
        for k in tot:
            tot[k] += r[k]
        for b, (c, h, p) in r["calib"].items():
            cc = calib.setdefault(b, [0, 0, 0])
            cc[0] += c; cc[1] += h; cc[2] += p
        n = r["n"]
        rows.append(f"| {sm.LEAGUES[lg][0]} | {n} | {(1 - r['brier_c'] / r['brier_c0']) * 100:+.1f}% | "
                    f"{(1 - r['brier_k'] / r['brier_k0']) * 100:+.1f}% |")
    if not tot["n"]:
        sys.exit("Backtest: no data downloaded.")
    gc = (1 - tot["brier_c"] / tot["brier_c0"]) * 100
    gk = (1 - tot["brier_k"] / tot["brier_k0"]) * 100
    with open(os.path.join(HERE, "backtest.json"), "w") as f:
        json.dump({"season": test, "n": tot["n"], "corners_gain": gc, "cards_gain": gk}, f)
    md = [f"# Model backtest — season 20{test[:2]}/{test[2:]}", "",
          "Each game was predicted using only games played before it.",
          "'Gain' = how much more accurate than just using the league average (Brier score). "
          "Positive = the team/referee stats add real information.", "",
          "| League | Games | Corners gain | Cards gain |", "|---|---|---|---|", *rows,
          f"| **All** | {tot['n']} | **{gc:+.1f}%** | **{gk:+.1f}%** |", "",
          "## Calibration (when it said X%, how often did it happen?)",
          "| Said | Games | Happened |", "|---|---|---|"]
    for b in sorted(calib):
        c, h, p = calib[b]
        md.append(f"| {p / c * 100:.0f}% | {c} | {h / c * 100:.0f}% |")
    md += ["", "Good calibration = the two % columns are close. Beating the league average does NOT "
           "prove you beat bet365 — only tracking real prices over time can show that."]
    with open(os.path.join(HERE, "backtest.md"), "w") as f:
        f.write("\n".join(md) + "\n")
    print("\n".join(md))
    telegram([f"🧪 <b>Model check</b> (20{test[:2]}/{test[2:]} season, {tot['n']} games)\n"
              f"Corners: {gc:+.1f}% more accurate than league average\n"
              f"Cards: {gk:+.1f}% more accurate than league average\n"
              "Full table in backtest.md."])


def cmd_telegram_id():
    token = os.getenv("TELEGRAM_BOT_TOKEN") or sys.exit("Set TELEGRAM_BOT_TOKEN first.")
    ups = get_json(f"https://api.telegram.org/bot{token}/getUpdates").get("result", [])
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in ups if "message" in u}
    if not chats:
        sys.exit("No messages yet. Open your bot in Telegram, press Start, send 'hi', then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID = {cid}   ({name})")


def cmd_telegram_test():
    telegram(["✅ Picks bot connected. Weekly report Mondays, updates at 8am and 6pm when something changes."])


if __name__ == "__main__":
    cmds = {"weekly": cmd_weekly, "daily": cmd_daily, "backtest": cmd_backtest,
            "telegram-id": cmd_telegram_id, "telegram-test": cmd_telegram_test}
    if len(sys.argv) != 2 or sys.argv[1] not in cmds:
        sys.exit(__doc__)
    cmds[sys.argv[1]]()
