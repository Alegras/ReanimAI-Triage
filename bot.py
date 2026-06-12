import os
import re
import math
import asyncio
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, field_validator
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
# WATCHDOG — счётчики активности
# =============================
_stats: dict = {
    "started_at": None,
    "messages":   0,
    "callbacks":  0,
    "last_ok":    None,
}

# =============================
# CONFIG — настраиваемые пороги
# =============================
CONFIG = {
    "targets": {
        "map":               65,
        "lactate_clearance": 0.10,
        "spo2":              94,
        "uop":               0.5,
    },
    "thresholds": {
        "map_critical":      60,
        "map_warn":          65,
        "lactate_critical":  4.0,
        "lactate_warn":      2.0,
        "pf_severe":         150,
        "pf_ards":           300,
        "gcs_intubate":      8,
        "gcs_warn":          13,
        "creatinine_rrt":    300,
        "creatinine_warn":   200,
        "potassium_high":    6.0,
        "potassium_low":     3.0,
        "ph_critical":       7.20,
        "ph_warn":           7.25,
        "sofa_critical":     8,
        "sofa_warn":         4,
        "apache_critical":   25,
        "apache_warn":       15,
    },
}

# =============================
# PATIENT MODEL (Pydantic — валидация и нормализация)
# =============================
class Patient(BaseModel):
    age:        Optional[int]   = None
    height:     Optional[int]   = None
    sbp:        Optional[int]   = None
    map:        Optional[float] = None
    hr:         Optional[int]   = None
    rr:         Optional[int]   = None
    gcs:        Optional[int]   = None
    temp:       Optional[float] = None
    spo2:       Optional[int]   = None
    pao2:       Optional[float] = None
    fio2:       Optional[float] = None
    paco2:      Optional[int]   = None
    hco3:       Optional[int]   = None
    ph:         Optional[float] = None
    sodium:     Optional[int]   = None
    potassium:  Optional[float] = None
    chloride:   Optional[int]   = None
    creatinine: Optional[int]   = None
    bilirubin:  Optional[int]   = None
    wbc:        Optional[float] = None
    plt:        Optional[int]   = None
    lactate:    Optional[float] = None
    uop:        Optional[float] = None
    weight:     Optional[int]   = None
    norad_ml_h: Optional[float] = None
    norad_mg:   Optional[float] = None

    @field_validator("ph")
    @classmethod
    def validate_ph(cls, v):
        if v is not None and not (6.5 <= v <= 7.9):
            raise ValueError(f"pH {v} вне диапазона 6.5–7.9")
        return v

    @field_validator("fio2")
    @classmethod
    def validate_fio2(cls, v):
        if v is not None and not (0.21 <= v <= 1.0):
            raise ValueError(f"FiO₂ {v} вне диапазона 0.21–1.0")
        return v

    @field_validator("spo2")
    @classmethod
    def validate_spo2(cls, v):
        if v is not None and not (0 <= v <= 100):
            raise ValueError(f"SpO₂ {v} вне диапазона 0–100")
        return v

    @classmethod
    def from_dict(cls, d: dict) -> "Patient":
        skip = {"lactate_history", "delta_sofa"}
        return cls(**{k: v for k, v in d.items() if k not in skip})

    def pf(self) -> Optional[float]:
        if self.pao2 and self.fio2:
            return round(self.pao2 / self.fio2, 1)
        return None

    def aa(self) -> Optional[float]:
        if self.pao2 and self.fio2 and self.paco2:
            return round(self.fio2 * (760 - 47) - self.paco2 / 0.8 - self.pao2, 1)
        return None

    def ideal_weight(self) -> Optional[float]:
        return round(50 + 0.91 * (self.height - 152.4), 1) if self.height else None


# =============================
# STATE  (multi-patient)
# =============================
def _ensure_patients(ctx):
    if "patients" not in ctx.user_data:
        ctx.user_data["patients"]       = {1: {"lactate_history": []}}
        ctx.user_data["current_pt_id"]  = 1


def get_pt(ctx):
    _ensure_patients(ctx)
    pid = ctx.user_data.get("current_pt_id", 1)
    if pid not in ctx.user_data["patients"]:
        ctx.user_data["patients"][pid] = {"lactate_history": []}
    return ctx.user_data["patients"][pid]


def current_pid(ctx) -> int:
    _ensure_patients(ctx)
    return ctx.user_data.get("current_pt_id", 1)


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
    cl = re.search(r"(хлор|Cl)\s*(\d+)", text, re.I)
    if cl:
        pt["chloride"] = int(cl.group(2))

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

    # Антропометрия — вес
    w = re.search(r"(?:вес|weight)\s*(\d+)", text, re.I)
    if w:
        pt["weight"] = int(w.group(1))

    # ── Вазопрессоры / инотропы ───────────────────────────────
    # Словарь: паттерн → каноническое имя
    _VASOPRESS_PAT = [
        (r"норадреналин|норэпинефрин|norepinephrine",   "норадреналин"),
        (r"адреналин|эпинефрин|adrenaline|epinephrine", "адреналин"),
        (r"допамин|дофамин|dopamine",                   "допамин"),
        (r"добутамин|dobutamine",                       "добутамин"),
        (r"мезатон|фенилефрин|phenylephrine",           "мезатон"),
        (r"вазопрессин|vasopressin",                    "вазопрессин"),
    ]
    # Разбиваем текст на «предложения» по разделителям
    chunks = re.split(r"[,;\n]", text)
    if "vasopressors" not in pt:
        pt["vasopressors"] = []

    for chunk in chunks:
        detected = None
        for pat, name in _VASOPRESS_PAT:
            if re.search(pat, chunk, re.I):
                detected = name
                break
        if not detected:
            continue
        rate_m = re.search(r"(?:скорость|мл[/\s]?ч|rate)\s*([\d.]+)", chunk, re.I)
        conc_m = re.search(r"(?:концентрация|конц|conc)\s*([\d.]+)", chunk, re.I)
        if rate_m or conc_m:
            # обновляем запись с тем же препаратом или добавляем новую
            existing = next((v for v in pt["vasopressors"] if v["drug"] == detected), None)
            if existing is None:
                existing = {"drug": detected, "rate": None, "conc_mg": None}
                pt["vasopressors"].append(existing)
            if rate_m:
                existing["rate"] = float(rate_m.group(1))
            if conc_m:
                existing["conc_mg"] = float(conc_m.group(1))

    # ── Обратный расчёт: целевая доза → нужная скорость ────────
    # Паттерн: «[препарат] доза N [конц N]» или «доза N [препарат] [конц N]»
    if "target_doses" not in pt:
        pt["target_doses"] = []
    for chunk in chunks:
        target_m = re.search(r"(?:целевая\s*)?доза\s*([\d.]+)", chunk, re.I)
        if not target_m:
            continue
        t_drug = None
        for pat, name in _VASOPRESS_PAT:
            if re.search(pat, chunk, re.I):
                t_drug = name
                break
        if not t_drug:
            # ищем имя препарата в соседних чанках (ввод одной строкой)
            for pat, name in _VASOPRESS_PAT:
                if re.search(pat, text, re.I):
                    t_drug = name
                    break
        if not t_drug:
            continue
        t_conc_m = re.search(r"(?:концентрация|конц|conc)\s*([\d.]+)", chunk, re.I)
        existing_td = next(
            (d for d in pt["target_doses"] if d["drug"] == t_drug), None
        )
        if existing_td is None:
            existing_td = {"drug": t_drug, "target_dose": None, "conc_mg": None}
            pt["target_doses"].append(existing_td)
        existing_td["target_dose"] = float(target_m.group(1))
        if t_conc_m:
            existing_td["conc_mg"] = float(t_conc_m.group(1))

    # Обратная совместимость: старые поля norad_ml_h / norad_mg
    if pt.get("norad_ml_h") or pt.get("norad_mg"):
        existing = next((v for v in pt["vasopressors"] if v["drug"] == "норадреналин"), None)
        if existing is None:
            pt["vasopressors"].append({
                "drug": "норадреналин",
                "rate": pt.get("norad_ml_h"),
                "conc_mg": pt.get("norad_mg"),
            })

    # Устаревшие поля — тоже парсим напрямую как раньше (если без имени препарата)
    rate = re.search(r"(?:скорость|мл[/\s]ч)\s*([\d.]+)", text, re.I)
    if rate:
        pt["norad_ml_h"] = float(rate.group(1))
    conc = re.search(r"(?:концентрация|конц)\s*([\d.]+)", text, re.I)
    if conc:
        pt["norad_mg"] = float(conc.group(1))

    return pt


# =============================
# КЛИНИЧЕСКИЕ РАСЧЁТЫ
# =============================

# Диапазоны доз (мкг/кг/мин, кроме вазопрессина — ед/мин)
_DOSE_TIERS = {
    "норадреналин": [
        (0.00, 0.10,  "низкая"),
        (0.10, 0.25,  "средняя"),
        (0.25, 0.50,  "высокая"),
        (0.50, 9999,  "⚠️ очень высокая"),
    ],
    "адреналин": [
        (0.00, 0.05,  "низкая"),
        (0.05, 0.20,  "средняя"),
        (0.20, 0.50,  "высокая"),
        (0.50, 9999,  "⚠️ очень высокая"),
    ],
    "допамин": [
        (0.00,  3.0,  "почечная / ↑ диурез"),
        (3.00, 10.0,  "кардиотропная (↑ СВ)"),
        (10.0, 20.0,  "вазопрессорная"),
        (20.0, 9999,  "⚠️ очень высокая"),
    ],
    "добутамин": [
        (0.00,  5.0,  "низкая"),
        (5.00, 10.0,  "средняя"),
        (10.0, 20.0,  "высокая"),
        (20.0, 9999,  "⚠️ очень высокая"),
    ],
    "мезатон": [
        (0.00,  0.5,  "низкая"),
        (0.50,  2.0,  "средняя"),
        (2.00, 9999,  "⚠️ высокая"),
    ],
    "вазопрессин": [           # ед/мин
        (0.00, 0.01,  "низкая"),
        (0.01, 0.03,  "стандартная (0.01–0.03 ед/мин)"),
        (0.03, 0.04,  "высокая"),
        (0.04, 9999,  "⚠️ выше рекомендованной"),
    ],
}


