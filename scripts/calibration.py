"""calibration.py — the honest scoreboard. Aggregate settled picks into WORKING/DEAD.

Phase-0 step #4b, the `calibration.md` analog. Groups graded picks by market + spot
(fav/dog or Over/Under) and reports the moat metrics, CLV FIRST:
  - mean CLV + CLV>0 rate   (leading indicator — the verdict driver)
  - ROI, Brier, W-L-P record (lagging / supporting)

Verdict is min-n gated so we never crown a signal on noise:
  INCONCLUSIVE  n < MIN_N
  WORKING       mean CLV > 0 AND CLV>0 rate >= 0.5
  DEAD          otherwise
A WORKING spot is still PAPER-only until n >= MONEY_N (the README hard gate: positive
CLV over >=300 paper bets before any real money / paid signals).

Writes data/calibration.md (gitignored, regenerable) and prints the same to stdout.

Usage:
    bash scripts/pyrun.sh scripts/calibration.py
    bash scripts/pyrun.sh scripts/calibration.py --min-n 50
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db  # noqa: E402

MIN_N = 30      # below this: INCONCLUSIVE
MONEY_N = 300   # README hard gate for real money / paid signals


def _spot(market, bet_price, outcome):
    if market == "totals":
        return outcome.capitalize()        # Over / Under
    return "fav" if bet_price < 0 else "dog"


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def aggregate(conn):
    rows = conn.execute(
        """SELECT p.market, p.bet_price, p.outcome, p.stake_units,
                  s.clv, s.result, s.pnl_units, s.brier
             FROM settle s JOIN pick p ON p.pick_id = s.pick_id"""
    ).fetchall()

    groups = {}
    for r in rows:
        key = (r["market"], _spot(r["market"], r["bet_price"], r["outcome"]))
        groups.setdefault(key, []).append(r)
    groups[("ALL", "*")] = list(rows)
    return groups


def summarize(rows, min_n):
    clvs = [r["clv"] for r in rows if r["clv"] is not None]
    briers = [r["brier"] for r in rows if r["brier"] is not None]
    staked = sum(r["stake_units"] for r in rows)
    pnl = sum(r["pnl_units"] for r in rows)
    w = sum(1 for r in rows if r["result"] == "win")
    l = sum(1 for r in rows if r["result"] == "loss")
    push = sum(1 for r in rows if r["result"] == "push")

    clv_mean = _mean(clvs)
    clv_pos = (sum(1 for c in clvs if c > 0) / len(clvs)) if clvs else None
    roi = (pnl / staked) if staked else None

    n = len(rows)
    if n < min_n or clv_mean is None:
        verdict = "INCONCLUSIVE"
    elif clv_mean > 0 and clv_pos >= 0.5:
        verdict = "WORKING" if n >= MONEY_N else "WORKING(paper)"
    else:
        verdict = "DEAD"

    return {
        "n": n, "n_clv": len(clvs), "clv_mean": clv_mean, "clv_pos": clv_pos,
        "roi": roi, "brier": _mean(briers), "record": f"{w}-{l}-{push}", "verdict": verdict,
    }


def _pct(x):
    return "  n/a" if x is None else f"{x*100:5.1f}%"


def _sg(x, scale=1.0, w=7):
    return " " * (w - 3) + "n/a" if x is None else f"{x*scale:+{w}.2f}"


def render(groups, min_n):
    lines = ["# Calibration — CLV-first scoreboard", ""]
    lines.append(f"_min-n={min_n} for a verdict; WORKING needs n>={MONEY_N} for real money._")
    lines.append("")
    hdr = "| market | spot | n | n_clv | CLV(bps) | CLV>0 | ROI | Brier | W-L-P | verdict |"
    sep = "|---|---|---:|---:|---:|---:|---:|---:|---|---|"
    lines += [hdr, sep]
    # ALL row last, spots sorted for stable output
    keys = sorted(k for k in groups if k[0] != "ALL") + [k for k in groups if k[0] == "ALL"]
    for market, spot in keys:
        s = summarize(groups[(market, spot)], min_n)
        clv_bps = None if s["clv_mean"] is None else s["clv_mean"] * 10000  # prob -> basis points
        lines.append(
            f"| {market} | {spot} | {s['n']} | {s['n_clv']} | "
            f"{_sg(clv_bps, w=8)} | {_pct(s['clv_pos'])} | {_pct(s['roi'])} | "
            f"{_sg(s['brier'])} | {s['record']} | {s['verdict']} |"
        )
    return "\n".join(lines) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-n", type=int, default=MIN_N)
    args = ap.parse_args()

    conn = db.init()
    groups = aggregate(conn)
    report = render(groups, args.min_n)

    out_path = os.path.join(db.ROOT, "data", "calibration.md")
    with open(out_path, "w") as f:
        f.write(report)
    print(report)
    print(f"[calibration] wrote {out_path}")


if __name__ == "__main__":
    main()
