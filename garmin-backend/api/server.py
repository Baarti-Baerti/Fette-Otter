from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
import urllib.parse
from datetime import date, timedelta
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
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
app.url_map.strict_slashes = False   # /api/members/join/ works same as /api/members/join
CORS(app, origins="*")

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


def _range_days(p: str) -> int:
    from datetime import date
    today = date.today()
    if p == "thismonth":
        return today.day  # days elapsed in current month
    if p == "lastmonth":
        # days in previous month
        first_this = today.replace(day=1)
        last_month = first_this - timedelta(days=1)
        return last_month.day
    if p == "ytd":
        return (today - date(today.year, 1, 1)).days + 1
    # legacy fallback
    legacy = {"today": 1, "1w": 7, "4w": 28}
    return legacy.get(p, 28)


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


def load_user_data(member: dict[str, Any], range_days: int) -> dict[str, Any] | None:
    """Route to correct fetcher based on provider field."""
    if member.get("provider") == "strava":
        return load_strava_user_data(member, range_days)
    return load_garmin_user_data(member, range_days)


def load_garmin_user_data(member: dict[str, Any], range_days: int) -> dict[str, Any] | None:
    uid = member["id"]
    try:
        client = g.get_client(uid)
    except GarthException as exc:
        log.warning("User %s unauthenticated: %s", uid, exc)
        return None

    today = date.today()
    start = today - timedelta(days=range_days - 1)

    try:
        week_acts      = g.fetch_activities_last_n_days(client, range_days)
        week_summaries = g.fetch_daily_summaries(client, start, range_days)
        bmi            = g.fetch_latest_bmi(client)

        months_to_fetch = []
        for i in range(11, -1, -1):
            m_date = date(today.year, today.month, 1) - timedelta(days=30 * i)
            months_to_fetch.append((m_date.year, m_date.month))

        monthly_acts: dict[str, list] = {}
        monthly_bmis: dict[str, Any] = {}

        def _fetch_month(yr, mo):
            key = f"{yr}-{mo:02d}"
            acts = g.fetch_activities_for_month(client, yr, mo)
            return key, acts, bmi

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
            range_days=range_days,
            bmi=bmi,
        )
    except Exception as exc:
        log.exception("Garmin fetch failed user %s: %s", uid, exc)
        return None


def _build_month_from_normalised(acts: list[dict], year: int, month: int) -> dict:
    """Like build_month_summary but for already-normalised activity dicts (Strava)."""
    import calendar as cal_mod
    from garmin.transform import _km, _split_km
    cal    = sum(a["calories"]   for a in acts)
    sess   = len(acts)
    km     = _km(sum(a["distance_m"] for a in acts))
    actKcal= sum(a["active_kcal"] for a in acts)
    runKm  = _km(sum(a["distance_m"] for a in acts if a.get("type") == "Running"))
    _, last_day = cal_mod.monthrange(year, month)
    days: list[int] = []
    for day_num in range(1, 29):
        if day_num > last_day:
            days.append(0)
            continue
        d = date(year, month, day_num).isoformat()
        day_acts = [a for a in acts if a["date"] == d]
        days.append(sum(a["active_kcal"] for a in day_acts))
    return {
        "year": year, "month": month,
        "cal": cal, "sess": sess, "km": km, "runKm": runKm,
        "actKcal": actKcal, "bmi": None, "days": days,
    }


