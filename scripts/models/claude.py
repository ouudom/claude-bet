"""models/claude.py — Claude-driven fair-line engine with live web research.

The prediction adapter the pipeline dispatches to for BOTH leagues (NBA + soccer).
It is a *blend*, not a replacement: the deterministic Elo (models/nba.py) / Poisson
(models/soccer.py) engines still train and still produce a numeric PRIOR. This module
feeds that prior to Claude (claude-opus-4-8) along with live web evidence — injuries,
lineups, rest/B2B, congestion, form, news — gathered by Claude's server-side
`web_search` tool, and Claude returns a calibrated fair-line adjustment.

Why blend: the Elo/Poisson prior keeps an honest, reproducible math anchor (calibration
stays gradeable); Claude folds in the soft, time-sensitive information a static rating
can't see. We grade both (claude_pred stores Claude's prob AND the prior) so calibration
can tell us whether Claude's edits help or hurt — the continuous-learning loop.

Shape contract (models/base.py): sport_keys, gate_params, train, predict, model_prob.
- predict() makes ONE Claude call per game, persists `claude_pred` + `evidence` rows,
  and returns a pred dict carrying Claude-adjusted parameters.
- model_prob() reads those parameters with the SAME analytic cover math as the priors
  (normal-margin for NBA, Poisson grid for soccer) so any spread line resolves locally
  without another model call.

Auth: resolves from the Claude Code profile / `ant auth login` OAuth (anthropic.Anthropic()
reads the active credential) — no ANTHROPIC_API_KEY required.
"""
import datetime as dt
import json
import math
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from lib import db  # noqa: E402
from models import nba, soccer  # noqa: E402

# This adapter owns every sport key the numeric priors own — registry points here.
sport_keys = nba.sport_keys + soccer.sport_keys

MODEL_ID = "claude-opus-4-8"
PROMPT_VER = "v1"
_NBA_KEYS = set(nba.sport_keys)


def gate_params(sport_key):
    """Delegate gate config to whichever prior owns this sport key (NBA vs soccer)."""
    return (nba if sport_key in _NBA_KEYS else soccer).gate_params(sport_key)


def train(conn, sport_key=None):
    """No-op: the numeric priors (nba.train / soccer.train) own `rating`. Claude reads it."""
    return {}


def _now():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _norm_cdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _calibration_note():
    """Tail of data/calibration.md (if any) so Claude self-corrects from past grades."""
    path = os.path.join(db.ROOT, "data", "calibration.md")
    if not os.path.exists(path):
        return ""
    try:
        with open(path) as f:
            txt = f.read().strip()
    except OSError:
        return ""
    return txt[-2000:]


# --------------------------------------------------------------------------- #
# Claude call
# --------------------------------------------------------------------------- #
def _client():
    import anthropic  # local import: only needed when actually predicting
    return anthropic.Anthropic()


def _ask_claude(home, away, sport_key, commence, prior, is_nba):
    """One web-grounded prediction call. Returns (adjustment_dict, evidence_list)."""
    cal = _calibration_note()
    if is_nba:
        prior_txt = (
            f"Elo prior: home win prob {prior['ml_home']*100:.1f}%, "
            f"fair margin {prior['fair_margin']:+.1f} pts (home)."
        )
        schema = ('{"ml_home": <0..1 home win prob>, '
                  '"fair_margin": <home expected margin, points, + = home favored>, '
                  '"rationale": "<=20 words: the decisive factors"}')
        markets = "NBA moneyline + point spread"
    else:
        prior_txt = (
            f"Poisson prior: 1X2 = home {prior['p_home']*100:.1f}% / "
            f"draw {prior['p_draw']*100:.1f}% / away {prior['p_away']*100:.1f}%, "
            f"expected goals home {prior['lambda_home']:.2f} away {prior['lambda_away']:.2f}."
        )
        schema = ('{"p_home": <0..1>, "p_draw": <0..1>, "p_away": <0..1>, '
                  '"lambda_home": <expected home goals>, "lambda_away": <expected away goals>, '
                  '"rationale": "<=20 words: the decisive factors"}')
        markets = "soccer 1X2 + Asian handicap"

    sys_prompt = (
        "You are a disciplined sports-betting quant. Produce a CALIBRATED fair probability "
        "for an upcoming match, not a hopeful one. Start from the supplied statistical prior "
        "and adjust ONLY for information the prior cannot see — confirmed injuries, lineup/rotation "
        "news, rest/back-to-back or fixture congestion, travel, weather, motivation/stakes, and "
        "recent form. Search the web for the latest before deciding. Be conservative: most games "
        "the prior is already close; move it only with real evidence. Probabilities must be honest "
        "(sum to 1 where applicable). Output ONLY a single JSON object on the final line."
    )
    user = (
        f"Match: {away} @ {home}\nLeague key: {sport_key}\nTip-off (UTC): {commence}\n"
        f"Markets: {markets}\n{prior_txt}\n"
    )
    if cal:
        user += f"\nYour recent calibration (learn from over/under-confidence):\n{cal}\n"
    user += (
        "\nResearch the matchup, then return the adjusted fair line as JSON:\n"
        f"{schema}\nJSON only on the last line."
    )

    client = _client()
    messages = [{"role": "user", "content": user}]
    tools = [{"type": "web_search_20260209", "name": "web_search"}]

    resp = None
    for _ in range(6):  # bound the server-side tool loop (pause_turn continuations)
        resp = client.messages.create(
            model=MODEL_ID,
            max_tokens=4000,
            system=sys_prompt,
            thinking={"type": "adaptive"},
            tools=tools,
            messages=messages,
        )
        if resp.stop_reason == "pause_turn":
            messages = [{"role": "user", "content": user},
                        {"role": "assistant", "content": resp.content}]
            continue
        break

    adj = _parse_json(resp)
    evidence = _extract_evidence(resp)
    return adj, evidence


