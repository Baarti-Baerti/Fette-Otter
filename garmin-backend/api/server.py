from __future__ import annotations

import json
import logging
import os
import sys
import threading
import urllib.request
import urllib.parse
from datetime import date, timedelta, datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from pathlib import Path

# ── Path setup — must happen before any local imports ────────────────────────
_HERE = Path(__file__).resolve().parent        # /app/api
_ROOT = _HERE.parent                            # /app
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from flask import Flask, jsonify, request, abort, redirect
from flask_cors import CORS
from garth.exc import GarthException, GarthHTTPError
import garmin as g
from garmin import strava as sv
from api.cache import (
    init_db, get_cached, set_cached, cache_age_seconds,
    refresh_all_periods, start_scheduler, last_refresh_log,
    CACHED_PERIODS,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

# ── Background refresh state ──────────────────────────────────────────────────
_refresh_lock        = threading.Lock()
_refresh_in_progress = {}  # { active, id, started_at, completed, finished_at }
log = logging.getLogger("squad_stats")

# ── Token bootstrap (cloud deployments) ──────────────────────────────────────
_tokens_b64 = os.environ.get("GARTH_TOKENS_B64", "")
if _tokens_b64:
    try:
        from token_export import import_tokens_from_env
        _squad_home = Path(os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad"))
        import_tokens_from_env(_tokens_b64, _squad_home)
    except Exception as _e:
        print(f"⚠️  Token bootstrap failed: {_e}", flush=True)

app = Flask(__name__)
app.url_map.strict_slashes = False
CORS(app, origins="*")

# Initialise cache DB on startup
init_db()

# Always return JSON for errors, never HTML
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"error": "Method not allowed"}), 405

@app.errorhandler(500)
def internal_error(e):
    return jsonify({"error": "Internal server error", "detail": str(e)}), 500

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")


def _range_dates(p: str) -> tuple[date, date]:
    """Return (start, end) calendar dates for a given period key."""
    from datetime import date
    today = date.today()
    if p == "thismonth":
        return date(today.year, today.month, 1), today
    if p == "lastmonth":
        first_this = today.replace(day=1)
        last_month_end = first_this - timedelta(days=1)
        last_month_start = last_month_end.replace(day=1)
        return last_month_start, last_month_end
    if p == "ytd":
        return date(today.year, 1, 1), today
    # legacy fallback
    legacy_days = {"today": 1, "1w": 7, "4w": 28}
    days = legacy_days.get(p, 28)
    return today - timedelta(days=days - 1), today


def _range_days(p: str) -> int:
    """Legacy: number of days for the period (kept for Strava path compatibility)."""
    start, end = _range_dates(p)
    return (end - start).days + 1


def verify_google_id_token(id_token: str) -> dict[str, Any]:
    url = "https://oauth2.googleapis.com/tokeninfo?id_token=" + urllib.parse.quote(id_token)
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read())
    except Exception as exc:
        raise ValueError(f"Token request failed: {exc}") from exc
    if payload.get("error_description"):
        raise ValueError(f"Invalid token: {payload['error_description']}")
    if GOOGLE_CLIENT_ID and payload.get("aud") != GOOGLE_CLIENT_ID:
        raise ValueError("Token audience mismatch")
    return payload


def load_user_data(member: dict[str, Any], range_start: date, range_end: date) -> dict[str, Any] | None:
    """Route to correct fetcher based on provider field."""
    if member.get("provider") == "strava":
        return load_strava_user_data(member, range_start, range_end)
    return load_garmin_user_data(member, range_start, range_end)


def load_garmin_user_data(member: dict[str, Any], range_start: date, range_end: date) -> dict[str, Any] | None:
    uid = member["id"]
    try:
        client = g.get_client(uid)
    except GarthException as exc:
        log.warning("User %s unauthenticated: %s", uid, exc)
        return None

    range_days = (range_end - range_start).days + 1
    today = date.today()

    try:
        week_acts      = g.fetch_activities(client, range_start, range_end)
        week_summaries = g.fetch_daily_summaries(client, range_start, range_days)
        # Height: prefer manually stored value in roster, fall back to Garmin profile
        height_m = member.get("height_m") or g.fetch_user_height(client) or None
        bmi            = g.fetch_latest_bmi(client, height_m_override=height_m)
        steps          = g.fetch_steps_range(client, range_start, range_end)

        # Fetch and cache profile picture if not already stored
        if not member.get("picture"):
            pic = g.fetch_profile_picture(client)
            if pic:
                g.update_member(uid, {"picture": pic})
                member = g.get_member(uid)  # refresh

        # Fetch Jan through current month of the current year
        months_to_fetch = [(today.year, mo) for mo in range(1, today.month + 1)]
        # Include last month if range covers it and it's in a prior year (edge case)
        if range_start.year == today.year and range_start.month not in [m for _, m in months_to_fetch]:
            months_to_fetch.insert(0, (range_start.year, range_start.month))

        monthly_acts: dict[str, list] = {}
        monthly_bmis: dict[str, Any] = {}

        def _fetch_month(yr, mo):
            key      = f"{yr}-{mo:02d}"
            acts     = g.fetch_activities_for_month(client, yr, mo)
            mo_bmi   = g.fetch_bmi_for_month(client, yr, mo, height_m_override=height_m)
            return key, acts, mo_bmi if mo_bmi is not None else bmi

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_month, yr, mo): (yr, mo)
                       for yr, mo in months_to_fetch}
            for fut in as_completed(futures):
                try:
                    key, acts, m_bmi = fut.result()
                    monthly_acts[key] = acts
                    monthly_bmis[key] = m_bmi
                except Exception as exc:
                    yr, mo = futures[fut]
                    monthly_acts[f"{yr}-{mo:02d}"] = []
                    monthly_bmis[f"{yr}-{mo:02d}"] = bmi

        return g.build_user_payload(
            roster_entry=member,
            week_activities=week_acts,
            week_summaries=week_summaries,
            monthly_activities=monthly_acts,
            monthly_bmis=monthly_bmis,
            range_start=range_start,
            range_end=range_end,
            bmi=bmi,
            steps=steps,
            height_m=height_m,
        )
    except Exception as exc:
        log.exception("Garmin fetch failed user %s: %s", uid, exc)
        return None


