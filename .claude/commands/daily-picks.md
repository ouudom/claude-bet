---
description: Run the daily Claude betting pipeline (grade → predict → Telegram notify)
---

Run the daily sports-betting pipeline from the project root:

```bash
bash scripts/run_daily.sh
```

This grades yesterday's settled slates (CLV/Brier/ROI → `data/calibration.md`), refreshes
schedule + odds for NBA + top-5 European leagues + UCL + World Cup, retrains the Elo/Poisson
priors, generates Claude-blend value picks for the local game window **5pm today → 12pm
tomorrow** (`edge.py --window 17:00 12:00`), and pushes them to Telegram (`notify.py`).

After it finishes:
- Report how many picks were written and notified (tail the script output / `data/cron.log`).
- If `run_daily.sh` exits non-zero or any step errors, surface the failing step and its
  output — do not silently continue.
- Claude prediction auth comes from the active Claude Code profile; Telegram + odds keys
  come from `.env`. If a required key is missing, say which one.
