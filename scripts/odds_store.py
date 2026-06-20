"""odds_store.py — CLV substrate. Snapshot NBA lines from the-odds-api into index.db.

This is Phase-0 step #1 and the foundation of the whole engine: closing-line value
cannot be reconstructed after the fact, so this must run on a schedule starting NOW.
Run it a few times a day; each run appends an odds_snapshot row per book/market/outcome.
The last snapshot before tip-off becomes the de-facto closing line at settle time.

Usage:
    bash scripts/pyrun.sh scripts/odds_store.py
    bash scripts/pyrun.sh scripts/odds_store.py --regions us,eu --markets h2h,spreads,totals

Env: ODDS_API_KEY (the-odds-api.com free tier). Copy .env.example -> .env and fill it.
"""
import argparse
import datetime as dt
import os
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db  # noqa: E402

SPORT = "basketball_nba"
BASE = "https://api.the-odds-api.com/v4"


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_odds(api_key, regions, markets):
    url = f"{BASE}/sports/{SPORT}/odds"
    params = {
        "apiKey": api_key,
        "regions": regions,            # us,eu (eu includes pinnacle = sharp anchor)
        "markets": markets,            # h2h,spreads,totals
        "oddsFormat": "american",
        "dateFormat": "iso",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    print(f"[odds_store] api quota: used={used} remaining={remaining}")
    return r.json()


def store(conn, events):
    captured_at = _now()
    n_games = n_rows = 0
    for ev in events:
        gid = ev["id"]
        conn.execute(
            """INSERT INTO game (game_id, sport, commence, home, away)
               VALUES (?,?,?,?,?)
               ON CONFLICT(game_id) DO UPDATE SET commence=excluded.commence""",
            (gid, SPORT, ev["commence_time"], ev["home_team"], ev["away_team"]),
        )
        n_games += 1
        for bk in ev.get("bookmakers", []):
            book = bk["key"]
            for mk in bk.get("markets", []):
                market = mk["key"]
                for oc in mk.get("outcomes", []):
                    conn.execute(
                        """INSERT OR IGNORE INTO odds_snapshot
                           (game_id, captured_at, book, market, outcome, point, price)
                           VALUES (?,?,?,?,?,?,?)""",
                        (gid, captured_at, book, market,
                         oc["name"], oc.get("point"), int(oc["price"])),
                    )
                    n_rows += 1
    conn.commit()
    print(f"[odds_store] {captured_at}: {n_games} games, {n_rows} snapshot rows")


def main():
    load_dotenv(os.path.join(db.ROOT, ".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--regions", default="us,eu")
    ap.add_argument("--markets", default="h2h,spreads,totals")
    args = ap.parse_args()

    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        sys.exit("[odds_store] ODDS_API_KEY missing. Copy .env.example -> .env and fill it "
                 "(free key: https://the-odds-api.com).")

    conn = db.init()
    events = fetch_odds(api_key, args.regions, args.markets)
    store(conn, events)


if __name__ == "__main__":
    main()
