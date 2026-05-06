import os
import re
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")


# -----------------------------
# Парсинг
# -----------------------------
def parse_patient(text):
    data = {}

    age = re.search(r"(\d+)\s*(лет|год)", text, re.I)
    if age:
        data["age"] = int(age.group(1))

    bp = re.search(r"АД\s*(\d+)/?(\d*)", text)
    if bp:
        sys_bp = int(bp.group(1))
        dia_bp = int(bp.group(2)) if bp.group(2) else int(sys_bp // 1.5)
        data["sbp"] = sys_bp
        data["map"] = round((sys_bp + 2 * dia_bp) / 3)

    hr = re.search(r"(ЧСС|пульс)\s*(\d+)", text, re.I)
    if hr:
        data["hr"] = int(hr.group(2))

    rr = re.search(r"ЧД\s*(\d+)", text)
    if rr:
        data["rr"] = int(rr.group(1))

    gcs = re.search(r"GCS\s*(\d+)", text)
    if gcs:
        data["gcs"] = int(gcs.group(1))

    temp = re.search(r"температура\s*([\d.]+)", text, re.I)
    if temp:
        data["temp"] = float(temp.group(1))

    spo2 = re.search(r"SpO2\s*(\d+)", text, re.I)
    if spo2:
        data["spo2"] = int(spo2.group(1))

    ph = re.search(r"pH\s*([\d.]+)", text, re.I)
    if ph:
        data["ph"] = float(ph.group(1))

    na = re.search(r"(натрий|Na)\s*(\d+)", text, re.I)
    if na:
        data["sodium"] = int(na.group(2))

    k = re.search(r"(калий|K)\s*([\d.]+)", text, re.I)
    if k:
        data["potassium"] = float(k.group(2))

    wbc = re.search(r"лейкоциты?\s*([\d.]+)", text, re.I)
    if wbc:
        data["wbc"] = float(wbc.group(1))

    cr = re.search(r"креатинин\s*(\d+)", text, re.I)
    if cr:
        data["creatinine"] = int(cr.group(1))

    bil = re.search(r"билирубин\s*(\d+)", text, re.I)
    if bil:
        data["bilirubin"] = int(bil.group(1))

    return data


# -----------------------------
# APACHE II
# -----------------------------
def apache_score(d):
    score = 0

    if "age" in d:
        if d["age"] >= 65:
            score += 6
        elif d["age"] >= 55:
            score += 3

    if "temp" in d:
        t = d["temp"]
        if t >= 41 or t < 30:
            score += 4
        elif t >= 39 or t < 32:
            score += 3
        elif t >= 38.5 or t < 34:
            score += 1

    if "map" in d:
        m = d["map"]
        if m >= 160 or m < 50:
            score += 4
        elif m >= 130 or m < 70:
            score += 3
        elif m >= 110:
            score += 2

    if "hr" in d:
        h = d["hr"]
        if h >= 180 or h < 40:
            score += 4
        elif h >= 140 or h < 55:
            score += 3
        elif h >= 110:
            score += 2

    if "rr" in d:
        r = d["rr"]
        if r >= 50 or r < 6:
            score += 4
        elif r >= 35:
            score += 3
        elif r >= 25 or r < 10:
            score += 1

    if "spo2" in d:
        s = d["spo2"]
        if s < 85:
            score += 4
        elif s < 90:
            score += 3
        elif s < 95:
            score += 1

    if "ph" in d:
        ph = d["ph"]
        if ph >= 7.7 or ph < 7.15:
            score += 4
        elif ph >= 7.6 or ph < 7.25:
            score += 3
        elif ph >= 7.5:
            score += 1
        elif ph < 7.33:
            score += 2

    if "sodium" in d:
        na = d["sodium"]
        if na >= 180 or na <= 110:
            score += 4
        elif na >= 160 or na < 120:
            score += 3
        elif na >= 155 or na < 130:
            score += 2
        elif na >= 150:
            score += 1

    if "potassium" in d:
        k = d["potassium"]
        if k >= 7 or k < 2.5:
            score += 4
        elif k >= 6:
            score += 3
        elif k >= 5.5 or (k >= 3 and k < 3.5):
            score += 1
        elif k < 3:
            score += 2

    if "creatinine" in d:
        cr = d["creatinine"]
        if cr >= 300:
            score += 4
        elif cr >= 170:
            score += 3
        elif cr >= 130:
            score += 2

    if "wbc" in d:
        w = d["wbc"]
        if w >= 40 or w < 1:
            score += 4
        elif w >= 20 or w < 3:
            score += 2
        elif w >= 15:
            score += 1

    if "gcs" in d:
        score += max(0, 15 - d["gcs"])

    return min(score, 71)


# -----------------------------
# SOFA
# -----------------------------
def sofa_score(d):
    score = 0

    # ЦНС — GCS
    if "gcs" in d:
        g = d["gcs"]
        if g < 6:
            score += 4
        elif g < 10:
            score += 3
        elif g < 13:
            score += 2
        elif g < 15:
            score += 1

    # Сердечно-сосудистая — MAP
    if "map" in d and d["map"] < 70:
        score += 1

    # Дыхание — SpO₂ как прокси
    if "spo2" in d:
        s = d["spo2"]
        if s < 85:
            score += 4
        elif s < 90:
            score += 3
        elif s < 94:
            score += 2
        elif s < 97:
            score += 1

    # Печень — билирубин (мкмоль/л)
    if "bilirubin" in d:
        b = d["bilirubin"]
        if b > 204:
            score += 4
        elif b >= 102:
            score += 3
        elif b >= 33:
            score += 2
        elif b >= 20:
            score += 1

    # Почки — креатинин (мкмоль/л)
    if "creatinine" in d:
        cr = d["creatinine"]
        if cr > 440:
            score += 4
        elif cr >= 300:
            score += 3
        elif cr >= 171:
            score += 2
        elif cr >= 110:
            score += 1

    return score


# -----------------------------
# qSOFA
# -----------------------------
def qsofa_score(d):
    score = 0
    if d.get("rr", 0) >= 22:
        score += 1
    if d.get("gcs", 15) < 15:
        score += 1
    if d.get("sbp", 120) <= 100:
        score += 1
    return score


def qsofa_label(score):
    if score >= 2:
        return f"🔴 qSOFA: {score}/3 — высокий риск сепсиса"
    elif score == 1:
        return f"🟡 qSOFA: {score}/3 — наблюдение"
    else:
        return f"🟢 qSOFA: {score}/3 — низкий риск"


# -----------------------------
# Триаж
# -----------------------------
def triage_level(d, apache, sofa):
    if apache >= 25 or sofa >= 8 or d.get("map", 100) < 65 or d.get("gcs", 15) < 10:
        return "🔴 РЕАНИМАЦИЯ"
    elif apache >= 15 or sofa >= 4 or d.get("map", 100) < 75 or d.get("gcs", 15) < 13:
        return "🟡 ИВЛ / ОИМ"
    else:
        return "🟢 ПАЛАТА"


# -----------------------------
# Риск смертности
# -----------------------------
def mortality_risk(apache):
    if apache >= 25:
        return min(99, 50 + apache // 2), min(99, 70 + apache // 3)
    elif apache >= 15:
        return min(99, 20 + apache // 3), min(99, 40 + apache // 4)
    else:
        return apache // 2, apache


# -----------------------------
# Динамический протокол
# -----------------------------
def build_protocol(d):
    steps = [
        "1. Клинический мониторинг",
        "2. В/в доступ + базовые анализы",
        "3. Контроль витals q1h",
    ]
    extras = []
    if d.get("map", 100) < 70:
        extras.append("⚡ Вазопрессоры (норадреналин)")
    if d.get("spo2", 100) < 90:
        extras.append("💨 Кислородотерапия / ИВЛ")
    if d.get("ph", 7.4) < 7.25:
        extras.append("🧪 Коррекция ацидоза (NaHCO₃)")
    if d.get("wbc", 0) >= 15:
        extras.append("🦠 Исключить сепсис — а/б терапия")
    if d.get("potassium", 4) >= 6:
        extras.append("⚠️ Гиперкалиемия — Ca глюконат, ЭКГ")
    if d.get("potassium", 4) < 3:
        extras.append("⚠️ Гипокалиемия — в/в коррекция")
    if d.get("sodium", 140) < 125:
        extras.append("⚠️ Гипонатриемия — ограничение жидкости")
    if "hr" in d and (d["hr"] >= 140 or d["hr"] < 50):
        extras.append("💔 Нарушение ритма — ЭКГ срочно")
    if d.get("bilirubin", 0) >= 33:
        extras.append("🟡 Печёночная дисфункция — контроль функции печени")
    return "\n".join(steps), "\n".join(extras) if extras else None


# -----------------------------
# Форматирование данных
# -----------------------------
def format_data(d):
    labels = {
        "age":        ("Возраст",      "лет"),
        "sbp":        ("АД сист.",     "мм рт.ст."),
        "map":        ("MAP",          "мм рт.ст."),
        "hr":         ("ЧСС",          "/мин"),
        "rr":         ("ЧД",           "/мин"),
        "gcs":        ("GCS",          ""),
        "temp":       ("Температура",  "°C"),
        "spo2":       ("SpO₂",         "%"),
        "ph":         ("pH",           ""),
        "sodium":     ("Натрий",       "ммоль/л"),
        "potassium":  ("Калий",        "ммоль/л"),
        "wbc":        ("Лейкоциты",    "×10⁹/л"),
        "creatinine": ("Креатинин",    "мкмоль/л"),
        "bilirubin":  ("Билирубин",    "мкмоль/л"),
    }
    skip = {"sbp"}
    lines = []
    for k, v in d.items():
        if k in skip:
            continue
        label, unit = labels.get(k, (k, ""))
        lines.append(f"  {label}: {v} {unit}".rstrip())
    return "\n".join(lines)


# -----------------------------
# Инлайн-кнопки
# -----------------------------
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📋 Протокол сепсиса", callback_data="sepsis"),
         InlineKeyboardButton("⚡ Протокол шока",    callback_data="shock")],
        [InlineKeyboardButton("🔄 Пересчитать",      callback_data="recalc"),
         InlineKeyboardButton("❌ Очистить",          callback_data="clear")],
    ])


