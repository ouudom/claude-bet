"""models/soccer.py — fair-line engine for soccer. Attack/defense Poisson ratings.

Sibling of models/nba.py for the soccer adapter (wiki/soccer.md). Soccer goals don't
fit NBA's Normal-margin Elo: scoring is low and discrete, draws are common, and markets
are 3-way (1X2) + Asian Handicap, not 2-way ML + point spread. This module trains a
per-team attack/defense Poisson model (the standard football rating shape — full
Dixon-Coles also fits a low-score correlation term `tau`; we skip that correction for
Phase 0 honesty: every number here is plain independent-Poisson, traceable like nba.py).

Two separate rating pools (wiki/soccer.md):
  club — top-5 European leagues + UCL share one pool (`league` is just an attribute)
  intl — national teams, separate pool, neutral-venue aware (home_adv=1.0)

Fitting: iterative proportional fitting (IPF) on goals scored/conceded — a closed-form,
scipy-free fixed point that converges to the same Poisson-regression MLE as attack_i,
defense_i, home_adv multiplicative factors. No external numeric deps, matching this
repo's "pure, dependency-free" stance (lib/odds.py).

Usage:
    bash scripts/pyrun.sh scripts/models/soccer.py --train --pool club
    bash scripts/pyrun.sh scripts/models/soccer.py --train --pool intl --neutral
    bash scripts/pyrun.sh scripts/models/soccer.py --predict "England" "France" --pool intl
"""
import argparse
import datetime as dt
import json
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db  # noqa: E402

# generic label for rating/sport_param rows — `pool` already disambiguates club vs
# intl, and club spans 6 separate the-odds-api sport keys sharing one pool.
SPORT = "soccer"

# the-odds-api sport keys per pool (wiki/soccer.md "Two rating pools")
CLUB_SPORTS = (
    "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
    "soccer_germany_bundesliga", "soccer_france_ligue_one", "soccer_uefa_champs_league",
)
INTL_SPORTS = ("soccer_fifa_world_cup", "soccer_uefa_euro", "soccer_uefa_european_championship")
sport_keys = CLUB_SPORTS + INTL_SPORTS

MAX_GOALS = 10        # Poisson grid cutoff (P(goals>10) is negligible)
IPF_ITERS = 200


def pool_sports(pool):
    return CLUB_SPORTS if pool == "club" else INTL_SPORTS


def pool_for(sport_key):
    return "club" if sport_key in CLUB_SPORTS else "intl"


def gate_params(sport_key):
    return {"pool": pool_for(sport_key), "congestion_days": 7, "markets": ["h2h", "spreads"]}


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def train(conn, pool):
    """IPF-fit attack/defense/home_adv from settled games in this pool. Persists + returns dict."""
    sports = pool_sports(pool)
    placeholders = ",".join("?" * len(sports))
    rows = conn.execute(
        f"""SELECT home, away, home_score, away_score FROM game
             WHERE status='final' AND home_score IS NOT NULL AND away_score IS NOT NULL
               AND sport IN ({placeholders})""",
        sports,
    ).fetchall()

    teams = sorted({g["home"] for g in rows} | {g["away"] for g in rows})
    if not teams:
        print(f"[models.soccer] pool={pool}: no settled games yet, nothing to train")
        return {}

    attack = {t: 1.0 for t in teams}
    defense = {t: 1.0 for t in teams}
    home_adv = 1.3
    total_goals = sum(g["home_score"] + g["away_score"] for g in rows)
    avg_goals = total_goals / (2 * len(rows))

    for _ in range(IPF_ITERS):
        # attack_i update: match actual vs expected goals scored by team i
        scored_actual = {t: 0.0 for t in teams}
        scored_expected = {t: 0.0 for t in teams}
        conceded_actual = {t: 0.0 for t in teams}
        conceded_expected = {t: 0.0 for t in teams}
        home_goals_actual = home_goals_expected = 0.0

        for g in rows:
            h, a, hs, as_ = g["home"], g["away"], g["home_score"], g["away_score"]
            lam_h = home_adv * attack[h] * defense[a]
            lam_a = attack[a] * defense[h]

            scored_actual[h] += hs; scored_expected[h] += lam_h
            scored_actual[a] += as_; scored_expected[a] += lam_a
            conceded_actual[h] += as_; conceded_expected[h] += lam_a
            conceded_actual[a] += hs; conceded_expected[a] += lam_h
            home_goals_actual += hs; home_goals_expected += lam_h

        for t in teams:
            if scored_expected[t] > 0:
                attack[t] *= scored_actual[t] / scored_expected[t]
            if conceded_expected[t] > 0:
                defense[t] *= conceded_actual[t] / conceded_expected[t]
        if home_goals_expected > 0:
            home_adv *= home_goals_actual / home_goals_expected

        # re-anchor scale: attack*defense product is over-determined (can drift together)
        gm_attack = math.exp(sum(math.log(attack[t]) for t in teams) / len(teams))
        gm_defense = math.exp(sum(math.log(defense[t]) for t in teams) / len(teams))
        for t in teams:
            attack[t] /= gm_attack
            defense[t] /= gm_defense

    now = _now()
    games_played = {t: 0 for t in teams}
    for g in rows:
        games_played[g["home"]] += 1
        games_played[g["away"]] += 1
    for t in teams:
        conn.execute(
            """INSERT INTO rating (sport, pool, team, params, games, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(sport, pool, team) DO UPDATE SET
                   params=excluded.params, games=excluded.games, updated_at=excluded.updated_at""",
            (SPORT, pool, t, json.dumps({"attack": attack[t], "defense": defense[t]}),
             games_played[t], now),
        )
    conn.execute(
        """INSERT INTO sport_param (sport, pool, params, updated_at)
           VALUES (?,?,?,?)
           ON CONFLICT(sport, pool) DO UPDATE SET
               params=excluded.params, updated_at=excluded.updated_at""",
        (SPORT, pool, json.dumps({"home_adv": home_adv, "avg_goals": avg_goals}), now),
    )
    conn.commit()
    print(f"[models.soccer] pool={pool}: trained on {len(rows)} final games, "
          f"{len(teams)} teams, home_adv={home_adv:.3f}, avg_goals={avg_goals:.2f}")
    return {"attack": attack, "defense": defense, "home_adv": home_adv}


