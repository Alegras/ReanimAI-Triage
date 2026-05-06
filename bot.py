import os
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


def apache_score(data):
    score = 0

    # Возраст
    if "age" in data:
        if data["age"] >= 65:
            score += 6
        elif data["age"] >= 55:
            score += 3

    # Температура
    if "temp" in data:
        t = data["temp"]
        if t >= 41 or t < 30:
            score += 4
        elif t >= 39 or t < 32:
            score += 3
        elif t >= 38.5 or t < 34:
            score += 1

    # MAP (среднее АД)
    if "map" in data:
        m = data["map"]
        if m >= 160 or m < 50:
            score += 4
        elif m >= 130 or m < 70:
            score += 3
        elif m >= 110:
            score += 2

    # ЧД
    if "rr" in data:
        r = data["rr"]
        if r >= 50 or r < 6:
            score += 4
        elif r >= 35:
            score += 3
        elif r >= 25 or r < 10:
            score += 1

    # GCS
    if "gcs" in data:
        score += max(0, 15 - data["gcs"])

    # Креатинин
    if "creatinine" in data:
        cr = data["creatinine"]
        if cr >= 300:
            score += 4
        elif cr >= 170:
            score += 3
        elif cr >= 130:
            score += 2

    # SpO₂ (приближение оксигенации)
    if "spo2" in data:
        s = data["spo2"]
        if s < 85:
            score += 4
        elif s < 90:
            score += 3
        elif s < 95:
            score += 1

    # Лейкоциты (×10⁹/л)
    if "wbc" in data:
        w = data["wbc"]
        if w >= 40 or w < 1:
            score += 4
        elif w >= 20:
            score += 2
        elif w < 3:
            score += 2
        elif w >= 15:
            score += 1

    # pH крови
    if "ph" in data:
        ph = data["ph"]
        if ph >= 7.7 or ph < 7.15:
            score += 4
        elif ph >= 7.6 or ph < 7.25:
            score += 3
        elif ph >= 7.5:
            score += 1
        elif ph < 7.33:
            score += 2

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

    # SpO₂
    spo2 = re.search(r"SpO2\s*(\d+)", text, re.I)
    if spo2:
        data["spo2"] = int(spo2.group(1))

    # Лейкоциты
    wbc = re.search(r"лейкоциты?\s*([\d.]+)", text, re.I)
    if wbc:
        data["wbc"] = float(wbc.group(1))

    # pH
    ph = re.search(r"pH\s*([\d.]+)", text, re.I)
    if ph:
        data["ph"] = float(ph.group(1))

    return data


def format_data(data):
    labels = {
        "age": "Возраст",
        "map": "MAP (ср. АД)",
        "rr": "ЧД",
        "gcs": "GCS",
        "creatinine": "Креатинин",
        "temp": "Температура",
        "spo2": "SpO₂",
        "wbc": "Лейкоциты",
        "ph": "pH",
    }
    units = {
        "age": "лет",
        "map": "мм рт.ст.",
        "rr": "/мин",
        "creatinine": "мкмоль/л",
        "temp": "°C",
        "spo2": "%",
        "wbc": "×10⁹/л",
    }
    lines = []
    for k, v in data.items():
        label = labels.get(k, k)
        unit = units.get(k, "")
        lines.append(f"  {label}: {v} {unit}".rstrip())
    return "\n".join(lines)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Пришли данные пациента в свободной форме.\n\n"
        "Поддерживаемые параметры:\n"
        "  • Возраст: 67 лет\n"
        "  • АД: АД 90/60\n"
        "  • ЧД: ЧД 28\n"
        "  • GCS: GCS 12\n"
        "  • Креатинин: креатинин 250\n"
        "  • Температура: температура 38.5\n"
        "  • SpO₂: SpO2 88\n"
        "  • Лейкоциты: лейкоциты 18.5\n"
        "  • pH: pH 7.28\n\n"
        "Пример:\n"
        "67 лет, АД 90/60, ЧД 28, GCS 12, креатинин 250, "
        "температура 38.5, SpO2 88, лейкоциты 18.5, pH 7.28"
    )


async def triage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    data = parse_patient(text)

    if not data:
        await update.message.reply_text(
            "Не удалось распознать данные.\n"
            "Отправь /start чтобы увидеть пример."
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
    if "spo2" in data and data["spo2"] < 90:
        protocol.insert(0 if "map" not in data or data["map"] >= 70 else 1,
                        "0. Кислородотерапия / ИВЛ (SpO₂ < 90%)")
    if "ph" in data and data["ph"] < 7.25:
        protocol.append("4. Коррекция ацидоза (NaHCO₃ под контролем)")
    if "wbc" in data and data["wbc"] >= 15:
        protocol.append("4. Исключить сепсис — гемокультуры, антибиотики")

    response = (
        f"{triage_level}\n"
        f"📊 APACHE II: {apache}\n"
        f"⚠️ Риск: {risk_24h}% (24ч) | {risk_30d}% (30сут)\n\n"
        f"✅ Протокол:\n" + "\n".join(protocol) + "\n\n"
        f"📋 Данные пациента:\n{format_data(data)}"
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
