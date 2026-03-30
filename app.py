"""
APM Assessment Feedback System
A unified Flask app for feedback collection and intelligent analysis.
"""

import os
import json
import csv
import time
import hashlib
import threading
from datetime import datetime
from pathlib import Path
from functools import wraps
from io import StringIO, BytesIO

from flask import (
    Flask, render_template, request, jsonify,
    redirect, url_for, send_file, make_response, session
)
import anthropic
from supabase import create_client, ClientOptions

# ──────────────────────────────────────────────────────────
# App Setup
# ──────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "apm-feedback-secret-2024")

DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")  # empty = no password

supabase = create_client(
    os.environ.get("SUPABASE_URL", ""),
    os.environ.get("SUPABASE_KEY", ""),
    options=ClientOptions(postgrest_client_timeout=10)
)

# In-memory cache
_cache = {
    "analysis": None,
    "last_hash": None,
    "last_processed": 0,
    "status": "idle",   # "idle" | "running" | "error"
    "error": None,
    "retry_count": 0,
    "retry_after": 0.0,  # epoch time before which retries are suppressed
}
_analysis_lock = threading.Lock()

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
    """Read all submissions from Supabase."""
    response = supabase.table("submissions").select("*").order("timestamp").execute()
    submissions = []
    for row in response.data:
        row["_id"] = row["id"]
        submissions.append(row)
    return submissions


def submissions_hash(submissions):
    raw = json.dumps([s.get("_id") for s in submissions], sort_keys=True)
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
  "gm_headline": "One clear, direct sentence summarising the overall verdict for a senior manager who has 10 seconds to read it. Lead with the overall sentiment, name the biggest strength and biggest concern. Example style: 'Colleagues find the questionnaire well-structured and relevant, but consistently flag jargon and unclear time expectations as barriers to completion.'",
  "overall_sentiment": "positive OR mixed OR negative — choose based on the balance of sentiment counts",
  "executive_summary": "Three structured paragraphs: (1) Overall picture — what respondents think of the assessment in plain language. (2) Key strengths — what is working well, with specific examples. (3) Key concerns and recommendations — what needs to change and concrete actions to take. Write for a general manager with no technical background. Use plain language, no jargon.",
  "top_recommendations": [
    "Specific, actionable recommendation 1 — start with a verb, be concrete",
    "Specific, actionable recommendation 2",
    "Specific, actionable recommendation 3"
  ],
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
        "gm_headline": "No submissions received yet.",
        "overall_sentiment": "neutral",
        "executive_summary": "No submissions received yet.",
        "top_recommendations": [],
        "total_feedback_points": 0,
        "total_suggestions": 0,
        "sentiment_counts": {"positive": 0, "neutral": 0, "negative": 0},
        "themes": [],
        "colleague_summaries": []
    }


def _run_analysis_background(submissions, h):
    try:
        analysis = analyse_with_claude(submissions)
        _cache["analysis"] = analysis
        _cache["last_hash"] = h
        _cache["last_processed"] = time.time()
        _cache["status"] = "idle"
        _cache["error"] = None
        _cache["retry_count"] = 0
        _cache["retry_after"] = 0.0
    except Exception as e:
        # Always update last_hash on failure so /api/check stops reporting
        # changed=True in a loop — the notification clears until a genuinely
        # new submission arrives.
        _cache["last_hash"] = h
        _cache["status"] = "error"
        _cache["error"] = str(e)
        _cache["retry_count"] += 1
        # Exponential backoff: 30s, 60s, 120s (capped)
        backoff = min(120, 30 * _cache["retry_count"])
        _cache["retry_after"] = time.time() + backoff


def get_analysis(force=False):
    """Return cached analysis immediately; trigger background refresh if stale."""
    submissions = get_submissions()
    h = submissions_hash(submissions)

    # Cache is fresh — return straight away
    if not force and _cache["analysis"] and _cache["last_hash"] == h:
        return _cache["analysis"], submissions

    # No submissions — short-circuit
    if not submissions:
        _cache["analysis"] = empty_analysis()
        _cache["last_hash"] = h
        _cache["status"] = "idle"
        return _cache["analysis"], submissions

    # Kick off background analysis if not already running
    with _analysis_lock:
        if _cache["status"] != "running":
            # Respect retry backoff unless the caller forced a refresh
            if (not force
                    and _cache["status"] == "error"
                    and time.time() < _cache["retry_after"]):
                return _cache["analysis"] or empty_analysis(), submissions
            _cache["status"] = "running"
            _cache["error"] = None
            threading.Thread(
                target=_run_analysis_background,
                args=(submissions, h),
                daemon=True,
            ).start()

    return _cache["analysis"] or empty_analysis(), submissions


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

    supabase.table("submissions").insert({
        "timestamp": datetime.now().isoformat(),
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
    }).execute()

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
            "current_hash": submissions_hash(submissions),
            "last_processed": _cache["last_processed"],
            "status": _cache["status"],
            "error": _cache["error"],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/check")