def _build_month_from_normalised(acts: list[dict], year: int, month: int) -> dict:
    """Like build_month_summary but for already-normalised activity dicts (Strava)."""
    from garmin.transform import _km, _split_km, _challenge_km
    # Reuse build_month_summary by passing already-normalised acts as raw
    # (build_month_summary calls _normalise_activity internally, but since acts
    # are already normalised we wrap them to pass through unchanged)
    # Simplest: just replicate the logic directly using the shared helpers
    import calendar as cal_mod
    cal     = sum(a["calories"]    for a in acts)
    sess    = len(acts)
    km      = _km(sum(a["distance_m"] for a in acts))
    actKcal = sum(a["calories"] for a in acts)  # Strava: use total calories (no active_kcal distinction)
    durSec  = round(sum(a["duration_s"] for a in acts))
    split   = _split_km(acts)
    challengeKm = _challenge_km(acts)

    # Day when cumulative challengeKm first crossed the goal
    GOAL = 66.67
    _, last_day = cal_mod.monthrange(year, month)
    goal_day: int | None = None
    cumulative = 0.0
    for day_num in range(1, last_day + 1):
        d = date(year, month, day_num).isoformat()
        day_acts = [a for a in acts if a["date"] == d]
        cumulative += _challenge_km(day_acts)
        if goal_day is None and cumulative >= GOAL:
            goal_day = day_num

    days: list[int] = []
    for day_num in range(1, 29):
        if day_num > last_day:
            days.append(0)
            continue
        d = date(year, month, day_num).isoformat()
        day_acts = [a for a in acts if a["date"] == d]
        days.append(sum(a["calories"] for a in day_acts))  # Strava: use total calories

    return {
        "year":        year,
        "month":       month,
        "cal":         cal,
        "sess":        sess,
        "km":          km,
        "runKm":       split["runKm"],
        "cycleKm":     split["cycleKm"],
        "virtualKm":   split["virtualKm"],
        "swimKm":      split["swimKm"],
        "walkKm":      split["walkKm"],
        "challengeKm": challengeKm,
        "durationSec": durSec,
        "goalDay":     goal_day,
        "actKcal":     actKcal,
        "bmi":         None,
        "days":        days,
    }