# -----------------------------
# Формирование ответа
# -----------------------------
def build_response(data):
    apache = apache_score(data)
    sofa = sofa_score(data)
    qsofa = qsofa_score(data)
    level = triage_level(data, apache, sofa)
    r24, r30 = mortality_risk(apache)
    steps, extras = build_protocol(data)

    lines = [
        f"{level}",
        f"📊 APACHE II: {apache}  |  SOFA: {sofa}",
        qsofa_label(qsofa),
        f"⚠️ Риск: {r24}% (24ч)  |  {r30}% (30сут)",
        "",
        "✅ Протокол:",
        steps,
    ]
    if qsofa >= 2:
        lines += ["", "🦠 qSOFA ≥ 2 — исключить сепсис:", "  • Гемокультуры × 2", "  • Лактат крови", "  • А/б в первый час"]
    if extras:
        lines += ["", "🚨 Неотложно:", extras]
    lines += ["", "📋 Данные:", format_data(data)]

    return "\n".join(lines)


# -----------------------------
# Протоколы
# -----------------------------
SEPSIS_TEXT = (
    "🦠 ПРОТОКОЛ СЕПСИСА (Surviving Sepsis Campaign)\n\n"
    "Первый час:\n"
    "  1. Гемокультуры × 2 до антибиотиков\n"
    "  2. Антибиотики широкого спектра — в первый час\n"
    "  3. Кристаллоиды 30 мл/кг при MAP < 65\n"
    "  4. Контроль лактата\n\n"
    "Вазопрессоры:\n"
    "  • Норадреналин 0.01–3 мкг/кг/мин\n"
    "  • Цель: MAP ≥ 65 мм рт.ст.\n\n"
    "Мониторинг:\n"
    "  • Диурез ≥ 0.5 мл/кг/ч\n"
    "  • Лактат через 2 ч (цель < 2 ммоль/л)"
)

