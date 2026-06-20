#!/usr/bin/env bash
# Cross-environment Python launcher (mirrors swing-trading/scripts/pyrun.sh).
#   - macOS local: project .venv if its interpreter has the deps.
#   - Linux sandbox: system python3 + persistent .pydeps.
# Usage:  bash scripts/pyrun.sh scripts/odds_store.py
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

VENV_PY="$ROOT/.venv/bin/python"
if [ -x "$VENV_PY" ] && "$VENV_PY" -c 'import requests, dotenv' >/dev/null 2>&1; then
  exec "$VENV_PY" "$@"
fi

if [ "${1:-}" = "--setup" ]; then
  echo "[pyrun] installing deps into $ROOT/.pydeps (from requirements.txt) ..."
  python3 -m pip install --target="$ROOT/.pydeps" -r "$ROOT/requirements.txt"
  echo "[pyrun] done."
  exit 0
fi
export PYTHONPATH="$ROOT/.pydeps${PYTHONPATH:+:$PYTHONPATH}"
exec python3 "$@"