def _dose_tier(drug: str, dose: float) -> str:
    for lo, hi, label in _DOSE_TIERS.get(drug, []):
        if lo <= dose < hi:
            return label
    return ""


def _calc_vasopress_dose(entry: dict, weight: float) -> dict | None:
    """
    Рассчитывает дозу одного вазопрессора.
    Возвращает dict с полями drug, dose, unit, tier или None если данных нет.
    """
    drug     = entry.get("drug", "")
    rate     = entry.get("rate")
    conc_mg  = entry.get("conc_mg")
    if rate is None or conc_mg is None:
        return None

    if drug == "вазопрессин":
        # Стандарт: N ед в 50 мл, доза в ед/мин
        dose = round((rate * conc_mg) / (50 * 60), 4)
        unit = "ед/мин"
    else:
        # мкг/кг/мин для всех катехоламинов
        dose = round((rate * conc_mg * 1000) / (50 * 60 * weight), 3)
        unit = "мкг/кг/мин"

    return {
        "drug": drug,
        "dose": dose,
        "unit": unit,
        "tier": _dose_tier(drug, dose),
    }


def _calc_target_rate(drug: str, target_dose: float, conc_mg: float,
                      weight: float) -> dict:
    """
    Обратный расчёт: целевая доза → скорость инфузии (мл/ч).
    drug        — название препарата
    target_dose — цель мкг/кг/мин (ед/мин для вазопрессина)
    conc_mg     — мг (или ед для вазопрессина) в 50 мл шприце
    weight      — вес пациента, кг
    """
    if drug == "вазопрессин":
        # rate = dose(ед/мин) × 50(мл) × 60(мин/ч) / conc(ед)
        rate = (target_dose * 50 * 60) / conc_mg
        unit = "ед/мин"
    else:
        # rate = dose(мкг/кг/мин) × weight × 50 × 60 / (conc_mg × 1000)
        rate = (target_dose * weight * 50 * 60) / (conc_mg * 1000)
        unit = "мкг/кг/мин"
    return {
        "drug":        drug,
        "target_dose": target_dose,
        "unit":        unit,
        "conc_mg":     conc_mg,
        "rate_ml_h":   round(rate, 1),
        "tier":        _dose_tier(drug, target_dose),
    }


def calculate_clinical_params(pt: dict) -> dict:
    """
    Возвращает расчётные клинические параметры:
      vasopressor_results — список рассчитанных доз вазопрессоров
      target_rate_results — обратный расчёт (доза → скорость)
      weight_used / weight_assumed — какой вес использовался
      vt_range / pbw_val  — ARDSnet VT
    """
    result = {}
    weight = pt.get("weight")
    w      = weight or 80
    result["weight_used"]    = w
    result["weight_assumed"] = weight is None

    # ── Вазопрессоры ────────────────────────────────────────────
    vasolist = list(pt.get("vasopressors") or [])

    # Обратная совместимость: старые поля norad_ml_h / norad_mg
    old_rate = pt.get("norad_ml_h")
    old_conc = pt.get("norad_mg")
    if old_rate is not None and old_conc is not None:
        if not any(v["drug"] == "норадреналин" and v.get("rate") for v in vasolist):
            vasolist.append({"drug": "норадреналин", "rate": old_rate, "conc_mg": old_conc})

    vaso_results = []
    for entry in vasolist:
        r = _calc_vasopress_dose(entry, w)
        if r:
            vaso_results.append(r)

    if vaso_results:
        result["vasopressor_results"] = vaso_results

    # ── Обратный расчёт: доза → скорость ───────────────────────
    rate_results = []
    for td in pt.get("target_doses") or []:
        drug   = td.get("drug")
        t_dose = td.get("target_dose")
        # приоритет конц из target_doses, иначе из vasopressors
        conc = td.get("conc_mg")
        if conc is None:
            vaso_entry = next(
                (v for v in (pt.get("vasopressors") or []) if v["drug"] == drug), None
            )
            conc = (vaso_entry or {}).get("conc_mg")
        if drug and t_dose is not None and conc:
            rate_results.append(_calc_target_rate(drug, t_dose, conc, w))
    if rate_results:
        result["target_rate_results"] = rate_results

    # ── ARDSnet: целевой VT ─────────────────────────────────────
    height = pt.get("height")
    if height:
        ideal = round(50 + 0.91 * (height - 152.4), 1)
        result["pbw_val"]  = ideal
        result["vt_range"] = f"{int(ideal * 6)}–{int(ideal * 8)} мл"

    return result


# =============================
# КАЛЬКУЛЯТОР ТИТРОВАНИЯ
# =============================

# Рекомендуемый интервал между шагами (мин) и шаг (% от диапазона)
_TITRATE_CONFIG = {
    "норадреналин": {"interval_min": 5,  "n_steps": 6},
    "адреналин":    {"interval_min": 5,  "n_steps": 6},
    "мезатон":      {"interval_min": 5,  "n_steps": 5},
    "допамин":      {"interval_min": 10, "n_steps": 5},
    "добутамин":    {"interval_min": 10, "n_steps": 5},
    "вазопрессин":  {"interval_min": 5,  "n_steps": 4},
}
_TITRATE_DEFAULT = {"interval_min": 10, "n_steps": 5}


def calculate_titration(drug: str, dose_start: float, dose_end: float,
                        conc_mg: float, weight: float) -> str:
    """
    Строит ASCII-таблицу шагов титрования.
    Возвращает готовый текст для Telegram (моноширинный блок).
    """
    cfg      = _TITRATE_CONFIG.get(drug, _TITRATE_DEFAULT)
    n_steps  = cfg["n_steps"]
    interval = cfg["interval_min"]

    is_vaso  = (drug == "вазопрессин")
    unit     = "ед/мин" if is_vaso else "мкг/кг/мин"

    # Генерируем промежуточные дозы (включая старт и финиш)
    step_size = (dose_end - dose_start) / n_steps
    doses = [round(dose_start + i * step_size, 4) for i in range(n_steps + 1)]

    def dose_to_rate(d):
        if is_vaso:
            return round((d * 50 * 60) / conc_mg, 1)
        return round((d * weight * 50 * 60) / (conc_mg * 1000), 1)

    direction = "↑" if dose_end > dose_start else "↓"
    header = (
        f"🎯 ТИТРОВАНИЕ — {drug.capitalize()} {direction}\n"
        f"Вес: {weight} кг  |  Конц: {conc_mg} мг/50мл\n"
        f"{'─'*36}\n"
        f"{'Шаг':<4} {'Доза':>10}  {'мл/ч':>6}  Время\n"
        f"{'─'*36}"
    )

    rows = []
    for i, d in enumerate(doses):
        rate    = dose_to_rate(d)
        time_lbl = "старт" if i == 0 else f"+{i * interval:02d} мин"
        goal_lbl = " ✓" if i == len(doses) - 1 else ""
        rows.append(
            f"{i:<4} {d:>8.3f}     {rate:>5.1f}  {time_lbl}{goal_lbl}"
        )

    footer = (
        f"{'─'*36}\n"
        f"Шаг ≈ {abs(step_size):.3f} {unit}  |  {interval} мин/шаг\n"
        f"Общее время: {n_steps * interval} мин"
    )

    return f"```\n{header}\n" + "\n".join(rows) + f"\n{footer}\n```"


def _parse_titrate_args(text: str) -> dict | None:
    """
    Парсит аргументы из строки вида:
      «норадреналин текущая 0.1 цель 0.3 конц 8 вес 75»
    Возвращает dict или None если данных недостаточно.
    """
    _VASOPRESS_PAT_LOCAL = [
        (r"норадреналин|норэпинефрин", "норадреналин"),
        (r"адреналин|эпинефрин",       "адреналин"),
        (r"допамин|дофамин",           "допамин"),
        (r"добутамин",                 "добутамин"),
        (r"мезатон|фенилефрин",        "мезатон"),
        (r"вазопрессин",               "вазопрессин"),
    ]
    drug = None
    for pat, name in _VASOPRESS_PAT_LOCAL:
        if re.search(pat, text, re.I):
            drug = name
            break

    start_m = re.search(r"(?:текущ\w*|от|from|start)\s*([\d.]+)", text, re.I)
    end_m   = re.search(r"(?:цел\w*|до|to|target|end)\s*([\d.]+)", text, re.I)
    conc_m  = re.search(r"(?:концентрация|конц|conc)\s*([\d.]+)", text, re.I)
    weight_m= re.search(r"(?:вес|weight)\s*(\d+)", text, re.I)

    if not all([drug, start_m, end_m, conc_m]):
        return None

    return {
        "drug":       drug,
        "dose_start": float(start_m.group(1)),
        "dose_end":   float(end_m.group(1)),
        "conc_mg":    float(conc_m.group(1)),
        "weight":     int(weight_m.group(1)) if weight_m else 80,
        "assumed_w":  weight_m is None,
    }


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
# ALERTS ENGINE
# =============================
def alerts(pt) -> list[str]:
    T = CONFIG["thresholds"]
    a = []

    if pt.get("map") is not None and pt["map"] < T["map_critical"]:
        a.append(f"🔴 КРИТИЧНО: MAP {pt['map']} — тяжёлая гипотензия")
    if pt.get("lactate", 0) >= T["lactate_critical"]:
        a.append(f"🔴 КРИТИЧНО: лактат {pt['lactate']} ммоль/л — тяжёлый шок")
    if pt.get("gcs") is not None and pt["gcs"] <= T["gcs_intubate"]:
        a.append(f"🔴 КРИТИЧНО: GCS {pt['gcs']} — риск аспирации, показания к интубации")

    pf = pf_ratio(pt)
    if pf is not None and pf < 100:
        a.append(f"🔴 КРИТИЧНО: PaO₂/FiO₂ {pf} — тяжёлый ARDS")
    if pt.get("ph") is not None and pt["ph"] < 7.15:
        a.append(f"🔴 КРИТИЧНО: pH {pt['ph']} — жизнеугрожающий ацидоз")
    if pt.get("potassium") is not None and pt["potassium"] >= 6.5:
        a.append(f"🔴 КРИТИЧНО: K⁺ {pt['potassium']} — риск остановки сердца")
    if pt.get("spo2") is not None and pt["spo2"] < 85:
        a.append(f"🔴 КРИТИЧНО: SpO₂ {pt['spo2']}%")

    if pt.get("map") is not None and T["map_critical"] <= pt["map"] < T["map_warn"]:
        a.append(f"🟡 ВНИМАНИЕ: MAP {pt['map']} — гипотензия")
    if pt.get("lactate", 0) >= T["lactate_warn"] and pt.get("lactate", 0) < T["lactate_critical"]:
        a.append(f"🟡 ВНИМАНИЕ: лактат {pt['lactate']} — гиперлактатемия")
    if pt.get("gcs") is not None and T["gcs_intubate"] < pt["gcs"] < T["gcs_warn"]:
        a.append(f"🟡 ВНИМАНИЕ: GCS {pt['gcs']} — нарушение сознания")
    if pt.get("creatinine", 0) >= T["creatinine_rrt"]:
        a.append(f"🟡 ВНИМАНИЕ: Кр-нин {pt['creatinine']} — рассмотреть ЗПТ")

    return a


