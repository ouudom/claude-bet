"""model.py — fair-line engine. Elo power ratings -> vig-free fair probabilities.

Phase-0 step #3. The "weekly forecast" analog: turn settled history into a forward
view of each game. Pure, deterministic Elo (FiveThirtyEight NBA flavor) so calibration
is honest and reproducible — no opaque ML, every number traceable.

What it produces today (robust from Elo alone):
  - moneyline  : fair home/away win probability (logistic Elo)
  - spreads    : fair margin (points) + cover probability for any line (normal approx)
What it does NOT produce yet (needs pace + off/def efficiency = box-score stats via
nba_api, Phase 1): totals. We refuse to fake a totals number — see predict()['total'].

Training reads settled `game` rows (status='final') in chronological order and walks
Elo game-by-game, writing `team_rating`. Off-season the table may be empty; train is a
no-op until results.py (or a historical backfill) has populated finals.

Usage:
    bash scripts/pyrun.sh scripts/model.py --train
    bash scripts/pyrun.sh scripts/model.py --predict "Boston Celtics" "Miami Heat"
    bash scripts/pyrun.sh scripts/model.py --predict "Boston Celtics" "Miami Heat" --line -5.5
"""
import argparse
import datetime as dt
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db  # noqa: E402

# --- Elo constants (FiveThirtyEight NBA calibration) ---
BASE_RATING = 1500.0     # cold-start rating for an unseen team
HCA_ELO = 100.0          # home-court advantage in Elo points (~3.5 pts)
K = 20.0                 # update step size
ELO_PER_POINT = 28.0     # ~28 Elo ≈ 1 point of spread (538)
MARGIN_SD = 12.0         # SD of NBA game margin vs prediction (points)


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _expected_home(r_home, r_away):
    """Logistic Elo win prob for the home team (HCA already folded into r_home)."""
    return 1.0 / (1.0 + 10.0 ** (-(r_home - r_away) / 400.0))


def _mov_mult(mov, winner_elo_diff):
    """538 margin-of-victory multiplier — caps blowout impact, autocorrelation-adjusted."""
    return ((abs(mov) + 3.0) ** 0.8) / (7.5 + 0.006 * winner_elo_diff)


def train(conn):
    """Walk settled games chronologically, update Elo, persist team_rating. Returns dict."""
    rows = conn.execute(
        """SELECT home, away, home_score, away_score
             FROM game
            WHERE status='final' AND home_score IS NOT NULL AND away_score IS NOT NULL
            ORDER BY commence ASC"""
    ).fetchall()

    ratings, counts = {}, {}
    for g in rows:
        home, away = g["home"], g["away"]
        r_home = ratings.get(home, BASE_RATING)
        r_away = ratings.get(away, BASE_RATING)
        eff_home = r_home + HCA_ELO                  # apply HCA for this game only
        e_home = _expected_home(eff_home, r_away)

        mov = g["home_score"] - g["away_score"]
        s_home = 1.0 if mov > 0 else 0.0             # NBA has no ties
        winner_elo_diff = (eff_home - r_away) if mov > 0 else (r_away - eff_home)
        delta = K * _mov_mult(mov, winner_elo_diff) * (s_home - e_home)

        ratings[home] = r_home + delta
        ratings[away] = r_away - delta
        counts[home] = counts.get(home, 0) + 1
        counts[away] = counts.get(away, 0) + 1

    now = _now()
    for team, r in ratings.items():
        conn.execute(
            """INSERT INTO team_rating (team, rating, games, updated_at)
               VALUES (?,?,?,?)
               ON CONFLICT(team) DO UPDATE SET
                   rating=excluded.rating, games=excluded.games, updated_at=excluded.updated_at""",
            (team, r, counts[team], now),
        )
    conn.commit()
    print(f"[model] trained on {len(rows)} final games, {len(ratings)} teams rated")
    return ratings


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def predict(conn, home, away, line=None, ratings=None):
    """Fair probabilities for a single matchup. `line` = home spread (e.g. -5.5).

    ratings: optional in-memory dict (from train) to avoid a db read.
    """
    def _get(team):
        if ratings is not None:
            return ratings.get(team, BASE_RATING)
        row = conn.execute("SELECT rating FROM team_rating WHERE team=?", (team,)).fetchone()
        return row["rating"] if row else BASE_RATING

    r_home = _get(home) + HCA_ELO
    r_away = _get(away)
    diff = r_home - r_away

    ml_home = _expected_home(r_home, r_away)
    fair_margin = diff / ELO_PER_POINT               # home expected to win by this many pts
    out = {
        "home": home, "away": away,
        "fair_margin": fair_margin,                  # +ve = home favored
        "fair_home_spread": -fair_margin,            # the pick'em line for home
        "ml_home": ml_home,
        "ml_away": 1.0 - ml_home,
        "total": None,                               # Phase 1: needs pace/efficiency
    }
    if line is not None:
        # P(home covers home spread `line`): home_margin + line > 0  ->  margin > -line
        z = (fair_margin - (-line)) / MARGIN_SD
        out["line"] = line
        out["cover_prob_home"] = _norm_cdf(z)
        out["cover_prob_away"] = 1.0 - out["cover_prob_home"]
    return out


def _fmt(p):
    lines = [
        f"  {p['away']} @ {p['home']}",
        f"  fair margin   : home {p['fair_margin']:+.1f} pts  (fair home line {p['fair_home_spread']:+.1f})",
        f"  moneyline     : home {p['ml_home']*100:5.1f}%   away {p['ml_away']*100:5.1f}%",
        "  total         : n/a (Phase 1 — needs pace/efficiency)",
    ]
    if "line" in p:
        lines.append(
            f"  cover @ {p['line']:+.1f} : home {p['cover_prob_home']*100:5.1f}%   "
            f"away {p['cover_prob_away']*100:5.1f}%"
        )
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true", help="rebuild team_rating from settled games")
    ap.add_argument("--predict", nargs=2, metavar=("HOME", "AWAY"), help="fair line for one matchup")
    ap.add_argument("--line", type=float, default=None, help="home spread to grade cover prob (e.g. -5.5)")
    args = ap.parse_args()

    if not args.train and not args.predict:
        ap.error("nothing to do: pass --train and/or --predict HOME AWAY")

    conn = db.init()
    if args.train:
        train(conn)
    if args.predict:
        print(_fmt(predict(conn, args.predict[0], args.predict[1], line=args.line)))


if __name__ == "__main__":
    main()