def load_strava_user_data(member: dict[str, Any], range_start: date, range_end: date) -> dict[str, Any] | None:
    """Load activity data for a Strava-connected member."""
    uid = member["id"]
    if not sv.is_authenticated(uid):
        log.warning("Strava user %s has no token", uid)
        return None
    try:
        today = date.today()

        # Fetch entire YTD without calorie enrichment (fast, no rate limit risk)
        import calendar as cal_mod
        ytd_start = date(today.year, 1, 1)
        _, last_day_cur = cal_mod.monthrange(today.year, today.month)
        ytd_end = date(today.year, today.month, last_day_cur)
        all_ytd_acts = sv.fetch_and_normalise(uid, ytd_start, ytd_end, enrich_calories=False)

        # Enrich only current month activities with calories (minimises API calls)
        cur_month_start = date(today.year, today.month, 1).isoformat()
        cur_month_end   = ytd_end.isoformat()
        cur_month_ids   = {a["id"] for a in all_ytd_acts if cur_month_start <= a["date"] <= cur_month_end}
        if cur_month_ids:
            enriched_acts = sv.fetch_and_normalise(uid, date(today.year, today.month, 1), ytd_end, enrich_calories=True)
            enriched_map  = {a["id"]: a for a in enriched_acts}
            all_ytd_acts  = [enriched_map.get(a["id"], a) for a in all_ytd_acts]

        # Filter to the requested period
        range_start_iso = range_start.isoformat()
        range_end_iso   = range_end.isoformat()
        period_acts = [a for a in all_ytd_acts if range_start_iso <= a["date"] <= range_end_iso]

        # Build week flags from last 7 days for the activity dots
        week_start = today - timedelta(days=6)
        week_dates = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
        day_flags = [1 if any(a["date"] == d for a in period_acts) else 0 for d in week_dates]
        day_cals  = [sum(a["calories"] for a in period_acts if a["date"] == d) for d in week_dates]

        from garmin.transform import _km, _split_km, _km_by_type, _challenge_km
        split = _split_km(period_acts)
        challengeKm = _challenge_km(period_acts)
        week = {
            "calories":     sum(a["calories"]   for a in period_acts),
            "workouts":     len(period_acts),
            "km":           _km(sum(a["distance_m"] for a in period_acts)),
            "actKcal":      sum(a["calories"] for a in period_acts),  # Strava: use total calories
            "week":         day_flags,
            "weekCalories": day_cals,
            "kmByType":     _km_by_type(period_acts),
            **split,
        }

        # Monthly data — split the already-fetched YTD activities by month
        months_keys: list[tuple[int, int]] = [
            (today.year, mo) for mo in range(1, today.month + 1)
        ]

        monthly: list[dict] = []
        for (yr, mo) in months_keys:
            _, last_day = cal_mod.monthrange(yr, mo)
            mo_start = date(yr, mo, 1).isoformat()
            mo_end   = date(yr, mo, last_day).isoformat()
            acts = [a for a in all_ytd_acts if mo_start <= a["date"] <= mo_end]
            monthly.append(_build_month_from_normalised(acts, yr, mo))

        # Derive activity types
        types = list(dict.fromkeys(a["type"] for a in period_acts if a["type"]))

        return {
            **{k: member.get(k, "") for k in ("id","name","role","emoji","color","bg","garminDevice","picture","provider")},
            "types":        types,
            "calories":     week["calories"],
            "workouts":     week["workouts"],
            "km":           week["km"],
            "runKm":        split["runKm"],
            "cycleKm":      split["cycleKm"],
            "virtualKm":    split["virtualKm"],
            "swimKm":       split["swimKm"],
            "skiKm":        split["skiKm"],
            "walkKm":       split["walkKm"],
            "otherKm":      split["otherKm"],
            "actKcal":      week["actKcal"],
            "steps":        0,
            "bmi":          0.0,
            "height_m":     member.get("height_m") or None,
            "week":         week["week"],
            "weekCalories": week["weekCalories"],
            "kmByType":     week["kmByType"],
            "challengeKm":  challengeKm,
            "monthly":      monthly,
            "provider":     "strava",
        }
    except Exception as exc:
        log.exception("Strava fetch failed user %s: %s", uid, exc)
        return None


def _stub(member: dict[str, Any]) -> dict[str, Any]:
    z = {"year": 0, "month": 0, "cal": 0, "sess": 0, "km": 0.0, "runKm": 0.0,
         "actKcal": 0, "bmi": None, "days": [0]*28}
    safe = ("id","name","role","emoji","color","bg","garminDevice","types","picture","google_email")
    return {
        **{k: member.get(k,"") for k in safe},
        "calories":0,"workouts":0,"km":0.0,"actKcal":0,"steps":0,"bmi":0.0,
        "height_m": member.get("height_m") or None,
        "runKm":0.0,"cycleKm":0.0,"virtualKm":0.0,"swimKm":0.0,
        "skiKm":0.0,"walkKm":0.0,"otherKm":0.0,
        "week":[0]*7,"weekCalories":[0]*7,"kmByType":{},
        "monthly":[dict(z) for _ in range(12)],
        "provider": member.get("provider","garmin"),
        "_stub": True,
    }


@app.get("/")
def root():
    return jsonify({"name": "Fette Otter API", "status": "ok"})


# ── Strava OAuth endpoints ────────────────────────────────────────────────────

@app.get("/api/strava/auth")
def strava_auth():
    """Redirect user to Strava OAuth page. Pass ?name=... and optionally ?user_id=..."""
    name    = request.args.get("name", "").strip()
    user_id = request.args.get("user_id", "").strip()
    if not name:
        return jsonify({"error": "name parameter required"}), 400
    state = urllib.parse.urlencode({"name": name, "user_id": user_id})
    url   = sv.auth_url(state=state)
    return redirect(url)


