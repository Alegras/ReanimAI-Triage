import os
import re
import math
from datetime import datetime
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


# =============================
# STATE
# =============================
def get_pt(ctx):
    if "pt" not in ctx.user_data:
        ctx.user_data["pt"] = {"lactate_history": []}
    return ctx.user_data["pt"]


# =============================
# ПАРСИНГ (накопительный)
# =============================
def parse(text, pt):
    def grab(pattern, key, cast=float):
        m = re.search(pattern, text, re.I)
        if m:
            pt[key] = cast(m.group(1))

    # Демография
    grab(r"(\d+)\s*(лет|год)", "age", int)
    grab(r"рост\s*(\d+)", "height", int)

    # Гемодинамика
    bp = re.search(r"АД\s*(\d+)/?(\d*)", text)
    if bp:
        sys_bp = int(bp.group(1))
        dia_bp = int(bp.group(2)) if bp.group(2) else int(sys_bp // 1.5)
        pt["sbp"] = sys_bp
        pt["map"] = round((sys_bp + 2 * dia_bp) / 3)

    hr = re.search(r"(ЧСС|пульс)\s*(\d+)", text, re.I)
    if hr:
        pt["hr"] = int(hr.group(2))

    # Дыхание
    grab(r"ЧД\s*(\d+)", "rr", int)
    grab(r"SpO2\s*(\d+)", "spo2", int)
    grab(r"PaO2\s*(\d+)", "pao2", int)
    grab(r"FiO2\s*([\d.]+)", "fio2")
    grab(r"PaCO2\s*(\d+)", "paco2", int)
    grab(r"HCO3\s*(\d+)", "hco3", int)

    # Неврология
    grab(r"GCS\s*(\d+)", "gcs", int)

    # Температура
    grab(r"температура\s*([\d.]+)", "temp")

    # pH
    grab(r"pH\s*([\d.]+)", "ph")

    # Электролиты
    na = re.search(r"(натрий|Na)\s*(\d+)", text, re.I)
    if na:
        pt["sodium"] = int(na.group(2))
    k = re.search(r"(калий|K)\s*([\d.]+)", text, re.I)
    if k:
        pt["potassium"] = float(k.group(2))

    # Лаборатория
    grab(r"креатинин\s*(\d+)", "creatinine", int)
    grab(r"билирубин\s*(\d+)", "bilirubin", int)
    grab(r"лейкоциты?\s*([\d.]+)", "wbc")
    grab(r"тромбоцит[ы]*\s*(\d+)", "plt", int)

    # Перфузия
    if re.search(r"лактат\s*([\d.]+)", text, re.I):
        val = float(re.search(r"лактат\s*([\d.]+)", text, re.I).group(1))
        pt["lactate"] = val
        pt["lactate_history"].append(val)

    grab(r"диурез\s*([\d.]+)", "uop")

    return pt


# =============================
# РАСЧЁТЫ
# =============================

def pbw(height, male=True):
    if not height:
        return None
    return round((50 if male else 45.5) + 0.91 * (height - 152.4), 1)


def aa_gradient(pt):
    if "pao2" in pt and "fio2" in pt and "paco2" in pt:
        pao2_alv = pt["fio2"] * (760 - 47) - pt["paco2"] / 0.8
        return round(pao2_alv - pt["pao2"], 1)
    return None


def pf_ratio(pt):
    if "pao2" in pt and "fio2" in pt and pt["fio2"] > 0:
        return round(pt["pao2"] / pt["fio2"], 1)
    return None


def lactate_clearance(pt):
    h = pt.get("lactate_history", [])
    if len(h) >= 2 and h[-2] > 0:
        return round((h[-2] - h[-1]) / h[-2] * 100, 1)
    return None


def shock_type(pt):
    if pt.get("lactate", 0) > 2 and pt.get("map", 100) < 65:
        return "дистрибутивный (септический)"
    if pt.get("uop", 1) < 0.5:
        return "гиповолемический"
    return None


# =============================
# APACHE II (полный, 12 параметров)
# =============================
def apache_score(pt):
    s = 0

    if "age" in pt:
        if pt["age"] >= 65: s += 6
        elif pt["age"] >= 55: s += 3

    if "temp" in pt:
        t = pt["temp"]
        if t >= 41 or t < 30: s += 4
        elif t >= 39 or t < 32: s += 3
        elif t >= 38.5 or t < 34: s += 1

    if "map" in pt:
        m = pt["map"]
        if m >= 160 or m < 50: s += 4
        elif m >= 130 or m < 70: s += 3
        elif m >= 110: s += 2

    if "hr" in pt:
        h = pt["hr"]
        if h >= 180 or h < 40: s += 4
        elif h >= 140 or h < 55: s += 3
        elif h >= 110: s += 2

    if "rr" in pt:
        r = pt["rr"]
        if r >= 50 or r < 6: s += 4
        elif r >= 35: s += 3
        elif r >= 25 or r < 10: s += 1

    pf = pf_ratio(pt)
    if pf is not None:
        if pf < 100: s += 4
        elif pf < 200: s += 3
        elif pf < 300: s += 2
    elif "spo2" in pt:
        sp = pt["spo2"]
        if sp < 85: s += 4
        elif sp < 90: s += 3
        elif sp < 95: s += 1

    if "ph" in pt:
        ph = pt["ph"]
        if ph >= 7.7 or ph < 7.15: s += 4
        elif ph >= 7.6 or ph < 7.25: s += 3
        elif ph >= 7.5: s += 1
        elif ph < 7.33: s += 2

    if "sodium" in pt:
        na = pt["sodium"]
        if na >= 180 or na <= 110: s += 4
        elif na >= 160 or na < 120: s += 3
        elif na >= 155 or na < 130: s += 2
        elif na >= 150: s += 1

    if "potassium" in pt:
        k = pt["potassium"]
        if k >= 7 or k < 2.5: s += 4
        elif k >= 6: s += 3
        elif k >= 5.5 or (3 <= k < 3.5): s += 1
        elif k < 3: s += 2

    if "creatinine" in pt:
        cr = pt["creatinine"]
        if cr >= 300: s += 4
        elif cr >= 170: s += 3
        elif cr >= 130: s += 2

    if "wbc" in pt:
        w = pt["wbc"]
        if w >= 40 or w < 1: s += 4
        elif w >= 20 or w < 3: s += 2
        elif w >= 15: s += 1

    if "gcs" in pt:
        s += max(0, 15 - pt["gcs"])

    return min(s, 71)


# =============================
# SOFA (полный, с PLT и PaO2/FiO2)
# =============================
def sofa_score(pt):
    s = 0

    pf = pf_ratio(pt)
    if pf is not None:
        if pf < 100: s += 4
        elif pf < 200: s += 3
        elif pf < 300: s += 2
        elif pf < 400: s += 1
    elif "spo2" in pt:
        sp = pt["spo2"]
        if sp < 85: s += 4
        elif sp < 90: s += 3
        elif sp < 94: s += 2
        elif sp < 97: s += 1

    if "plt" in pt:
        p = pt["plt"]
        if p < 20: s += 4
        elif p < 50: s += 3
        elif p < 100: s += 2
        elif p < 150: s += 1

    if "bilirubin" in pt:
        b = pt["bilirubin"]
        if b >= 204: s += 4
        elif b >= 102: s += 3
        elif b >= 33: s += 2
        elif b >= 20: s += 1

    if pt.get("map", 100) < 70:
        s += 1

    if "gcs" in pt:
        g = pt["gcs"]
        if g < 6: s += 4
        elif g < 10: s += 3
        elif g < 13: s += 2
        elif g < 15: s += 1

    if "creatinine" in pt:
        cr = pt["creatinine"]
        if cr > 440: s += 4
        elif cr >= 300: s += 3
        elif cr >= 171: s += 2
        elif cr >= 110: s += 1

    return s


# =============================
# qSOFA
# =============================
def qsofa_score(pt):
    s = 0
    if pt.get("rr", 0) >= 22: s += 1
    if pt.get("gcs", 15) < 15: s += 1
    if pt.get("sbp", 120) <= 100: s += 1
    return s


# =============================
# Sepsis-3
# =============================
def is_sepsis(pt, sofa):
    return sofa >= 2 and (pt.get("temp", 36) > 38 or pt.get("lactate", 0) >= 2)


# =============================
# Триаж
# =============================
def triage_level(pt, apache, sofa):
    if apache >= 25 or sofa >= 8 or pt.get("map", 100) < 65 or pt.get("gcs", 15) < 10:
        return "🔴 РЕАНИМАЦИЯ"
    elif apache >= 15 or sofa >= 4 or pt.get("map", 100) < 75 or pt.get("gcs", 15) < 13:
        return "🟡 ИВЛ / ОИМ"
    return "🟢 ПАЛАТА"


# =============================
# Смертность (байесовская модель)
# =============================
def bayesian_mortality(pt, apache, sofa, delta_sofa=None):
    logit = -3.5 + 0.15 * apache
    prior = 1 / (1 + math.exp(-logit))

    lr = 1.0

    if sofa >= 8:
        lr *= 2.5
    elif sofa >= 4:
        lr *= 1.5

    if pt.get("lactate", 0) >= 4:
        lr *= 2.0
    elif pt.get("lactate", 0) >= 2:
        lr *= 1.3

    if pt.get("map", 100) < 65:
        lr *= 1.8

    if delta_sofa is not None:
        if delta_sofa > 2:
            lr *= 2.2
        elif delta_sofa > 0:
            lr *= 1.3
        elif delta_sofa < 0:
            lr *= 0.7

    odds = prior / (1 - prior)
    post_odds = odds * lr
    posterior = post_odds / (1 + post_odds)

    p30 = min(99, round(posterior * 100))
    p24 = min(99, round(p30 * 0.45))
    return p24, p30


# =============================
# Решения (Decision Engine)
# =============================
def decisions(pt):
    sofa = sofa_score(pt)
    apache = apache_score(pt)
    pf = pf_ratio(pt)
    rec = []

    # ── Гемодинамика ──────────────────────────────────────────
    if pt.get("map", 100) < 65:
        rec.append(
            "Шок: норадреналин 0.05→0.3 мкг/кг/мин, "
            "цель MAP ≥ 65; при рефрактерности — вазопрессин 0.03 ед/мин"
        )
    elif pt.get("map", 100) < 75 and pt.get("lactate", 0) >= 2:
        rec.append("MAP 65–75 + лактат ↑ — болюс кристаллоидов 500 мл, переоценить через 30 мин")

    # ── Дыхание / оксигенация ─────────────────────────────────
    if pf is not None and pf < 100:
        w = pbw(pt.get("height"))
        vt = f"{int(w * 6)} мл" if w else "≈6 мл/кг ИМТ"
        rec.append(
            f"Тяжёлый ARDS (PF {pf}): VT {vt}, PEEP escalation по ARDSNet, "
            f"Pplat < 30; прон-позиция ≥ 16 ч"
        )
    elif pf is not None and pf < 150:
        w = pbw(pt.get("height"))
        vt = f"{int(w * 6)} мл" if w else "≈6 мл/кг ИМТ"
        rec.append(
            f"ARDS (PF {pf}): VT {vt}, PEEP по ARDSNet, Pplat < 30"
        )
    elif pt.get("gcs", 15) <= 8:
        rec.append("GCS ≤ 8 — оценить защиту дыхательных путей, показания к интубации")

    if pt.get("spo2", 100) < 90 and pf is None:
        rec.append("SpO₂ < 90% — высокопоточная O₂ или НИВ, контроль ABG")

    # ── Перфузия / лактат ─────────────────────────────────────
    if pt.get("lactate", 0) >= 4:
        rec.append(
            "Лактат ≥ 4 — агрессивная ресусцитация, контроль каждые 2 ч; "
            "цель клиренс > 10%"
        )
    elif pt.get("lactate", 0) >= 2:
        rec.append(
            "Лактат 2–4 — кристаллоиды 30 мл/кг, контроль каждые 2–4 ч; "
            "цель клиренс > 10%"
        )

    # ── Инфекция / сепсис ─────────────────────────────────────
    if sofa >= 2 and (pt.get("temp", 36) > 38 or pt.get("wbc", 0) >= 12 or pt.get("lactate", 0) >= 2):
        rec.append(
            "Sepsis bundle: гемокультуры × 2 → антибиотики < 1 ч → "
            "source control; лактат, диурез"
        )
    elif pt.get("wbc", 0) >= 15 or pt.get("temp", 36) > 38.5:
        rec.append("Гемокультуры × 2, антибиотики широкого спектра — в первый час")

    # ── Почки ─────────────────────────────────────────────────
    if pt.get("creatinine", 0) > 300 or pt.get("uop", 1) < 0.3:
        rec.append(
            "ОПП тяжёлое — нефролог, рассмотреть ЗПТ (CRRT); "
            "диурез ≥ 0.5 мл/кг/ч, отменить нефротоксины"
        )
    elif pt.get("creatinine", 0) > 200:
        rec.append(
            "ОПП — контроль диуреза, рассмотреть ЗПТ при нарастании; "
            "избегать нефротоксинов"
        )

    # ── Электролиты / КЩС ────────────────────────────────────
    if pt.get("potassium", 4) >= 6:
        rec.append("Гиперкалиемия — Ca глюконат 10% 10 мл в/в, ЭКГ, инсулин + глюкоза")
    elif pt.get("potassium", 4) < 3:
        rec.append("Гипокалиемия — KCl в/в под ЭКГ-контролем, не > 20 мэкв/ч")

    if pt.get("sodium", 140) < 125:
        rec.append("Гипонатриемия — ограничение жидкости, 3% NaCl при симптомах")

    if pt.get("ph", 7.4) < 7.20:
        rec.append("Тяжёлый ацидоз — NaHCO₃ 1–2 ммоль/кг при pH < 7.1, контроль ABG")
    elif pt.get("ph", 7.4) < 7.25:
        rec.append("Ацидоз — контроль ABG, устранить причину; NaHCO₃ при pH < 7.1")

    return rec[:6]


# =============================
# Форматирование данных
# =============================
def format_data(pt):
    labels = {
        "age":        ("Возраст",      "лет"),
        "height":     ("Рост",         "см"),
        "sbp":        ("АД сист.",     "мм рт.ст."),
        "map":        ("MAP",          "мм рт.ст."),
        "hr":         ("ЧСС",          "/мин"),
        "rr":         ("ЧД",           "/мин"),
        "gcs":        ("GCS",          ""),
        "temp":       ("Температура",  "°C"),
        "spo2":       ("SpO₂",         "%"),
        "pao2":       ("PaO₂",         "мм рт.ст."),
        "fio2":       ("FiO₂",         ""),
        "paco2":      ("PaCO₂",        "мм рт.ст."),
        "hco3":       ("HCO₃",         "ммоль/л"),
        "ph":         ("pH",           ""),
        "sodium":     ("Натрий",       "ммоль/л"),
        "potassium":  ("Калий",        "ммоль/л"),
        "wbc":        ("Лейкоциты",    "×10⁹/л"),
        "plt":        ("Тромбоциты",   "×10⁹/л"),
        "creatinine": ("Креатинин",    "мкмоль/л"),
        "bilirubin":  ("Билирубин",    "мкмоль/л"),
        "lactate":    ("Лактат",       "ммоль/л"),
        "uop":        ("Диурез",       "мл/кг/ч"),
    }
    skip = {"sbp", "lactate_history"}
    lines = []
    for k, v in pt.items():
        if k in skip:
            continue
        label, unit = labels.get(k, (k, ""))
        lines.append(f"  {label}: {v} {unit}".rstrip())
    return "\n".join(lines)


# =============================
# Построение ответа
# =============================
def build_response(pt):
    apache = apache_score(pt)
    sofa = sofa_score(pt)
    qsofa = qsofa_score(pt)
    level = triage_level(pt, apache, sofa)
    delta_sofa = pt.get("delta_sofa")
    r24, r30 = bayesian_mortality(pt, apache, sofa, delta_sofa)
    sep = is_sepsis(pt, sofa)
    shock = shock_type(pt)
    lc = lactate_clearance(pt)
    aa = aa_gradient(pt)
    pf = pf_ratio(pt)
    acts = decisions(pt)

    qsofa_line = (
        f"🔴 qSOFA: {qsofa}/3 — высокий риск сепсиса" if qsofa >= 2
        else f"🟡 qSOFA: {qsofa}/3 — наблюдение" if qsofa == 1
        else f"🟢 qSOFA: {qsofa}/3"
    )

    sofa_line = f"📊 APACHE II: {apache}  |  SOFA: {sofa}"
    if delta_sofa is not None:
        arrow = "↑" if delta_sofa > 0 else ("↓" if delta_sofa < 0 else "→")
        sofa_line += f"  ({arrow}{abs(delta_sofa):+d})"

    lines = [
        level,
        sofa_line,
        qsofa_line,
        f"⚠️ Риск*: {r24}% (24ч)  |  {r30}% (30сут)",
        f"🧠 Sepsis-3: {'ДА' if sep else 'нет'}",
    ]

    if shock:
        lines.append(f"🫀 Шок: {shock}")
    if pf is not None:
        lines.append(f"🫁 PaO₂/FiO₂: {pf}")
    if aa is not None:
        lines.append(f"🫁 A-a градиент: {aa}")
    if lc is not None:
        trend = "↓" if lc > 0 else "↑"
        lines.append(f"📉 Лактат-клиренс: {lc}% {trend}")
    if delta_sofa is not None:
        lines.append(f"📈 ΔSOFA: {delta_sofa:+d} — {'ухудшение' if delta_sofa > 0 else 'улучшение' if delta_sofa < 0 else 'стабильно'}")

    if acts:
        lines.append("\n✅ Решения:")
        for i, a in enumerate(acts, 1):
            lines.append(f"  {i}. {a}")

    if qsofa >= 2:
        lines += ["\n🦠 qSOFA ≥ 2 — скрининг сепсиса:", "  • Гемокультуры × 2", "  • Лактат", "  • А/б — в первый час"]

    lines += ["\n📋 Данные:", format_data(pt)]
    lines.append("\n* байесовская оценка: APACHE II prior + SOFA / лактат / MAP / ΔSOFA")
    return "\n".join(lines)


# =============================
# ПРОТОКОЛЫ
# =============================
SEPSIS_TEXT = (
    "🦠 ПРОТОКОЛ СЕПСИСА (Surviving Sepsis Campaign)\n\n"
    "Первый час:\n"
    "  1. Гемокультуры × 2 до антибиотиков\n"
    "  2. Антибиотики широкого спектра — немедленно\n"
    "  3. Кристаллоиды 30 мл/кг при MAP < 65\n"
    "  4. Лактат — измерить, повторить ч/з 2 ч\n\n"
    "Вазопрессоры:\n"
    "  • Норадреналин 0.01–3 мкг/кг/мин\n"
    "  • Цель: MAP ≥ 65 мм рт.ст.\n"
    "  • Вазопрессин 0.03 ед/мин при рефрактерности\n\n"
    "Мониторинг:\n"
    "  • Диурез ≥ 0.5 мл/кг/ч\n"
    "  • Лактат < 2 ммоль/л — цель\n"
    "  • ScvO₂ ≥ 70%"
)

SHOCK_TEXT = (
    "⚡ ПРОТОКОЛ ШОКА\n\n"
    "Цели:\n"
    "  • MAP ≥ 65 мм рт.ст.\n"
    "  • ЧСС 60–100 /мин\n"
    "  • SpO₂ ≥ 94%\n"
    "  • Диурез ≥ 0.5 мл/кг/ч\n\n"
    "Вазопрессоры (по приоритету):\n"
    "  1. Норадреналин — первая линия\n"
    "  2. Вазопрессин 0.03 ед/мин\n"
    "  3. Эпинефрин — при кардиогенном компоненте\n\n"
    "Инфузия:\n"
    "  • Болюс 250–500 мл, оценить ответ\n"
    "  • Контроль волемии: ЦВД / ЭхоКГ"
)

VENT_TEXT = (
    "🫁 ИВЛ — ARDSNet протокол\n\n"
    "Параметры:\n"
    "  • VT: 6 мл/кг ИМТ (идеальная масса)\n"
    "  • Pplat ≤ 30 см H₂O\n"
    "  • DP (driving pressure) ≤ 15 см H₂O\n"
    "  • PEEP: по таблице ARDSNet (FiO₂/PEEP)\n"
    "  • ЧД: 14–35 /мин, рСО₂ цель 35–45\n\n"
    "PaO₂/FiO₂:\n"
    "  • > 300 — норма\n"
    "  • 200–300 — лёгкий ARDS\n"
    "  • 100–200 — умеренный\n"
    "  • < 100 — тяжёлый → прон-позиция ≥ 16 ч\n\n"
    "ПБМ (мужчины): 50 + 0.91 × (рост − 152.4)\n"
    "ПБМ (женщины): 45.5 + 0.91 × (рост − 152.4)"
)

PRESS_TEXT = (
    "💉 ВАЗОПРЕССОРЫ\n\n"
    "1. Норадреналин (первая линия)\n"
    "   0.01–3 мкг/кг/мин, цель MAP ≥ 65\n\n"
    "2. Вазопрессин\n"
    "   0.03–0.04 ед/мин — при рефрактерном шоке\n\n"
    "3. Эпинефрин\n"
    "   0.01–1 мкг/кг/мин — кардиогенный компонент\n\n"
    "4. Добутамин\n"
    "   2–20 мкг/кг/мин — при снижении СВ\n\n"
    "Мониторинг:\n"
    "  • АД инвазивное (A-line)\n"
    "  • Лактат каждые 2 ч\n"
    "  • Диурез ч/з мочевой катетер"
)

ABG_TEXT = (
    "🧪 ABG — интерпретация\n\n"
    "Нормы:\n"
    "  pH: 7.35–7.45\n"
    "  PaCO₂: 35–45 мм рт.ст.\n"
    "  HCO₃: 22–26 ммоль/л\n"
    "  PaO₂: 80–100 мм рт.ст.\n\n"
    "Введи данные для пересчёта:\n"
    "pH 7.28, PaCO2 38, HCO3 18, PaO2 65, FiO2 0.5"
)

LAC_TEXT = (
    "📉 ЛАКТАТ\n\n"
    "Интерпретация:\n"
    "  < 2.0 ммоль/л — норма\n"
    "  2–4 ммоль/л — гиперлактатемия\n"
    "  > 4 ммоль/л — лактат-ацидоз\n\n"
    "Клиренс (цель ≥ 10% за 2 ч):\n"
    "  (Лактат₁ − Лактат₂) / Лактат₁ × 100%\n\n"
    "Введи повторный лактат в сообщении:\n"
    "лактат 1.8"
)


# =============================
# ЭКСПОРТ
# =============================
def build_export(pt):
    apache = apache_score(pt)
    sofa = sofa_score(pt)
    qsofa = qsofa_score(pt)
    level = triage_level(pt, apache, sofa)
    delta_sofa = pt.get("delta_sofa")
    r24, r30 = bayesian_mortality(pt, apache, sofa, delta_sofa)
    sep = is_sepsis(pt, sofa)
    shock = shock_type(pt)
    lc = lactate_clearance(pt)
    aa = aa_gradient(pt)
    pf = pf_ratio(pt)
    acts = decisions(pt)
    now = datetime.now().strftime("%d.%m.%Y %H:%M")

    labels = {
        "age":        ("Возраст",      "лет"),
        "height":     ("Рост",         "см"),
        "sbp":        ("АД сист.",     "мм рт.ст."),
        "map":        ("MAP",          "мм рт.ст."),
        "hr":         ("ЧСС",          "/мин"),
        "rr":         ("ЧД",           "/мин"),
        "gcs":        ("GCS",          ""),
        "temp":       ("Температура",  "°C"),
        "spo2":       ("SpO₂",         "%"),
        "pao2":       ("PaO₂",         "мм рт.ст."),
        "fio2":       ("FiO₂",         ""),
        "paco2":      ("PaCO₂",        "мм рт.ст."),
        "hco3":       ("HCO₃",         "ммоль/л"),
        "ph":         ("pH",           ""),
        "sodium":     ("Натрий",       "ммоль/л"),
        "potassium":  ("Калий",        "ммоль/л"),
        "wbc":        ("Лейкоциты",    "×10⁹/л"),
        "plt":        ("Тромбоциты",   "×10⁹/л"),
        "creatinine": ("Креатинин",    "мкмоль/л"),
        "bilirubin":  ("Билирубин",    "мкмоль/л"),
        "lactate":    ("Лактат",       "ммоль/л"),
        "uop":        ("Диурез",       "мл/кг/ч"),
    }
    skip = {"sbp", "lactate_history"}

    vitals_keys = {"age", "height", "sbp", "map", "hr", "rr", "gcs", "temp", "spo2", "uop"}
    abg_keys = {"pao2", "fio2", "paco2", "hco3", "ph"}
    lab_keys = {"sodium", "potassium", "wbc", "plt", "creatinine", "bilirubin", "lactate"}

    def section(keys):
        lines = []
        for k in keys:
            if k in pt and k not in skip:
                label, unit = labels.get(k, (k, ""))
                lines.append(f"  {label}: {pt[k]} {unit}".rstrip())
        return "\n".join(lines) if lines else "  —"

    lines = [
        "=" * 36,
        "     КАРТА ПАЦИЕНТА — ICU CDSS",
        f"     {now}",
        "=" * 36,
        "",
        f"ТРИАЖ:    {level.replace('🔴 ', '').replace('🟡 ', '').replace('🟢 ', '')}",
        f"APACHE II: {apache}",
        f"SOFA:      {sofa}",
        f"qSOFA:     {qsofa}/3",
        f"Риск:      {r24}% (24ч) / {r30}% (30сут)",
        f"Sepsis-3:  {'ДА' if sep else 'нет'}",
    ]

    if shock:
        lines.append(f"Шок:       {shock}")
    if pf is not None:
        lines.append(f"PaO₂/FiO₂: {pf}")
    if aa is not None:
        lines.append(f"A-a град.: {aa}")
    if lc is not None:
        lines.append(f"Лактат-кл: {lc}%")
    if delta_sofa is not None:
        lines.append(f"ΔSOFA:     {delta_sofa:+d}")

    lines += [
        "",
        "── ВИТАЛЬНЫЕ ПОКАЗАТЕЛИ ──────────",
        section(vitals_keys),
        "",
        "── ГАЗЫ КРОВИ (ABG) ──────────────",
        section(abg_keys),
        "",
        "── ЛАБОРАТОРНЫЕ ДАННЫЕ ───────────",
        section(lab_keys),
    ]

    if acts:
        lines += ["", "── РЕКОМЕНДАЦИИ ──────────────────"]
        for i, a in enumerate(acts, 1):
            lines.append(f"  {i}. {a}")

    lh = pt.get("lactate_history", [])
    if len(lh) >= 2:
        lines += ["", "── ДИНАМИКА ЛАКТАТА ──────────────"]
        for i, v in enumerate(lh, 1):
            lines.append(f"  [{i}] {v} ммоль/л")

    lines += ["", "=" * 36]
    return "\n".join(lines)


# =============================
# КНОПКИ
# =============================
def main_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧮 Пересчитать",      callback_data="recalc"),
         InlineKeyboardButton("❌ Сброс",             callback_data="clear")],
        [InlineKeyboardButton("🦠 Сепсис",           callback_data="sepsis"),
         InlineKeyboardButton("⚡ Шок",               callback_data="shock")],
        [InlineKeyboardButton("🫁 ИВЛ / ARDSNet",    callback_data="vent"),
         InlineKeyboardButton("💉 Вазопрессоры",     callback_data="press")],
        [InlineKeyboardButton("🧪 ABG",               callback_data="abg"),
         InlineKeyboardButton("📉 Лактат",            callback_data="lac")],
    ])


