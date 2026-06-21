# Soccer Adapter — Plan (Phase 0)

Second domain on the same engine (`README.md` loop: forecast → gate → CLV calibration).
**Architecture: pluggable adapter**, not a fork. NBA + soccer share the pipeline; a sport
adapter swaps the parts that differ. Scope: **club** (top-5 European leagues + UCL, shared
club rating pool) + **international** (World Cup / Euro, separate national-team pool).

## ⏰ Time-sensitive
World Cup 2026 (group stage ~June 11 – July 19 2026) is **live now**. CLV can't backfill —
point `odds_store.py` at `soccer_fifa_world_cup` and start snapshotting before kickoffs.

## What breaks vs NBA (and the fix)
| NBA assumption | soccer reality | fix |
|---|---|---|
| 2-way moneyline | 3-way 1X2 (draw ~25-30%) | `devig_n_way` in lib/odds.py |
| margin ~ Normal(sd 12) | goals ~ Poisson, low-scoring | Dixon-Coles / bivariate Poisson model |
| spreads | Asian Handicap (.25/.75 = split, half-push) | AH grade logic in settle |
| one league | many leagues, year-round | `game.league` col; ONE club pool across comps |
| home court fixed | neutral venues (WC/finals) | adapter home-adv = 0 on neutral |
| rest = B2B | fixture congestion + rotation | congestion gate; lineup feed weight up |

## Two rating pools
- **Club**: top-5 (EPL/LaLiga/SerieA/Bundesliga/Ligue1) + UCL share clubs → single pool,
  `league` is an attribute. UCL cross-league games calibrate cross-league strength.
- **International**: national teams, separate pool. Neutral-venue aware. 1X2 settles on
  90-min regulation result (ET/penalties only for advance/outright markets — skip Phase 0).

## Carries over unchanged
`pipeline/odds_store.py`, `pipeline/results.py`, `pipeline/fetch.py` (swap SPORT key, add
`league`), `lib/db.py` core tables, `pyrun.sh`, CLV-first calibration (min-n + n≥300
money gate).

## Build order (reuse vs new) — done
1. `lib/odds.py` — `devig_n_way` (3-way 1X2). ✅
2. `lib/registry.py` + `models/base.py` — adapter interface: sport keys, model, grade fn,
   gate params, home-adv/neutral. NBA + soccer implement it. ✅
3. `pipeline/fetch.py` / `odds_store.py` / `results.py` — multi-league via `--sport`. ✅
4. `models/soccer.py` — attack/defense Poisson: strengths + home adv → (λ_home,
   λ_away) → score matrix → P(1/X/2), P(O/U 2.5), AH cover. Train on historical scores.
   Separate club vs international fit (`rating`/`sport_param`, pool-keyed). ✅
5. `pipeline/settle.py` — grade_result: draw + Asian-handicap quarter-line
   (half-win/half-push). ✅
6. `pipeline/gates.py` — fixture congestion (≥3 games/7d, via `models/soccer.gate_params`),
   rotation risk, neutral venue. ✅
7. `pipeline/calibration.py` — spots = 1X2 / O-U / AH per league. ✅ (reused unchanged)

## Data stack (free)
- Odds + AH: the-odds-api soccer keys (`soccer_epl`, `soccer_spain_la_liga`,
  `soccer_italy_serie_a`, `soccer_germany_bundesliga`, `soccer_france_ligue_one`,
  `soccer_uefa_champs_league`, `soccer_fifa_world_cup`). Pinnacle anchor via `eu` region.
- Results/fixtures: football-data.org free tier, or the-odds-api `/scores`.
- **xG** (soccer's honest leading feature = NBA-efficiency analog): FBref / understat.
  Dixon-Coles priors + quality signal. Phase 1.
- Confirmed XI / injuries / rotation: bigger swing than NBA — Phase 1 feed.

## Hard gate (same as NBA)
Positive CLV over n≥300 paper bets per spot before real money / paid signals.
```