SHOCK_TEXT = (
    "⚡ ПРОТОКОЛ ШОКА\n\n"
    "Цели стабилизации:\n"
    "  • MAP ≥ 65 мм рт.ст.\n"
    "  • ЧСС 60–100 /мин\n"
    "  • SpO₂ ≥ 94%\n"
    "  • Диурез ≥ 0.5 мл/кг/ч\n\n"
    "Вазопрессоры:\n"
    "  1. Норадреналин — первая линия\n"
    "  2. Вазопрессин 0.03 ед/мин — при рефрактерном шоке\n"
    "  3. Эпинефрин — при кардиогенном компоненте\n\n"
    "Инфузия:\n"
    "  • Кристаллоиды болюс 250–500 мл, оценить ответ\n"
    "  • Избегать перегрузки — контроль ЦВД / эхо"
)


# -----------------------------
# Обработчики
# -----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Введи данные пациента в свободной форме.\n\n"
        "Поддерживаемые параметры:\n"
        "  • Возраст:     67 лет\n"
        "  • АД:          АД 90/60\n"
        "  • ЧСС / пульс: ЧСС 110\n"
        "  • ЧД:          ЧД 28\n"
        "  • GCS:         GCS 12\n"
        "  • Температура: температура 38.5\n"
        "  • SpO₂:        SpO2 88\n"
        "  • pH:          pH 7.28\n"
        "  • Натрий:      натрий 148  или  Na 148\n"
        "  • Калий:       калий 5.8   или  K 5.8\n"
        "  • Лейкоциты:   лейкоциты 18.5\n"
        "  • Креатинин:   креатинин 250\n"
        "  • Билирубин:   билирубин 45\n\n"
        "Пример:\n"
        "67 лет, АД 90/60, ЧСС 110, ЧД 28, GCS 12, температура 38.5, "
        "SpO2 88, pH 7.28, натрий 148, калий 5.8, лейкоциты 18.5, "
        "креатинин 250, билирубин 45"
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    data = parse_patient(text)

    if not data:
        await update.message.reply_text(
            "Не удалось распознать данные.\n"
            "Отправь /start чтобы увидеть пример."
        )
        return

    context.user_data["last_data"] = data
    await update.message.reply_text(build_response(data), reply_markup=main_keyboard())


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "sepsis":
        await query.message.reply_text(SEPSIS_TEXT)

    elif query.data == "shock":
        await query.message.reply_text(SHOCK_TEXT)

    elif query.data == "recalc":
        data = context.user_data.get("last_data")
        if data:
            await query.message.reply_text(build_response(data), reply_markup=main_keyboard())
        else:
            await query.message.reply_text("Нет сохранённых данных. Введи данные пациента заново.")

    elif query.data == "clear":
        context.user_data.clear()
        await query.message.reply_text("Данные очищены.")


# -----------------------------
# Запуск
# -----------------------------
def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Добавь его в секреты.")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
