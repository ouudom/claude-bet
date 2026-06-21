"""notify.py — push fresh picks to a Telegram bot, then mark them sent.

Final step of the daily run. Reads `pick` rows that haven't been notified yet
(`notified_at IS NULL`), optionally scoped to the same local game window edge.py used,
joins game + the latest claude_pred rationale, formats an NBA section and a soccer
section, and POSTs one message to the Telegram Bot API. On success it stamps
`notified_at` so a re-run never double-sends the same pick.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (see .env.example).
Usage:
    bash scripts/pyrun.sh scripts/pipeline/notify.py
    bash scripts/pyrun.sh scripts/pipeline/notify.py --window 17:00 12:00
"""
import argparse
import datetime as dt
import os
import sys

import requests
from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
from lib import db  # noqa: E402
from edge import _today_window  # noqa: E402  (reuse the same window math)

_NBA = "basketball_nba"


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_picks(conn, win_start, win_end):
    q = """SELECT p.pick_id, p.market, p.outcome, p.point, p.bet_price, p.edge,
                  p.stake_units, p.model_prob, g.home, g.away, g.sport, g.commence
             FROM pick p JOIN game g ON g.game_id = p.game_id
            WHERE p.notified_at IS NULL"""
    params = []
    if win_start and win_end:
        q += " AND g.commence >= ? AND g.commence < ?"
        params = [win_start, win_end]
    q += " ORDER BY g.commence ASC"
    return conn.execute(q, params).fetchall()


def _rationale(conn, home, away):
    row = conn.execute(
        """SELECT rationale FROM claude_pred cp JOIN game g ON g.game_id = cp.game_id
            WHERE g.home=? AND g.away=? AND cp.rationale<>''
            ORDER BY cp.pred_id DESC LIMIT 1""",
        (home, away),
    ).fetchone()
    return row["rationale"] if row else ""


def _fmt_pick(conn, r):
    pt = "" if r["point"] is None else f" {r['point']:+g}"
    price = r["bet_price"]
    price_s = f"+{price}" if price > 0 else str(price)
    line = (f"• {r['away']} @ {r['home']} — {r['market']} {r['outcome']}{pt} "
            f"@ {price_s}  (edge {r['edge']*100:+.1f}%, {r['stake_units']:.2f}u)")
    why = _rationale(conn, r["home"], r["away"])
    return line + (f"\n   ↳ {why}" if why else "")


def build_message(conn, picks):
    nba = [p for p in picks if p["sport"] == _NBA]
    soc = [p for p in picks if p["sport"] != _NBA]
    parts = [f"🎯 Picks — {dt.datetime.now().astimezone():%a %b %d %H:%M %Z}"]
    if nba:
        parts.append("\n🏀 NBA")
        parts += [_fmt_pick(conn, p) for p in nba]
    if soc:
        parts.append("\n⚽ Soccer")
        parts += [_fmt_pick(conn, p) for p in soc]
    if not nba and not soc:
        parts.append("No qualifying picks in this window.")
    return "\n".join(parts)


def send(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text,
                                 "disable_web_page_preview": True}, timeout=30)
    r.raise_for_status()
    return r.json()


def main():
    load_dotenv(os.path.join(db.ROOT, ".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", nargs=2, metavar=("START", "END"),
                    help="local game window HH:MM HH:MM (today START -> tomorrow END)")
    ap.add_argument("--dry-run", action="store_true", help="print message, don't send/mark")
    args = ap.parse_args()

    ws = we = None
    if args.window:
        ws, we = _today_window(args.window[0], args.window[1])

    conn = db.init()
    picks = _fetch_picks(conn, ws, we)
    msg = build_message(conn, picks)

    if args.dry_run:
        print(msg)
        return
    if not picks:
        print("[notify] nothing new to send")
        return

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not token or not chat_id:
        sys.exit("[notify] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing in .env")

    send(token, chat_id, msg)
    now = _now()
    conn.executemany("UPDATE pick SET notified_at=? WHERE pick_id=?",
                     [(now, p["pick_id"]) for p in picks])
    conn.commit()
    print(f"[notify] sent {len(picks)} picks, marked notified")


if __name__ == "__main__":
    main()
