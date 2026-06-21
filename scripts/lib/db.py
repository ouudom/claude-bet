"""Canonical SQLite store for the multi-sport betting engine.

Source of truth = data/index.db (gitignored). Tables written live by the pipeline:
  odds_snapshot  — every poll of every book's line for a game (CLV substrate)
  game           — schedule + final result (settle target)
  rating         — current per-team model params, generic across sports (JSON params)
  sport_param    — per-sport/pool scalars (home_adv, MARGIN_SD, etc — JSON params)
  sport_config   — sport_key -> which model adapter + gate params to use (lib/registry.py)
  pick           — model value picks (the "Trading Zone" analog)
  settle         — replay grades: CLV, W/L, Brier, ROI (the "trade_outcome" analog)
  claude_pred    — Claude's fair-prob output + numeric prior + rationale (audit/learning)
  evidence       — web-search citations Claude used per game (audit/learning substrate)
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

CREATE TABLE IF NOT EXISTS rating (
    sport       TEXT NOT NULL,             -- the-odds-api sport key
    pool        TEXT NOT NULL DEFAULT '',  -- '' for NBA; 'club'|'intl' for soccer
    team        TEXT NOT NULL,             -- team name (matches game.home/away)
    params      TEXT NOT NULL,             -- JSON, shape owned by the model adapter
    games       INTEGER NOT NULL DEFAULT 0,-- games trained through
    updated_at  TEXT NOT NULL,             -- ISO8601 UTC of last train pass
    PRIMARY KEY (sport, pool, team)
);

CREATE TABLE IF NOT EXISTS sport_param (
    sport       TEXT NOT NULL,             -- the-odds-api sport key
    pool        TEXT NOT NULL DEFAULT '',  -- '' for NBA; 'club'|'intl' for soccer
    params      TEXT NOT NULL,             -- JSON, shape owned by the model adapter
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (sport, pool)
);

CREATE TABLE IF NOT EXISTS sport_config (
    sport_key       TEXT PRIMARY KEY,      -- the-odds-api sport key
    model           TEXT NOT NULL,         -- registry key: nba | soccer
    pool            TEXT NOT NULL DEFAULT '',
    congestion_days INTEGER,               -- NULL = disabled (NBA default)
    markets         TEXT NOT NULL          -- JSON list, e.g. ["h2h","spreads"]
);

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
    source       TEXT NOT NULL DEFAULT 'claude',  -- predictor that produced this pick
    notified_at  TEXT,                     -- ISO8601 UTC when pushed to telegram (NULL=unsent)
    FOREIGN KEY (game_id) REFERENCES game(game_id)
);

CREATE TABLE IF NOT EXISTS claude_pred (
    pred_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      TEXT NOT NULL,
    created_at   TEXT NOT NULL,            -- ISO8601 UTC of the prediction call
    market       TEXT NOT NULL,            -- h2h|spreads (the candidate the prob is for)
    outcome      TEXT NOT NULL,            -- team name | Draw
    point        REAL,                     -- spread line (NULL for h2h)
    prob         REAL NOT NULL,            -- Claude's calibrated fair probability
    prior_prob   REAL,                     -- Elo/Poisson prior for the same outcome
    rationale    TEXT,                     -- one-line reason (injuries/form/news)
    model_id     TEXT NOT NULL,            -- e.g. claude-opus-4-8
    prompt_ver   TEXT NOT NULL,            -- prompt schema version, for replay analysis
    FOREIGN KEY (game_id) REFERENCES game(game_id)
);
CREATE INDEX IF NOT EXISTS ix_cpred_game ON claude_pred(game_id, market, outcome);

CREATE TABLE IF NOT EXISTS evidence (
    evid_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    game_id      TEXT NOT NULL,
    captured_at  TEXT NOT NULL,            -- ISO8601 UTC of the prediction call
    query        TEXT,                     -- search query Claude issued
    source_url   TEXT,                     -- citation URL
    title        TEXT,                     -- citation title
    snippet      TEXT,                     -- cited text / encrypted index excerpt
    FOREIGN KEY (game_id) REFERENCES game(game_id)
);
CREATE INDEX IF NOT EXISTS ix_evidence_game ON evidence(game_id);

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


def _migrate(conn):
    """Additive column migrations for DBs created before a column existed."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(pick)").fetchall()}
    if "source" not in cols:
        conn.execute("ALTER TABLE pick ADD COLUMN source TEXT NOT NULL DEFAULT 'claude'")
    if "notified_at" not in cols:
        conn.execute("ALTER TABLE pick ADD COLUMN notified_at TEXT")
    conn.commit()


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


if __name__ == "__main__":
    init()
    print(f"[db] schema ready at {DB_PATH}")
