"""
APM Assessment Feedback System
A unified Flask app for feedback collection and intelligent analysis.
"""

import os
import json
import csv
import time
import hashlib
from datetime import datetime
from pathlib import Path
from functools import wraps
from io import StringIO

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, send_file, make_response, session
)
import anthropic

# ──────────────────────────────────────────────────────────
# App Setup
# ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "apm-feedback-secret-2024")

SUBMISSIONS_DIR = Path("feedback_submissions")
SUBMISSIONS_DIR.mkdir(exist_ok=True)

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")  # empty = no password

# In-memory cache
_cache = {
    "analysis": None,
    "last_hash": None,
    "last_processed": 0,
}

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))


# ──────────────────────────────────────────────────────────
# Auth
# ──────────────────────────────────────────────────────────

def require_dashboard_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not DASHBOARD_PASSWORD:
            return f(*args, **kwargs)
        if session.get("dashboard_authed"):
            return f(*args, **kwargs)
        return redirect(url_for("dashboard_login"))
    return decorated


# ──────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────

def get_submissions():
    """Read all JSON submissions from folder."""
    submissions = []
    for path in sorted(SUBMISSIONS_DIR.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                data["_filename"] = path.name
                submissions.append(data)
        except Exception:
            pass
    return submissions


def submissions_hash(submissions):
    raw = json.dumps([s.get("_filename") for s in submissions], sort_keys=True)
    return hashlib.md5(raw.encode()).hexdigest()


def analyse_with_claude(submissions):
    """Send all submissions to Claude for deep analysis."""
    if not submissions:
        return empty_analysis()

    submissions_text = json.dumps(submissions, indent=2, ensure_ascii=False)

    prompt = f"""You are analysing feedback submissions for the APM (Assessment) questionnaire.

Here are all feedback submissions in JSON format:
{submissions_text}

Please analyse this feedback and return ONLY a valid JSON object (no markdown, no backticks) with this exact structure:

{{
  "executive_summary": "A concise 3-5 sentence executive summary of all feedback combined.",
  "total_feedback_points": <integer>,
  "total_suggestions": <integer>,
  "sentiment_counts": {{
    "positive": <integer>,
    "neutral": <integer>,
    "negative": <integer>
  }},
  "themes": [
    {{
      "theme": "Theme Name",
      "description": "Brief description",
      "sentiment": "positive|neutral|negative",
      "points": [
        {{
          "contributor_name": "Name exactly as in submission",
          "contributor_role": "Role exactly as in submission",
          "text": "Exact verbatim feedback text - do not paraphrase",
          "field": "which feedback field this came from",
          "sentiment": "positive|neutral|negative"
        }}
      ]
    }}
  ],
  "colleague_summaries": [
    {{
      "contributor_name": "Name",
      "contributor_role": "Role",
      "contributor_email": "email",
      "timestamp": "timestamp string",
      "positive_count": <integer>,
      "neutral_count": <integer>,
      "negative_count": <integer>,
      "overall_rating": "their overall assessment dropdown value",
      "summary": "One sentence summary of their feedback",
      "points": [
        {{
          "field": "field name",
          "text": "exact verbatim text",
          "sentiment": "positive|neutral|negative"
        }}
      ]
    }}
  ]
}}

IMPORTANT RULES:
1. Never paraphrase — always use exact original text from submissions.
2. Themes should be meaningful groupings (e.g., "Jargon & Terminology", "Question Clarity", "Time Estimate", "Structure & Flow", "Specific Questions", "Positive Feedback").
3. Count each distinct feedback point separately.
4. Attribute every point to the exact contributor.
5. Return ONLY the JSON object, nothing else."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8000,
        messages=[{"role": "user", "content": prompt}]
    )

    text = response.content[0].text.strip()
    # Strip any accidental markdown fences
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


def empty_analysis():
    return {
        "executive_summary": "No submissions received yet.",
        "total_feedback_points": 0,
        "total_suggestions": 0,
        "sentiment_counts": {"positive": 0, "neutral": 0, "negative": 0},
        "themes": [],
        "colleague_summaries": []
    }


def get_analysis(force=False):
    """Return cached analysis or refresh if data changed."""
    submissions = get_submissions()
    h = submissions_hash(submissions)

    if not force and _cache["analysis"] and _cache["last_hash"] == h:
        return _cache["analysis"], submissions

    if not submissions:
        _cache["analysis"] = empty_analysis()
        _cache["last_hash"] = h
        return _cache["analysis"], submissions

    analysis = analyse_with_claude(submissions)
    _cache["analysis"] = analysis
    _cache["last_hash"] = h
    _cache["last_processed"] = time.time()
    return analysis, submissions


# ──────────────────────────────────────────────────────────
# Routes — Form
# ──────────────────────────────────────────────────────────

@app.route("/")
def index():
    return redirect(url_for("form"))


@app.route("/form")
def form():
    return render_template("form.html")


@app.route("/submit", methods=["POST"])
def submit():
    data = request.form
    contributor_name = data.get("contributor_name", "").strip()
    contributor_role = data.get("contributor_role", "").strip()
    contributor_email = data.get("contributor_email", "").strip()

    if not contributor_name:
        return jsonify({"error": "Name is required"}), 400

    timestamp = datetime.now().isoformat()
    safe_name = "".join(c for c in contributor_name if c.isalnum() or c in " _-").replace(" ", "_")
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_name}_{ts_str}.json"

    submission = {
        "timestamp": timestamp,
        "contributor_name": contributor_name,
        "contributor_role": contributor_role,
        "contributor_email": contributor_email,
        "feedback": {
            "clarity": data.get("clarity", ""),
            "structure": data.get("structure", ""),
            "relevance": data.get("relevance", ""),
            "jargon": data.get("jargon", ""),
            "length": data.get("length", ""),
            "specific_issues": data.get("specific_issues", ""),
            "specific_positive": data.get("specific_positive", ""),
            "suggestions": data.get("suggestions", ""),
            "overall": data.get("overall", ""),
            "additional": data.get("additional", ""),
        }
    }

    with open(SUBMISSIONS_DIR / filename, "w", encoding="utf-8") as f:
        json.dump(submission, f, indent=2, ensure_ascii=False)

    return jsonify({"success": True, "message": "Feedback submitted successfully!"})


# ──────────────────────────────────────────────────────────
# Routes — Dashboard Auth
# ──────────────────────────────────────────────────────────

@app.route("/dashboard/login", methods=["GET", "POST"])
def dashboard_login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["dashboard_authed"] = True
            return redirect(url_for("dashboard"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/dashboard/logout")
def dashboard_logout():
    session.pop("dashboard_authed", None)
    return redirect(url_for("dashboard_login"))


# ──────────────────────────────────────────────────────────
# Routes — Dashboard
# ──────────────────────────────────────────────────────────

@app.route("/dashboard")
@require_dashboard_auth
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/data")
@require_dashboard_auth
def api_data():
    force = request.args.get("force") == "1"
    try:
        analysis, submissions = get_analysis(force=force)
        return jsonify({
            "analysis": analysis,
            "submission_count": len(submissions),
            "last_processed": _cache["last_processed"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/check")
@require_dashboard_auth
def api_check():
    """Lightweight check — has data changed?"""
    submissions = get_submissions()
    h = submissions_hash(submissions)
    changed = h != _cache["last_hash"]
    return jsonify({
        "changed": changed,
        "submission_count": len(submissions),
        "current_hash": h
    })


@app.route("/ask", methods=["POST"])
@require_dashboard_auth
def ask():
    question = request.json.get("question", "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    analysis, submissions = get_analysis()
    context = json.dumps(analysis, indent=2, ensure_ascii=False)

    prompt = f"""You are an expert analyst for an APM Assessment questionnaire feedback system.

Here is the full analysis data (themes, colleague summaries, sentiment):
{context}

A stakeholder is asking: "{question}"

Answer the question accurately, citing specific contributors and exact quotes where relevant.
Be concise but thorough. Use bullet points where helpful. Always attribute quotes to the correct person."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1500,
        messages=[{"role": "user", "content": prompt}]
    )

    return jsonify({"answer": response.content[0].text})