# =============================
# Решения (Decision Engine)
# =============================
def decisions(pt):
    T   = CONFIG["thresholds"]
    TGT = CONFIG["targets"]
    sofa = sofa_score(pt)
    pf   = pf_ratio(pt)
    rec  = []

    # ── Гемодинамика ──────────────────────────────────────────
    if pt.get("map", 100) < TGT["map"]:
        rec.append(
            f"Шок: норадреналин 0.05→0.3 мкг/кг/мин, цель MAP ≥ {TGT['map']}; "
            "при рефрактерности — вазопрессин 0.03 ед/мин"
        )
    elif pt.get("map", 100) < 75 and pt.get("lactate", 0) >= T["lactate_warn"]:
        rec.append("MAP 65–75 + лактат ↑ — болюс кристаллоидов 500 мл, переоценить ч/з 30 мин")

    # ── Дыхание / оксигенация ─────────────────────────────────
    if pf is not None and pf < 100:
        w  = pbw(pt.get("height"))
        vt = f"{int(w * 6)} мл" if w else "≈6 мл/кг ИМТ"
        rec.append(
            f"Тяжёлый ARDS (PF {pf}): VT {vt}, PEEP escalation, Pplat < 30; прон ≥ 16 ч"
        )
    elif pf is not None and pf < T["pf_severe"]:
        w  = pbw(pt.get("height"))
        vt = f"{int(w * 6)} мл" if w else "≈6 мл/кг ИМТ"
        rec.append(f"ARDS (PF {pf}): VT {vt}, PEEP по ARDSNet, Pplat < 30")
    elif pt.get("gcs", 15) <= T["gcs_intubate"]:
        rec.append(f"GCS ≤ {T['gcs_intubate']} — защита ДП, показания к интубации")

    if pt.get("spo2", 100) < TGT["spo2"] and pf is None:
        rec.append(f"SpO₂ < {TGT['spo2']}% — высокопоточная O₂ или НИВ, контроль ABG")

    # ── Перфузия / лактат ─────────────────────────────────────
    if pt.get("lactate", 0) >= T["lactate_critical"]:
        rec.append(
            f"Лактат ≥ {T['lactate_critical']} — агрессивная ресусцитация, контроль каждые 2 ч; "
            f"цель клиренс > {int(TGT['lactate_clearance']*100)}%"
        )
    elif pt.get("lactate", 0) >= T["lactate_warn"]:
        rec.append(
            f"Лактат ≥ {T['lactate_warn']} — кристаллоиды 30 мл/кг, контроль каждые 2–4 ч; "
            f"цель клиренс > {int(TGT['lactate_clearance']*100)}%"
        )

    # ── Инфекция / сепсис ─────────────────────────────────────
    if sofa >= 2 and (pt.get("temp", 36) > 38 or pt.get("wbc", 0) >= 12 or
                      pt.get("lactate", 0) >= T["lactate_warn"]):
        rec.append(
            "Sepsis bundle: гемокультуры × 2 → антибиотики < 1 ч → source control"
        )
    elif pt.get("wbc", 0) >= 15 or pt.get("temp", 36) > 38.5:
        rec.append("Гемокультуры × 2, антибиотики широкого спектра — в первый час")

    # ── Почки ─────────────────────────────────────────────────
    if pt.get("creatinine", 0) > T["creatinine_rrt"] or pt.get("uop", 1) < 0.3:
        rec.append(
            f"ОПП тяжёлое — нефролог, рассмотреть ЗПТ/CRRT; диурез ≥ {TGT['uop']} мл/кг/ч"
        )
    elif pt.get("creatinine", 0) > T["creatinine_warn"]:
        rec.append("ОПП — контроль диуреза, рассмотреть ЗПТ при нарастании; без нефротоксинов")

    # ── Электролиты / КЩС ────────────────────────────────────
    if pt.get("potassium", 4) >= T["potassium_high"]:
        rec.append("Гиперкалиемия — Ca глюконат 10% 10 мл в/в, ЭКГ, инсулин + глюкоза")
    elif pt.get("potassium", 4) < T["potassium_low"]:
        rec.append("Гипокалиемия — KCl в/в под ЭКГ-контролем, не > 20 мэкв/ч")

    if pt.get("sodium", 140) < 125:
        rec.append("Гипонатриемия — ограничение жидкости, 3% NaCl при симптомах")

    if pt.get("ph", 7.4) < T["ph_critical"]:
        rec.append(f"Тяжёлый ацидоз (pH < {T['ph_critical']}) — NaHCO₃, контроль ABG")
    elif pt.get("ph", 7.4) < T["ph_warn"]:
        rec.append("Ацидоз — устранить причину; NaHCO₃ при pH < 7.1")

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
        "weight":     ("Вес",          "кг"),
        "norad_ml_h": ("Норадр. скор.","мл/ч"),
        "norad_mg":   ("Норадр. конц.","мг/50мл"),
    }
    skip = {"sbp", "lactate_history", "delta_sofa"}
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

    # Валидация через Pydantic (предупреждения о невалидных значениях)
    validation_warns = []
    try:
        Patient.from_dict(pt)
    except Exception as e:
        validation_warns.append(f"⚠️ Данные: {e}")

    al = alerts(pt)

    lines = []
    if al:
        lines += ["🚨 АЛЕРТЫ:"] + al + [""]
    if validation_warns:
        lines += validation_warns + [""]

    lines += [
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

    # ── Клинические расчёты ───────────────────────────────────
    cp = calculate_clinical_params(pt)
    cp_lines = []
    w_note = f" (вес {cp['weight_used']} кг{'*' if cp['weight_assumed'] else ''})"
    for vr in cp.get("vasopressor_results", []):
        tier_str = f" — {vr['tier']}" if vr["tier"] else ""
        cp_lines.append(
            f"  💉 {vr['drug'].capitalize()}: {vr['dose']} {vr['unit']}{tier_str}{w_note}"
        )
    for tr in cp.get("target_rate_results", []):
        tier_str = f" ({tr['tier']})" if tr["tier"] else ""
        cp_lines.append(
            f"  🎯 {tr['drug'].capitalize()} → цель {tr['target_dose']} {tr['unit']}{tier_str}: "
            f"скорость {tr['rate_ml_h']} мл/ч (конц {tr['conc_mg']} мг/50мл){w_note}"
        )
    if "vt_range" in cp:
        cp_lines.append(f"  🫁 VT (ARDSnet): {cp['vt_range']}  |  PBW {cp['pbw_val']} кг")
    if cp.get("weight_assumed") and cp_lines:
        cp_lines.append("  * вес не введён — использовано 80 кг по умолчанию")
    if cp_lines:
        lines += ["\n🔢 Клинические расчёты:"] + cp_lines

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
# ИНТЕРПРЕТАЦИЯ КЩС (ABG)
# =============================
def interpret_abg(pt):
    ph    = pt.get("ph")
    paco2 = pt.get("paco2")
    hco3  = pt.get("hco3")
    na    = pt.get("sodium")
    cl    = pt.get("chloride")
    pao2  = pt.get("pao2")
    fio2  = pt.get("fio2")

    if not all([ph, paco2, hco3]):
        return None

    lines = [
        "🧪 КЩС — ИНТЕРПРЕТАЦИЯ",
        f"pH {ph}  PaCO₂ {paco2}  HCO₃ {hco3}",
        "",
    ]

    # ── Шаг 1: оценка pH ──────────────────────────────────────
    if ph < 7.35:
        ph_str = "Ацидоз (pH < 7.35)"
        disorder = "acidosis"
    elif ph > 7.45:
        ph_str = "Алкалоз (pH > 7.45)"
        disorder = "alkalosis"
    else:
        ph_str = "pH в норме (7.35–7.45)"
        disorder = "normal"
    lines.append(f"1️⃣  {ph_str}")

    # ── Шаг 2: первичное нарушение ────────────────────────────
    primary = "unknown"
    if disorder == "acidosis":
        if paco2 > 45 and hco3 >= 22:
            primary = "resp_acid"
            lines.append("2️⃣  Первичное: дыхательный ацидоз (PaCO₂ ↑)")
        elif hco3 < 22 and paco2 <= 45:
            primary = "met_acid"
            lines.append("2️⃣  Первичное: метаболический ацидоз (HCO₃ ↓)")
        elif paco2 > 45 and hco3 < 22:
            primary = "mixed_acid"
            lines.append("2️⃣  Смешанное: дыхательный + метаболический ацидоз ❗")
        else:
            lines.append("2️⃣  Первичное нарушение неясно")

    elif disorder == "alkalosis":
        if paco2 < 35 and hco3 <= 26:
            primary = "resp_alk"
            lines.append("2️⃣  Первичное: дыхательный алкалоз (PaCO₂ ↓)")
        elif hco3 > 26 and paco2 >= 35:
            primary = "met_alk"
            lines.append("2️⃣  Первичное: метаболический алкалоз (HCO₃ ↑)")
        elif paco2 < 35 and hco3 > 26:
            primary = "mixed_alk"
            lines.append("2️⃣  Смешанное: дыхательный + метаболический алкалоз")
        else:
            lines.append("2️⃣  Первичное нарушение неясно")

    else:
        if paco2 < 35 and hco3 < 22:
            lines.append("2️⃣  Компенсированное смешанное (дых. алкалоз + мет. ацидоз)")
        elif paco2 > 45 and hco3 > 26:
            lines.append("2️⃣  Компенсированное смешанное (дых. ацидоз + мет. алкалоз)")
        else:
            lines.append("2️⃣  Норма или полная компенсация")

    # ── Шаг 3: компенсация ────────────────────────────────────
    if primary == "met_acid":
        exp = 1.5 * hco3 + 8
        lines.append(f"3️⃣  Ожидаемый PaCO₂ (Winters): {exp-2:.0f}–{exp+2:.0f} мм рт.ст.")
        if paco2 < exp - 2:
            lines.append("    → PaCO₂ ниже ожидаемого: + дыхательный алкалоз")
        elif paco2 > exp + 2:
            lines.append("    → PaCO₂ выше ожидаемого: + дыхательный ацидоз")
        else:
            lines.append("    → Компенсация адекватная ✓")

    elif primary == "met_alk":
        exp = 0.7 * hco3 + 21
        lines.append(f"3️⃣  Ожидаемый PaCO₂: {exp-2:.0f}–{exp+2:.0f} мм рт.ст.")
        if paco2 < exp - 2:
            lines.append("    → + дыхательный алкалоз")
        elif paco2 > exp + 2:
            lines.append("    → + дыхательный ацидоз")
        else:
            lines.append("    → Компенсация адекватная ✓")

    elif primary == "resp_acid":
        exp_acute   = 24 + (paco2 - 40) / 10
        exp_chronic = 24 + 3.5 * (paco2 - 40) / 10
        lines.append(f"3️⃣  Ожидаемый HCO₃:")
        lines.append(f"    Острый:    {exp_acute:.1f}  |  Хронический: {exp_chronic:.1f} ммоль/л")
        if hco3 < exp_acute - 2:
            lines.append("    → + метаболический ацидоз")
        elif hco3 > exp_chronic + 2:
            lines.append("    → + метаболический алкалоз")
        else:
            lines.append("    → В пределах компенсации ✓")

    elif primary == "resp_alk":
        exp_acute   = 24 - 2   * (40 - paco2) / 10
        exp_chronic = 24 - 5   * (40 - paco2) / 10
        lines.append(f"3️⃣  Ожидаемый HCO₃:")
        lines.append(f"    Острый:    {exp_acute:.1f}  |  Хронический: {exp_chronic:.1f} ммоль/л")
        if hco3 < exp_chronic - 2:
            lines.append("    → + метаболический ацидоз")
        elif hco3 > exp_acute + 2:
            lines.append("    → + метаболический алкалоз")
        else:
            lines.append("    → В пределах компенсации ✓")

    # ── Шаг 4: анионный разрыв ────────────────────────────────
    if na is not None and cl is not None:
        ag = na - (cl + hco3)
        lines.append(f"4️⃣  Анионный разрыв: {ag} мэкв/л (норма 8–12)")
        if ag > 12:
            lines.append("    → Высокий АР: лактат, кетоны, уремия, токсины (MUDPILES)")
            if hco3 < 24:
                delta_r = (ag - 12) / (24 - hco3)
                lines.append(f"    Delta-ratio: {delta_r:.1f}")
                if delta_r > 2:
                    lines.append("    → + метаболический алкалоз поверх высокого АР")
                elif delta_r >= 1:
                    lines.append("    → Чистый высокий АР ацидоз")
                else:
                    lines.append("    → + нормальный АР ацидоз (гиперхлоремия)")
        elif ag < 6:
            lines.append("    → Низкий АР: гипоальбуминемия, миелома, ошибка измерения")
        else:
            lines.append("    → АР в норме ✓")
    elif na is not None:
        lines.append(f"4️⃣  Na − HCO₃ = {na - hco3} (добавь хлор: «Cl 102» для точного АР)")

    # ── Шаг 5: оксигенация ────────────────────────────────────
    if pao2 is not None and fio2 is not None:
        pf = round(pao2 / fio2, 1)
        aa = round(fio2 * (760 - 47) - paco2 / 0.8 - pao2, 1)
        lines.append(f"5️⃣  PaO₂/FiO₂: {pf}  |  A-a градиент: {aa} мм рт.ст.")
        if pf < 100:
            lines.append("    → Тяжёлый ARDS (< 100)")
        elif pf < 200:
            lines.append("    → Умеренный ARDS (100–200)")
        elif pf < 300:
            lines.append("    → Лёгкий ARDS (200–300)")
        else:
            lines.append("    → Оксигенация в норме ✓")

    return "\n".join(lines)


# =============================
# ДАШБОРД
# =============================
def _val(pt, key, unit="", fmt=None):
    v = pt.get(key)
    if v is None:
        return "—"
    return f"{fmt.format(v) if fmt else v} {unit}".strip()

def _flag(v, lo_bad=None, lo_warn=None, hi_warn=None, hi_bad=None):
    if v is None:
        return ""
    if (lo_bad is not None and v < lo_bad) or (hi_bad is not None and v > hi_bad):
        return " ❗"
    if (lo_warn is not None and v < lo_warn) or (hi_warn is not None and v > hi_warn):
        return " ⚠️"
    return " ✓"

def dashboard(pt):
    apache = apache_score(pt)
    sofa   = sofa_score(pt)
    delta_sofa = pt.get("delta_sofa")
    pf     = pf_ratio(pt)
    lc     = lactate_clearance(pt)

    # байесовская posterior (сырое значение для бара)
    logit  = -3.5 + 0.15 * apache
    prior  = 1 / (1 + math.exp(-logit))
    lr = 1.0
    if sofa >= 8:   lr *= 2.5
    elif sofa >= 4: lr *= 1.5
    if pt.get("lactate", 0) >= 4:  lr *= 2.0
    elif pt.get("lactate", 0) >= 2: lr *= 1.3
    if pt.get("map", 100) < 65:    lr *= 1.8
    if delta_sofa is not None:
        if delta_sofa > 2:   lr *= 2.2
        elif delta_sofa > 0: lr *= 1.3
        elif delta_sofa < 0: lr *= 0.7
    odds = prior / (1 - prior)
    post = min(0.99, (odds * lr) / (1 + odds * lr))
    mort_pct = round(post * 100)

    # визуальный бар смертности
    filled = round(post * 10)
    bar = "█" * filled + "░" * (10 - filled)

    # динамика SOFA
    if delta_sofa is not None:
        d_str = f"{delta_sofa:+d}"
        d_ico = "📈" if delta_sofa > 0 else ("📉" if delta_sofa < 0 else "➡️")
    else:
        d_str, d_ico = "—", ""

    # PF статус
    if pf is None:
        pf_str = "—"
    elif pf < 100:
        pf_str = f"{pf} ❗ тяжёлый ARDS"
    elif pf < 200:
        pf_str = f"{pf} ⚠️ умеренный"
    elif pf < 300:
        pf_str = f"{pf} ⚠️ лёгкий"
    else:
        pf_str = f"{pf} ✓"

    map_v = pt.get("map")
    gcs_v = pt.get("gcs")
    rr_v  = pt.get("rr")
    lac_v = pt.get("lactate")
    cr_v  = pt.get("creatinine")
    hr_v  = pt.get("hr")
    t_v   = pt.get("temp")
    sp_v  = pt.get("spo2")
    uop_v = pt.get("uop")

    lines = [
        "🧾 ICU DASHBOARD",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "  ВИТАЛЬНЫЕ",
        f"  🫀 MAP:  {map_v or '—'} мм рт.ст.{_flag(map_v, lo_bad=55, lo_warn=65, hi_warn=130, hi_bad=160)}",
        f"  💓 ЧСС:  {hr_v or '—'} /мин{_flag(hr_v, lo_bad=40, lo_warn=55, hi_warn=110, hi_bad=140)}",
        f"  🌡️ Темп: {t_v or '—'} °C{_flag(t_v, lo_warn=36, hi_warn=38.5, hi_bad=39)}",
        f"  🫁 ЧД:   {rr_v or '—'} /мин{_flag(rr_v, hi_warn=25, hi_bad=35)}",
        f"  🔵 SpO₂: {sp_v or '—'} %{_flag(sp_v, lo_bad=85, lo_warn=90)}",
        f"  🧠 GCS:  {gcs_v or '—'}{_flag(gcs_v, lo_bad=9, lo_warn=13)}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "  ЛАБОРАТОРИЯ",
        f"  🫁 P/F:     {pf_str}",
        f"  🧪 Лактат: {lac_v or '—'} ммоль/л{_flag(lac_v, hi_warn=2, hi_bad=4)}",
        f"  💧 Диурез: {uop_v or '—'} мл/кг/ч{_flag(uop_v, lo_bad=0.3, lo_warn=0.5)}",
        f"  🫘 Кр-нин: {cr_v or '—'} мкмоль/л{_flag(cr_v, hi_warn=130, hi_bad=300)}",
    ]
    if lc is not None:
        trend = "↓ улучшение" if lc > 0 else "↑ ухудшение"
        lines.append(f"  📉 Лактат-клиренс: {lc}% {trend}")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        "  СКОРИНГ",
        f"  APACHE II: {apache}",
        f"  SOFA:      {sofa}  {d_ico} ΔSOFA: {d_str}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "  БАЙЕСОВСКАЯ СМЕРТНОСТЬ (30 сут)",
        f"  [{bar}] {mort_pct}%",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    return "\n".join(lines)


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
        "weight":     ("Вес",          "кг"),
        "norad_ml_h": ("Норадр. скор.","мл/ч"),
        "norad_mg":   ("Норадр. конц.","мг/50мл"),
    }
    skip = {"sbp", "lactate_history", "delta_sofa"}

    vitals_keys = {"age", "height", "weight", "sbp", "map", "hr", "rr", "gcs", "temp", "spo2", "uop"}
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
# КНОПКИ  (иерархическое меню)
# =============================
def _IKB(text, cb):  # сокращение: второй аргумент всегда callback_data
    return InlineKeyboardButton(text, callback_data=cb)