@require_dashboard_auth
def api_check():
    """Lightweight check — has data changed?"""
    try:
        submissions = get_submissions()
        h = submissions_hash(submissions)
        changed = h != _cache["last_hash"]
        return jsonify({
            "changed": changed,
            "submission_count": len(submissions),
            "current_hash": h
        })
    except Exception as e:
        # On Supabase timeout/error return a safe no-change response so the
        # client doesn't show a spurious notification or crash the worker.
        return jsonify({
            "changed": False,
            "submission_count": None,
            "current_hash": _cache["last_hash"],
            "error": str(e)
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


@app.route("/export/docx")
@require_dashboard_auth
def export_docx():
    from docx import Document
    from docx.shared import RGBColor
    analysis, submissions = get_analysis()
    generated = datetime.now().strftime("%d %B %Y at %H:%M")

    doc = Document()

    # Title
    title = doc.add_heading("APM Assessment — Feedback Report", 0)
    title.runs[0].font.color.rgb = RGBColor(0x2e, 0x5c, 0xff)

    doc.add_paragraph(f"Generated: {generated} · {len(submissions)} submission{'s' if len(submissions) != 1 else ''}")

    # Executive summary
    doc.add_heading("Executive Summary", 1)
    doc.add_paragraph(analysis.get("executive_summary", ""))

    # Stats table
    doc.add_heading("Overview", 1)
    sc = analysis.get("sentiment_counts", {})
    stats_table = doc.add_table(rows=2, cols=5)
    stats_table.style = "Table Grid"
    headers = ["Submissions", "Feedback Points", "Suggestions", "Positive", "Negative"]
    values = [
        str(len(submissions)),
        str(analysis.get("total_feedback_points", 0)),
        str(analysis.get("total_suggestions", 0)),
        str(sc.get("positive", 0)),
        str(sc.get("negative", 0)),
    ]
    for i, (h, v) in enumerate(zip(headers, values)):
        stats_table.cell(0, i).text = h
        stats_table.cell(0, i).paragraphs[0].runs[0].font.bold = True
        stats_table.cell(1, i).text = v

    # Themes
    doc.add_heading("Themes", 1)
    for theme in analysis.get("themes", []):
        doc.add_heading(f"{theme['theme']} ({theme.get('sentiment', '')})", 2)
        if theme.get("description"):
            p = doc.add_paragraph(theme["description"])
            p.runs[0].italic = True
        for pt in theme.get("points", []):
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(f'"{pt["text"]}"')
            p.add_run(f"\n— {pt['contributor_name']}, {pt.get('contributor_role', '–')}").font.color.rgb = RGBColor(0x88, 0x88, 0x88)

    # Colleague summaries
    doc.add_heading("Colleagues", 1)
    for c in analysis.get("colleague_summaries", []):
        doc.add_heading(c["contributor_name"], 2)
        role_line = c.get("contributor_role", "–")
        if c.get("contributor_email"):
            role_line += f" · {c['contributor_email']}"
        doc.add_paragraph(role_line).runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        if c.get("overall_rating"):
            doc.add_paragraph(f"Overall rating: {c['overall_rating']}")
        if c.get("summary"):
            doc.add_paragraph(c["summary"])

    # Raw submissions
    doc.add_heading("Raw Submissions", 1)
    field_labels = {
        "clarity": "Clarity", "structure": "Structure", "relevance": "Relevance",
        "jargon": "Jargon", "length": "Length", "specific_issues": "Specific Issues",
        "specific_positive": "What Works", "suggestions": "Suggestions",
        "overall": "Overall Rating", "additional": "Additional",
    }
    for s in submissions:
        doc.add_heading(f"{s['contributor_name']} — {s['timestamp'][:10]}", 2)
        doc.add_paragraph(s.get("contributor_role", "–")).runs[0].font.color.rgb = RGBColor(0x88, 0x88, 0x88)
        fb = s.get("feedback", {})
        for key, label in field_labels.items():
            val = fb.get(key, "")
            if val:
                p = doc.add_paragraph()
                p.add_run(f"{label}: ").bold = True
                p.add_run(val)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    resp.headers["Content-Disposition"] = "attachment; filename=apm_feedback_report.docx"
    return resp


@app.route("/export/xlsx")
@require_dashboard_auth
def export_xlsx():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    analysis, submissions = get_analysis()

    wb = openpyxl.Workbook()

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill(fill_type="solid", fgColor="2E5CFF")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    def style_header_row(ws, row=1):
        for cell in ws[row]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align

    # ── Sheet 1: Summary ──
    ws1 = wb.active
    ws1.title = "Summary"
    ws1.append(["APM Assessment Feedback Report"])
    ws1["A1"].font = Font(bold=True, size=14)
    ws1.append([f"Generated: {datetime.now().strftime('%d %B %Y at %H:%M')}"])
    ws1.append([])
    ws1.append(["Metric", "Value"])
    style_header_row(ws1, 4)
    sc = analysis.get("sentiment_counts", {})
    for label, val in [
        ("Submissions", len(submissions)),
        ("Feedback Points", analysis.get("total_feedback_points", 0)),
        ("Suggestions", analysis.get("total_suggestions", 0)),
        ("Positive Points", sc.get("positive", 0)),
        ("Neutral Points", sc.get("neutral", 0)),
        ("Negative Points", sc.get("negative", 0)),
    ]:
        ws1.append([label, val])
    ws1.append([])
    ws1.append(["Executive Summary"])
    ws1["A9"].font = Font(bold=True)
    ws1.append([analysis.get("executive_summary", "")])
    ws1["A10"].alignment = Alignment(wrap_text=True)
    ws1.column_dimensions["A"].width = 25
    ws1.column_dimensions["B"].width = 60
    ws1.row_dimensions[10].height = 80

    # ── Sheet 2: Themes ──
    ws2 = wb.create_sheet("Themes")
    ws2.append(["Theme", "Description", "Theme Sentiment", "Contributor", "Role", "Feedback Text", "Field", "Sentiment"])
    style_header_row(ws2)
    for theme in analysis.get("themes", []):
        for pt in theme.get("points", []):
            ws2.append([
                theme.get("theme", ""),
                theme.get("description", ""),
                theme.get("sentiment", ""),
                pt.get("contributor_name", ""),
                pt.get("contributor_role", ""),
                pt.get("text", ""),
                pt.get("field", ""),
                pt.get("sentiment", ""),
            ])
    for col, width in zip("ABCDEFGH", [22, 30, 14, 18, 18, 55, 18, 12]):
        ws2.column_dimensions[col].width = width
    for row in ws2.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    # ── Sheet 3: Colleagues ──
    ws3 = wb.create_sheet("Colleagues")
    ws3.append(["Name", "Role", "Email", "Overall Rating", "Positive", "Neutral", "Negative", "Summary"])
    style_header_row(ws3)
    for c in analysis.get("colleague_summaries", []):
        ws3.append([
            c.get("contributor_name", ""),
            c.get("contributor_role", ""),
            c.get("contributor_email", ""),
            c.get("overall_rating", ""),
            c.get("positive_count", 0),
            c.get("neutral_count", 0),
            c.get("negative_count", 0),
            c.get("summary", ""),
        ])
    for col, width in zip("ABCDEFGH", [20, 20, 28, 16, 9, 9, 9, 50]):
        ws3.column_dimensions[col].width = width

    # ── Sheet 4: Raw Submissions ──
    ws4 = wb.create_sheet("Raw Submissions")
    ws4.append([
        "Timestamp", "Name", "Role", "Email",
        "Clarity", "Structure", "Relevance", "Jargon", "Length",
        "Specific Issues", "What Works", "Suggestions", "Overall Rating", "Additional",
    ])
    style_header_row(ws4)
    for s in submissions:
        fb = s.get("feedback", {})
        ws4.append([
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
    for col, width in zip("ABCDEFGHIJKLMN", [22, 18, 18, 28, 14, 14, 14, 14, 10, 40, 40, 40, 16, 40]):
        ws4.column_dimensions[col].width = width
    for row in ws4.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(wrap_text=True, vertical="top")

    # ── Charts Sheet (inserted at front) ──
    from openpyxl.chart import PieChart, BarChart, Reference

    wsc = wb.create_sheet("Charts", 0)
    wsc.sheet_view.showGridLines = False

    # Sentiment data table (A1:B4)
    for r, (label, val) in enumerate([
        ("Sentiment", "Count"),
        ("Positive",  sc.get("positive", 0)),
        ("Neutral",   sc.get("neutral", 0)),
        ("Negative",  sc.get("negative", 0)),
    ], start=1):
        wsc.cell(row=r, column=1).value = label
        wsc.cell(row=r, column=2).value = val
    wsc.cell(1, 1).font = Font(bold=True)
    wsc.cell(1, 2).font = Font(bold=True)

    pie = PieChart()
    pie.title = "Overall Sentiment"
    pie.style = 10
    pie.add_data(Reference(wsc, min_col=2, min_row=1, max_row=4), titles_from_data=True)
    pie.set_categories(Reference(wsc, min_col=1, min_row=2, max_row=4))
    pie.width = 16
    pie.height = 12
    wsc.add_chart(pie, "D1")

    # Theme data table
    themes = analysis.get("themes", [])
    theme_row = 6
    wsc.cell(row=theme_row, column=1).value = "Theme"
    wsc.cell(row=theme_row, column=2).value = "Points"
    wsc.cell(theme_row, 1).font = Font(bold=True)
    wsc.cell(theme_row, 2).font = Font(bold=True)
    for i, t in enumerate(themes):
        wsc.cell(row=theme_row + 1 + i, column=1).value = t.get("theme", "")
        wsc.cell(row=theme_row + 1 + i, column=2).value = len(t.get("points", []))

    if themes:
        bar = BarChart()
        bar.type = "col"
        bar.title = "Feedback Points by Theme"
        bar.y_axis.title = "Points"
        bar.style = 10
        bar.add_data(
            Reference(wsc, min_col=2, min_row=theme_row, max_row=theme_row + len(themes)),
            titles_from_data=True,
        )
        bar.set_categories(
            Reference(wsc, min_col=1, min_row=theme_row + 1, max_row=theme_row + len(themes))
        )
        bar.width = 22
        bar.height = 14
        wsc.add_chart(bar, "D14")

    # Colleague sentiment data table
    colleagues = analysis.get("colleague_summaries", [])
    col_row = theme_row + len(themes) + 3
    for col_idx, label in enumerate(["Colleague", "Positive", "Neutral", "Negative"], start=1):
        cell = wsc.cell(row=col_row, column=col_idx)
        cell.value = label
        cell.font = Font(bold=True)
    for i, c in enumerate(colleagues):
        wsc.cell(row=col_row + 1 + i, column=1).value = c.get("contributor_name", "")
        wsc.cell(row=col_row + 1 + i, column=2).value = c.get("positive_count", 0)
        wsc.cell(row=col_row + 1 + i, column=3).value = c.get("neutral_count", 0)
        wsc.cell(row=col_row + 1 + i, column=4).value = c.get("negative_count", 0)

    if colleagues:
        bar2 = BarChart()
        bar2.type = "col"
        bar2.grouping = "stacked"
        bar2.overlap = 100
        bar2.title = "Sentiment by Colleague"
        bar2.y_axis.title = "Points"
        bar2.style = 10
        for col_idx in [2, 3, 4]:
            bar2.add_data(
                Reference(wsc, min_col=col_idx, min_row=col_row, max_row=col_row + len(colleagues)),
                titles_from_data=True,
            )
        bar2.set_categories(
            Reference(wsc, min_col=1, min_row=col_row + 1, max_row=col_row + len(colleagues))
        )
        bar2.width = 22
        bar2.height = 14
        wsc.add_chart(bar2, "D30")

    wsc.column_dimensions["A"].width = 30
    wsc.column_dimensions["B"].width = 10
    wsc.column_dimensions["C"].width = 10

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    resp = make_response(buf.read())
    resp.headers["Content-Type"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    resp.headers["Content-Disposition"] = "attachment; filename=apm_feedback_report.xlsx"
    return resp


# ──────────────────────────────────────────────────────────
# Run
# ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
