# Picks bot

Sends **only the best bets** to your phone on Telegram:

- **Monday 8am** — best picks for the week + a parlay idea
- **8am and 6pm daily** — only if something changed (new pick, a pick's chance moved, or a pick no longer qualifies)

**You place every bet yourself.** Nothing here touches your bet365 account or your money.

## How it decides

| Pick type | How it's found | Sports |
|---|---|---|
| 💰 **Price beats the sharp market** | Removes the margin from the sharpest bookmaker (Pinnacle, or the Betfair exchange for soccer) to get the true chance, then finds sportsbooks paying more than that. The sharp line already reflects injuries, lineups and news within minutes. | NHL, NFL, NBA, MLB, soccer — moneyline, spreads, totals |
| 📊 **Stats model** | Learns each team's corners won/conceded and cards from the last two seasons (recent games count more), plus each referee's card habits. Only picks games that are clearly unusual, where the stats add information. | Soccer corners and cards (Premier League, La Liga, Serie A, Bundesliga, Ligue 1) |
| 🎯 **Parlay idea** | The 2–3 strongest picks from *different* games, with each leg's chance and the combined chance. | Mixed |

Every pick shows a **chance** and a **"bet if ≥" number**. Only bet when bet365's odds are at or above it. Stats picks need a bigger cushion (8%) than market picks (2%), because a model can be wrong in ways the market isn't.

**Before betting corners/cards on bet365:** check how bet365 counts cards in that market (some count a red as 2, or use "booking points"). The model counts yellow = 1, red = 1.

---

## Setup — step by step (about 20 minutes)

### 1. Make your Telegram bot (5 min)
1. Install **Telegram** on your phone and sign up.
2. In Telegram, search **@BotFather** (blue check mark) → tap **Start**.
3. Send `/newbot`. Give it a name (e.g. `Randy Picks`) and a username ending in `bot` (e.g. `randy_picks_bot`).
4. BotFather replies with a **token** like `7123456789:AAH...`. Copy it somewhere safe. Don't share it.
5. Tap the link to your new bot → **Start** → send it `hi`. (The bot can only message you after you've messaged it.)

### 2. Get the odds API key (2 min)
1. Go to <https://the-odds-api.com> → **Get API Key** → **Starter (free)**.
2. The key arrives by email.

### 3. Put the code on GitHub (5 min)
1. On <https://github.com> → **New repository** → name it `picks` → choose **Private** → Create.
2. Unzip this folder on your computer. On the empty repo page click **uploading an existing file**, drag **everything inside the folder** in, including the `.github` folder, then **Commit changes**.
   *(Or from a terminal: `git init && git add . && git commit -m init && git branch -M main && git remote add origin <repo-url> && git push -u origin main`.)*
3. Repo → **Settings → Actions → General → Workflow permissions** → choose **Read and write permissions** → Save. (The bot saves its results back to the repo.)

### 4. Add your keys (3 min)
Repo → **Settings → Secrets and variables → Actions → New repository secret**. Add:

| Name | Value |
|---|---|
| `ODDS_API_KEY` | key from step 2 |
| `TELEGRAM_BOT_TOKEN` | token from step 1 |

### 5. Find your chat ID and test
1. Repo → **Actions** tab → **picks** → **Run workflow** → command: `telegram-id` → Run.
2. Open the finished run → **Run** step. Copy the number after `TELEGRAM_CHAT_ID =`.
3. Add it as a third secret: `TELEGRAM_CHAT_ID`.
4. Run workflow again with `telegram-test` → your phone should get **"✅ Picks bot connected"**.

### 6. First real runs
1. Run workflow with `scan` → collects the first market prices.
2. Run workflow with `backtest` → sends a **Model check**: how accurate the corners/cards model was on last season.
3. Run workflow with `weekly` → your first best-picks message.

From now on it runs on its own. Nothing to keep open on your computer.

---

## Upgrading to the $30 USD plan (all sports & markets)

1. On the-odds-api.com, upgrade to the **20K** plan. Your key stays the same.
2. Repo → **Settings → Secrets and variables → Actions → Variables** tab → **New repository variable**:

| Name | Value |
|---|---|
| `SPORTS` | `icehockey_nhl,americanfootball_nfl,basketball_nba,baseball_mlb,soccer_epl,soccer_spain_la_liga,soccer_italy_serie_a,soccer_germany_bundesliga,soccer_france_ligue_one` |
| `MARKETS` | `h2h,spreads,totals` |

That uses about 9,000 of the 20,000 monthly credits. Off-season sports simply return no games.

---

## Files the bot keeps updated in the repo

| File | What's in it |
|---|---|
| `weekly_report.md` | This week's best picks + every soccer game broken down |
| `report.md` | Scorecard of every price-value pick: did it beat the closing line, paper profit/loss, by sport/market/book |
| `backtest.md` | How accurate the stats model was last season |

**The honest scorecard is CLV in `report.md`.** If after 200+ picks the average CLV is positive, the price picks have a real edge. Wins and losses over a few weeks are mostly luck.

## Tuning (repository variables, optional)

| Variable | Default | Meaning |
|---|---|---|
| `MIN_EV` | `0.02` | Minimum edge for a price pick |
| `MODEL_MARGIN` | `0.08` | Cushion required on stats picks |
| `STAT_MIN_Z` | `0.8` | How unusual a game must be for a stats pick (higher = fewer, stronger) |
| `TOP_VALUE` / `TOP_STAT` | `8` / `5` | Max picks per message |
| `PARLAY_LEGS` | `3` | Legs in the parlay idea |
| `LEAGUES` | all five | e.g. `E0,SP1` for Premier League + La Liga only |

(Variables other than SPORTS/MARKETS/REGIONS also need a matching `env:` line in `.github/workflows/picks.yml` — ask Claude.)

## Known limits

- **Prices come from US/UK feeds**, not Ontario's apps. Always check the actual bet365 price against the "bet if" number.
- **Price picks can disappear fast.** A pick found at 4:45pm may be gone by 7pm; the 6pm update tells you if it no longer qualifies.
- **Corners/cards picks don't see injuries or lineups**, and there's no bet365 corner price to compare automatically.
- **Parlays multiply risk.** Three 70% legs = 34%.
- **Sportsbooks limit winning accounts.** See the chat for details.

## Tests

```
python test_ev.py
python test_picks.py
```
