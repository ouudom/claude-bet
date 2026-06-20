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

## Build order (each gates the next)
1. **`odds_store.py`** ✅ — CLV substrate. Snapshot lines on a schedule (run NOW; CLV
   can't be backfilled). Writes `game` + `odds_snapshot`.
2. `fetch.py` + `results.py` — schedule, box scores, final scores → settle `game`.
3. `model.py` — Elo/power-ratings + pace → fair total/spread → vig-free prob.
4. `settle.py` + `calibration.py` — CLV-first grader.
5. `gates.py` + `edge.py` — value picks + pre-lock gating (LLM qualitative read).

## Hard gate before any real money / paid signals
Positive **CLV over n ≥ 300 paper bets** — not just positive ROI.

## Run
```bash
cp .env.example .env          # add ODDS_API_KEY (free: https://the-odds-api.com)
bash scripts/pyrun.sh --setup # first run in a fresh/Linux env
bash scripts/pyrun.sh scripts/odds_store.py
```
Schedule `odds_store.py` ~3x/day to capture open→close movement.

## Data stack (all free tier)
- Odds + line movement: the-odds-api.com (regions `us,eu` — eu pulls Pinnacle anchor)
- NBA stats/schedule/box: nba_api
- Injuries/lineups: ESPN / Rotowire (Phase 1)
