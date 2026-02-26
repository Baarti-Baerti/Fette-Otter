"""
garmin/fetcher.py
─────────────────
Raw data fetching from Garmin Connect using garth.

All functions accept a garth.Client and return raw API responses (dicts/lists).
The transform layer (garmin/transform.py) is responsible for normalising these
into the shape expected by the dashboard.

Garmin Connect endpoints used:
  - /usersummary-service/usersummary/daily/{date}          → daily summary
  - /wellness-service/wellness/dailyMovement/{date}        → steps / distance
  - /wellness-service/wellness/bodyComposition/{date}      → BMI / weight
  - /activitylist-service/activities/search/activities     → activity list
  - /fitnessstats-service/fitness/stats/user/{date}/{n}    → multi-day fitness
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import garth


# ── helpers ──────────────────────────────────────────────────────────────────

def _date_str(d: date | str) -> str:
    return d.isoformat() if isinstance(d, date) else d


def _date_range(start: date, days: int) -> list[str]:
    return [_date_str(start + timedelta(days=i)) for i in range(days)]


# ── daily summary ─────────────────────────────────────────────────────────────

def fetch_daily_summary(client: garth.Client, for_date: date) -> dict[str, Any]:
    """
    Fetches the Garmin user daily summary for a given date.
    Includes: totalKilocalories, activeKilocalories, bmrKilocalories,
              totalSteps, totalDistanceMeters, averageStressLevel, etc.
    """
    ds = _date_str(for_date)
    return client.connectapi(
        f"/usersummary-service/usersummary/daily/{ds}",
        params={"calendarDate": ds},
    )


def fetch_daily_summaries(
    client: garth.Client, start: date, days: int
) -> list[dict[str, Any]]:
    """Fetch daily summaries for `days` consecutive days starting from `start`."""
    results = []
    for ds in _date_range(start, days):
        try:
            data = client.connectapi(
                f"/usersummary-service/usersummary/daily/{ds}",
                params={"calendarDate": ds},
            )
            results.append(data)
        except Exception:
            results.append({"calendarDate": ds})
    return results


def fetch_steps_range(client: garth.Client, start: date, end: date) -> int:
    """
    Returns total steps between start and end (inclusive).
    Tries several known Garmin endpoints, returns 0 if none work.
    """
    total = 0

    # Strategy 1: /usersummary-service/usersummary/daily/range — bulk daily summaries
    try:
        data = client.connectapi(
            "/usersummary-service/usersummary/daily/range",
            params={"startDate": _date_str(start), "endDate": _date_str(end)},
        )
        # Response is either a list or {"dailySummaries": [...]}
        entries = data if isinstance(data, list) else data.get("dailySummaries") or data.get("allSummaries") or []
        if entries:
            for e in entries:
                total += int(e.get("totalSteps") or e.get("steps") or 0)
            return total
    except Exception:
        pass

    # Strategy 2: /wellness-service/wellness/dailyMovement/{date} — per-day steps
    try:
        for ds in _date_range(start, (end - start).days + 1):
            try:
                data = client.connectapi(f"/wellness-service/wellness/dailyMovement/{ds}")
                # Response has a list of step entries; sum them
                entries = data if isinstance(data, list) else []
                for e in entries:
                    total += int(e.get("steps") or e.get("totalSteps") or 0)
            except Exception:
                pass
        if total > 0:
            return total
    except Exception:
        pass

    # Strategy 3: /usersummary-service/usersummary/daily/{date} — one at a time
    try:
        for ds in _date_range(start, (end - start).days + 1):
            try:
                data = client.connectapi(
                    f"/usersummary-service/usersummary/daily/{ds}",
                    params={"calendarDate": ds},
                )
                total += int(data.get("totalSteps") or data.get("steps") or 0)
            except Exception:
                pass
        return total
    except Exception:
        pass

    return 0


# ── body composition / BMI ────────────────────────────────────────────────────

def fetch_body_composition(
    client: garth.Client, start: date, end: date
) -> dict[str, Any]:
    """
    Fetches body composition data for a date range.
    Tries multiple known Garmin endpoint variants and returns the first
    response that contains weight entries.
    """
    endpoints = [
        ("/weight-service/weight/dateRange", {"startDate": _date_str(start), "endDate": _date_str(end)}),
        ("/weight-service/weight/range",     {"startDate": _date_str(start), "endDate": _date_str(end)}),
    ]
    for path, params in endpoints:
        try:
            data = client.connectapi(path, params=params)
            entries = data.get("dateWeightList") or data.get("allWeightMetrics") or []
            if entries:
                return data
        except Exception:
            continue
    return {}


def _extract_bmi(entries: list, height_m: float | None = None) -> float | None:
    """
    Given weight entries (newest last), return the last BMI value.
    Uses the bmi field directly if present, otherwise calculates from
    weight (grams in Garmin) + height_m.
    """
    for entry in reversed(entries):
        bmi = entry.get("bmi")
        if bmi is not None and float(bmi) > 0:
            return round(float(bmi), 1)
        if height_m and height_m > 0:
            weight = entry.get("weight")
            if weight:
                # Garmin stores weight in grams
                weight_kg = weight / 1000.0 if weight > 500 else float(weight)
                calculated = weight_kg / (height_m ** 2)
                if 10 < calculated < 60:   # sanity check
                    return round(calculated, 1)
    return None


def fetch_user_height(client: garth.Client) -> float | None:
    """Returns the user's height in metres from their Garmin profile, or None."""
    try:
        profile = client.connectapi("/userprofile-service/userprofile")
        height_cm = (
            profile.get("userInfo", {}).get("height")
            or profile.get("height")
        )
        if height_cm and float(height_cm) > 0:
            return float(height_cm) / 100.0
    except Exception:
        pass
    return None


