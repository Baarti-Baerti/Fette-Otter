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

    return {
        "id":        act.get("activityId"),
        "name":      act.get("activityName", ""),
        "type_raw":  atype_raw,
        "type":      ACTIVITY_TYPE_MAP.get(atype_raw, atype_raw.replace("_", " ").title()),
        "date":      (act.get("startTimeLocal") or "")[:10],  # YYYY-MM-DD
        "calories":  int(act.get("calories") or 0),
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
    """Return runKm, cycleKm, otherKm split for leaderboard sorting."""
    run = cycle = other = 0.0
    for a in activities:
        if a["distance_m"] <= 0:
            continue
        km = a["distance_m"] / 1000
        t = a["type"] or ""
        if t == "Running":
            run += km
        elif t == "Cycling":
            cycle += km
        else:
            other += km
    return {
        "runKm":   round(run, 1),
        "cycleKm": round(cycle, 1),
        "otherKm": round(other, 1),
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

    # Daily active kcal: prefer daily summary activeKilocalories, fall back to activity sum
    daily_active: dict[str, int] = {}
    for s in summaries:
        d = s.get("calendarDate", "")
        daily_active[d] = int(s.get("activeKilocalories") or 0)

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
        "calories":  int(sum(a["calories"] for a in total_activities) * scale),
        "workouts":  len(total_activities),
        "km":        _km(sum(a["distance_m"] for a in total_activities)),
        "actKcal":   int(sum(a["active_kcal"] for a in total_activities) * scale),
        "week":      day_flags,
        "weekCalories": day_cals,
        "kmByType":  _km_by_type(total_activities),
        "runKm":     split["runKm"],
        "cycleKm":   split["cycleKm"],
        "otherKm":   split["otherKm"],
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
        "cal":      cal,
        "sess":     sess,
        "km":       km,
        "actKcal":  actKcal,
        "bmi":      round(bmi, 1) if bmi else None,
        "days":     days,
    }


# ── full user payload ─────────────────────────────────────────────────────────

def build_user_payload(
    roster_entry: dict[str, Any],
    week_activities: list[dict[str, Any]],
    week_summaries: list[dict[str, Any]],
    monthly_activities: dict[str, list[dict[str, Any]]],  # key: "YYYY-MM"
    monthly_bmis: dict[str, float | None],                # key: "YYYY-MM"
    range_days: int = 7,
    bmi: float | None = None,
) -> dict[str, Any]:
    """
    Assemble the complete user data object the dashboard frontend needs.

    Args:
        roster_entry:       Config entry from config/team.py
        week_activities:    Activities list for the selected week range
        week_summaries:     Daily summary list for the same range
        monthly_activities: Dict of "YYYY-MM" -> activity list, 12 months
        monthly_bmis:       Dict of "YYYY-MM" -> BMI float or None
        range_days:         1, 7, or 28 (today / 1W / 4W selector)
        bmi:                Most recent BMI for this user
    """
    week = build_week_summary(week_activities, week_summaries, range_days)

    # Build monthly array in the same order as the dashboard: Feb→Jan
    monthly = []
    today = date.today()
    # Generate 12 months ending with the current month
    months_keys: list[tuple[int, int]] = []
    for i in range(11, -1, -1):
        m_date = date(today.year, today.month, 1) - timedelta(days=30 * i)
        months_keys.append((m_date.year, m_date.month))

    for (yr, mo) in months_keys:
        key = f"{yr}-{mo:02d}"
        acts = monthly_activities.get(key, [])
        m_bmi = monthly_bmis.get(key, bmi)
        monthly.append(build_month_summary(acts, m_bmi, yr, mo))

    # Derive activity types from recent activities
    recent_norms = [_normalise_activity(a) for a in week_activities]
    seen_types = list(dict.fromkeys(
        a["type"] for a in recent_norms if a["type"]
    ))
    types = seen_types or roster_entry.get("types", [])

    return {
        **{k: roster_entry[k] for k in ("id", "name", "role", "emoji", "color", "bg", "garminDevice")},
        "types":       types,
        "calories":    week["calories"],
        "workouts":    week["workouts"],
        "km":          week["km"],
        "runKm":       week.get("runKm", 0),
        "cycleKm":     week.get("cycleKm", 0),
        "otherKm":     week.get("otherKm", 0),
        "actKcal":     week["actKcal"],
        "bmi":         round(bmi, 1) if bmi else 0.0,
        "week":        week["week"],
        "weekCalories": week["weekCalories"],
        "kmByType":    week.get("kmByType", {}),
        "monthly":     monthly,
    }