def main_keyboard(ctx=None):
    if ctx is not None:
        _ensure_patients(ctx)
        pid   = ctx.user_data.get("current_pt_id", 1)
        total = len(ctx.user_data["patients"])
        pt_label = f"👥 Пациент {pid}/{total}"
    else:
        pt_label = "👥 Пациенты"
    return InlineKeyboardMarkup([
        [_IKB("📊 Дашборд",     "dash"),
         _IKB("🔄 Пересчитать", "recalc"),
         _IKB("❌ Сброс",        "clear")],
        [_IKB("🩺 Протоколы",   "menu_protocols"),
         _IKB("💉 Вазопрессоры","menu_vasos"),
         _IKB("📋 Отчёты",      "menu_reports")],
        [_IKB(pt_label,          "menu_patients")],
    ])


def protocols_keyboard():
    return InlineKeyboardMarkup([
        [_IKB("🦠 Сепсис",        "sepsis"),
         _IKB("⚡ Шок",            "shock")],
        [_IKB("🫁 ИВЛ / ARDSNet", "vent"),
         _IKB("🧪 ABG",            "abg")],
        [_IKB("📉 Лактат",         "lac"),
         _IKB("🦠 Sepsis Bundle",  "checklist_open")],
        [_IKB("← Главное меню",    "menu_main")],
    ])


