"""edge.py — value-pick generator. The "Trading Zone" analog: model vs the market.

Phase-0 step #5b and the loop's source: writes the `pick` rows that settle.py /
calibration.py grade. For every upcoming game it compares the model's fair probability
to the best available vig-free price and keeps picks where:

    edge = model_prob - vigfree_implied(best price) > threshold      (default 3%)

then runs gates.py (rest / line-move / stale / locked) and only writes picks that pass.
Stake is quarter-Kelly on the model edge, clamped — paper units, no capital at risk.

We bet the BEST soft-book price (max payout = max edge and max CLV vs the sharp close),
devigging that same book's two-way market for the honest implied. Pinnacle is the grading
anchor (settle.py), not a bet target. Markets: h2h + spreads (totals = Phase 1).

Requires team_rating (run `model.py --train` first) and recent odds_store.py snapshots.

Usage:
    bash scripts/pyrun.sh scripts/edge.py
    bash scripts/pyrun.sh scripts/edge.py --threshold 0.04 --kelly 0.25
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db, odds  # noqa: E402
import model  # noqa: E402
import gates  # noqa: E402

KELLY_CAP = 3.0          # max paper units per pick


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_lines(conn, game_id):
    """Per (book, market, outcome) latest snapshot -> {(book,market,outcome): (point, price)}."""
    rows = conn.execute(
        """SELECT s.book, s.market, s.outcome, s.point, s.price
             FROM odds_snapshot s
             JOIN (SELECT book, market, outcome, MAX(captured_at) AS mx
                     FROM odds_snapshot WHERE game_id=? GROUP BY book, market, outcome) t
               ON s.book=t.book AND s.market=t.market AND s.outcome=t.outcome
                  AND s.captured_at=t.mx
            WHERE s.game_id=?""",
        (game_id, game_id),
    ).fetchall()
    out = {}
    for r in rows:
        out[(r["book"], r["market"], r["outcome"])] = (r["point"], r["price"])
    return out


def _model_prob(pred, market, outcome, point, home, away):
    """Model fair prob for a specific candidate outcome/point. None if unsupported."""
    if market == "h2h":
        return pred["ml_home"] if outcome == home else (pred["ml_away"] if outcome == away else None)
    if market == "spreads":
        # home covers if home_margin + point > 0 ; away if -home_margin + point > 0
        if outcome == home:
            z = (pred["fair_margin"] + point) / model.MARGIN_SD
        elif outcome == away:
            z = (-pred["fair_margin"] + point) / model.MARGIN_SD
        else:
            return None
        return model._norm_cdf(z)
    return None  # totals = Phase 1


def _vigfree_at(lines, book, market, outcome):
    """Devigged implied for `outcome` using `book`'s own two-way market. None if not clean."""
    sides = {oc: pr for (b, mk, oc), (pt, pr) in lines.items() if b == book and mk == market}
    if outcome not in sides or len(sides) != 2:
        return None
    other = next(o for o in sides if o != outcome)
    p, _ = odds.devig_two_way(sides[outcome], sides[other])
    return p


def _quarter_kelly(model_prob, price, frac, cap):
    b = odds.american_to_decimal(price) - 1.0
    if b <= 0:
        return 0.0
    f = (b * model_prob - (1.0 - model_prob)) / b      # full Kelly fraction
    return max(0.0, min(cap, frac * f))


def find_edges(conn, threshold, kelly_frac, verbose):
    games = conn.execute(
        "SELECT * FROM game WHERE status='scheduled' ORDER BY commence ASC"
    ).fetchall()
    now = _now()
    n_written = n_blocked = n_existing = 0

    for g in games:
        gid, home, away = g["game_id"], g["home"], g["away"]
        lines = _latest_lines(conn, gid)
        if not lines:
            continue
        pred = model.predict(conn, home, away)

        # best price per (market, outcome) across books = best payout = best edge/CLV
        best = {}
        for (book, market, outcome), (point, price) in lines.items():
            if market not in ("h2h", "spreads"):
                continue
            dec = odds.american_to_decimal(price)
            key = (market, outcome, point)
            if key not in best or dec > best[key][1]:
                best[key] = (book, dec, price)

        for (market, outcome, point), (book, _dec, price) in best.items():
            mp = _model_prob(pred, market, outcome, point, home, away)
            if mp is None:
                continue
            implied = _vigfree_at(lines, book, market, outcome)
            if implied is None:
                continue
            edge = mp - implied
            if edge <= threshold:
                continue

            our_team = None if market == "totals" else outcome
            opp_team = None if our_team is None else (away if our_team == home else home)
            ok, gs = gates.run_gates(conn, gid, market, outcome, our_team, opp_team, g["commence"])
            tag = f"{away}@{home} {market} {outcome} {point if point is not None else ''} " \
                  f"@{price}({book}) edge={edge*100:+.1f}%"
            if not ok:
                n_blocked += 1
                if verbose:
                    blocks = ", ".join(f"{x.name}:{x.detail}" for x in gs if not x.ok)
                    print(f"  BLOCK {tag} -> {blocks}")
                continue

            dup = conn.execute(
                "SELECT 1 FROM pick WHERE game_id=? AND market=? AND outcome=?",
                (gid, market, outcome),
            ).fetchone()
            if dup:
                n_existing += 1
                continue

            stake = _quarter_kelly(mp, price, kelly_frac, KELLY_CAP)
            if stake <= 0:
                continue
            conn.execute(
                """INSERT INTO pick (game_id, created_at, market, outcome, point,
                                     bet_price, model_prob, implied_prob, edge, stake_units)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (gid, now, market, outcome, point, price, mp, implied, edge, stake),
            )
            n_written += 1
            print(f"  PICK  {tag} stake={stake:.2f}u (model {mp*100:.1f}% vs imp {implied*100:.1f}%)")

    conn.commit()
    print(f"[edge] wrote {n_written} picks, blocked {n_blocked}, {n_existing} already existed")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.03, help="min vig-free edge to bet")
    ap.add_argument("--kelly", type=float, default=0.25, help="Kelly fraction (0.25 = quarter)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show blocked candidates")
    args = ap.parse_args()
    conn = db.init()
    find_edges(conn, args.threshold, args.kelly, args.verbose)


if __name__ == "__main__":
    main()
