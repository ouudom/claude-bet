"""Canonical SQLite store for the NBA betting engine (mirrors swing-trading/scripts/db.py).

Source of truth = data/index.db (gitignored). Tables written live by the pipeline:
  odds_snapshot  — every poll of every book's line for a game (CLV substrate)
  game           — schedule + final result (settle target)
  pick           — model value picks (the "Trading Zone" analog)
  settle         — replay grades: CLV, W/L, Brier, ROI (the "trade_outcome" analog)
"""
import os
import sqlite3

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.path.join(ROOT, "data", "index.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS game (
    game_id     TEXT PRIMARY KEY,          -- the-odds-api event id
    sport       TEXT NOT NULL,
    commence    TEXT NOT NULL,             -- ISO8601 UTC tip-off
    home        TEXT NOT NULL,
    away        TEXT NOT NULL,
    home_score  INTEGER,                   -- NULL until settled
    away_score  INTEGER,
    status      TEXT NOT NULL DEFAULT 'scheduled'  -- scheduled|final
);

CREATE TABLE IF NOT EXISTS odds_snapshot (
    game_id     TEXT NOT NULL,
    captured_at TEXT NOT NULL,             -- ISO8601 UTC poll time
    book        TEXT NOT NULL,             -- e.g. pinnacle, draftkings
    market      TEXT NOT NULL,             -- h2h|spreads|totals
    outcome     TEXT NOT NULL,             -- team name | Over | Under
    point       REAL,                      -- spread/total line (NULL for h2h)
    price       INTEGER NOT NULL,          -- American odds
    PRIMARY KEY (game_id, captured_at, book, market, outcome),
    FOREIGN KEY (game_id) REFERENCES game(game_id)
);
CREATE INDEX IF NOT EXISTS ix_snap_game_market ON odds_snapshot(game_id, market, book);

CREATE TABLE IF NOT EXISTS pick (
    pick_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    market       TEXT NOT NULL,
    outcome      TEXT NOT NULL,
    point        REAL,
    bet_price    INTEGER NOT NULL,         -- price we'd take at pick time (best soft book)
    model_prob   REAL NOT NULL,            -- model fair probability
    implied_prob REAL NOT NULL,            -- vig-free implied prob at bet_price
    edge         REAL NOT NULL,            -- model_prob - implied_prob
    stake_units  REAL NOT NULL DEFAULT 1.0,
    FOREIGN KEY (game_id) REFERENCES game(game_id)
);

CREATE TABLE IF NOT EXISTS settle (
    pick_id      INTEGER PRIMARY KEY,
    closing_price INTEGER,                 -- sharp-anchor closing price for same outcome
    clv          REAL,                     -- vig-free prob gain: implied(close) - implied(bet)
    result       TEXT,                     -- win|loss|push
    pnl_units    REAL,
    brier        REAL,                     -- (model_prob - outcome)^2
    FOREIGN KEY (pick_id) REFERENCES pick(pick_id)
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


if __name__ == "__main__":
    init()
    print(f"[db] schema ready at {DB_PATH}")