def vasos_keyboard():
    return InlineKeyboardMarkup([
        [_IKB("💉 Протокол вазопрессоров", "press")],
        [_IKB("🎯 Скорость по дозе", "rate_help"),
         _IKB("📈 Титрование",        "titrate_help")],
        [_IKB("← Главное меню",       "menu_main")],
    ])


def reports_keyboard():
    return InlineKeyboardMarkup([
        [_IKB("📋 Пересменка",  "shift_report"),
         _IKB("📤 Экспорт CSV", "export_csv")],
        [_IKB("📈 Динамика",    "trend_report")],
        [_IKB("← Главное меню", "menu_main")],
    ])


def patients_keyboard(ctx):
    _ensure_patients(ctx)
    patients = ctx.user_data["patients"]
    pid      = ctx.user_data.get("current_pt_id", 1)
    btns = []
    for p_id in sorted(patients.keys()):
        mark  = "✓ " if p_id == pid else ""
        has   = any(k not in ("lactate_history", "delta_sofa") for k in patients[p_id])
        tag   = "" if has else " (пуст)"
        btns.append([_IKB(f"{mark}Пациент {p_id}{tag}", f"pt_switch:{p_id}")])
    btns.append([_IKB("➕ Новый пациент", "pt_new")])
    btns.append([_IKB("← Главное меню",  "menu_main")])
    return InlineKeyboardMarkup(btns)


# =============================
# COMPLETENESS CHECK (SOFA)
# =============================

# Priority order — самое критичное спрашиваем первым
SOFA_REQUIRED = [
    ("cv",    "map"),
    ("cns",   "gcs"),
    ("renal", "creatinine"),
    ("liver", "bilirubin"),
    ("coag",  "plt"),
    ("resp",  "pao2"),
    ("resp",  "fio2"),
]

FIELD_NAMES = {
    "map":        "MAP (среднее АД)",
    "gcs":        "GCS (шкала ком)",
    "creatinine": "креатинин",
    "bilirubin":  "билирубин",
    "plt":        "тромбоциты",
    "pao2":       "PaO₂",
    "fio2":       "FiO₂",
}

FIELD_HINTS = {
    "map":
        "❗ Нет MAP — гемодинамика не оценена.\n"
        "Введи среднее АД (мм рт.ст.), напр. 62",
    "gcs":
        "❗ Нет GCS — ЦНС не оценена.\n"
        "Введи баллы по ШКГ (3–15), напр. 13",
    "creatinine":
        "❗ Нет креатинина → риск ОПП не оценён.\n"
        "Введи значение (мкмоль/л), напр. 280",
    "bilirubin":
        "❗ Нет билирубина → функция печени неизвестна.\n"
        "Введи значение (мкмоль/л), напр. 40",
    "plt":
        "❗ Нет тромбоцитов → коагуляция не оценена.\n"
        "Введи значение (×10⁹/л), напр. 120",
    "pao2":
        "❗ Нет PaO₂ → оксигенация не оценена.\n"
        "Введи значение (мм рт.ст.), напр. 65",
    "fio2":
        "❗ Нет FiO₂ → оксигенация не оценена.\n"
        "Введи долю кислорода (0.21–1.0), напр. 0.4",
}

# Целочисленные поля — конвертируем int при вводе
_INT_FIELDS = {"gcs", "plt", "creatinine", "bilirubin"}


def missing_sofa_fields(pt: dict, skipped: set) -> list:
    return [
        (sys_, field)
        for sys_, field in SOFA_REQUIRED
        if pt.get(field) is None and field not in skipped
    ]


def skip_keyboard(field: str) -> InlineKeyboardMarkup:
    name = FIELD_NAMES.get(field, field)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(f"⏭ Пропустить {name}", callback_data=f"skip:{field}")
    ]])


def _finalize(pt: dict, ctx) -> str:
    """Вычисляем delta_sofa, сохраняем снапшот и возвращаем build_response."""
    new_sofa = sofa_score(pt)
    base = pt.pop("_base_sofa", None)
    if base is not None:
        pt["delta_sofa"] = new_sofa - base
    snap = {
        "ts":         datetime.now().strftime("%H:%M"),
        "sofa":       new_sofa,
        "apache":     apache_score(pt),
        "qsofa":      qsofa_score(pt),
        "map":        pt.get("map"),
        "hr":         pt.get("hr"),
        "rr":         pt.get("rr"),
        "temp":       pt.get("temp"),
        "spo2":       pt.get("spo2"),
        "gcs":        pt.get("gcs"),
        "lactate":    pt.get("lactate"),
        "creatinine": pt.get("creatinine"),
        "bilirubin":  pt.get("bilirubin"),
        "plt":        pt.get("plt"),
        "ph":         pt.get("ph"),
        "pf":         pf_ratio(pt),
    }
    snaps = pt.setdefault("_snapshots", [])
    snaps.append(snap)
    if len(snaps) > 5:
        pt["_snapshots"] = snaps[-5:]
    return build_response(pt)


# =============================
# ОБРАБОТЧИКИ
# =============================
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    ctx.user_data["patients"]      = {1: {"lactate_history": []}}
    ctx.user_data["current_pt_id"] = 1
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


# =============================
# SEPSIS BUNDLE CHECKLIST
# =============================
from datetime import timezone

CHECKLIST_ITEMS = [
    ("lactate",    "🧪 Измерить лактат"),
    ("cultures",   "🧫 Гемокультуры × 2 (до антибиотиков)"),
    ("abx",        "💊 Антибиотики широкого спектра < 1 ч"),
    ("fluids",     "💧 Кристаллоиды 30 мл/кг"),
    ("vasopressors","💉 Вазопрессоры → MAP ≥ 65"),
    ("repeat_lac", "🔁 Повторный лактат через 2 ч"),
]


def _checklist_display(cl: dict) -> tuple[str, InlineKeyboardMarkup]:
    """
    Формирует текст и клавиатуру для текущего состояния чеклиста.
    cl = {"started_at": float, "items": {id: {"done": bool, "done_at": float}}}
    """
    started  = cl["started_at"]
    now      = datetime.now(timezone.utc).timestamp()
    elapsed  = int((now - started) / 60)
    start_dt = datetime.fromtimestamp(started).strftime("%H:%M")

    done_count = sum(1 for v in cl["items"].values() if v["done"])
    total      = len(CHECKLIST_ITEMS)
    all_done   = done_count == total

    status_icon = "✅" if all_done else ("🟡" if done_count > 0 else "🔴")
    lines = [
        f"🦠 SEPSIS BUNDLE {status_icon}",
        f"⏱ Начат: {start_dt}  |  Прошло: {elapsed} мин",
        "─" * 34,
    ]

    buttons = []
    for item_id, label in CHECKLIST_ITEMS:
        state = cl["items"].get(item_id, {"done": False})
        if state["done"]:
            done_at = state.get("done_at", started)
            delta   = int((done_at - started) / 60)
            lines.append(f"  ✅ {label}  (+{delta} мин)")
        else:
            lines.append(f"  ⬜ {label}")
            buttons.append([InlineKeyboardButton(
                f"✅ {label}", callback_data=f"checklist:{item_id}"
            )])

    lines += [
        "─" * 34,
        f"Выполнено: {done_count}/{total}",
    ]
    if all_done:
        lines.append("🎉 Sepsis bundle выполнен!")

    buttons.append([
        InlineKeyboardButton("🔄 Начать заново", callback_data="checklist_reset"),
    ])

    return "\n".join(lines), InlineKeyboardMarkup(buttons)


# =============================
# ОТЧЁТ НА ПЕРЕСМЕНКУ
# =============================