@app.get("/api/strava/callback")
def strava_callback():
    """Strava OAuth callback — exchange code, create/update member, redirect to dashboard."""
    DASHBOARD = "https://baarti-baerti.github.io/Fette-Otter/fette-otter.html"

    error = request.args.get("error")
    if error:
        return redirect(f"{DASHBOARD}?strava_error={urllib.parse.quote(error)}")

    code  = request.args.get("code", "")
    state = request.args.get("state", "")
    params = dict(urllib.parse.parse_qsl(state))
    name    = params.get("name", "").strip()
    user_id = params.get("user_id", "").strip()

    if not code:
        return redirect(f"{DASHBOARD}?strava_error=no_code")

    try:
        token = sv.exchange_code(code)
    except Exception as exc:
        log.exception("Strava token exchange failed: %s", exc)
        return redirect(f"{DASHBOARD}?strava_error=token_exchange_failed")

    athlete = token.get("athlete", {})
    strava_id  = str(athlete.get("id", ""))
    first      = athlete.get("firstname", "")
    last       = athlete.get("lastname", "")
    full_name  = name or f"{first} {last}".strip() or f"Strava {strava_id}"
    picture    = athlete.get("profile_medium") or athlete.get("profile") or ""
    google_sub = f"strava_{strava_id}"

    # Upsert member
    existing = g.get_by_google_sub(google_sub)
    if existing:
        member = existing
        # Update name/picture if provided
        if name and name != member.get("name"):
            g.update_member(member["id"], {"name": name})
    else:
        member = g.add_member(
            google_sub=google_sub,
            google_email=f"{strava_id}@strava",
            name=full_name,
            picture=picture,
            garmin_email="",
            role="Fette Otter",
        )
        # Mark as Strava provider
        g.update_member(member["id"], {"provider": "strava", "picture": picture})

    sv.save_token(member["id"], token)
    log.info("Strava member %s (id=%s) authenticated", full_name, member["id"])

    # Redirect back to dashboard with member info so frontend can store session
    qs = urllib.parse.urlencode({
        "strava_ok":  "1",
        "member_id":  member["id"],
        "member_name": full_name,
    })
    return redirect(f"{DASHBOARD}?{qs}")


@app.get("/api/health")
def health():
    import os
    from pathlib import Path
    squad_home = Path(os.environ.get("GARTH_SQUAD_HOME", Path.home() / ".garth_squad"))
    members = g.all_members()
    debug = {
        "status": "ok",
        "team_size": len(members),
        "squad_home": str(squad_home),
        "squad_home_exists": squad_home.exists(),
        "squad_home_contents": [str(p) for p in squad_home.iterdir()] if squad_home.exists() else [],
        "members": [{"id": m["id"], "name": m["name"], "authenticated": g.is_authenticated(m["id"])} for m in members],
    }
    return jsonify(debug)


@app.get("/api/status")
def auth_status():
    return jsonify([
        {"id": m["id"], "name": m["name"], "authenticated": g.is_authenticated(m["id"])}
        for m in g.all_members()
    ])


@app.get("/api/members")
def list_members():
    safe = ("id","name","role","emoji","color","bg","garminDevice",
            "types","picture","google_email","joined_at")
    return jsonify([{k: m.get(k) for k in safe} for m in g.all_members()])


@app.post("/api/members/join")
def join():
    try:
        body            = request.get_json(silent=True) or {}
        name            = (body.get("name")            or "").strip()
        garmin_email    = (body.get("garmin_email")    or "").strip()
        garmin_password = (body.get("garmin_password") or "").strip()

        if not name:
            return jsonify({"error": "name is required"}), 400
        if not garmin_email or not garmin_password:
            return jsonify({"error": "garmin_email and garmin_password are required"}), 400

        # Check if Garmin email already registered
        existing = next((m for m in g.all_members() if m.get("garmin_email") == garmin_email), None)

        # Determine user_id and member record before attempting login
        if existing:
            if g.is_authenticated(existing["id"]):
                safe = {k: existing.get(k) for k in
                        ("id","name","role","emoji","color","bg","garminDevice",
                         "types","picture","google_email","joined_at")}
                return jsonify({"member": safe, "message": "Already in the squad!", "rejoined": True}), 200
            member   = existing
            is_new   = False
            user_id  = existing["id"]
        else:
            google_sub = f"garmin_{garmin_email}"
            stale = g.get_by_google_sub(google_sub)
            if stale:
                g.remove_member(stale["id"])
            member = g.add_member(
                google_sub=google_sub,
                google_email=garmin_email,
                name=name,
                picture="",
                garmin_email=garmin_email,
                role="Fette Otter",
            )
            is_new  = True
            user_id = member["id"]

        # Attempt Garmin login
        try:
            status, result = g.login_start(garmin_email, garmin_password)
        except Exception as exc:
            if is_new:
                g.remove_member(user_id)
            return jsonify({"error": f"Garmin Connect login failed — check your email and password. ({exc})"}), 422

        if status == "needs_mfa":
            # Store partial session, return mfa_token to frontend
            client, resume_data = result
            mfa_token = g.store_pending_mfa(client, resume_data, user_id, member, is_new)
            return jsonify({"needs_mfa": True, "mfa_token": mfa_token}), 202

        # Login succeeded without MFA
        g.save_client(result, user_id)
        safe = {k: member.get(k) for k in
                ("id","name","role","emoji","color","bg","garminDevice",
                 "types","picture","google_email","joined_at")}
        code = 201 if is_new else 200
        msg  = "Welcome to Fette Otter! 🦦" if is_new else "Welcome back! 🎉"
        return jsonify({"member": safe, "message": msg, "rejoined": not is_new}), code

    except Exception as exc:
        log.exception("Unexpected error in /api/members/join: %s", exc)
        return jsonify({"error": f"Server error: {str(exc)}"}), 500


