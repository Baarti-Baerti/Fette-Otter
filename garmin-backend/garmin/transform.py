"""
garmin/transform.py
────────────────────
Transforms raw Garmin Connect API responses into the exact data shape
consumed by the Squad Stats dashboard frontend.

The dashboard expects a top-level user object with this structure:

{
  id, name, role, emoji, color, bg, garminDevice, types,
  calories,    # int — total kcal for selected time range
  workouts,    # int — session count
  km,          # float — distance in km
  actKcal,     # int — active (non-BMR) kcal
  bmi,         # float — most recent BMI
  week,        # [bool x 7] — active day flags Mon→Sun
  weekCalories,# [int x 7] — daily active kcal Mon→Sun
  monthly: [   # 12 elements, one per month
    { cal, sess, km, actKcal, bmi, days: [int x 28] }
  ]
}
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any

from .fetcher import ACTIVITY_TYPE_MAP


# ── activity normalisation ────────────────────────────────────────────────────

def _normalise_activity(act: dict[str, Any]) -> dict[str, Any]:
    """Flatten and normalise a single activity dict from the activities API."""
    atype_raw = (
        act.get("activityType", {}).get("typeKey", "")
        if isinstance(act.get("activityType"), dict)
        else str(act.get("activityType", ""))
    ).lower()

    # Explicit map first, then prefix-based fallback so unknown variants
    # (e.g. "indoor_running", "fitness_equipment_running") still resolve correctly
    if atype_raw in ACTIVITY_TYPE_MAP:
        mapped = ACTIVITY_TYPE_MAP[atype_raw]
    elif "run" in atype_raw:
        mapped = "Running"
    elif "walk" in atype_raw or "hik" in atype_raw:
        mapped = "Walking"
    elif "cycl" in atype_raw or "bik" in atype_raw or "ride" in atype_raw:
        mapped = "Cycling"
    elif "swim" in atype_raw:
        mapped = "Swimming"
    elif "ski" in atype_raw or "snowboard" in atype_raw:
        mapped = "Skiing"
    else:
        mapped = atype_raw.replace("_", " ").title()

    return {
        "id":          act.get("activityId"),
        "name":        act.get("activityName", ""),
        "type_raw":    atype_raw,
        "type":        mapped,
        "date":        (act.get("startTimeLocal") or "")[:10],
        "calories":    int(act.get("calories") or 0),
        "active_kcal": int(act.get("activeKilocalories") or act.get("calories") or 0),
        "distance_m":  float(act.get("distance") or 0),
        "duration_s":  float(act.get("duration") or 0),
    }


def _km(distance_m: float) -> float:
    return round(distance_m / 1000, 1)


def _km_by_type(activities: list[dict[str, Any]]) -> dict[str, float]:
    """Return a dict of {activity_type: total_km} for activities with distance."""
    result: dict[str, float] = {}
    for a in activities:
        if a["distance_m"] > 0:
            t = a["type"] or "Other"
            result[t] = round(result.get(t, 0) + a["distance_m"] / 1000, 1)
    return result


def _split_km(activities: list[dict[str, Any]]) -> dict[str, float]:
    """Return per-category km split for leaderboard columns."""
    run = cycle = vcycle = swim = ski = walk = other = 0.0
    for a in activities:
        if a["distance_m"] <= 0:
            continue
        km = a["distance_m"] / 1000
        t = a["type"] or ""
        if t == "Running":        run   += km
        elif t == "Cycling":      cycle  += km
        elif t == "VirtualCycling": vcycle += km
        elif t == "Swimming":     swim  += km
        elif t == "Skiing":       ski   += km
        elif t == "Walking":      walk  += km
        else:                     other += km
    return {
        "runKm":      round(run, 1),
        "cycleKm":    round(cycle, 1),
        "virtualKm":  round(vcycle, 1),
        "swimKm":     round(swim, 1),
        "skiKm":      round(ski, 1),
        "walkKm":     round(walk, 1),
        "otherKm":    round(other, 1),
    }


# ── weekly summary ────────────────────────────────────────────────────────────

def build_week_summary(
    activities: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
    range_days: int = 7,
) -> dict[str, Any]:
    """
    Build the weekly-level stats consumed by the overview page.

    Returns:
        calories, workouts, km, actKcal,
        week (7 bools), weekCalories (7 ints)
    """
    norms = [_normalise_activity(a) for a in activities]

    today = date.today()
    # Build a 7-day window ending today (Mon=0 … Sun=6 in ISO weekday)
    week_start = today - timedelta(days=6)
    week_dates = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]

    # Daily active kcal and steps from daily summaries
    daily_active: dict[str, int] = {}
    total_steps = 0
    for s in summaries:
        d = s.get("calendarDate", "")
        daily_active[d] = int(s.get("activeKilocalories") or 0)
        # Garmin may return steps under different field names depending on endpoint/device
        step_val = (
            s.get("totalSteps")
            or s.get("steps")
            or s.get("dailySteps")
            or s.get("stepCount")
            or 0
        )
        total_steps += int(step_val or 0)

    # Per-day activity flags and calorie totals
    day_flags = []
    day_cals = []
    for d in week_dates:
        day_acts = [a for a in norms if a["date"] == d]
        day_flags.append(1 if day_acts else 0)
        # Sum active kcal from daily summary if available; else from activities
        if d in daily_active and daily_active[d] > 0:
            day_cals.append(daily_active[d])
        else:
            day_cals.append(sum(a["active_kcal"] for a in day_acts))

    # Scale calories/km/actKcal to the selected range
    scale = range_days / 7
    total_activities = [a for a in norms]  # all fetched

    split = _split_km(total_activities)
    return {
        "calories":    int(sum(a["calories"] for a in total_activities) * scale),
        "workouts":    len(total_activities),
        "km":          _km(sum(a["distance_m"] for a in total_activities)),
        "actKcal":     int(sum(a["active_kcal"] for a in total_activities) * scale),
        "steps":       total_steps,
        "week":        day_flags,
        "weekCalories": day_cals,
        "kmByType":    _km_by_type(total_activities),
        "runKm":       split["runKm"],
        "cycleKm":     split["cycleKm"],
        "virtualKm":   split["virtualKm"],
        "swimKm":      split["swimKm"],
        "skiKm":       split["skiKm"],
        "walkKm":      split["walkKm"],
        "otherKm":     split["otherKm"],
    }


# ── monthly summary ───────────────────────────────────────────────────────────

def build_month_summary(
    activities: list[dict[str, Any]],
    bmi: float | None,
    year: int,
    month: int,
) -> dict[str, Any]:
    """
    Build one month's entry for the `monthly` array.

    Returns:
        { cal, sess, km, actKcal, bmi, days }
    where `days` is a 28-element list of daily active kcal (0 = rest day).
    """
    norms = [_normalise_activity(a) for a in activities]

    cal    = sum(a["calories"]   for a in norms)
    sess   = len(norms)
    km     = _km(sum(a["distance_m"] for a in norms))
    actKcal= sum(a["active_kcal"] for a in norms)
    runKm  = _km(sum(a["distance_m"] for a in norms if a["type"] == "Running"))

    # Build 28-day array (we cap at 28 for display uniformity)
    _, last_day = calendar.monthrange(year, month)
    days: list[int] = []
    for day_num in range(1, 29):
        if day_num > last_day:
            days.append(0)
            continue
        d = date(year, month, day_num).isoformat()
        day_acts = [a for a in norms if a["date"] == d]
        days.append(sum(a["active_kcal"] for a in day_acts))

    return {
        "year":     year,
        "month":    month,
        "cal":      cal,
        "sess":     sess,
        "km":       km,
        "runKm":    runKm,
        "actKcal":  actKcal,
        "bmi":      round(bmi, 1) if bmi else None,
        "days":     days,
    }


# ── full user payload ─────────────────────────────────────────────────────────

def build_user_payload(
    roster_entry: dict[str, Any],
    week_activities: list[dict[str, Any]],
    week_summaries: list[dict[str, Any]],
    monthly_activities: dict[str, list[dict[str, Any]]],
    monthly_bmis: dict[str, float | None],
    range_start: date | None = None,
    range_end: date | None = None,
    range_days: int = 7,   # kept for backward compat, ignored if range_start provided
    bmi: float | None = None,
    steps: int = 0,
) -> dict[str, Any]:
    """
    Assemble the complete user data object the dashboard frontend needs.
    """
    today = date.today()
    # Determine the exact date window for the overview leaderboard split
    if range_start is None:
        range_start = today - timedelta(days=range_days - 1)
    if range_end is None:
        range_end = today

    week = build_week_summary(week_activities, week_summaries, range_days)

    # Build monthly array: Jan → current month of current year
    monthly = []
    months_keys: list[tuple[int, int]] = [(today.year, mo) for mo in range(1, today.month + 1)]
    # Also include last month if range_start is in the prior month (e.g. "last month" in January)
    if range_start.month != today.month or range_start.year != today.year:
        lm = (range_start.year, range_start.month)
        if lm not in months_keys:
            months_keys = [lm] + months_keys

    for (yr, mo) in months_keys:
        key = f"{yr}-{mo:02d}"
        acts = monthly_activities.get(key, [])
        m_bmi = monthly_bmis.get(key, bmi)
        monthly.append(build_month_summary(acts, m_bmi, yr, mo))

    # Derive split km from monthly data filtered to the exact range window.
    # This guarantees overview leaderboard matches the points table.
    range_start_str = range_start.isoformat()
    range_end_str   = range_end.isoformat()
    range_acts = [
        a
        for (yr, mo) in months_keys
        for a in [_normalise_activity(raw) for raw in monthly_activities.get(f"{yr}-{mo:02d}", [])]
        if range_start_str <= a["date"] <= range_end_str
    ]
    range_split = _split_km(range_acts)

    # Derive activity types from recent activities
    recent_norms = [_normalise_activity(a) for a in week_activities]
    seen_types = list(dict.fromkeys(a["type"] for a in recent_norms if a["type"]))
    types = seen_types or roster_entry.get("types", [])

    return {
        **{k: roster_entry[k] for k in ("id", "name", "role", "emoji", "color", "bg", "garminDevice")},
        "types":        types,
        "calories":     week["calories"],
        "workouts":     len(range_acts),
        "km":           round(sum(a["distance_m"] for a in range_acts) / 1000, 1),
        "runKm":        range_split["runKm"],
        "cycleKm":      range_split["cycleKm"],
        "virtualKm":    range_split["virtualKm"],
        "swimKm":       range_split["swimKm"],
        "skiKm":        range_split["skiKm"],
        "walkKm":       range_split["walkKm"],
        "otherKm":      range_split["otherKm"],
        "actKcal":      week["actKcal"],
        "steps":        steps,
        "bmi":          round(bmi, 1) if bmi else 0.0,
        "week":         week["week"],
        "weekCalories": week["weekCalories"],
        "kmByType":     _km_by_type(range_acts),
        "monthly":      monthly,
    }
