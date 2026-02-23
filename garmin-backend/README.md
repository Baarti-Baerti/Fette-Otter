# Squad Stats — Garmin Backend

A Python/Flask backend that connects your Squad Stats dashboard to real Garmin
Connect data using the [`garth`](https://github.com/matin/garth) library.

---

## Architecture

```
garmin-backend/
├── auth_setup.py            ← One-time authentication CLI
├── requirements.txt
├── config/
│   └── team.py              ← Team roster (edit this with real emails)
├── garmin/
│   ├── session.py           ← garth token management (load/save/resume)
│   ├── fetcher.py           ← Raw Garmin Connect API calls via garth
│   └── transform.py         ← Converts API responses → dashboard shape
├── api/
│   └── server.py            ← Flask REST API
└── dashboard_api_integration.js  ← JS snippet to wire the dashboard to the API
```

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure your team

Edit `config/team.py` and replace the email addresses with each team member's
real Garmin Connect account email. The other fields (name, role, emoji, color)
are purely cosmetic.

```python
TEAM = [
    {
        "id": 1,
        "email": "alex.chen@yourcompany.com",   # ← real Garmin email
        "name": "Alex Chen",
        ...
    },
    ...
]
```

### 3. Authenticate each team member

Each person runs this **once** on their own machine (or you collect passwords
securely and run it centrally):

```bash
# Authenticate a single user (prompts for their Garmin password)
python auth_setup.py --user 1

# Check who's authenticated
python auth_setup.py --status

# Authenticate everyone at once
python auth_setup.py --all
```

Tokens are saved to `~/.garth_squad/<id>/` and last ~1 year before needing
refresh (garth auto-refreshes the short-lived OAuth2 token).

### 4. Start the API server

```bash
python api/server.py
# → Running on http://0.0.0.0:5050
```

Optional environment variables:

| Variable          | Default           | Description                          |
|-------------------|-------------------|--------------------------------------|
| `PORT`            | `5050`            | HTTP port                            |
| `FLASK_DEBUG`     | `false`           | Enable Flask debug mode              |
| `GARTH_SQUAD_HOME`| `~/.garth_squad`  | Token storage root                   |

### 5. Connect the dashboard

In `garmin-dashboard.html`, replace the JavaScript section that begins with:

```js
// ─── DATA ───
const users = [ ... ];
```

with the contents of `dashboard_api_integration.js`, and change the last line
from:

```js
renderHero(); renderLB(); renderDetail(); renderTypes(); renderCalChart(); renderRings();
```

to:

```js
initDashboard();  // ← fetches from API, renders when ready
```

Also update the `setWeek` function call in each week button's `onclick` to use
`setWeekLive` instead of `setWeek`:

```html
<!-- Before -->
<button class="week-btn" onclick="setWeek(this,'1w')">1W</button>

<!-- After -->
<button class="week-btn" onclick="setWeekLive(this,'1w')">1W</button>
```

---

## API Endpoints

| Method | Path                          | Description                            |
|--------|-------------------------------|----------------------------------------|
| GET    | `/api/health`                 | Liveness check                         |
| GET    | `/api/status`                 | Auth status for each team member       |
| GET    | `/api/team?range=1w`          | Full team array (range: today/1w/4w)   |
| GET    | `/api/user/<id>?range=1w`     | Single user payload                    |
| GET    | `/api/user/<id>/monthly`      | 12-month history for one user          |

### Example response (single user)

```json
{
  "id": 1,
  "name": "Alex Chen",
  "role": "Engineering",
  "emoji": "🦁",
  "color": "#7c3aed",
  "bg": "#ede9fe",
  "garminDevice": "Forerunner 965",
  "types": ["Running", "Cycling"],
  "calories": 4820,
  "workouts": 6,
  "km": 68.4,
  "actKcal": 3210,
  "bmi": 22.1,
  "week": [1, 1, 0, 1, 1, 0, 1],
  "weekCalories": [720, 680, 0, 810, 650, 0, 960],
  "monthly": [
    { "cal": 18500, "sess": 22, "km": 210.3, "actKcal": 12400, "bmi": 22.0, "days": [...] },
    ...
  ]
}
```

---

## Data Mapping

| Dashboard Field | Garmin Source                                          |
|-----------------|--------------------------------------------------------|
| `calories`      | Sum of `calories` across activities in range           |
| `workouts`      | Count of activities                                    |
| `km`            | Sum of `distance` (metres → km) across activities      |
| `actKcal`       | Sum of `activeKilocalories` (activity kcal, not BMR)   |
| `bmi`           | Most recent body composition entry (last 90 days)      |
| `week[i]`       | Whether any activity was logged on day i (Mon=0)       |
| `weekCalories`  | `activeKilocalories` from daily summary per day        |
| `monthly[m]`    | Aggregated from all activities in calendar month m     |
| `types`         | Activity type keys from activity list, normalised      |

---

## Privacy & Security

- **Passwords are never stored** — only OAuth tokens (valid ~1 year)
- Token files are locked to `chmod 600` automatically
- The API server defaults to localhost only; add a reverse proxy (nginx/caddy)
  with TLS if you expose it externally
- For a shared/hosted deployment, consider a per-user token exchange flow
  rather than centralised token storage

---

## Troubleshooting

**"No tokens found for user X"**
→ Run `python auth_setup.py --user X`

**"Session expired for user X"**
→ Re-run `python auth_setup.py --user X`; garth OAuth2 tokens auto-refresh
  but the OAuth1 token (used for re-issuing) lasts ~1 year

**API returns zeros for a user**
→ That user's account either isn't authenticated (`python auth_setup.py --status`)
  or had a Garmin API error (check server logs)

**`GarthHTTPError 429`**
→ Garmin rate limit hit. Wait a few minutes and retry. The backend's
  per-month parallelism (4 threads) keeps this rare.
