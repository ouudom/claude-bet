"""gates.py — pre-lock gate. The /validate analog: veto value picks before they lock.

Phase-0 step #5a. A model edge is necessary but not sufficient; "bet only where model
AND the sharp line agree." These gates encode the cheap, data-derivable vetoes:

  locked     — tip-off already passed (can't bet) -> BLOCK
  stale      — newest line is too old before tip (price likely gone) -> BLOCK
  line_move  — sharp (pinnacle) moved AGAINST our side since open -> BLOCK
               (we'd be taking the wrong side of sharp money = negative-CLV setup)
  rest       — our side on a back-to-back while the opponent rested -> BLOCK

Qualitative injury/lineup reads need a feed (ESPN/Rotowire) and are Phase 1 — the
`lineup` gate is a stub that passes with a note so the wiring is ready.

Each gate returns Gate(name, ok, detail). run_gates() short-circuits to the blocks.
"""
import datetime as dt
import os
import sys
from collections import namedtuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import odds  # noqa: E402

Gate = namedtuple("Gate", "name ok detail")

STALE_HOURS = 12.0       # newest line older than this before tip => stale
MOVE_BLOCK = 0.03        # sharp vig-free prob drop on our side that vetoes (3%)
ANCHOR = "pinnacle"


def _parse(ts):
    return dt.datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)


def _now():
    return dt.datetime.now(dt.timezone.utc)


def gate_locked(commence):
    return Gate("locked", _parse(commence) > _now(), f"tip {commence}")


def gate_stale(conn, game_id, market, commence):
    row = conn.execute(
        """SELECT MAX(captured_at) AS c FROM odds_snapshot
            WHERE game_id=? AND market=? AND captured_at<=?""",
        (game_id, market, commence),
    ).fetchone()
    if not row or not row["c"]:
        return Gate("stale", False, "no line snapshot")
    age_h = (_parse(commence) - _parse(row["c"])).total_seconds() / 3600.0
    return Gate("stale", age_h <= STALE_HOURS, f"newest line {age_h:.1f}h pre-tip")


def _anchor_vigfree(conn, game_id, market, outcome, captured_at):
    """Devigged anchor prob for `outcome` at a given snapshot time. None if not 2-way."""
    rows = conn.execute(
        """SELECT outcome, price FROM odds_snapshot
            WHERE game_id=? AND market=? AND book=? AND captured_at=?""",
        (game_id, market, ANCHOR, captured_at),
    ).fetchall()
    m = {r["outcome"]: r["price"] for r in rows}
    if outcome not in m or len(m) != 2:
        return None
    other = next(o for o in m if o != outcome)
    p, _ = odds.devig_two_way(m[outcome], m[other])
    return p


def gate_line_move(conn, game_id, market, outcome, commence):
    """Block if the sharp anchor moved our side's vig-free prob DOWN since open."""
    times = conn.execute(
        """SELECT MIN(captured_at) AS open, MAX(captured_at) AS close
             FROM odds_snapshot
            WHERE game_id=? AND market=? AND book=? AND captured_at<=?""",
        (game_id, market, ANCHOR, commence),
    ).fetchone()
    if not times or not times["open"]:
        return Gate("line_move", True, "no sharp anchor (skip)")
    p_open = _anchor_vigfree(conn, game_id, market, outcome, times["open"])
    p_close = _anchor_vigfree(conn, game_id, market, outcome, times["close"])
    if p_open is None or p_close is None:
        return Gate("line_move", True, "anchor not 2-way (skip)")
    move = p_close - p_open
    return Gate("line_move", move >= -MOVE_BLOCK, f"sharp moved {move*100:+.1f}% our side")


def _prior_game(conn, team, commence):
    row = conn.execute(
        """SELECT commence FROM game
            WHERE (home=? OR away=?) AND commence<?
            ORDER BY commence DESC LIMIT 1""",
        (team, team, commence),
    ).fetchone()
    return row["commence"] if row else None


def _is_b2b(conn, team, commence):
    prev = _prior_game(conn, team, commence)
    if not prev:
        return False
    return (_parse(commence) - _parse(prev)).total_seconds() / 3600.0 <= 30.0


def gate_rest(conn, our_team, opp_team, commence):
    """Block if our side is on a back-to-back and the opponent is not."""
    if our_team is None:                       # totals: no team side
        return Gate("rest", True, "n/a (total)")
    ours = _is_b2b(conn, our_team, commence)
    theirs = _is_b2b(conn, opp_team, commence)
    if ours and not theirs:
        return Gate("rest", False, "our side B2B, opp rested")
    return Gate("rest", True, f"b2b ours={ours} opp={theirs}")


def gate_lineup(conn, game_id):
    return Gate("lineup", True, "stub — injury/lineup feed = Phase 1")


def run_gates(conn, game_id, market, outcome, our_team, opp_team, commence):
    """All gates for one candidate. Returns (ok_all, [Gate,...])."""
    gs = [
        gate_locked(commence),
        gate_stale(conn, game_id, market, commence),
        gate_line_move(conn, game_id, market, outcome, commence),
        gate_rest(conn, our_team, opp_team, commence),
        gate_lineup(conn, game_id),
    ]
    return all(g.ok for g in gs), gs
