"""settle.py — CLV-first grader. Score every settle-able pick into the `settle` table.

Phase-0 step #4a, the "trade_outcome replay" analog. For each pick whose game is final
and not yet graded:
  - CLV   : vig-free prob gain vs the sharp closing line (the PRIMARY grade — the moat).
  - result: win/loss/push from the final score against the bet's market/outcome/point.
  - pnl   : units won/lost at the bet price.
  - brier : (model_prob - outcome)^2 — model calibration (NULL on push).

CLV is computed against a sharp anchor's closing line (default pinnacle): the last
snapshot at/before tip-off, devigged across the two-way market. Beating it is the
proven leading indicator of long-run edge, so a pick with no anchor close stays
ungraded for CLV (clv=NULL) rather than guessing.

Usage:
    bash scripts/pyrun.sh scripts/settle.py
    bash scripts/pyrun.sh scripts/settle.py --anchor pinnacle
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db, odds  # noqa: E402


def closing_line(conn, game_id, market, anchor, commence):
    """Outcome->price map for the anchor's last snapshot at/before tip-off.

    Falls back to whichever single book has the latest pre-tip snapshot if the
    anchor never posted this game/market.
    """
    def _map(book):
        cap = conn.execute(
            """SELECT captured_at FROM odds_snapshot
                WHERE game_id=? AND market=? AND book=? AND captured_at<=?
                ORDER BY captured_at DESC LIMIT 1""",
            (game_id, market, book, commence),
        ).fetchone()
        if not cap:
            return None, None
        rows = conn.execute(
            """SELECT outcome, price FROM odds_snapshot
                WHERE game_id=? AND market=? AND book=? AND captured_at=?""",
            (game_id, market, book, cap["captured_at"]),
        ).fetchall()
        return book, {r["outcome"]: r["price"] for r in rows}

    book, m = _map(anchor)
    if m:
        return book, m
    # fallback: any book with the latest pre-tip snapshot for this market
    alt = conn.execute(
        """SELECT book FROM odds_snapshot
            WHERE game_id=? AND market=? AND captured_at<=?
            ORDER BY captured_at DESC LIMIT 1""",
        (game_id, market, commence),
    ).fetchone()
    if not alt:
        return None, None
    return _map(alt["book"])


def grade_result(market, outcome, point, home, away, hs, away_s):
    """win/loss/push for the bet against the final score. None if unrecognized."""
    if market == "h2h":
        winner = home if hs > away_s else away
        return "win" if outcome == winner else "loss"
    if market == "spreads":
        if outcome == home:
            m = (hs - away_s) + point
        elif outcome == away:
            m = (away_s - hs) + point
        else:
            return None
        return "push" if m == 0 else ("win" if m > 0 else "loss")
    if market == "totals":
        total = hs + away_s
        o = outcome.lower()
        if o == "over":
            d = total - point
        elif o == "under":
            d = point - total
        else:
            return None
        return "push" if d == 0 else ("win" if d > 0 else "loss")
    return None


def _vigfree_close(close_map, outcome):
    """Devigged closing prob for `outcome` (needs both sides). (prob, our_price) or (None, price)."""
    if not close_map or outcome not in close_map:
        return None, None
    our_price = close_map[outcome]
    others = [oc for oc in close_map if oc != outcome]
    if len(others) != 1:
        return None, our_price          # not a clean 2-way market -> no honest devig
    p_out, _ = odds.devig_two_way(our_price, close_map[others[0]])
    return p_out, our_price


def settle_all(conn, anchor):
    picks = conn.execute(
        """SELECT p.*, g.home, g.away, g.home_score, g.away_score, g.commence, g.status
             FROM pick p JOIN game g ON g.game_id = p.game_id
            WHERE p.pick_id NOT IN (SELECT pick_id FROM settle)"""
    ).fetchall()

    n_settled = n_pending = n_noclv = 0
    for p in picks:
        if p["status"] != "final" or p["home_score"] is None:
            n_pending += 1
            continue

        result = grade_result(p["market"], p["outcome"], p["point"],
                              p["home"], p["away"], p["home_score"], p["away_score"])
        pnl = odds.payout_units(result, p["bet_price"], p["stake_units"])
        y = None if result == "push" else (1.0 if result == "win" else 0.0)
        brier = None if y is None else (p["model_prob"] - y) ** 2

        _, close_map = closing_line(conn, p["game_id"], p["market"], anchor, p["commence"])
        vf_close, close_price = _vigfree_close(close_map, p["outcome"])
        clv = None if vf_close is None else (vf_close - p["implied_prob"])
        if clv is None:
            n_noclv += 1

        conn.execute(
            """INSERT INTO settle (pick_id, closing_price, clv, result, pnl_units, brier)
               VALUES (?,?,?,?,?,?)""",
            (p["pick_id"], close_price, clv, result, pnl, brier),
        )
        n_settled += 1

    conn.commit()
    print(f"[settle] graded {n_settled} picks ({n_noclv} w/o anchor close -> clv=NULL), "
          f"{n_pending} pending (game not final)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--anchor", default="pinnacle", help="sharp book key for the closing line")
    args = ap.parse_args()
    conn = db.init()
    settle_all(conn, args.anchor)


if __name__ == "__main__":
    main()
