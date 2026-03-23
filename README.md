# APM Assessment Feedback System

A unified Flask web application for collecting and intelligently analysing APM questionnaire feedback. Colleagues submit feedback via a public form; you access an AI-powered dashboard that auto-processes submissions.

---

## Features

- **Public feedback form** — mobile-responsive, no login needed
- **AI-powered dashboard** — Claude analyses all submissions automatically
- **4 dashboard tabs**: Overview · Themes · Colleagues · Ask AI
- **Live updates** — polls for new submissions every 30 seconds
- **Export** — JSON, CSV, HTML report
- **Optional password** — protect the dashboard from public access

---

## Quick Start (Local)

```bash
# 1. Clone / download the project
cd apm-feedback

# 2. Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment
cp .env.example .env
# Edit .env and add your ANTHROPIC_API_KEY

# 5. Run the app
python app.py
```

Visit:
- Form: http://localhost:5000/form
- Dashboard: http://localhost:5000/dashboard

---

## Deploy to Render.com (Recommended — Free)

Render gives you a public HTTPS URL that works from anywhere.

### Step 1 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/apm-feedback.git
git push -u origin main
```

### Step 2 — Create a Render account

Go to [render.com](https://render.com) and sign up (free).

### Step 3 — New Web Service

1. Click **New → Web Service**
2. Connect your GitHub repo
3. Render auto-detects Python — confirm these settings:
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `gunicorn app:app`

### Step 4 — Add Environment Variables

In Render dashboard → **Environment** tab, add:

| Key | Value |
|-----|-------|
| `ANTHROPIC_API_KEY` | `sk-ant-...` |
| `DASHBOARD_PASSWORD` | `your-secret-password` (or leave blank) |
| `SECRET_KEY` | Any random string |

### Step 5 — Add Persistent Disk

> ⚠️ Important: Without a disk, feedback submissions are lost on restart.

1. In Render → **Disks** tab → **Add Disk**
2. Name: `feedback-data`
3. Mount Path: `/opt/render/project/src/feedback_submissions`
4. Size: 1 GB (free tier)

### Step 6 — Deploy

Click **Deploy**. In ~2 minutes you'll have a URL like:

```
https://apm-feedback-system.onrender.com
```

Share with colleagues:
- **Form**: `https://apm-feedback-system.onrender.com/form`
- **Dashboard**: `https://apm-feedback-system.onrender.com/dashboard`

---

## Deploy to Railway.app (Alternative)

```bash
# Install Railway CLI
npm i -g @railway/cli

# Login and deploy
railway login
railway init
railway up
```

Add environment variables in the Railway dashboard.

---

## Deploy to Heroku

```bash
# Install Heroku CLI, then:
heroku create apm-feedback-tool
heroku config:set ANTHROPIC_API_KEY=sk-ant-...
heroku config:set DASHBOARD_PASSWORD=yourpassword

git push heroku main
heroku open
```

Note: Heroku's free tier was discontinued. Use the eco dyno ($5/month) or choose Render instead.

---

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Your Anthropic API key |
| `DASHBOARD_PASSWORD` | No | Password for dashboard (blank = no password) |
| `SECRET_KEY` | No | Random string for session security |
| `PORT` | No | Port to run on (default: 5000) |

---

## File Structure

```
apm-feedback/
├── app.py                    ← Main Flask application
├── requirements.txt
├── render.yaml               ← Render.com config
├── .env.example
├── .gitignore
├── README.md
├── feedback_submissions/     ← JSON files saved here (auto-created)
└── templates/
    ├── base.html
    ├── form.html             ← Public feedback form
    ├── dashboard.html        ← Analysis dashboard
    ├── login.html            ← Dashboard password page
    └── report.html           ← Printable HTML report
```

---

## How It Works

1. Colleague visits `/form` and fills in their feedback
2. Submission saved as `Name_YYYYMMDD_HHMMSS.json` in `feedback_submissions/`
3. Dashboard at `/dashboard` reads all JSON files
4. All submissions sent to Claude API for analysis
5. Results cached in memory (re-analysed when new files detected)
6. Dashboard polls `/api/check` every 30s for new submissions

---

## Dashboard Routes

| Route | Description |
|-------|-------------|
| `GET /form` | Public feedback form |
| `POST /submit` | Form submission handler |
| `GET /dashboard` | Analysis dashboard |
| `GET /api/data` | JSON analysis data |
| `GET /api/check` | Lightweight change detection |
| `POST /ask` | AI Q&A endpoint |
| `GET /export/json` | Download full JSON export |
| `GET /export/csv` | Download CSV |
| `GET /export/html` | Download HTML report |

---

## Troubleshooting

**"No submissions" even after submitting**
- Check that `feedback_submissions/` folder exists and is writable
- On Render: ensure the disk is mounted at the correct path

**Analysis not updating**
- Click the **↺ Refresh** button in the dashboard header
- Or wait 30 seconds for the auto-poll to detect changes

**Claude API errors**
- Verify `ANTHROPIC_API_KEY` is set correctly
- Check your Anthropic account has credits

**Dashboard password not working**
- Ensure `DASHBOARD_PASSWORD` env var is set (restart the server after changing)
- Leave blank to disable password entirely