@app.post("/api/members/join/mfa")
def join_mfa():
    """Complete a pending Garmin 2FA login with the OTP code."""
    try:
        body      = request.get_json(silent=True) or {}
        mfa_token = (body.get("mfa_token") or "").strip()
        otp_code  = (body.get("otp_code")  or "").strip()

        if not mfa_token or not otp_code:
            return jsonify({"error": "mfa_token and otp_code are required"}), 400

        try:
            client, user_id, member, is_new = g.complete_mfa_login(mfa_token, otp_code)
        except KeyError as e:
            return jsonify({"error": str(e)}), 410   # Gone — session expired
        except Exception as exc:
            return jsonify({"error": f"Invalid MFA code — please try again. ({exc})"}), 422

        g.save_client(client, user_id)
        safe = {k: member.get(k) for k in
                ("id","name","role","emoji","color","bg","garminDevice",
                 "types","picture","google_email","joined_at")}
        code = 201 if is_new else 200
        msg  = "Welcome to Fette Otter! 🦦" if is_new else "Welcome back! 🎉"
        return jsonify({"member": safe, "message": msg, "rejoined": not is_new}), code

    except Exception as exc:
        log.exception("Unexpected error in /api/members/join/mfa: %s", exc)
        return jsonify({"error": f"Server error: {str(exc)}"}), 500


@app.post("/api/members/<int:member_id>/height")
def set_member_height(member_id: int):
    """Set a member's height (cm) for BMI calculation.
    Can be called by the member themselves or by an admin."""
    body       = request.get_json(silent=True) or {}
    ADMIN_NAME = os.environ.get("ADMIN_NAME", "Martin")
    requester  = (body.get("admin_name") or body.get("name") or "").strip()
    # Allow if requester is admin OR if they are the member themselves
    member = g.get_member(member_id)
    if not member:
        abort(404)
    is_admin = requester == ADMIN_NAME
    is_self  = requester == member.get("name", "")
    if not is_admin and not is_self:
        abort(403)
    height_cm = body.get("height_cm")
    if not height_cm or not (100 < float(height_cm) < 250):
        return jsonify({"error": "height_cm must be between 100 and 250"}), 400
    height_m = round(float(height_cm) / 100.0, 3)
    g.update_member(member_id, {"height_m": height_m})
    return jsonify({"ok": True, "member_id": member_id, "height_m": height_m})


@app.delete("/api/members/<int:member_id>")
def remove_member_route(member_id: int):
    body       = request.get_json(silent=True) or {}
    admin_name = (body.get("admin_name") or "").strip()

    # Admin check — only "Martin" can remove members
    ADMIN_NAME = os.environ.get("ADMIN_NAME", "Martin")
    if admin_name != ADMIN_NAME:
        return jsonify({"error": "Forbidden — admin only"}), 403

    # Verify the requesting member actually exists with that name
    all_m = g.all_members()
    admin = next((m for m in all_m if m.get("name") == ADMIN_NAME), None)
    if not admin:
        return jsonify({"error": "Admin account not found"}), 403

    member = g.get_member(member_id)
    if not member:
        return jsonify({"error": "Member not found"}), 404
    if member.get("name") == ADMIN_NAME:
        return jsonify({"error": "Cannot remove the admin"}), 403

    g.remove_member(member_id)
    log.info("Admin removed member %s (id=%s)", member["name"], member_id)
    return jsonify({"message": f"Removed {member['name']} from Fette Otter"}), 200


@app.get("/api/debug/strava/<int:user_id>")
def debug_strava(user_id: int):
    """Debug Strava data fetch for a user."""
    member = g.get_member(user_id)
    if not member:
        return jsonify({"error": "member not found"}), 404
    if member.get("provider") != "strava":
        return jsonify({"error": "not a strava member", "provider": member.get("provider")}), 400

    results = {"member": member["name"], "steps": {}}

    # Step 1: check token
    token = sv.load_token(user_id)
    if not token:
        return jsonify({"error": "no strava token found"}), 404
    results["token_keys"] = list(token.keys())
    results["expires_at"] = token.get("expires_at")
    results["has_athlete"] = "athlete" in token

    # Step 2: get access token (triggers refresh if needed)
    try:
        access_token = sv.get_access_token(user_id)
        results["steps"]["get_access_token"] = "OK"
    except Exception as exc:
        results["steps"]["get_access_token"] = f"FAILED: {exc}"
        return jsonify(results), 500

    # Step 3: fetch athlete profile
    try:
        athlete = sv.get_athlete(user_id)
        results["steps"]["get_athlete"] = f"OK — {athlete.get('firstname')} {athlete.get('lastname')}"
    except Exception as exc:
        results["steps"]["get_athlete"] = f"FAILED: {exc}"

    # Step 4: fetch last 30 days of activities
    try:
        today = date.today()
        start = today - timedelta(days=30)
        acts = sv.fetch_activities(user_id, start, today)
        results["steps"]["fetch_activities_30d"] = f"OK — {len(acts)} activities"
        results["sample_activity_summary"] = acts[0] if acts else None
        results["activity_types"] = list({
            (a.get("sport_type") or a.get("type", "?")) for a in acts
        })
        # Step 5: fetch detail for first activity to check calories field
        if acts:
            try:
                access_token = sv.get_access_token(user_id)
                detail = sv.fetch_activity_detail(acts[0]["id"], access_token)
                results["steps"]["fetch_activity_detail"] = "OK"
                results["sample_activity_detail_calories"] = detail.get("calories")
                results["sample_activity_detail_keys"] = list(detail.keys())
            except Exception as exc:
                results["steps"]["fetch_activity_detail"] = f"FAILED: {exc}"
    except Exception as exc:
        results["steps"]["fetch_activities_30d"] = f"FAILED: {exc}"

    # Step 6: test full load_strava_user_data
    try:
        member = g.get_member(user_id)
        today = date.today()
        import calendar as cal_mod
        _, last_day = cal_mod.monthrange(today.year, today.month)
        payload = load_strava_user_data(member, date(today.year, today.month, 1), date(today.year, today.month, last_day))
        if payload:
            results["steps"]["load_strava_user_data"] = "OK"
            results["payload_keys"] = list(payload.keys())
            results["actKcal"] = payload.get("actKcal")
            results["challengeKm"] = payload.get("challengeKm")
            results["monthly_count"] = len(payload.get("monthly", []))
        else:
            results["steps"]["load_strava_user_data"] = "FAILED: returned None"
    except Exception as exc:
        import traceback
        results["steps"]["load_strava_user_data"] = f"FAILED: {exc}"
        results["load_strava_traceback"] = traceback.format_exc()

    return jsonify(results)