def build_shift_report(pt: dict) -> str:
    """
    Собирает полный отчёт на пересменку из состояния пациента.
    """
    now_str = datetime.now().strftime("%d.%m.%Y %H:%M")
    lines   = [f"📋 ПЕРЕСМЕНКА — {now_str}", "═" * 36]

    # ── Пациент ────────────────────────────────
    patient_parts = []
    if "age"    in pt: patient_parts.append(f"{pt['age']} лет")
    if "weight" in pt: patient_parts.append(f"{pt['weight']} кг")
    if "height" in pt: patient_parts.append(f"{pt['height']} см")
    lines.append("👤 ПАЦИЕНТ")
    lines.append("  " + ("  |  ".join(patient_parts) if patient_parts else "данные не введены"))

    # ── Скоры ──────────────────────────────────
    apache = apache_score(pt)
    sofa   = sofa_score(pt)
    qsofa  = qsofa_score(pt)
    delta  = pt.get("delta_sofa")
    p24, p30 = bayesian_mortality(pt, apache, sofa, delta)
    triage = triage_level(pt, apache, sofa)

    lines.append("")
    lines.append("📊 СКОРЫ")
    delta_str = f"  (ΔSOFA {'+' if delta >= 0 else ''}{delta})" if delta is not None else ""
    lines.append(f"  SOFA:      {sofa} б{delta_str}")
    lines.append(f"  APACHE II: {apache} б")
    lines.append(f"  qSOFA:     {qsofa} б")
    lines.append(f"  Триаж:     {triage}")
    lines.append(f"  Летальность: 24ч {p24}%  |  30д {p30}%")

    # ── Витальные ──────────────────────────────
    vitals = []
    if "map"  in pt:
        flag = " ⚠️" if pt["map"] < 65 else ""
        vitals.append(f"MAP {pt['map']}{flag}")
    if "hr"   in pt: vitals.append(f"ЧСС {pt['hr']}")
    if "rr"   in pt:
        flag = " ⚠️" if pt["rr"] >= 22 else ""
        vitals.append(f"ЧД {pt['rr']}{flag}")
    if "spo2" in pt:
        flag = " ⚠️" if pt["spo2"] < 94 else ""
        vitals.append(f"SpO₂ {pt['spo2']}%{flag}")
    if "temp" in pt: vitals.append(f"T {pt['temp']}°C")
    if "gcs"  in pt:
        flag = " ⚠️" if pt["gcs"] < 13 else ""
        vitals.append(f"GCS {pt['gcs']}{flag}")
    if "sbp"  in pt: vitals.append(f"АДс {pt['sbp']}")

    if vitals:
        lines.append("")
        lines.append("🫀 ВИТАЛЬНЫЕ")
        lines.append("  " + "  |  ".join(vitals))

    # ── Вазопрессоры ───────────────────────────
    vasos = pt.get("vasopressors") or []
    if vasos:
        lines.append("")
        lines.append("💉 ВАЗОПРЕССОРЫ")
        w = pt.get("weight") or 80
        for v in vasos:
            drug = v["drug"].capitalize()
            rate = v.get("rate")
            conc = v.get("conc_mg")
            if rate and conc:
                is_vaso = v["drug"] == "вазопрессин"
                if is_vaso:
                    dose = round((rate * conc) / (50 * 60), 4)
                    unit = "ед/мин"
                else:
                    dose = round((rate * conc * 1000) / (50 * 60 * w), 3)
                    unit = "мкг/кг/мин"
                conc_str = f"конц {conc} мг/50мл"
                lines.append(f"  {drug}: {dose} {unit} → {rate} мл/ч  ({conc_str})")
            elif rate:
                lines.append(f"  {drug}: {rate} мл/ч")
            else:
                lines.append(f"  {drug}: (нет данных о скорости)")

    # ── Лактат ─────────────────────────────────
    lac = pt.get("lactate")
    lac_hist = pt.get("lactate_history") or []
    if lac is not None or lac_hist:
        lines.append("")
        lines.append("🧪 ЛАКТАТ")
        if lac is not None:
            flag = " 🔴" if lac >= 4 else (" 🟡" if lac >= 2 else " 🟢")
            lines.append(f"  Текущий: {lac} ммоль/л{flag}")
        if len(lac_hist) > 1:
            trend = " → ".join(str(x) for x in lac_hist[-5:])
            lines.append(f"  Тренд:   {trend}")

    # ── Газы / ABG ─────────────────────────────
    abg_parts = []
    pf = pf_ratio(pt)
    if pf  is not None: abg_parts.append(f"P/F {int(pf)}")
    if "ph"  in pt: abg_parts.append(f"pH {pt['ph']}")
    if "pco2" in pt: abg_parts.append(f"pCO₂ {pt['pco2']}")
    if "hco3" in pt: abg_parts.append(f"HCO₃ {pt['hco3']}")
    if "pao2" in pt: abg_parts.append(f"PaO₂ {pt['pao2']}")
    if "fio2" in pt: abg_parts.append(f"FiO₂ {pt['fio2']}")
    if abg_parts:
        lines.append("")
        lines.append("🫁 ГАЗЫ / ABG")
        lines.append("  " + "  |  ".join(abg_parts))

    # ── Sepsis Bundle ───────────────────────────
    cl = pt.get("checklist")
    if cl:
        started  = cl["started_at"]
        elapsed  = int((datetime.now(timezone.utc).timestamp() - started) / 60)
        done_ids = [iid for iid, v in cl["items"].items() if v["done"]]
        skip_ids = [iid for iid, v in cl["items"].items() if not v["done"]]
        total    = len(CHECKLIST_ITEMS)
        done_cnt = len(done_ids)
        start_str = datetime.fromtimestamp(started).strftime("%H:%M")

        lines.append("")
        lines.append("🦠 SEPSIS BUNDLE")
        lines.append(f"  Начат: {start_str}  |  Прошло: {elapsed} мин")

        label_map = dict(CHECKLIST_ITEMS)
        if done_ids:
            done_labels = ", ".join(
                label_map[i].split(" ", 1)[1] for i in done_ids if i in label_map
            )
            lines.append(f"  ✅ {done_labels}")
        if skip_ids:
            skip_labels = ", ".join(
                label_map[i].split(" ", 1)[1] for i in skip_ids if i in label_map
            )
            lines.append(f"  ⬜ {skip_labels}")
        lines.append(f"  Выполнено: {done_cnt}/{total}")

    # ── Незаполненные поля SOFA ─────────────────
    skipped  = pt.get("skipped_fields", set())
    missing  = missing_sofa_fields(pt, skipped)
    if missing:
        label_map_sofa = {
            "map":        "MAP",
            "gcs":        "GCS",
            "creatinine": "Креатинин",
            "bilirubin":  "Билирубин",
            "plt":        "Тромбоциты",
            "pao2":       "PaO₂",
            "fio2":       "FiO₂",
        }
        names = ", ".join(label_map_sofa.get(f, f) for f in missing)
        lines.append("")
        lines.append(f"⚠️ СОFA не заполнен: {names}")

    lines += ["═" * 36, f"Сформирован: {now_str}"]
    return "\n".join(lines)


# =============================
# ОТЧЁТ ДИНАМИКИ (/trend)
# =============================
def build_trend_report(pt: dict) -> str:
    snaps = pt.get("_snapshots", [])
    if len(snaps) < 2:
        return (
            "⚠️ Недостаточно данных для анализа динамики.\n\n"
            "Введи новые данные после следующего осмотра — "
            "бот сравнит два замера автоматически."
        )
    a, b = snaps[-2], snaps[-1]

    # higher_better=True  → рост хорош (🟢↑), падение плохо (🔴↓)
    # higher_better=False → рост плох  (🔴↑), падение хорошо (🟢↓)
    FIELDS = [
        # (ключ, метка, ед., higher_better, формат)
        ("sofa",       "SOFA",          "",         False, ".0f"),
        ("apache",     "APACHE II",     "",         False, ".0f"),
        ("qsofa",      "qSOFA",         "",         False, ".0f"),
        ("map",        "MAP",           "мм рт.ст", True,  ".0f"),
        ("hr",         "ЧСС",          "уд/мин",   None,  ".0f"),
        ("rr",         "ЧД",           "/мин",     False, ".0f"),
        ("temp",       "Темп",         "°C",       None,  ".1f"),
        ("spo2",       "SpO₂",         "%",        True,  ".0f"),
        ("gcs",        "GCS",          "",         True,  ".0f"),
        ("pf",         "P/F",          "",         True,  ".0f"),
        ("lactate",    "Лактат",       "ммол/л",   False, ".1f"),
        ("creatinine", "Креатинин",    "мкмол/л",  False, ".0f"),
        ("bilirubin",  "Билирубин",    "мкмол/л",  False, ".0f"),
        ("plt",        "Тромбоциты",  "×10⁹",     True,  ".0f"),
        ("ph",         "pH",           "",         True,  ".2f"),
    ]

    def arrow(va, vb, hb):
        diff = vb - va
        if abs(diff) < 0.005 * max(abs(va), 1):
            return "→", "⚪"
        up = diff > 0
        if hb is None:
            return ("↑" if up else "↓"), "⚪"
        good = (up and hb) or (not up and not hb)
        return ("↑" if up else "↓"), ("🟢" if good else "🔴")

    lines = [
        "📈 ДИНАМИКА",
        f"Замер 1: {a['ts']}  →  Замер 2: {b['ts']}",
        "─" * 32,
    ]
    for key, label, unit, hb, fmt in FIELDS:
        va, vb = a.get(key), b.get(key)
        if va is None or vb is None:
            continue
        arr, icon = arrow(va, vb, hb)
        diff = vb - va
        sign = "+" if diff >= 0 else ""
        diff_str = f"{sign}{diff:{fmt}}"
        u = f" {unit}" if unit else ""
        lines.append(
            f"{icon}{arr} {label}{u}: {va:{fmt}} → {vb:{fmt}} ({diff_str})"
        )
    lines.append("─" * 32)
    lines.append("🟢 = улучшение  🔴 = ухудшение  ⚪ = нейтрально")
    return "\n".join(lines)


