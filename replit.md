# Medical Triage Telegram Bot

A Telegram bot that parses Russian-language patient data, calculates an APACHE II score, and returns triage level, mortality risk estimates, and a clinical protocol.

## Run & Operate

- **Run**: `python bot.py`
- **Required env vars**: `TELEGRAM_BOT_TOKEN` (secret from @BotFather)

## Stack

- Python 3.11
- python-telegram-bot 20.7
- pydantic 2.6 — Patient model, field validation, normalization
- No database — stateless, per-user_data session

## Where things live

- `bot.py` — all logic in layered structure:
  - `CONFIG` — configurable thresholds and targets (edit to tune rules)
  - `Patient` (Pydantic) — validation/normalization model, derived features (pf, aa, ideal_weight)
  - `parse()` — regex ingestion layer (Russian free-form text)
  - `sofa_score()`, `apache_score()`, `qsofa_score()` — scoring layer
  - `bayesian_mortality()` — probabilistic risk layer
  - `alerts()` — critical/warning alert engine (🔴/🟡)
  - `decisions()` — rule-based decision engine using CONFIG thresholds
  - `interpret_abg()` — 5-step ABG analysis
  - `dashboard()` — visual ICU monitor display
  - `build_response()` — presentation layer (alerts first, then scores, then actions)

## Architecture decisions

- Layered architecture: Ingestion → Normalization (Pydantic) → Scoring → Bayesian Risk → Alerts → Decisions → Presentation
- CONFIG dict centralises all clinical thresholds — change MAP target, lactate cutoffs, etc. in one place
- Pydantic Patient model validates incoming data (pH range, FiO₂ range, SpO₂ range) and exposes derived features; validation warnings surfaced to clinician in response
- Alerts engine runs independently of decisions — critical flags (🔴) always appear at the top of the response
- APACHE II score is a simplified subset (age, MAP, RR, GCS, creatinine)
- Bayesian risk: APACHE II logistic prior × likelihood ratios for SOFA/lactate/MAP/ΔSOFA
- Token loaded from `TELEGRAM_BOT_TOKEN` env secret, never hardcoded
- Bot runs in polling mode

## Product

- Users send free-form text describing a patient (age, blood pressure, respiratory rate, GCS, creatinine, temperature)
- Bot responds with: triage level (🔴/🟡/🟢), APACHE II score, 24h and 30-day mortality risk, and a clinical action protocol

## User preferences

- Language: Russian for bot responses, code in English/Russian mix as provided

## Gotchas

- Regex expects Cyrillic keywords: `АД`, `ЧД`, `GCS`, `креатинин`, `температура`, `лет/год`
- If no data is parsed, bot prompts the user with the expected format
- `run_polling()` blocks — this process should run as a persistent workflow

## Pointers

- [python-telegram-bot docs](https://docs.python-telegram-bot.org/)
- [APACHE II scoring reference](https://www.mdcalc.com/calc/1868/apache-ii-score)
