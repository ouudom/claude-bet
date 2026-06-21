# Sports Betting Engine — NBA (Phase 0)

Sibling product to `swing-trading`. Same loop, different domain:
**periodic forecast → event-gated validation → replay-scored calibration** over
time-series (odds) + scheduled catalysts (injuries/lineups/rest).

- **League:** NBA (high volume → fast calibration).
- **Edge:** Hybrid — model generates a fair line; sharp consensus (Pinnacle) grades
  + gates it. Bet only where model AND CLV agree.
- **Product:** Signals / SaaS — sell forecasts + CLV/Brier transparency. No capital
  at risk, no betting-account compliance.

## The moat = honest calibration
Real edge isn't picks, it's **CLV (closing line value)** — beating the closing line is
the proven leading indicator of long-run profit (the `would-be R` of this domain).
Grade on CLV first, ROI second. ROI without CLV = luck.

## Engine map (trading → sports)
| swing-trading | here |
|---|---|
| instrument | league |
| weekly forecast | pre-slate model → fair prob/game |
| Trading Zone (R1) | value pick: `model_prob − vig-free implied > threshold` |
| /validate gate | pre-lock gate: lineup/injury/rest/weather/line-move |
| CB + econ calendar | injury report, lineup drop, rest/B2B |
| SL/TP, $2000 risk | Kelly / unit staking |
| trade_outcome replay | CLV + W/L + Brier + ROI replay |
| calibration.md | WORKING/DEAD by market/spot (min-n gated) |
| ohlc_store | odds_store (open→close lines) |

## Project layout
```
scripts/
  lib/        db.py (schema), odds.py (devig math), registry.py (sport_key -> model adapter)
  models/     base.py (adapter interface), nba.py (Elo), soccer.py (attack/defense Poisson)
  pipeline/   fetch, odds_store, results, edge, gates, settle, calibration — sport-agnostic I/O
```
Adding a sport = one new module in `models/` (see `models/base.py`) + register it in
`lib/registry.py`. No pipeline file changes.

## Build order (each gates the next)
1. **`pipeline/odds_store.py`** ✅ — CLV substrate. Snapshot lines on a schedule (run NOW;
   CLV can't be backfilled). Writes `game` + `odds_snapshot`.
2. `pipeline/fetch.py` + `pipeline/results.py` ✅ — schedule (free `/events`) + final
   scores (`/scores`) → settle `game`. Same event ids as odds_snapshot, so no cross-source
   id mapping.
3. `models/nba.py` ✅ — Elo power ratings → fair ML prob + spread margin/cover prob.
   Totals deferred (needs pace/efficiency = box stats, Phase 1). Trains `rating`.
4. `pipeline/settle.py` + `pipeline/calibration.py` ✅ — CLV-first grader. settle: CLV vs
   sharp close (devigged) + W/L/push + pnl + Brier → `settle`. calibration: WORKING/DEAD by
   market/spot, min-n gated, n≥300 for money. Writes `data/calibration.md`.
5. `pipeline/gates.py` + `pipeline/edge.py` ✅ — edge: value picks where
   `model_prob − vig-free implied > threshold`, best soft price, quarter-Kelly stake →
   `pick` (dispatches to the right model adapter via `lib/registry.py`). gates: rest/B2B,
   sharp line-move, stale, locked vetoes (injury/lineup = Phase 1 stub).

## Hard gate before any real money / paid signals
Positive **CLV over n ≥ 300 paper bets** — not just positive ROI.

## Run
```bash
cp .env.example .env          # add ODDS_API_KEY (free: https://the-odds-api.com)
bash scripts/pyrun.sh --setup # first run in a fresh/Linux env
bash scripts/pyrun.sh scripts/pipeline/odds_store.py
```
Schedule `odds_store.py` ~3x/day to capture open→close movement.

## Second domain: soccer ✅ (see `wiki/soccer.md`)
Same pipeline, sport-agnostic via `--sport` (the-odds-api key, e.g. `soccer_epl`,
`soccer_fifa_world_cup`). What differs: `models/soccer.py` (attack/defense Poisson
ratings, two pools — `club` shares top-5 + UCL, `intl` is national teams), 3-way 1X2
devig (`lib/odds.devig_n_way`), Asian-Handicap quarter-line settle (`half_win`/`half_loss`
in `pipeline/settle.py`), and a fixture-congestion gate (`gates.gate_congestion`, soccer-only
— NBA's `models/nba.gate_params()` returns `congestion_days=None`).
```bash
bash scripts/pyrun.sh scripts/pipeline/odds_store.py --sport soccer_fifa_world_cup
bash scripts/pyrun.sh scripts/pipeline/fetch.py --sport soccer_fifa_world_cup
bash scripts/pyrun.sh scripts/pipeline/results.py --sport soccer_fifa_world_cup
bash scripts/pyrun.sh scripts/models/soccer.py --train --pool intl --neutral
bash scripts/pyrun.sh scripts/pipeline/edge.py   # sport-dispatches automatically per game row
```

## Data stack (all free tier)
- Odds + line movement: the-odds-api.com (regions `us,eu` — eu pulls Pinnacle anchor)
- NBA stats/schedule/box: nba_api
- Injuries/lineups: ESPN / Rotowire (Phase 1)