def _parse_json(resp):
    """Pull the last JSON object out of Claude's text blocks."""
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    matches = re.findall(r"\{[^{}]*\}", text, re.DOTALL)
    for chunk in reversed(matches):
        try:
            return json.loads(chunk)
        except json.JSONDecodeError:
            continue
    return None


def _extract_evidence(resp):
    """Collect (query, url, title, snippet) tuples from web_search tool blocks."""
    out = []
    last_query = None
    for b in resp.content:
        bt = getattr(b, "type", None)
        if bt == "server_tool_use" and getattr(b, "name", "") == "web_search":
            inp = getattr(b, "input", {}) or {}
            last_query = inp.get("query") if isinstance(inp, dict) else None
        elif bt == "web_search_tool_result":
            content = getattr(b, "content", None) or []
            for item in content:
                url = getattr(item, "url", None)
                if url is None:
                    continue
                # page_age is human-readable freshness; encrypted_content is opaque -> skip
                out.append((
                    last_query,
                    url,
                    getattr(item, "title", None),
                    getattr(item, "page_age", None),
                ))
    return out


# --------------------------------------------------------------------------- #
# Adapter interface
# --------------------------------------------------------------------------- #
def predict(conn, home, away, sport_key=None, commence=None, **kw):
    """One Claude call per game. Persists claude_pred + evidence, returns pred dict."""
    is_nba = sport_key in _NBA_KEYS
    base = (nba if is_nba else soccer)
    prior = base.predict(conn, home, away, sport_key=sport_key)

    try:
        adj, evidence = _ask_claude(home, away, sport_key, commence or "unknown", prior, is_nba)
    except Exception as e:  # API/network/parse failure -> fall back to the pure prior
        print(f"  [claude] {away}@{home}: call failed ({e}); using prior unchanged")
        adj, evidence = None, []

    now = _now()
    if is_nba:
        ml_home = _clamp01(_get(adj, "ml_home", prior["ml_home"]))
        fair_margin = _num(_get(adj, "fair_margin", prior["fair_margin"]))
        pred = {"_kind": "nba", "home": home, "away": away,
                "ml_home": ml_home, "ml_away": 1.0 - ml_home, "fair_margin": fair_margin}
        rows = [("h2h", home, None, ml_home, prior["ml_home"])]
    else:
        ph = _clamp01(_get(adj, "p_home", prior["p_home"]))
        pd_ = _clamp01(_get(adj, "p_draw", prior["p_draw"]))
        pa = _clamp01(_get(adj, "p_away", prior["p_away"]))
        s = ph + pd_ + pa or 1.0
        ph, pd_, pa = ph / s, pd_ / s, pa / s          # renormalize to a valid 1X2
        lam_h = max(0.05, _num(_get(adj, "lambda_home", prior["lambda_home"])))
        lam_a = max(0.05, _num(_get(adj, "lambda_away", prior["lambda_away"])))
        grid = [[soccer._poisson_pmf(h, lam_h) * soccer._poisson_pmf(a, lam_a)
                 for a in range(soccer.MAX_GOALS + 1)] for h in range(soccer.MAX_GOALS + 1)]
        pred = {"_kind": "soccer", "home": home, "away": away,
                "p_home": ph, "p_draw": pd_, "p_away": pa,
                "lambda_home": lam_h, "lambda_away": lam_a, "_grid": grid}
        rows = [("h2h", home, None, ph, prior["p_home"]),
                ("h2h", "Draw", None, pd_, prior["p_draw"]),
                ("h2h", away, None, pa, prior["p_away"])]

    rationale = (_get(adj, "rationale", "") or "")[:300] if adj else ""
    pred["rationale"] = rationale

    gid = _game_id(conn, home, away)
    if gid:
        for market, outcome, point, prob, prior_p in rows:
            conn.execute(
                """INSERT INTO claude_pred (game_id, created_at, market, outcome, point,
                                            prob, prior_prob, rationale, model_id, prompt_ver)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (gid, now, market, outcome, point, prob, prior_p, rationale, MODEL_ID, PROMPT_VER),
            )
        for query, url, title, snippet in evidence:
            conn.execute(
                """INSERT INTO evidence (game_id, captured_at, query, source_url, title, snippet)
                   VALUES (?,?,?,?,?,?)""",
                (gid, now, query, url, title, snippet),
            )
        conn.commit()
    return pred


def model_prob(pred, market, outcome, point, home, away):
    """Claude-adjusted fair prob for a candidate. Same cover math as the priors."""
    if pred.get("_kind") == "nba":
        if market == "h2h":
            return pred["ml_home"] if outcome == home else (
                pred["ml_away"] if outcome == away else None)
        if market == "spreads":
            if outcome == home:
                z = (pred["fair_margin"] + point) / nba.MARGIN_SD
            elif outcome == away:
                z = (-pred["fair_margin"] + point) / nba.MARGIN_SD
            else:
                return None
            return _norm_cdf(z)
        return None
    # soccer — reuse the Poisson-grid 1X2 / Asian-handicap math
    return soccer.model_prob(pred, market, outcome, point, home, away)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _get(d, k, default):
    if not isinstance(d, dict) or d.get(k) is None:
        return default
    return d[k]


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp01(v):
    return max(0.0, min(1.0, _num(v)))


def _game_id(conn, home, away):
    row = conn.execute(
        "SELECT game_id FROM game WHERE home=? AND away=? ORDER BY commence DESC LIMIT 1",
        (home, away),
    ).fetchone()
    return row["game_id"] if row else None
