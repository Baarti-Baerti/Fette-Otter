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


# ── body composition / BMI ────────────────────────────────────────────────────

def fetch_body_composition(
    client: garth.Client, start: date, end: date
) -> dict[str, Any]:
    """
    Fetches body composition data (includes BMI, weight) for a date range.
    Returns the raw response dict with a 'dateWeightList' key.
    """
    return client.connectapi(
        "/weight-service/weight/range",
        params={
            "startDate": _date_str(start),
            "endDate": _date_str(end),
        },
    )


def fetch_latest_bmi(client: garth.Client) -> float | None:
    """
    Returns the most recently recorded BMI value, or None if unavailable.
    Searches back up to 90 days.
    """
    try:
        today = date.today()
        data = fetch_body_composition(client, today - timedelta(days=90), today)
        entries = data.get("dateWeightList") or data.get("allWeightMetrics", [])
        bmi_entries = [e for e in entries if e.get("bmi") is not None]
        if not bmi_entries:
            return None
        return bmi_entries[-1].get("bmi")
    except Exception:
        return None


# ── activities ────────────────────────────────────────────────────────────────

ACTIVITY_TYPE_MAP = {
    "running":              "Running",
    "trail_running":        "Running",
    "treadmill_running":    "Running",
    "ultra_run":            "Running",
    "obstacle_run":         "Running",
    "cycling":              "Cycling",
    "mountain_biking":      "Cycling",
    "gravel_cycling":       "Cycling",
    "road_biking":          "Cycling",
    "cyclocross":           "Cycling",
    "bmx":                  "Cycling",
    "indoor_cycling":       "VirtualCycling",
    "virtual_ride":         "VirtualCycling",
    "virtual_cycling":      "VirtualCycling",
    "swimming":             "Swimming",
    "lap_swimming":         "Swimming",
    "open_water_swimming":  "Swimming",
    "skiing":               "Skiing",
    "resort_skiing_snowboarding_ws": "Skiing",
    "backcountry_skiing_snowboarding_ws": "Skiing",
    "skate_skiing_ws":      "Skiing",
    "cross_country_skiing_ws": "Skiing",
    "snowboarding":         "Skiing",
    "yoga":                 "Yoga",
    "strength_training":    "Strength",
    "hiit":                 "HIIT",
    "cardio_training":      "HIIT",
    "walking":              "Walking",
    "hiking":               "Walking",
}


def fetch_activities(
    client: garth.Client, start: date, end: date, limit: int = 100
) -> list[dict[str, Any]]:
    """
    Fetch activity list for a date range.
    Returns a list of activity dicts with keys like:
        activityId, activityName, activityType, startTimeLocal,
        distance, duration, calories, averageHR, maxHR
    """
    return client.connectapi(
        "/activitylist-service/activities/search/activities",
        params={
            "startDate": _date_str(start),
            "endDate": _date_str(end),
            "limit": limit,
            "start": 0,
            "_": "",
        },
    ) or []


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