async def cmd_shift(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pt     = get_pt(ctx)
    report = build_shift_report(pt)
    await update.message.reply_text(report)


async def cmd_trend(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pt = get_pt(ctx)
    await update.message.reply_text(build_trend_report(pt))


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 КОМАНДЫ ICU CDSS\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "/start — новая сессия, сброс данных\n"
        "/missing — список недостающих полей SOFA\n"
        "/trend — динамика между двумя осмотрами\n"
        "/shift — отчёт на пересменку\n"
        "/checklist — Sepsis Bundle 1-hour checklist\n"
        "/titrate — калькулятор титрования вазопрессора\n"
        "/export — экспорт данных текущего пациента\n"
        "/pt — управление пациентами (переключить/новый)\n"
        "/help — эта справка\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "Ввод свободным текстом:\n"
        "  67 лет, АД 90/60, ЧД 28, GCS 12\n"
        "  лактат 3.2, креатинин 280, тромбоциты 95\n"
        "  PaO2 65 FiO2 0.5, pH 7.28\n"
        "  норадреналин 0.15 мкг/кг/мин\n\n"
        "Данные накапливаются — можно вводить частями."
    )


async def cmd_checklist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pt = get_pt(ctx)

    # Создаём или сбрасываем чеклист
    if "checklist" not in pt or update.message.text.strip().endswith("new"):
        pt["checklist"] = {
            "started_at": datetime.now(timezone.utc).timestamp(),
            "items": {item_id: {"done": False} for item_id, _ in CHECKLIST_ITEMS},
        }

    # Предзаполняем лактат если уже измерен
    cl = pt["checklist"]
    if pt.get("lactate") is not None and not cl["items"]["lactate"]["done"]:
        cl["items"]["lactate"]["done"]    = True
        cl["items"]["lactate"]["done_at"] = cl["started_at"]

    text, kbd = _checklist_display(cl)
    await update.message.reply_text(text, reply_markup=kbd)


async def cmd_titrate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /titrate [норадреналин текущая 0.1 цель 0.3 конц 8 вес 75]
    Или без аргументов — показывает справку.
    """
    raw  = update.message.text or ""
    # Убираем саму команду
    args = re.sub(r"^/titrate\S*\s*", "", raw, flags=re.I).strip()

    # Если аргументы не переданы — пробуем собрать из состояния пациента
    if not args:
        pt = get_pt(ctx)
        if pt.get("vasopressors") or pt.get("target_doses"):
            # Показываем интерактивные кнопки по известным препаратам
            known = {v["drug"] for v in (pt.get("vasopressors") or [])
                     if v.get("rate") and v.get("conc_mg")}
            if known:
                btns = [[InlineKeyboardButton(
                    f"🎯 Титрование {d}", callback_data=f"titrate:{d}"
                )] for d in known]
                await update.message.reply_text(
                    "Выбери препарат для расчёта титрования:",
                    reply_markup=InlineKeyboardMarkup(btns)
                )
                return
        await update.message.reply_text(
            "🎯 КАЛЬКУЛЯТОР ТИТРОВАНИЯ\n\n"
            "Введи команду с параметрами:\n\n"
            "/titrate норадреналин текущая 0.1 цель 0.3 конц 8 вес 75\n"
            "/titrate допамин текущая 3 цель 10 конц 200 вес 80\n"
            "/titrate адреналин текущая 0.05 цель 0.2 конц 2\n\n"
            "Ключевые слова:\n"
            "  текущая N  — стартовая доза\n"
            "  цель N     — целевая доза\n"
            "  конц N     — мг препарата в 50 мл шприце\n"
            "  вес N      — вес пациента (по умолчанию 80 кг)\n\n"
            "Работает для всех 6 вазопрессоров."
        )
        return

    params = _parse_titrate_args(args)
    if not params:
        await update.message.reply_text(
            "Не удалось разобрать параметры.\n"
            "Пример: /titrate норадреналин текущая 0.1 цель 0.3 конц 8 вес 75"
        )
        return

    note = " (вес по умолчанию 80 кг)" if params["assumed_w"] else ""
    table = calculate_titration(
        params["drug"], params["dose_start"], params["dose_end"],
        params["conc_mg"], params["weight"]
    )
    await update.message.reply_text(table + note, parse_mode="Markdown")


async def cmd_missing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pt      = get_pt(ctx)
    skipped = pt.setdefault("skipped_fields", set())

    SYSTEM_LABELS = {
        "cv":    "Гемодинамика",
        "cns":   "ЦНС",
        "renal": "Почки",
        "liver": "Печень",
        "coag":  "Коагуляция",
        "resp":  "Дыхание",
    }

    lines   = ["📋 SOFA — статус параметров:\n"]
    buttons = []

    for sys_, field in SOFA_REQUIRED:
        name   = FIELD_NAMES.get(field, field)
        val    = pt.get(field)
        sys_lbl = SYSTEM_LABELS.get(sys_, sys_)

        if val is not None:
            lines.append(f"  ✅ {sys_lbl} / {name}: {val}")
        elif field in skipped:
            lines.append(f"  ⏭ {sys_lbl} / {name}: пропущено")
            buttons.append([InlineKeyboardButton(
                f"Ввести {name}", callback_data=f"enter:{field}"
            )])
        else:
            lines.append(f"  ❗ {sys_lbl} / {name}: не задано")
            buttons.append([InlineKeyboardButton(
                f"Ввести {name}", callback_data=f"enter:{field}"
            )])

    if not buttons:
        lines.append("\n✓ Все параметры заполнены — бот готов к расчёту.")
        await update.message.reply_text("\n".join(lines))
    else:
        lines.append(f"\nНажми кнопку, чтобы ввести значение:")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=InlineKeyboardMarkup(buttons)
        )


async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    pt      = get_pt(ctx)
    text    = update.message.text.strip()
    skipped = pt.setdefault("skipped_fields", set())

    # ── Режим ожидания конкретного поля ──────────────────────────
    if "waiting_for" in pt:
        field = pt.pop("waiting_for")
        try:
            val = float(text.replace(",", "."))
            pt[field] = int(val) if field in _INT_FIELDS else val
        except ValueError:
            pt["waiting_for"] = field
            await update.message.reply_text(
                "Некорректное значение — введи число.",
                reply_markup=skip_keyboard(field)
            )
            return

    else:
        # ── Обычный ввод: парсим свободный текст ─────────────────
        has_data = any(k not in ("lactate_history", "delta_sofa") for k in pt)
        pt["_base_sofa"]    = sofa_score(pt) if has_data else None
        pt["skipped_fields"] = set()
        skipped              = pt["skipped_fields"]

        parse(text, pt)

        if not any(k not in ("lactate_history", "delta_sofa") for k in pt):
            await update.message.reply_text(
                "Не удалось распознать данные.\n"
                "Отправь /start чтобы увидеть список параметров."
            )
            return

    # ── Проверяем полноту SOFA ────────────────────────────────────
    missing = missing_sofa_fields(pt, skipped)
    if missing:
        _, field = missing[0]
        pt["waiting_for"] = field
        await update.message.reply_text(
            FIELD_HINTS.get(field, f"❗ Нужен: {FIELD_NAMES.get(field, field)}"),
            reply_markup=skip_keyboard(field)
        )
        return

    # ── Все данные есть → считаем ─────────────────────────────────
    _stats["messages"] += 1
    await update.message.reply_text(_finalize(pt, ctx), reply_markup=main_keyboard(ctx))


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _stats["callbacks"] += 1
    pt = get_pt(ctx)

    if q.data == "dash":
        if len([k for k in pt if k not in ("lactate_history", "delta_sofa")]) == 0:
            await q.message.reply_text("Нет данных. Введи данные пациента.")
        else:
            await q.message.reply_text(
                f"```\n{dashboard(pt)}\n```",
                parse_mode="Markdown"
            )

    elif q.data == "recalc":
        if len([k for k in pt if k != "lactate_history"]) == 0:
            await q.message.reply_text("Нет данных. Введи данные пациента.")
        else:
            await q.message.reply_text(build_response(pt), reply_markup=main_keyboard(ctx))

    elif q.data.startswith("enter:"):
        field = q.data.split(":", 1)[1]
        pt.setdefault("skipped_fields", set()).discard(field)
        pt["waiting_for"] = field
        name = FIELD_NAMES.get(field, field)
        await q.message.reply_text(
            FIELD_HINTS.get(field, f"Введи {name}:"),
            reply_markup=skip_keyboard(field)
        )

    elif q.data.startswith("skip:"):
        field = q.data.split(":", 1)[1]
        pt.setdefault("skipped_fields", set()).add(field)
        pt.pop("waiting_for", None)

        missing = missing_sofa_fields(pt, pt.get("skipped_fields", set()))
        if missing:
            _, next_field = missing[0]
            pt["waiting_for"] = next_field
            await q.message.reply_text(
                FIELD_HINTS.get(next_field, f"❗ Нужен: {FIELD_NAMES.get(next_field, next_field)}"),
                reply_markup=skip_keyboard(next_field)
            )
        else:
            await q.message.reply_text(_finalize(pt, ctx), reply_markup=main_keyboard(ctx))

    elif q.data == "clear":
        pid = current_pid(ctx)
        ctx.user_data["patients"][pid] = {"lactate_history": []}
        await q.message.reply_text(
            f"Данные пациента {pid} сброшены.",
            reply_markup=main_keyboard(ctx)
        )

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
        interp = interpret_abg(pt)
        if interp:
            await q.message.reply_text(interp)
        else:
            await q.message.reply_text(ABG_TEXT)

    elif q.data == "lac":
        await q.message.reply_text(LAC_TEXT)

    elif q.data == "shift_report":
        report = build_shift_report(pt)
        await q.message.reply_text(report)

    elif q.data == "trend_report":
        await q.message.reply_text(build_trend_report(pt))

    elif q.data == "export_csv":
        if len([k for k in pt if k != "lactate_history"]) == 0:
            await q.message.reply_text("Нет данных для экспорта.")
        else:
            await q.message.reply_text(
                f"```\n{build_export(pt)}\n```", parse_mode="Markdown"
            )

    elif q.data == "menu_main":
        await q.message.edit_reply_markup(reply_markup=main_keyboard(ctx))

    elif q.data == "menu_protocols":
        await q.message.edit_reply_markup(reply_markup=protocols_keyboard())

    elif q.data == "menu_vasos":
        await q.message.edit_reply_markup(reply_markup=vasos_keyboard())

    elif q.data == "menu_reports":
        await q.message.edit_reply_markup(reply_markup=reports_keyboard())

    elif q.data == "menu_patients":
        await q.message.edit_reply_markup(reply_markup=patients_keyboard(ctx))

    elif q.data == "pt_new":
        _ensure_patients(ctx)
        new_id = max(ctx.user_data["patients"].keys()) + 1
        ctx.user_data["patients"][new_id]  = {"lactate_history": []}
        ctx.user_data["current_pt_id"]     = new_id
        pt = get_pt(ctx)
        await q.message.edit_reply_markup(reply_markup=main_keyboard(ctx))
        await q.message.reply_text(
            f"Создан Пациент {new_id}. Введи данные."
        )

    elif q.data.startswith("pt_switch:"):
        new_id = int(q.data.split(":", 1)[1])
        _ensure_patients(ctx)
        if new_id not in ctx.user_data["patients"]:
            ctx.user_data["patients"][new_id] = {"lactate_history": []}
        ctx.user_data["current_pt_id"] = new_id
        await q.message.edit_reply_markup(reply_markup=main_keyboard(ctx))
        await q.message.reply_text(f"Переключено на Пациент {new_id}.")

    elif q.data == "checklist_open":
        if "checklist" not in pt:
            pt["checklist"] = {
                "started_at": datetime.now(timezone.utc).timestamp(),
                "items": {iid: {"done": False} for iid, _ in CHECKLIST_ITEMS},
            }
            if pt.get("lactate") is not None:
                pt["checklist"]["items"]["lactate"]["done"]    = True
                pt["checklist"]["items"]["lactate"]["done_at"] = pt["checklist"]["started_at"]
        text, kbd = _checklist_display(pt["checklist"])
        await q.message.reply_text(text, reply_markup=kbd)

    elif q.data == "checklist_reset":
        pt["checklist"] = {
            "started_at": datetime.now(timezone.utc).timestamp(),
            "items": {iid: {"done": False} for iid, _ in CHECKLIST_ITEMS},
        }
        text, kbd = _checklist_display(pt["checklist"])
        await q.message.edit_text(text, reply_markup=kbd)

    elif q.data.startswith("checklist:"):
        item_id = q.data.split(":", 1)[1]
        if "checklist" not in pt:
            pt["checklist"] = {
                "started_at": datetime.now(timezone.utc).timestamp(),
                "items": {iid: {"done": False} for iid, _ in CHECKLIST_ITEMS},
            }
        pt["checklist"]["items"][item_id] = {
            "done":    True,
            "done_at": datetime.now(timezone.utc).timestamp(),
        }
        text, kbd = _checklist_display(pt["checklist"])
        await q.message.edit_text(text, reply_markup=kbd)

    elif q.data == "titrate_help":
        pt = get_pt(ctx)
        known = [v for v in (pt.get("vasopressors") or [])
                 if v.get("rate") and v.get("conc_mg")]
        if known:
            btns = [[InlineKeyboardButton(
                f"🎯 {v['drug'].capitalize()}  скорость {v['rate']} мл/ч  конц {v['conc_mg']} мг",
                callback_data=f"titrate:{v['drug']}"
            )] for v in known]
            await q.message.reply_text(
                "Выбери препарат — бот рассчитает шаги титрования от текущей дозы:",
                reply_markup=InlineKeyboardMarkup(btns)
            )
        else:
            await q.message.reply_text(
                "📈 КАЛЬКУЛЯТОР ТИТРОВАНИЯ\n\n"
                "Используй команду:\n"
                "/titrate норадреналин текущая 0.1 цель 0.3 конц 8 вес 75\n\n"
                "Или сначала введи данные о вазопрессоре:\n"
                "  норадреналин скорость 6 конц 8 вес 75\n"
                "— тогда кнопка предложит препараты автоматически."
            )

    elif q.data.startswith("titrate:"):
        drug = q.data.split(":", 1)[1]
        vasos = pt.get("vasopressors") or []
        entry = next((v for v in vasos if v["drug"] == drug), None)
        if not entry or not entry.get("rate") or not entry.get("conc_mg"):
            await q.message.reply_text(
                f"Нет данных о скорости/концентрации {drug}.\n"
                f"Введи: {drug} скорость N конц N"
            )
            return
        w = pt.get("weight") or 80
        # Текущая доза из скорости (прямой расчёт)
        is_vaso = (drug == "вазопрессин")
        if is_vaso:
            cur_dose = round((entry["rate"] * entry["conc_mg"]) / (50 * 60), 4)
        else:
            cur_dose = round(
                (entry["rate"] * entry["conc_mg"] * 1000) / (50 * 60 * w), 3
            )
        # Целевая доза из CONFIG или +50% от текущей
        td_entry = next(
            (d for d in (pt.get("target_doses") or []) if d["drug"] == drug), None
        )
        target = (td_entry or {}).get("target_dose") or round(cur_dose * 1.5, 3)
        table = calculate_titration(drug, cur_dose, target, entry["conc_mg"], w)
        assumed = "" if pt.get("weight") else " (вес 80 кг — по умолчанию)"
        await q.message.reply_text(table + assumed, parse_mode="Markdown")

    elif q.data == "rate_help":
        cp = calculate_clinical_params(pt)
        # Если есть данные — показываем готовые обратные расчёты
        tr_list = cp.get("target_rate_results", [])
        if tr_list:
            lines = ["🎯 РАСЧЁТ СКОРОСТИ ПО ЦЕЛЕВОЙ ДОЗЕ\n"]
            w_note = f"вес {cp['weight_used']} кг{'*' if cp['weight_assumed'] else ''}"
            for tr in tr_list:
                tier_str = f" ({tr['tier']})" if tr["tier"] else ""
                lines.append(
                    f"💉 {tr['drug'].capitalize()}: цель {tr['target_dose']} {tr['unit']}{tier_str}\n"
                    f"   → скорость {tr['rate_ml_h']} мл/ч\n"
                    f"   (конц {tr['conc_mg']} мг/50мл, {w_note})"
                )
            lines.append(
                "\nДля нового расчёта введи:\n"
                "  норадреналин доза 0.3 конц 8 вес 75\n"
                "  допамин доза 8 конц 200"
            )
            await q.message.reply_text("\n".join(lines))
        else:
            await q.message.reply_text(
                "🎯 РАСЧЁТ СКОРОСТИ ПО ЦЕЛЕВОЙ ДОЗЕ\n\n"
                "Формула: скорость (мл/ч) = доза × вес × 50 × 60 / (конц × 1000)\n\n"
                "Введи в чат:\n"
                "  норадреналин доза 0.2 конц 8 вес 75\n"
                "  допамин доза 5 конц 200 вес 80\n"
                "  адреналин доза 0.1 конц 2\n"
                "  вазопрессин доза 0.02 конц 20\n\n"
                "Поддерживаемые препараты:\n"
                "  норадреналин, адреналин, допамин,\n"
                "  добутамин, мезатон, вазопрессин\n\n"
                "💡 Концентрация — мг препарата в 50 мл шприце.\n"
                "   Если вес не указан — используется 80 кг."
            )


async def cmd_pt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /pt        — список пациентов с кнопками переключения
    /pt N      — переключиться на пациента N
    /pt new    — создать нового пациента
    """
    _ensure_patients(ctx)
    raw  = (update.message.text or "").strip()
    args = re.sub(r"^/pt\S*\s*", "", raw, flags=re.I).strip().lower()

    if args == "new":
        new_id = max(ctx.user_data["patients"].keys()) + 1
        ctx.user_data["patients"][new_id]  = {"lactate_history": []}
        ctx.user_data["current_pt_id"]     = new_id
        await update.message.reply_text(
            f"Создан Пациент {new_id}. Введи данные.",
            reply_markup=main_keyboard(ctx)
        )
        return

    if args.isdigit():
        new_id = int(args)
        if new_id not in ctx.user_data["patients"]:
            ctx.user_data["patients"][new_id] = {"lactate_history": []}
        ctx.user_data["current_pt_id"] = new_id
        await update.message.reply_text(
            f"Переключено на Пациент {new_id}.",
            reply_markup=main_keyboard(ctx)
        )
        return

    # Без аргументов — показываем список
    patients = ctx.user_data["patients"]
    pid      = ctx.user_data.get("current_pt_id", 1)
    lines    = ["👥 ПАЦИЕНТЫ\n"]
    for p_id in sorted(patients.keys()):
        p = patients[p_id]
        has = any(k not in ("lactate_history", "delta_sofa") for k in p)
        mark = "▶ " if p_id == pid else "  "
        sofa = sofa_score(p) if has else "—"
        apch = apache_score(p) if has else "—"
        tag  = f"SOFA {sofa}  APACHE {apch}" if has else "(нет данных)"
        lines.append(f"{mark}Пациент {p_id}: {tag}")

    btns = []
    for p_id in sorted(patients.keys()):
        mark = "✓ " if p_id == pid else ""
        btns.append([_IKB(f"{mark}Пациент {p_id}", f"pt_switch:{p_id}")])
    btns.append([_IKB("➕ Новый пациент", "pt_new")])
    btns.append([_IKB("← Главное меню",  "menu_main")])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(btns)
    )


