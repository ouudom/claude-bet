"""registry.py — sport_key -> model adapter lookup. The pipeline's single seam for sports.

Prediction now runs through the Claude blend adapter (models/claude.py) for EVERY sport
key: it owns all keys and internally folds the numeric prior (Elo for NBA, Poisson for
soccer) with live web research. So `for_sport` returns `claude` for any registered key.

The numeric priors (models/nba.py, models/soccer.py) are still first-class: they own
`gate_params` (rest/B2B vs fixture-congestion differ per sport) and they still train
`rating`, which claude.py reads. `ensure_config` therefore enumerates the priors to write
per-sport gate config; `_PRIORS` is the place to register sport #N's gate owner.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from models import claude, nba, soccer  # noqa: E402

# Gate-param / training owners, keyed by registry name (used by ensure_config).
_PRIORS = {"nba": nba, "soccer": soccer}

# sport_key -> the prior that owns its gate params.
_PRIOR_BY_KEY = {key: mod for mod in _PRIORS.values() for key in mod.sport_keys}


def for_sport(sport_key):
    """Prediction adapter for a sport key — always the Claude blend (owns all keys)."""
    if sport_key not in _PRIOR_BY_KEY:
        raise KeyError(f"no model registered for sport_key={sport_key!r}")
    return claude


def prior_for(sport_key):
    """The numeric prior adapter for a sport key (for --train and gate params)."""
    m = _PRIOR_BY_KEY.get(sport_key)
    if m is None:
        raise KeyError(f"no prior registered for sport_key={sport_key!r}")
    return m


def ensure_config(conn):
    """Upsert `sport_config` for every sport key. model='claude' (the predictor)."""
    for mod in _PRIORS.values():
        for key in mod.sport_keys:
            p = mod.gate_params(key)
            conn.execute(
                """INSERT INTO sport_config (sport_key, model, pool, congestion_days, markets)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(sport_key) DO UPDATE SET
                       model=excluded.model, pool=excluded.pool,
                       congestion_days=excluded.congestion_days, markets=excluded.markets""",
                (key, "claude", p["pool"], p["congestion_days"], json.dumps(p["markets"])),
            )
    conn.commit()
