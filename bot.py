import os
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def apache_score(data):
    score = 0

    if "age" in data:
        if data["age"] >= 65:
            score += 6
        elif data["age"] >= 55:
            score += 3

    if "temp" in data and data["temp"] > 39:
        score += 4

    if "map" in data and data["map"] < 70:
        score += 4

    if "rr" in data:
        if data["rr"] > 35:
            score += 4
        elif data["rr"] > 25:
            score += 3

    if "gcs" in data:
        score += max(0, 15 - data["gcs"])

    if "creatinine" in data and data["creatinine"] > 220:
        score += 6

    return min(score, 71)


def parse_patient(text):
    data = {}

    age = re.search(r"(\d+)\s*(лет|год)", text, re.I)
    if age:
        data["age"] = int(age.group(1))

    bp = re.search(r"АД\s*(\d+)/?(\d*)", text)
    if bp:
        sys_bp = int(bp.group(1))
        dia_bp = int(bp.group(2)) if bp.group(2) else int(sys_bp // 1.5)
        data["map"] = round((sys_bp + 2 * dia_bp) / 3)

    rr = re.search(r"ЧД\s*(\d+)", text)
    if rr:
        data["rr"] = int(rr.group(1))

    gcs = re.search(r"GCS\s*(\d+)", text)
    if gcs:
        data["gcs"] = int(gcs.group(1))

    cr = re.search(r"креатинин\s*(\d+)", text, re.I)
    if cr:
        data["creatinine"] = int(cr.group(1))

    temp = re.search(r"температура\s*([\d.]+)", text, re.I)
    if temp:
        data["temp"] = float(temp.group(1))

    return data


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Пришли данные пациента!\n\n"
        "Пример:\n"
        "Пациент, 67 лет, АД 90/60, ЧД 28, GCS 12, "
        "креатинин 250, температура 38.5"
    )


async def triage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text

    data = parse_patient(text)
    if not data:
        await update.message.reply_text(
            "Не удалось распознать данные.\n"
            "Укажи: возраст, АД, ЧД, GCS, креатинин, температуру"
        )
        return

    apache = apache_score(data)

    if apache >= 25:
        triage_level = "🔴 РЕАНИМАЦИЯ"
        risk_24h = min(99, 50 + apache // 2)
        risk_30d = min(99, 70 + apache // 3)
    elif apache >= 15:
        triage_level = "🟡 ИВЛ / ОИМ"
        risk_24h = min(99, 20 + apache // 3)
        risk_30d = min(99, 40 + apache // 4)
    else:
        triage_level = "🟢 ПАЛАТА"
        risk_24h = apache // 2
        risk_30d = apache

    protocol = [
        "1. Клинический мониторинг",
        "2. В/в доступ + базовые анализы",
        "3. Контроль витals q1h",
    ]

    if "map" in data and data["map"] < 70:
        protocol.insert(0, "0. Вазопрессоры (норадреналин)")

    parsed_lines = "\n".join(f"  {k}: {v}" for k, v in data.items())

    response = (
        f"{triage_level}\n"
        f"📊 APACHE II: {apache}\n"
        f"⚠️ Риск: {risk_24h}% (24ч) | {risk_30d}% (30сут)\n\n"
        f"✅ Протокол:\n" + "\n".join(protocol) + "\n\n"
        f"📋 Распознанные данные:\n{parsed_lines}"
    )

    await update.message.reply_text(response)


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Добавь его в секреты.")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, triage))
    app.run_polling()


if __name__ == "__main__":
    main()
