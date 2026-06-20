"""results.py — settle target. Pull final scores and mark `game` rows final.

Phase-0 step #2b. Pulls the-odds-api /scores endpoint (daysFrom up to 3) for completed
games and writes home_score/away_score + status='final'. Scores are keyed by the same
event id as odds_snapshot, so settle.py can later join close-line -> final result with
zero id mapping. Run a few times a day (after slates finish) to keep games settled.

Cost: /scores with daysFrom counts as 2 requests against the quota; without historical
days it is free. We default daysFrom=3 to backfill any games missed while down.

Usage:
    bash scripts/pyrun.sh scripts/results.py
    bash scripts/pyrun.sh scripts/results.py --days 1

Env: ODDS_API_KEY (the-odds-api.com free tier). Copy .env.example -> .env and fill it.
"""
import argparse
import os
import sys

import requests
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib import db  # noqa: E402

SPORT = "basketball_nba"
BASE = "https://api.the-odds-api.com/v4"


def fetch_scores(api_key, days):
    url = f"{BASE}/sports/{SPORT}/scores"
    params = {"apiKey": api_key, "dateFormat": "iso", "daysFrom": days}
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    remaining = r.headers.get("x-requests-remaining")
    used = r.headers.get("x-requests-used")
    print(f"[results] api quota: used={used} remaining={remaining}")
    return r.json()


def _score_for(scores, team):
    """the-odds-api returns scores as [{name, score}]; match by team name."""
    for s in scores or []:
        if s.get("name") == team:
            try:
                return int(s["score"])
            except (TypeError, ValueError, KeyError):
                return None
    return None


def store(conn, games):
    n_final = n_skip = 0
    for ev in games:
        if not ev.get("completed"):
            continue
        gid = ev["id"]
        home_pts = _score_for(ev.get("scores"), ev["home_team"])
        away_pts = _score_for(ev.get("scores"), ev["away_team"])
        if home_pts is None or away_pts is None:
            n_skip += 1
            continue
        # Upsert: settle if we already track the game, else insert it final outright
        # (covers games that finished before we ever pulled odds/schedule for them).
        conn.execute(
            """INSERT INTO game (game_id, sport, commence, home, away,
                                 home_score, away_score, status)
               VALUES (?,?,?,?,?,?,?, 'final')
               ON CONFLICT(game_id) DO UPDATE SET
                   home_score=excluded.home_score,
                   away_score=excluded.away_score,
                   status='final'""",
            (gid, SPORT, ev["commence_time"], ev["home_team"], ev["away_team"],
             home_pts, away_pts),
        )
        n_final += 1
    conn.commit()
    print(f"[results] settled {n_final} games final ({n_skip} completed w/o parseable score)")


def main():
    load_dotenv(os.path.join(db.ROOT, ".env"))
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=3, choices=range(0, 4),
                    help="daysFrom for historical completed games (0=free, 1-3=2 req)")
    args = ap.parse_args()

    api_key = os.environ.get("ODDS_API_KEY", "").strip()
    if not api_key:
        sys.exit("[results] ODDS_API_KEY missing. Copy .env.example -> .env and fill it "
                 "(free key: https://the-odds-api.com).")

    conn = db.init()
    games = fetch_scores(api_key, args.days)
    store(conn, games)


if __name__ == "__main__":
    main()
