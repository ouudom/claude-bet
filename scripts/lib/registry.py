"""registry.py — sport_key -> model adapter lookup. The pipeline's single seam for sports.

Every adapter in scripts/models/ implements the shape in models/base.py. Adding sport
#N means writing one new module there and registering it in `_MODELS` below —
fetch/odds_store/results/edge/gates/settle/calibration never change.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import nba, soccer  # noqa: E402

_MODELS = {"nba": nba, "soccer": soccer}

_BY_SPORT_KEY = {key: mod for mod in _MODELS.values() for key in mod.sport_keys}


def for_sport(sport_key):
    m = _BY_SPORT_KEY.get(sport_key)
    if m is None:
        raise KeyError(f"no model registered for sport_key={sport_key!r}")
    return m


def ensure_config(conn):
    """Upsert `sport_config` for every sport key every registered adapter owns."""
    for name, mod in _MODELS.items():
        for key in mod.sport_keys:
            p = mod.gate_params(key)
            conn.execute(
                """INSERT INTO sport_config (sport_key, model, pool, congestion_days, markets)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(sport_key) DO UPDATE SET
                       model=excluded.model, pool=excluded.pool,
                       congestion_days=excluded.congestion_days, markets=excluded.markets""",
                (key, name, p["pool"], p["congestion_days"], json.dumps(p["markets"])),
            )
    conn.commit()