def load_strava_user_data(member: dict[str, Any], range_days: int) -> dict[str, Any] | None:
    """Load activity data for a Strava-connected member."""
    uid = member["id"]
    if not sv.is_authenticated(uid):
        log.warning("Strava user %s has no token", uid)
        return None
    try:
        today = date.today()
        start = today - timedelta(days=range_days - 1)

        # Fetch current period activities (already normalised)
        period_acts = sv.fetch_and_normalise(uid, start, today)

        # Build week flags from last 7 days for the activity dots
        week_start = today - timedelta(days=6)
        week_dates = [(week_start + timedelta(days=i)).isoformat() for i in range(7)]
        day_flags = [1 if any(a["date"] == d for a in period_acts) else 0 for d in week_dates]
        day_cals  = [sum(a["active_kcal"] for a in period_acts if a["date"] == d) for d in week_dates]

        from garmin.transform import _km, _split_km, _km_by_type
        split = _split_km(period_acts)
        week = {
            "calories":     sum(a["calories"]   for a in period_acts),
            "workouts":     len(period_acts),
            "km":           _km(sum(a["distance_m"] for a in period_acts)),
            "actKcal":      sum(a["active_kcal"] for a in period_acts),
            "week":         day_flags,
            "weekCalories": day_cals,
            "kmByType":     _km_by_type(period_acts),
            **split,
        }

        # Monthly data — 12 months
        months_keys: list[tuple[int, int]] = []
        for i in range(11, -1, -1):
            m_date = date(today.year, today.month, 1) - timedelta(days=30 * i)
            months_keys.append((m_date.year, m_date.month))

        def _fetch_month_strava(yr: int, mo: int):
            import calendar
            _, last_day = calendar.monthrange(yr, mo)
            m_start = date(yr, mo, 1)
            m_end   = date(yr, mo, last_day)
            acts = sv.fetch_and_normalise(uid, m_start, m_end)
            return f"{yr}-{mo:02d}", acts

        monthly: list[dict] = []
        monthly_acts: dict[str, list] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(_fetch_month_strava, yr, mo): (yr, mo)
                       for yr, mo in months_keys}
            for fut in as_completed(futures):
                yr, mo = futures[fut]
                try:
                    key, acts = fut.result()
                    monthly_acts[key] = acts
                except Exception as exc:
                    log.warning("Strava monthly fetch failed %s-%s: %s", yr, mo, exc)
                    monthly_acts[f"{yr}-{mo:02d}"] = []

        for (yr, mo) in months_keys:
            key  = f"{yr}-{mo:02d}"
            acts = monthly_acts.get(key, [])
            monthly.append(_build_month_from_normalised(acts, yr, mo))

        # Derive activity types
        types = list(dict.fromkeys(a["type"] for a in period_acts if a["type"]))

        return {
            **{k: member.get(k, "") for k in ("id","name","role","emoji","color","bg","garminDevice")},
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
            "bmi":          0.0,
            "week":         week["week"],
            "weekCalories": week["weekCalories"],
            "kmByType":     week["kmByType"],
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
        "calories":0,"workouts":0,"km":0.0,"actKcal":0,"bmi":0.0,
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
        if existing:
            # If already authenticated, just return their profile
            if g.is_authenticated(existing["id"]):
                safe = {k: existing.get(k) for k in
                        ("id","name","role","emoji","color","bg","garminDevice",
                         "types","picture","google_email","joined_at")}
                return jsonify({"member": safe, "message": "Already in the squad!", "rejoined": True}), 200
            # Member exists but token is missing — re-authenticate
            try:
                g.login_and_save(existing["id"], garmin_email, garmin_password)
                safe = {k: existing.get(k) for k in
                        ("id","name","role","emoji","color","bg","garminDevice",
                         "types","picture","google_email","joined_at")}
                return jsonify({"member": safe, "message": "Welcome back! 🎉", "rejoined": True}), 200
            except Exception as exc:
                return jsonify({"error": f"Garmin Connect login failed — check your email and password. ({exc})"}), 422

        # Create member record — use garmin_email as unique key
        google_sub = f"garmin_{garmin_email}"
        # Remove any stale record with same google_sub (shouldn't happen but be safe)
        stale = g.get_by_google_sub(google_sub)
        if stale:
            g.remove_member(stale["id"])

        new_member = g.add_member(
            google_sub=google_sub,
            google_email=garmin_email,
            name=name,
            picture="",
            garmin_email=garmin_email,
            role="Fette Otter",
        )

        # Authenticate Garmin
        try:
            g.login_and_save(new_member["id"], garmin_email, garmin_password)
        except Exception as exc:
            g.remove_member(new_member["id"])
            return jsonify({"error": f"Garmin Connect login failed — check your email and password. ({exc})"}), 422

        safe = {k: new_member.get(k) for k in
                ("id","name","role","emoji","color","bg","garminDevice",
                 "types","picture","google_email","joined_at")}
        return jsonify({"member": safe, "message": "Welcome to Brew Crew! 🎉"}), 201

    except Exception as exc:
        log.exception("Unexpected error in /api/members/join: %s", exc)
        return jsonify({"error": f"Server error: {str(exc)}"}), 500


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
        results["sample_activity"] = acts[0] if acts else None
        results["activity_types"] = list({
            (a.get("sport_type") or a.get("type", "?")) for a in acts
        })
    except Exception as exc:
        results["steps"]["fetch_activities_30d"] = f"FAILED: {exc}"

    return jsonify(results)


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
    results = {"username": username, "member": member["name"], "steps": {}}

    # Test each API call individually
    try:
        acts = g.fetch_activities_last_n_days(client, 7)
        results["steps"]["activities"] = f"OK — {len(acts)} activities"
    except Exception as exc:
        results["steps"]["activities"] = f"FAILED: {type(exc).__name__}: {exc}"

    try:
        start = today - timedelta(days=6)
        sums = g.fetch_daily_summaries(client, start, 7)
        results["steps"]["summaries"] = f"OK — {len(sums)} days"
    except Exception as exc:
        results["steps"]["summaries"] = f"FAILED: {type(exc).__name__}: {exc}"

    try:
        bmi = g.fetch_latest_bmi(client)
        results["steps"]["bmi"] = f"OK — {bmi}"
    except Exception as exc:
        results["steps"]["bmi"] = f"FAILED: {type(exc).__name__}: {exc}"

    return jsonify(results)


@app.get("/api/team")
def get_team():
    members = g.all_members()
    if not members:
        return jsonify([])
    rd = _range_days(request.args.get("range", "1w"))
    results = []
    with ThreadPoolExecutor(max_workers=max(len(members), 1)) as pool:
        fmap = {pool.submit(load_user_data, m, rd): m for m in members}
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
    return jsonify(results)


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


if __name__ == "__main__":
    port  = int(os.environ.get("PORT", 5050))
    debug = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    log.info("Fette Otter API — port %d", port)
    app.run(host="0.0.0.0", port=port, debug=debug, threaded=True)
