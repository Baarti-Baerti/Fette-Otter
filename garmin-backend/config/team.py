# ────────────────────────────────────────────────────────────────
#  Squad Stats — Team Roster Config
# ────────────────────────────────────────────────────────────────
#
#  Each team member needs:
#    - A unique id (integer)
#    - Their Garmin Connect email (used for login / token storage)
#    - Display metadata (name, role, emoji, garminDevice)
#    - Color/bg for the dashboard UI
#
#  Tokens are stored per-user in:
#    ~/.garth_squad/<id>/
#
#  To authenticate a user for the first time, run:
#    python auth_setup.py --user <id>
# ────────────────────────────────────────────────────────────────

TEAM = [
    {
        "id": 1,
        "email": "alex.chen@yourcompany.com",
        "name": "Alex Chen",
        "role": "Engineering",
        "emoji": "🦁",
        "color": "#7c3aed",
        "bg": "#ede9fe",
        "garminDevice": "Forerunner 965",
        "types": ["Running", "Cycling", "HIIT"],
    },
    {
        "id": 2,
        "email": "sam.rivera@yourcompany.com",
        "name": "Sam Rivera",
        "role": "Design",
        "emoji": "🐯",
        "color": "#db2777",
        "bg": "#fce7f3",
        "garminDevice": "Venu 3",
        "types": ["Yoga", "Running", "Strength"],
    },
    {
        "id": 3,
        "email": "jordan.park@yourcompany.com",
        "name": "Jordan Park",
        "role": "Product",
        "emoji": "🦊",
        "color": "#0284c7",
        "bg": "#e0f2fe",
        "garminDevice": "Fenix 7",
        "types": ["Cycling", "Swimming", "Running"],
    },
    {
        "id": 4,
        "email": "taylor.kim@yourcompany.com",
        "name": "Taylor Kim",
        "role": "Marketing",
        "emoji": "🐺",
        "color": "#b45309",
        "bg": "#fef3c7",
        "garminDevice": "Instinct 2",
        "types": ["Running", "Walking", "Yoga"],
    },
    {
        "id": 5,
        "email": "morgan.wu@yourcompany.com",
        "name": "Morgan Wu",
        "role": "Data",
        "emoji": "🦅",
        "color": "#059669",
        "bg": "#d1fae5",
        "garminDevice": "Vivoactive 5",
        "types": ["HIIT", "Strength"],
    },
    {
        "id": 6,
        "email": "casey.patel@yourcompany.com",
        "name": "Casey Patel",
        "role": "Backend",
        "emoji": "🐬",
        "color": "#0e7490",
        "bg": "#cffafe",
        "garminDevice": "Swim 2",
        "types": ["Swimming", "Cycling"],
    },
    {
        "id": 7,
        "email": "drew.santos@yourcompany.com",
        "name": "Drew Santos",
        "role": "Frontend",
        "emoji": "🦋",
        "color": "#be185d",
        "bg": "#fdf2f8",
        "garminDevice": "Forerunner 55",
        "types": ["Walking", "Running"],
    },
    {
        "id": 8,
        "email": "quinn.lee@yourcompany.com",
        "name": "Quinn Lee",
        "role": "DevOps",
        "emoji": "🐉",
        "color": "#d97706",
        "bg": "#fffbeb",
        "garminDevice": "Lily 2",
        "types": ["Yoga", "Walking"],
    },
]