def _poisson_pmf(k, lam):
    return math.exp(-lam) * (lam ** k) / math.factorial(k)


def _get_team(conn, team, pool):
    row = conn.execute(
        "SELECT params FROM rating WHERE sport=? AND pool=? AND team=?", (SPORT, pool, team),
    ).fetchone()
    if not row:
        return 1.0, 1.0
    p = json.loads(row["params"])
    return p["attack"], p["defense"]


def _get_pool(conn, pool):
    row = conn.execute(
        "SELECT params FROM sport_param WHERE sport=? AND pool=?", (SPORT, pool),
    ).fetchone()
    if not row:
        return 1.3, 1.35
    p = json.loads(row["params"])
    return p["home_adv"], p["avg_goals"]


def predict(conn, home, away, sport_key=None, pool=None, neutral=None):
    """Fair 1X2 + goal-line probabilities for one matchup. Pure read from trained `rating`.

    `pool`/`neutral` can be given directly (CLI) or derived from `sport_key` (pipeline):
    sport_key picks the pool, and intl pools default to neutral venue unless overridden.
    """
    if pool is None:
        pool = pool_for(sport_key) if sport_key else "club"
    if neutral is None:
        neutral = (pool == "intl") if sport_key else False

    home_adv, _ = _get_pool(conn, pool)
    if neutral:
        home_adv = 1.0
    a_home, d_home = _get_team(conn, home, pool)
    a_away, d_away = _get_team(conn, away, pool)

    lam_home = home_adv * a_home * d_away
    lam_away = a_away * d_home

    grid = [[_poisson_pmf(h, lam_home) * _poisson_pmf(a, lam_away)
             for a in range(MAX_GOALS + 1)] for h in range(MAX_GOALS + 1)]

    p_home = sum(grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if h > a)
    p_draw = sum(grid[h][h] for h in range(MAX_GOALS + 1))
    p_away = sum(grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1) if h < a)
    p_over25 = sum(grid[h][a] for h in range(MAX_GOALS + 1) for a in range(MAX_GOALS + 1)
                    if h + a > 2.5)

    return {
        "home": home, "away": away, "pool": pool, "neutral": neutral,
        "lambda_home": lam_home, "lambda_away": lam_away,
        "p_home": p_home, "p_draw": p_draw, "p_away": p_away,
        "p_over25": p_over25, "p_under25": 1.0 - p_over25,
        "_grid": grid,
    }


def cover_prob(pred, side, point):
    """P(`side` covers Asian Handicap `point`) — margin+point>0, push excluded from win mass.

    `point` is the home spread convention used elsewhere in this engine (negative =
    home favored). Quarter lines (e.g. -0.25) settle as a half-stake split at the book —
    that split lives in settle.py; here we just score full-win probability mass.
    """
    grid = pred["_grid"]
    total = 0.0
    for h in range(MAX_GOALS + 1):
        for a in range(MAX_GOALS + 1):
            margin = (h - a) if side == "home" else (a - h)
            line = point if side == "home" else -point
            if margin + line > 0:
                total += grid[h][a]
    return total


def model_prob(pred, market, outcome, point, home, away):
    """Soccer model fair prob (1X2 + Asian Handicap) for a specific candidate."""
    if market == "h2h":
        if outcome == home:
            return pred["p_home"]
        if outcome == away:
            return pred["p_away"]
        if outcome.lower() == "draw":
            return pred["p_draw"]
        return None
    if market == "spreads":
        if outcome == home:
            return cover_prob(pred, "home", point)
        if outcome == away:
            return cover_prob(pred, "away", point)
        return None
    return None  # totals = Phase 1 (same as NBA)


def _fmt(p):
    lines = [
        f"  {p['away']} @ {p['home']}  (pool={p['pool']}, neutral={p['neutral']})",
        f"  expected goals: home {p['lambda_home']:.2f}  away {p['lambda_away']:.2f}",
        f"  1X2: home {p['p_home']*100:5.1f}%  draw {p['p_draw']*100:5.1f}%  "
        f"away {p['p_away']*100:5.1f}%",
        f"  O/U 2.5: over {p['p_over25']*100:5.1f}%  under {p['p_under25']*100:5.1f}%",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true", help="rebuild rating from settled games")
    ap.add_argument("--pool", default="club", choices=("club", "intl"))
    ap.add_argument("--predict", nargs=2, metavar=("HOME", "AWAY"))
    ap.add_argument("--neutral", action="store_true", help="neutral venue (home_adv=1.0)")
    args = ap.parse_args()

    if not args.train and not args.predict:
        ap.error("nothing to do: pass --train and/or --predict HOME AWAY")

    conn = db.init()
    if args.train:
        train(conn, args.pool)
    if args.predict:
        print(_fmt(predict(conn, args.predict[0], args.predict[1],
                            pool=args.pool, neutral=args.neutral)))


if __name__ == "__main__":
    main()