# ──────────────────────────────────────────────────────────
# Routes — Export
# ──────────────────────────────────────────────────────────

@app.route("/export/json")
@require_dashboard_auth
def export_json():
    analysis, submissions = get_analysis()
    payload = {"analysis": analysis, "raw_submissions": submissions}
    resp = make_response(json.dumps(payload, indent=2, ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json"
    resp.headers["Content-Disposition"] = "attachment; filename=apm_feedback_export.json"
    return resp


@app.route("/export/csv")
@require_dashboard_auth
def export_csv():
    submissions = get_submissions()
    si = StringIO()
    writer = csv.writer(si)
    writer.writerow([
        "Timestamp", "Name", "Role", "Email",
        "Clarity", "Structure", "Relevance", "Jargon",
        "Length", "Specific Issues", "What Works", "Suggestions",
        "Overall Rating", "Additional"
    ])
    for s in submissions:
        fb = s.get("feedback", {})
        writer.writerow([
            s.get("timestamp", ""),
            s.get("contributor_name", ""),
            s.get("contributor_role", ""),
            s.get("contributor_email", ""),
            fb.get("clarity", ""),
            fb.get("structure", ""),
            fb.get("relevance", ""),
            fb.get("jargon", ""),
            fb.get("length", ""),
            fb.get("specific_issues", ""),
            fb.get("specific_positive", ""),
            fb.get("suggestions", ""),
            fb.get("overall", ""),
            fb.get("additional", ""),
        ])
    resp = make_response(si.getvalue())
    resp.headers["Content-Type"] = "text/csv"
    resp.headers["Content-Disposition"] = "attachment; filename=apm_feedback_export.csv"
    return resp


@app.route("/export/html")
@require_dashboard_auth
def export_html():
    analysis, submissions = get_analysis()
    html = render_template("report.html", analysis=analysis, submissions=submissions,
                           generated=datetime.now().strftime("%d %B %Y at %H:%M"))
    resp = make_response(html)
    resp.headers["Content-Type"] = "text/html"
    resp.headers["Content-Disposition"] = "attachment; filename=apm_feedback_report.html"
    return resp


# ──────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