def fetch_latest_bmi(client: garth.Client) -> float | None:
    """
    Returns the most recently recorded BMI, searching back up to 365 days.
    Falls back to calculating from weight + height if bmi field is absent.
    """
    try:
        today    = date.today()
        height_m = fetch_user_height(client)
        data     = fetch_body_composition(client, today - timedelta(days=365), today)
        entries  = data.get("dateWeightList") or data.get("allWeightMetrics", [])
        return _extract_bmi(entries, height_m)
    except Exception:
        return None


def fetch_bmi_for_month(client: garth.Client, year: int, month: int) -> float | None:
    """
    Returns the last recorded BMI within a specific calendar month.
    Falls back to calculating from weight + height if bmi field is absent.
    """
    try:
        import calendar as cal_mod
        _, last_day = cal_mod.monthrange(year, month)
        start    = date(year, month, 1)
        end      = date(year, month, last_day)
        height_m = fetch_user_height(client)
        data     = fetch_body_composition(client, start, end)
        entries  = data.get("dateWeightList") or data.get("allWeightMetrics", [])
        return _extract_bmi(entries, height_m)
    except Exception:
        return None


def fetch_profile_picture(client: garth.Client) -> str:
    """Fetch the user's Garmin Connect profile picture URL."""
    try:
        profile = client.connectapi("/userprofile-service/socialProfile")
        return (
            profile.get("profileImageUrlLarge")
            or profile.get("profileImageUrlMedium")
            or profile.get("profileImageUrl")
            or ""
        )
    except Exception:
        return ""



# ── activities ────────────────────────────────────────────────────────────────

ACTIVITY_TYPE_MAP = {
    # ── Running (all variants → "Running") ───────────────────────
    "running":                          "Running",
    "trail_running":                    "Running",
    "treadmill_running":                "Running",
    "indoor_running":                   "Running",
    "ultra_run":                        "Running",
    "obstacle_run":                     "Running",
    "virtual_run":                      "Running",
    "street_running":                   "Running",
    "track_running":                    "Running",
    "fitness_equipment_running":        "Running",
    "snow_shoe_running":                "Running",
    "run":                              "Running",
    # ── Cycling ──────────────────────────────────────────────────
    "cycling":              "Cycling",
    "mountain_biking":      "Cycling",
    "gravel_cycling":       "Cycling",
    "road_biking":          "Cycling",
    "cyclocross":           "Cycling",
    "bmx":                  "Cycling",
    "indoor_cycling":       "VirtualCycling",
    "virtual_ride":         "VirtualCycling",
    "virtual_cycling":      "VirtualCycling",
    # ── Swimming ─────────────────────────────────────────────────
    "swimming":             "Swimming",
    "lap_swimming":         "Swimming",
    "open_water_swimming":  "Swimming",
    # ── Skiing ───────────────────────────────────────────────────
    "skiing":               "Skiing",
    "resort_skiing":        "Skiing",
    "resort_skiing_snowboarding_ws":          "Skiing",
    "backcountry_skiing_snowboarding_ws":     "Skiing",
    "backcountry_skiing":   "Skiing",
    "skate_skiing_ws":      "Skiing",
    "skate_skiing":         "Skiing",
    "cross_country_skiing_ws": "Skiing",
    "cross_country_skiing": "Skiing",
    "snowboarding":         "Skiing",
    "snow_shoe_ws":         "Skiing",
    "snow_shoe":            "Skiing",
    "nordic_combined":      "Skiing",
    "alpine_skiing":        "Skiing",
    "telemark_skiing":      "Skiing",
    # ── Walking ──────────────────────────────────────────────────
    "walking":              "Walking",
    "hiking":               "Walking",
    "trail_hiking":         "Walking",
    # ── Other ────────────────────────────────────────────────────
    "yoga":                 "Yoga",
    "strength_training":    "Strength",
    "hiit":                 "HIIT",
    "cardio_training":      "HIIT",
}


def fetch_activities(
    client: garth.Client, start: date, end: date, limit: int = 200
) -> list[dict[str, Any]]:
    """
    Fetch activity list for a date range.
    NOTE: Garmin's API may ignore startDate/endDate, so we filter client-side.
    """
    raw = client.connectapi(
        "/activitylist-service/activities/search/activities",
        params={
            "startDate": _date_str(start),
            "endDate":   _date_str(end),
            "limit":     limit,
            "start":     0,
            "_":         "",
        },
    ) or []

    # Filter client-side to ensure we only return activities in the date range
    start_str = start.isoformat()
    end_str   = end.isoformat()
    filtered = []
    for a in raw:
        act_date = (a.get("startTimeLocal") or "")[:10]
        if act_date and start_str <= act_date <= end_str:
            filtered.append(a)
    return filtered


def fetch_activities_for_month(
    client: garth.Client, year: int, month: int
) -> list[dict[str, Any]]:
    """Fetch all activities in a calendar month."""
    from calendar import monthrange
    _, last_day = monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)
    return fetch_activities(client, start, end)


# ── weekly/today range helpers ────────────────────────────────────────────────

def fetch_activities_last_n_days(
    client: garth.Client, days: int
) -> list[dict[str, Any]]:
    """Fetch activities from the last N days (today inclusive)."""
    today = date.today()
    start = today - timedelta(days=days - 1)
    return fetch_activities(client, start, today)
