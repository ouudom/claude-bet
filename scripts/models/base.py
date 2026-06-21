"""models/base.py — the sport-adapter interface every model module implements.

No runtime behavior — lib/registry.py duck-types against this shape. Adding a new
sport means writing one module here exposing the same names; nothing in the
pipeline (fetch/odds_store/results/edge/gates/settle/calibration) has to change.

  sport_keys              tuple[str]   the-odds-api sport keys this module owns
  gate_params(sport_key)  -> {"pool": str, "congestion_days": int|None, "markets": [str]}
  train(conn, ...)        rebuild `rating` (+ `sport_param`) from settled `game` rows
  predict(conn, home, away, ..., sport_key=None) -> dict   fair-line prediction
  model_prob(pred, market, outcome, point, home, away) -> float | None
"""