# =============================
# WATCHDOG — фоновая задача
# =============================
async def _watchdog_loop(app):
    """Каждые 60 сек проверяет связь с Telegram и печатает heartbeat."""
    _stats["started_at"] = datetime.now()
    while True:
        await asyncio.sleep(60)
        try:
            me = await app.bot.get_me()
            _stats["last_ok"] = datetime.now()
            uptime   = datetime.now() - _stats["started_at"]
            total_s  = int(uptime.total_seconds())
            h, rem   = divmod(total_s, 3600)
            m        = rem // 60
            print(
                f"[✓ watchdog] {datetime.now().strftime('%H:%M')} | "
                f"uptime {h}ч {m:02d}м | "
                f"msgs: {_stats['messages']} | "
                f"buttons: {_stats['callbacks']} | "
                f"@{me.username}"
            )
        except Exception as e:
            print(f"[⚠ watchdog] {datetime.now().strftime('%H:%M')} ОШИБКА: {e}")


async def _post_init(app):
    """Хук после инициализации приложения — запускаем watchdog."""
    asyncio.create_task(_watchdog_loop(app))


# =============================
# ЗАПУСК
# =============================
async def error_handler(update, context):
    import telegram.error
    err = context.error
    if isinstance(err, telegram.error.Conflict):
        await asyncio.sleep(5)
        return
    print(f"[ERROR] {type(err).__name__}: {err}")
    if update and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Произошла внутренняя ошибка при обработке запроса.\n"
                "Попробуй ещё раз или нажми /start чтобы начать заново."
            )
        except Exception:
            pass


def main():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN не задан. Добавь его в секреты.")

    import time, urllib.request, json as _json
    print("Ожидаю освобождения сессии Telegram (15 сек)...")
    time.sleep(15)
    try:
        resp = urllib.request.urlopen(
            f"https://api.telegram.org/bot{TOKEN}/getUpdates?offset=-1&timeout=0",
            timeout=5,
        )
        print("Сессия свободна, стартую polling.")
    except Exception:
        pass

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(10)
        .read_timeout(10)
        .write_timeout(10)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("help",      cmd_help))
    app.add_handler(CommandHandler("export",    cmd_export))
    app.add_handler(CommandHandler("missing",   cmd_missing))
    app.add_handler(CommandHandler("trend",     cmd_trend))
    app.add_handler(CommandHandler("titrate",   cmd_titrate))
    app.add_handler(CommandHandler("checklist", cmd_checklist))
    app.add_handler(CommandHandler("shift",     cmd_shift))
    app.add_handler(CommandHandler("pt",        cmd_pt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query"],
        poll_interval=2.0,
        timeout=10,
    )


if __name__ == "__main__":
    main()