@app.get("/api/debug/ski/<int:user_id>")
def debug_ski(user_id: int):
    """Show exactly which activities are counted as ski km and why."""
    member = g.get_member(user_id)
    if not member:
        return jsonify({"error": "member not found"}), 404

    range_param = request.args.get("range", "thismonth")
    rd = _range_days(range_param)
    today = date.today()
    start = today - timedelta(days=rd - 1)

    try:
        from garmin.fetcher import ACTIVITY_TYPE_MAP
        client = g.get_client(user_id)
        acts = g.fetch_activities_last_n_days(client, rd)

        ski_acts = []
        all_acts = []
        for a in acts:
            raw_type = (
                a.get("activityType", {}).get("typeKey", "unknown")
                if isinstance(a.get("activityType"), dict)
                else str(a.get("activityType", "unknown"))
            )
            mapped = ACTIVITY_TYPE_MAP.get(raw_type.lower(), f"UNMAPPED:{raw_type}")
            dist_m = float(a.get("distance") or 0)
            dist_km = round(dist_m / 1000, 2)
            act_date = (a.get("startTimeLocal") or "")[:10]
            entry = {
                "date": act_date,
                "name": a.get("activityName", ""),
                "raw_type": raw_type,
                "mapped_type": mapped,
                "distance_km": dist_km,
                "in_range": start.isoformat() <= act_date <= today.isoformat(),
            }
            all_acts.append(entry)
            if mapped == "Skiing":
                ski_acts.append(entry)

        total_ski_km = round(sum(a["distance_km"] for a in ski_acts if a["in_range"]), 2)
        total_ski_km_unfiltered = round(sum(a["distance_km"] for a in ski_acts), 2)

        return jsonify({
            "period": range_param,
            "range_days": rd,
            "date_range": f"{start.isoformat()} → {today.isoformat()}",
            "total_activities_fetched": len(acts),
            "ski_activities": ski_acts,
            "ski_km_in_range": total_ski_km,
            "ski_km_unfiltered": total_ski_km_unfiltered,
            "all_activities": all_acts,
        })
    except Exception as exc:
        log.exception("Ski debug failed: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.get("/api/debug/activity-types/<int:user_id>")
def debug_activity_types(user_id: int):
    """Show all raw activityType keys returned by Garmin for this user."""
    member = g.get_member(user_id)
    if not member:
        return jsonify({"error": "member not found"}), 404
    try:
        from datetime import date, timedelta
        client = g.get_client(user_id)
        # Fetch last 90 days to catch seasonal activities like skiing
        today = date.today()
        start = today - timedelta(days=365)
        acts = g.fetch_activities(client, start, today, limit=500)
        type_summary = {}
        for a in acts:
            raw = (
                a.get("activityType", {}).get("typeKey", "unknown")
                if isinstance(a.get("activityType"), dict)
                else str(a.get("activityType", "unknown"))
            )
            mapped = g.ACTIVITY_TYPE_MAP.get(raw.lower(), f"UNMAPPED:{raw}")
            key = f"{raw} → {mapped}"
            type_summary[key] = type_summary.get(key, 0) + 1
        return jsonify({"user": member["name"], "activity_types": type_summary,
                        "total_activities": len(acts)})
    except Exception as exc:
        return jsonify({"error": str(exc), "type": type(exc).__name__}), 500


@app.get("/api/debug/bmi/<int:user_id>")
def debug_bmi(user_id: int):
    """Show raw weight/BMI API responses for a user to diagnose missing BMI."""
    member = g.get_member(user_id)
    if not member:
        return jsonify({"error": "member not found"}), 404
    try:
        from datetime import date, timedelta
        client = g.get_client(user_id)
        today  = date.today()
        start  = date(today.year, 1, 1)
        height_m = g.fetch_user_height(client)

        probes = {}
        for path, params in [
            ("/weight-service/weight/dateRange",      {"startDate": str(start), "endDate": str(today)}),
            ("/weight-service/weight/range",          {"startDate": str(start), "endDate": str(today)}),
            ("/weight-service/weight",                {"startDate": str(start), "endDate": str(today)}),
        ]:
            try:
                raw = client.connectapi(path, params=params)
                entries = (
                    raw.get("dateWeightList")
                    or raw.get("allWeightMetrics")
                    or raw.get("weightList")
                    or raw.get("weights")
                    or (raw if isinstance(raw, list) else [])
                )
                probes[path] = {
                    "top_level_keys": list(raw.keys()) if isinstance(raw, dict) else "list",
                    "entry_count": len(entries),
                    "sample": entries[-3:] if entries else [],
                }
            except Exception as exc:
                probes[path] = {"error": str(exc)}

        latest_bmi = g.fetch_latest_bmi(client)
        return jsonify({
            "user": member["name"],
            "height_m": height_m,
            "latest_bmi_computed": latest_bmi,
            "probes": probes,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.get("/api/debug/<int:user_id>")
def debug_user(user_id: int):
    """Debug endpoint — shows exactly what happens when fetching a user."""
    from datetime import date, timedelta
    member = g.get_member(user_id)
    if not member:
        return jsonify({"error": "member not found"}), 404
    try:
        client = g.get_client(user_id)
        username = client.username
    except Exception as exc:
        return jsonify({"step": "get_client", "error": str(exc), "type": type(exc).__name__})

    today = date.today()
    start7 = today - timedelta(days=6)
    from garmin.fetcher import _date_str
    results = {"username": username, "member": member["name"], "steps_debug": {}}

    # Raw probe of every plausible steps endpoint — capture response or error explicitly
    probes = [
        ("range_endpoint", lambda: client.connectapi(
            "/usersummary-service/usersummary/daily/range",
            params={"startDate": _date_str(start7), "endDate": _date_str(today)},
        )),
        ("daily_today", lambda: client.connectapi(
            f"/usersummary-service/usersummary/daily/{today}",
            params={"calendarDate": str(today)},
        )),
        ("wellness_movement_today", lambda: client.connectapi(
            f"/wellness-service/wellness/dailyMovement/{today}",
        )),
        ("wellness_daily_today", lambda: client.connectapi(
            f"/wellness-service/wellness/daily/{today}",
            params={"date": str(today)},
        )),
        ("activity_steps_sample", lambda: [
            {"name": a.get("activityName"), "steps": a.get("steps"), "type": a.get("activityType",{}).get("typeKey")}
            for a in (client.connectapi(
                "/activitylist-service/activities/search/activities",
                params={"startDate": _date_str(start7), "endDate": _date_str(today), "limit": 10, "start": 0}
            ) or [])
        ]),
    ]

    def _safe_probe(fn):
        try:
            resp = fn()
            if isinstance(resp, list):
                return {"type": "list", "len": len(resp), "sample": resp[:2]}
            if isinstance(resp, dict):
                return {"type": "dict", "keys": list(resp.keys())[:15],
                        "totalSteps": resp.get("totalSteps"),
                        "steps": resp.get("steps"),
                        "sample": {k: resp[k] for k in list(resp.keys())[:8]}}
            return {"type": str(type(resp)), "value": str(resp)[:200]}
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    for name, fn in probes:
        results["steps_debug"][name] = _safe_probe(fn)

    # Also run fetch_steps_range and show result
    try:
        steps = g.fetch_steps_range(client, start7, today)
        results["steps_debug"]["fetch_steps_range_result"] = steps
    except Exception as exc:
        results["steps_debug"]["fetch_steps_range_result"] = f"FAILED: {exc}"

    try:
        bmi = g.fetch_latest_bmi(client)
        height_m = g.fetch_user_height(client)
        results["steps_debug"]["bmi"] = f"OK — {bmi}"
        results["steps_debug"]["height_m"] = f"OK — {height_m}"
    except Exception as exc:
        results["steps_debug"]["bmi"] = f"FAILED: {type(exc).__name__}: {exc}"

    # Raw weight API response for diagnosis
    try:
        from garmin.fetcher import fetch_body_composition
        raw = fetch_body_composition(client, today - timedelta(days=365), today)
        entries = raw.get("dateWeightList") or raw.get("allWeightMetrics") or []
        results["steps_debug"]["weight_raw_keys"] = list(raw.keys())
        results["steps_debug"]["weight_entry_count"] = len(entries)
    except Exception as exc:
        results["steps_debug"]["weight_raw"] = f"FAILED: {type(exc).__name__}: {exc}"

    return jsonify(results)


def load_team(period: str) -> list[dict]:
    """
    Fetch live data for all members for the given period.
    Used by both the scheduler and as a cache-miss fallback.
    """
    members = g.all_members()
    if not members:
        return []
    range_start, range_end = _range_dates(period)
    results = []
    with ThreadPoolExecutor(max_workers=max(len(members), 1)) as pool:
        fmap = {pool.submit(load_user_data, m, range_start, range_end): m for m in members}
        for fut in as_completed(fmap):
            member = fmap[fut]
            try:
                payload = fut.result()
            except Exception:
                payload = None
            if payload is None:
                payload = _stub(member)
            payload["picture"]      = member.get("picture", "")
            payload["google_email"] = member.get("google_email", "")
            results.append(payload)
    id_order = {m["id"]: i for i, m in enumerate(members)}
    results.sort(key=lambda u: id_order.get(u["id"], 999))
    return results


CACHE_MAX_AGE_SECONDS = 15 * 60  # 15 minutes

@app.get("/api/team")
def get_team():
    period = request.args.get("range", "thismonth")

    # Serve from cache if available and fresh
    cached, fetched_at = get_cached(period)
    if cached:
        age = cache_age_seconds(fetched_at)
        if age is not None and age < CACHE_MAX_AGE_SECONDS:
            resp = jsonify(cached)
            resp.headers["X-Cache"] = "HIT"
            resp.headers["X-Cache-Age"] = str(int(age))
            resp.headers["X-Cache-Fetched"] = fetched_at or ""
            return resp
        log.info("Cache stale for period=%s (age=%ds) — fetching live", period, int(age or 0))

    # Cache miss — fetch live and populate cache
    log.info("Cache miss for period=%s — fetching live", period)
    results = load_team(period)
    if results and period in CACHED_PERIODS:
        set_cached(period, results)
    resp = jsonify(results)
    resp.headers["X-Cache"] = "MISS"
    return resp


@app.post("/api/refresh")
def api_refresh():
    """Manually trigger a background refresh of all cached periods."""
    member_id = request.json.get("user_id") if request.json else None
    member = g.get_member(member_id) if member_id else None
    if not member or member.get("role") != "admin":
        abort(403)
    threading.Thread(
        target=refresh_all_periods,
        args=(load_team,),
        daemon=True,
    ).start()
    return jsonify({"status": "refresh started"})


@app.get("/api/admin/clear-cache")
def api_clear_cache():
    """Clear all cached periods and trigger a fresh background refresh."""
    from api.cache import _connect
    try:
        with _connect() as conn:
            conn.execute("DELETE FROM team_cache")
            conn.commit()
        log.info("Cache cleared via /api/admin/clear-cache")
    except Exception as exc:
        log.error("Cache clear failed: %s", exc)
        return jsonify({"error": str(exc)}), 500
    threading.Thread(
        target=refresh_all_periods,
        args=(load_team,),
        daemon=True,
    ).start()
    return jsonify({"status": "cache cleared, refresh started"})


@app.get("/api/team/trigger-refresh")
def api_trigger_refresh():
    """
    Trigger a background refresh for all cached periods without waiting.
    Returns immediately with a refresh_id the client can poll.
    Only triggers if no refresh is already in progress.
    """
    import uuid
    with _refresh_lock:
        if _refresh_in_progress.get("active"):
            return jsonify({"status": "already_running", "refresh_id": _refresh_in_progress.get("id")})
        refresh_id = str(uuid.uuid4())[:8]
        _refresh_in_progress["active"] = True
        _refresh_in_progress["id"] = refresh_id
        _refresh_in_progress["started_at"] = datetime.now(timezone.utc).isoformat()
        _refresh_in_progress["completed"] = False

    def _do_refresh():
        try:
            refresh_all_periods(load_team)
        finally:
            with _refresh_lock:
                _refresh_in_progress["active"] = False
                _refresh_in_progress["completed"] = True
                _refresh_in_progress["finished_at"] = datetime.now(timezone.utc).isoformat()

    threading.Thread(target=_do_refresh, daemon=True).start()
    return jsonify({"status": "started", "refresh_id": refresh_id})


@app.get("/api/team/refresh-status")
def api_refresh_status():
    """Poll this to check if a background refresh has completed."""
    _, fetched_at = get_cached("thismonth")
    return jsonify({
        "active":      _refresh_in_progress.get("active", False),
        "completed":   _refresh_in_progress.get("completed", False),
        "refresh_id":  _refresh_in_progress.get("id"),
        "started_at":  _refresh_in_progress.get("started_at"),
        "finished_at": _refresh_in_progress.get("finished_at"),
        "fetched_at":  fetched_at,
    })



def api_cache_status():
    """Show cache freshness and recent refresh log."""
    status = {}
    for period in CACHED_PERIODS:
        _, fetched_at = get_cached(period)
        age = cache_age_seconds(fetched_at)
        status[period] = {
            "fetched_at": fetched_at,
            "age_minutes": round(age / 60, 1) if age is not None else None,
        }
    return jsonify({"periods": status, "log": last_refresh_log(10)})


@app.get("/api/user/<int:user_id>")
def get_user(user_id: int):
    member = g.get_member(user_id)
    if not member:
        abort(404)
    rd      = _range_days(request.args.get("range", "1w"))
    payload = load_user_data(member, rd) or _stub(member)
    payload["picture"]      = member.get("picture", "")
    payload["google_email"] = member.get("google_email", "")
    return jsonify(payload)


# ── Start background scheduler (after load_team is defined) ──────────────────
# Background scheduler disabled — refreshes triggered by user visits only


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Fette Otter API — port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
