# Medical Triage Telegram Bot

A Telegram bot that parses Russian-language patient data, calculates an APACHE II score, and returns triage level, mortality risk estimates, and a clinical protocol.

## Run & Operate

- **Run**: `python bot.py`
- **Required env vars**: `TELEGRAM_BOT_TOKEN` (secret from @BotFather)

## Stack

- Python 3
- python-telegram-bot 20.7
- No database — stateless, per-message processing

## Where things live

- `bot.py` — all logic: parser, APACHE II scorer, Telegram handlers

## Architecture decisions

- APACHE II score is a simplified subset (age, temperature, MAP, RR, GCS, creatinine) — not the full 12-variable version
- Patient data is parsed from free-form Russian text via regex
- Token is loaded from `TELEGRAM_BOT_TOKEN` environment secret, never hardcoded
- Risk percentages are capped at 99% to avoid absurd output
- Bot runs in polling mode (no webhook needed for this use case)

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
