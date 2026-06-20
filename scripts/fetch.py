"""fetch.py — schedule substrate. Upsert the upcoming NBA slate into index.db.

Phase-0 step #2a. Pulls the-odds-api /events endpoint (FREE — does not count against
the 500 req/mo quota) so we know the full slate and its event ids before lines post.
Event ids here are the SAME ids odds_store.py and results.py key on, so no cross-source
id mapping is ever needed. Writes scheduled `game` rows; scores arrive later via results.py.

Usage:
    bash scripts/pyrun.sh scripts/fetch.py

Env: ODDS_API_KEY (the-odds-api.com free tier). Copy .env.example -> .env and fill it.
"""
import os
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db  # noqa: E402

SPORT = "basketball_nba"
BASE = "https://api.the-odds-api.com/v4"


def fetch_events(api_key):
    url = f"{BASE}/sports/{SPORT}/events"
    params = {"apiKey": api_key, "dateFormat": "iso"}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    print(f"[fetch] api quota: used={used} remaining={remaining} (events is free)")
    return r.json()


def store(conn, events):
    n = 0
    for ev in events:
        conn.execute(
            """INSERT INTO game (game_id, sport, commence, home, away)
               VALUES (?,?,?,?,?)
               ON CONFLICT(game_id) DO UPDATE SET commence=excluded.commence""",
            (ev["id"], SPORT, ev["commence_time"], ev["home_team"], ev["away_team"]),
        )
        n += 1
    conn.commit()
    print(f"[fetch] upserted {n} scheduled games")


def main():
    load_dotenv(os.path.join(db.ROOT, ".env"))
    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        sys.exit("[fetch] ODDS_API_KEY missing. Copy .env.example -> .env and fill it "
                 "(free key: https://the-odds-api.com).")

    conn = db.init()
    events = fetch_events(api_key)
    store(conn, events)


if __name__ == "__main__":
    main()