# =============================
# ОБРАБОТЧИКИ
# =============================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text(
        "ICU CDSS готов. Введи данные пациента.\n\n"
        "Поддерживаемые параметры:\n"
        "  • Возраст:      67 лет\n"
        "  • Рост:         рост 175\n"
        "  • АД:           АД 90/60\n"
        "  • ЧСС / пульс:  ЧСС 110\n"
        "  • ЧД:           ЧД 28\n"
        "  • GCS:          GCS 12\n"
        "  • Температура:  температура 38.5\n"
        "  • SpO₂:         SpO2 88\n"
        "  • PaO₂/FiO₂:   PaO2 65, FiO2 0.5\n"
        "  • ABG:          PaCO2 38, HCO3 18, pH 7.28\n"
        "  • Натрий/Калий: Na 142, K 5.2\n"
        "  • Лейкоциты:    лейкоциты 18.5\n"
        "  • Тромбоциты:   тромбоциты 95\n"
        "  • Креатинин:    креатинин 280\n"
        "  • Билирубин:    билирубин 45\n"
        "  • Лактат:       лактат 3.2\n"
        "  • Диурез:       диурез 0.4\n\n"
        "Данные накапливаются — можно вводить частями."
    )


async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pt = get_pt(ctx)
    if len([k for k in pt if k != "lactate_history"]) == 0:
        await update.message.reply_text(
            "Нет данных для экспорта. Введи данные пациента."
        )
        return
    await update.message.reply_text(
        f"```\n{build_export(pt)}\n```",
        parse_mode="Markdown"
    )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pt = get_pt(ctx)
    prev_sofa = sofa_score(pt) if len([k for k in pt if k not in ("lactate_history", "delta_sofa")]) > 0 else None
    parse(update.message.text, pt)

    if len([k for k in pt if k not in ("lactate_history", "delta_sofa")]) == 0:
        await update.message.reply_text(
            "Не удалось распознать данные.\n"
            "Отправь /start чтобы увидеть список параметров."
        )
        return

    new_sofa = sofa_score(pt)
    if prev_sofa is not None:
        pt["delta_sofa"] = new_sofa - prev_sofa

    await update.message.reply_text(build_response(pt), reply_markup=main_keyboard())


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pt = get_pt(ctx)

    if q.data == "recalc":
        if len([k for k in pt if k != "lactate_history"]) == 0:
            await q.message.reply_text("Нет данных. Введи данные пациента.")
        else:
            await q.message.reply_text(build_response(pt), reply_markup=main_keyboard())

    elif q.data == "clear":
        ctx.user_data.clear()
        await q.message.reply_text("Данные пациента сброшены.")

    elif q.data == "sepsis":
        await q.message.reply_text(SEPSIS_TEXT)

    elif q.data == "shock":
        await q.message.reply_text(SHOCK_TEXT)

    elif q.data == "vent":
        w = pbw(pt.get("height"))
        extra = f"\n\nДля этого пациента (рост {pt['height']} см): VT = {int(w*6)} мл" if w else ""
        await q.message.reply_text(VENT_TEXT + extra)

    elif q.data == "press":
        await q.message.reply_text(PRESS_TEXT)

    elif q.data == "abg":
        await q.message.reply_text(ABG_TEXT)

    elif q.data == "lac":
        await q.message.reply_text(LAC_TEXT)


# =============================
# ЗАПУСК
# =============================
def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Добавь его в секреты.")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.run_polling()


if __name__ == "__main__":
    main()
