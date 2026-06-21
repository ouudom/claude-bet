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

Requires `rating` (run the sport's model module with --train first, e.g.
models/nba.py) and recent odds_store.py snapshots.

Usage:
    bash scripts/pyrun.sh scripts/pipeline/edge.py
    bash scripts/pyrun.sh scripts/pipeline/edge.py --threshold 0.04 --kelly 0.25
"""
import argparse
import datetime as dt
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
from lib import db, odds, registry  # noqa: E402
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


def _vigfree_at(lines, book, market, outcome):
    """Devigged implied for `outcome` in `book`'s market (n-way: 2-way ML/AH, 3-way 1X2)."""
    sides = {oc: pr for (b, mk, oc), (pt, pr) in lines.items() if b == book and mk == market}
    if outcome not in sides or len(sides) < 2:
        return None
    outcomes = list(sides.keys())
    probs = odds.devig_n_way([sides[o] for o in outcomes])
    return probs[outcomes.index(outcome)]


def _quarter_kelly(model_prob, price, frac, cap):
    b = odds.american_to_decimal(price) - 1.0
    if b <= 0:
        return 0.0
    f = (b * model_prob - (1.0 - model_prob)) / b      # full Kelly fraction
    return max(0.0, min(cap, frac * f))


def find_edges(conn, threshold, kelly_frac, verbose, win_start=None, win_end=None):
    if win_start and win_end:
        games = conn.execute(
            """SELECT * FROM game WHERE status='scheduled'
                 AND commence >= ? AND commence < ? ORDER BY commence ASC""",
            (win_start, win_end),
        ).fetchall()
        print(f"[edge] game window {win_start} -> {win_end} UTC: {len(games)} games")
    else:
        games = conn.execute(
            "SELECT * FROM game WHERE status='scheduled' ORDER BY commence ASC"
        ).fetchall()
    now = _now()
    n_written = n_blocked = n_existing = 0

    for g in games:
        gid, home, away, sport = g["game_id"], g["home"], g["away"], g["sport"]
        lines = _latest_lines(conn, gid)
        if not lines:
            continue
        m = registry.for_sport(sport)
        pred = m.predict(conn, home, away, sport_key=sport, commence=g["commence"])

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
            mp = m.model_prob(pred, market, outcome, point, home, away)
            if mp is None:
                continue
            implied = _vigfree_at(lines, book, market, outcome)
            if implied is None:
                continue
            edge = mp - implied
            if edge <= threshold:
                continue

            no_side = market == "totals" or outcome.lower() == "draw"
            our_team = None if no_side else outcome
            opp_team = None if our_team is None else (away if our_team == home else home)
            congestion_days = m.gate_params(sport)["congestion_days"]
            ok, gs = gates.run_gates(conn, gid, market, outcome, our_team, opp_team, g["commence"],
                                      congestion_days=congestion_days)
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


def _today_window(start_hhmm, end_hhmm):
    """Local 'today HH:MM' -> 'tomorrow HH:MM' as UTC ISO bounds (local = machine tz)."""
    now = dt.datetime.now().astimezone()                 # local tz-aware
    sh, sm = (int(x) for x in start_hhmm.split(":"))
    eh, em = (int(x) for x in end_hhmm.split(":"))
    start = now.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = (start + dt.timedelta(days=1)).replace(hour=eh, minute=em)
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    return (start.astimezone(dt.timezone.utc).strftime(fmt),
            end.astimezone(dt.timezone.utc).strftime(fmt))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=float, default=0.03, help="min vig-free edge to bet")
    ap.add_argument("--kelly", type=float, default=0.25, help="Kelly fraction (0.25 = quarter)")
    ap.add_argument("-v", "--verbose", action="store_true", help="show blocked candidates")
    ap.add_argument("--window", nargs=2, metavar=("START", "END"),
                    help="local game window as HH:MM HH:MM (today START -> tomorrow END), "
                         "e.g. --window 17:00 12:00 for 5pm today -> noon tomorrow")
    args = ap.parse_args()
    conn = db.init()
    registry.ensure_config(conn)
    ws = we = None
    if args.window:
        ws, we = _today_window(args.window[0], args.window[1])
    find_edges(conn, args.threshold, args.kelly, args.verbose, ws, we)


if __name__ == "__main__":
    main()
