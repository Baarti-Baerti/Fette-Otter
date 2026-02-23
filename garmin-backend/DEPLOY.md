# Squad Stats — Cloud Deployment Guide

Deploy the backend to the cloud so your whole team can use the dashboard
without anyone running a local server.

---

## Which platform?

| Platform | Free tier | Persistent disk | Deploy time |
|----------|-----------|-----------------|-------------|
| **Railway** | $5 credit/mo | ✅ Volume | ~2 min |
| **Render**  | 750 hrs/mo   | ✅ Disk (paid) | ~3 min |
| **Fly.io**  | 3 free VMs   | ✅ Volume | ~3 min |

Railway is the fastest to set up. Instructions for all three are below.

---

## Before you deploy — export your Garmin tokens

The container can't open a browser to log in to Garmin, so you export
your tokens locally and inject them as an environment variable.

```bash
# 1. Authenticate yourself (and any teammates) locally
python auth_setup.py --user 1

# 2. Export all tokens to a single env var
python token_export.py --export
# → GARTH_TOKENS_B64=eyJtZW1iZXJz...  (copy this whole line)
```

You'll paste `GARTH_TOKENS_B64=<value>` into your cloud provider's
environment variables dashboard in the steps below.

> **After deploying:** new members who join via the "Join Squad" button
> authenticate directly against Garmin from the cloud server —
> no re-export needed. `token_export.py` is only for pre-seeding
> your own account before the first deploy.

---

## Option A — Railway (recommended)

### 1. Push to GitHub

```bash
cd garmin-backend
git init
git add .
git commit -m "Squad Stats API"
# Create a repo on github.com, then:
git remote add origin https://github.com/YOUR_USER/squad-stats-api.git
git push -u origin main
```

### 2. Create Railway project

1. Go to [railway.app](https://railway.app) → **New Project**
2. Choose **Deploy from GitHub repo** → select your repo
3. Railway detects the `Dockerfile` and `railway.toml` automatically

### 3. Add a Volume

1. In your Railway service → **Volumes** tab
2. Click **New Volume** → mount path: `/data/garth_squad`

### 4. Set environment variables

In the Railway service → **Variables** tab, add:

```
GOOGLE_CLIENT_ID    = your-client-id.apps.googleusercontent.com
GARTH_TOKENS_B64    = eyJtZW1iZXJz...   ← from token_export.py --export
GARTH_SQUAD_HOME    = /data/garth_squad
PORT                = 8080
```

### 5. Deploy

Railway deploys automatically on every push. Click **Deploy** to trigger
the first one manually.

### 6. Get your URL

Railway gives you a URL like `https://squad-stats-api-production.up.railway.app`.

---

## Option B — Render

### 1. Push to GitHub (same as above)

### 2. New Web Service

1. [render.com](https://render.com) → **New** → **Web Service**
2. Connect your GitHub repo
3. Render detects `render.yaml` and pre-fills the settings

### 3. Set environment variables

In Render dashboard → **Environment**:

```
GOOGLE_CLIENT_ID    = your-client-id.apps.googleusercontent.com
GARTH_TOKENS_B64    = eyJtZW1iZXJz...
```

The `render.yaml` already sets `GARTH_SQUAD_HOME` and `PORT`.

### 4. Add disk (for persistent token storage)

Render's free tier doesn't persist disk between deploys. Either:
- Upgrade to **Starter** ($7/mo) to get the disk from `render.yaml`
- Or rely on `GARTH_TOKENS_B64` only (tokens are re-imported on every boot)

---

## Option C — Fly.io

### 1. Install flyctl

```bash
# macOS
brew install flyctl

# Linux / Windows WSL
curl -L https://fly.io/install.sh | sh
```

### 2. Sign in and launch

```bash
cd garmin-backend
fly auth login
fly launch --no-deploy   # reads fly.toml, asks for app name
```

### 3. Create a volume

```bash
fly volumes create garth_tokens --size 1 --region iad
```

### 4. Set secrets

```bash
fly secrets set \
  GOOGLE_CLIENT_ID="your-client-id.apps.googleusercontent.com" \
  GARTH_TOKENS_B64="eyJtZW1iZXJz..."
```

### 5. Deploy

```bash
fly deploy
```

### 6. Get your URL

```bash
fly status
# → App URL: https://squad-stats-api.fly.dev
```

---

## Update the dashboard to point to your server

Once deployed, open `garmin-dashboard.html` and update two lines:

```js
// Line ~707
const API_BASE = 'https://YOUR-APP.up.railway.app';   // ← your URL here

// Line ~1330
const GOOGLE_CLIENT_ID = 'your-client-id.apps.googleusercontent.com';
```

Also add your deployed domain to the **Authorized JavaScript Origins**
in [Google Cloud Console](https://console.cloud.google.com) →
APIs & Services → Credentials → your OAuth client.

---

## Verifying the deployment

```bash
# Health check
curl https://your-app.up.railway.app/api/health
# → {"status":"ok","team_size":1}

# List members
curl https://your-app.up.railway.app/api/members

# Team data
curl https://your-app.up.railway.app/api/team
```

---

## Re-exporting tokens after adding members locally

If you run `auth_setup.py` to add someone locally and want to push
their tokens to the cloud:

```bash
python token_export.py --export
# → GARTH_TOKENS_B64=<new value>
```

Update the env var in your cloud dashboard and redeploy.
New members who join via the dashboard UI don't need this —
they authenticate directly through the cloud server.

---

## Local Docker test (optional)

Before pushing to the cloud, you can test the Docker build locally:

```bash
docker build -t squad-stats .

docker run -p 8080:8080 \
  -e GOOGLE_CLIENT_ID=your-id \
  -e GARTH_TOKENS_B64=$(python token_export.py --export | grep '=' | cut -d= -f2-) \
  -v squad_tokens:/data/garth_squad \
  squad-stats

# Test
curl http://localhost:8080/api/health
```
