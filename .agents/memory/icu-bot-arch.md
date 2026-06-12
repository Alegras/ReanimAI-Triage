---
name: ICU bot architecture
description: Single-file bot.py layered architecture; how to add new commands and callbacks
---

# ICU Bot Architecture

**Stack:** Python 3.11, python-telegram-bot 20.7, pydantic 2.6, no DB.

## Layers (in order)
1. Ingestion: `parse(text, pt)` — regex → pt dict
2. Normalization: `Patient.from_dict(pt)` — pydantic validation
3. Scoring: `sofa_score()`, `apache_score()`, `qsofa_score()`
4. Risk: `bayesian_mortality()`
5. Alerts: `alerts(pt)` — 🔴/🟡 threshold checks
6. Decisions: `decisions(pt)` — rule-based actions
7. Presentation: `build_response(pt)` → string

## Adding a new command
1. Write `async def cmd_foo(update, ctx)` function
2. Register: `app.add_handler(CommandHandler("foo", cmd_foo))` in `main()`
3. If needs inline: add `elif q.data == "foo":` in `handle_callback()`
4. Add to `/help` text in `cmd_help()`

## State shape
```python
pt = {
    # clinical fields: age, map, hr, rr, gcs, temp, spo2, pao2, fio2, paco2, hco3,
    #   ph, sodium, potassium, chloride, creatinine, bilirubin, wbc, plt,
    #   lactate, uop, weight, height, rass, bps, glucose
    # derived: sbp, norad_ml_h, norad_mg
    # lists: vasopressors, target_doses, lactate_history, _snapshots
    # dicts: fluids = {intake: [{time,ml,note}], output: [{time,ml,note}]}
    # special: delta_sofa, _base_sofa, skipped_fields, waiting_for, checklist
}
```

## Key globals
- `_admin_ids`: set of admin chat IDs, persisted in `admin.json`
- `_user_ids`: set of all user chat IDs, persisted in `users.json`
- `_stats`: dict with messages/callbacks/uptime
- `CONFIG`: all clinical thresholds and targets

## Watchdog
Runs every 60s via asyncio task in `post_init`. Alerts admins at 3 failures, auto-restarts via `os.execv` at 5.

**Why:** Polling bots can silently lose Telegram connection; watchdog detects and self-heals.
