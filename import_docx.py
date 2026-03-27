"""
import_docx.py — Import a colleague's Word-document feedback into the submission store.

Usage:
    python import_docx.py <path_to_docx> [--name "Full Name"] [--role "Role"] [--email "email"]

The script reads the .docx, uses Claude to map its content to the ten feedback form
fields, and writes a JSON file into feedback_submissions/ as if the colleague had
filled in the form themselves.

Requires:
    pip install python-docx
    ANTHROPIC_API_KEY set in environment or .env
"""

import sys
import os
import json
import argparse
from datetime import datetime
from pathlib import Path

# Force UTF-8 output on Windows terminals
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

try:
    import docx
except ImportError:
    sys.exit("python-docx is not installed. Run: pip install python-docx")

import urllib.request
import urllib.parse

import anthropic
from dotenv import load_dotenv

load_dotenv()

# ── Form field definitions (mirrors templates/form.html) ────────────────────

FORM_FIELDS = {
    "clarity": {
        "label": "Overall Clarity",
        "hint": "Are the questions clearly written and easy to understand?",
        "max": 1000,
    },
    "structure": {
        "label": "Question Structure & Flow",
        "hint": "Is the progression logical? Does it flow naturally from one topic to the next?",
        "max": 1000,
    },
    "relevance": {
        "label": "Relevance to Your Role",
        "hint": "Do the questions apply to your day-to-day work?",
        "max": 1000,
    },
    "jargon": {
        "label": "Jargon & Accessibility",
        "hint": "Are there terms that need explanation or a glossary?",
        "max": 1000,
    },
    "length": {
        "label": "Length & Time Estimate",
        "hint": "Is the 15-minute estimate accurate? Too long or too short?",
        "max": 500,
    },
    "specific_issues": {
        "label": "Specific Issues",
        "hint": "Which questions are problematic? Reference question numbers (e.g. Q7, Q12).",
        "max": 1500,
    },
    "specific_positive": {
        "label": "What Works Well",
        "hint": "Which questions do you find most effective and why?",
        "max": 1000,
    },
    "suggestions": {
        "label": "Suggestions for Improvement",
        "hint": "Actionable changes you'd recommend.",
        "max": 1500,
    },
    "overall": {
        "label": "Overall Assessment",
        "hint": "Overall rating. Must be one of the four allowed values.",
        "max": None,
        "allowed_values": [
            "Excellent - Ready to use as-is",
            "Good - Minor improvements needed",
            "Adequate - Some revisions required",
            "Needs Work - Significant changes needed",
        ],
    },
    "additional": {
        "label": "Additional Comments",
        "hint": "Anything else the reviewer wants to share.",
        "max": 1500,
    },
}

OVERALL_OPTIONS = FORM_FIELDS["overall"]["allowed_values"]


# ── Helpers ──────────────────────────────────────────────────────────────────

def extract_text(docx_path: str) -> str:
    """Return all paragraph text from a .docx file, joined by newlines."""
    doc = docx.Document(docx_path)
    lines = [p.text for p in doc.paragraphs if p.text.strip()]
    return "\n".join(lines)


def map_with_claude(document_text: str, contributor_name: str) -> dict:
    """Use Claude to map unstructured Word-doc feedback to the ten form fields."""
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    field_descriptions = "\n".join(
        f'- "{key}": {info["label"]} — {info["hint"]}'
        + (f' (max {info["max"]} chars)' if info["max"] else "")
        + (f' — must be one of: {info["allowed_values"]}' if "allowed_values" in info else "")
        for key, info in FORM_FIELDS.items()
    )

    prompt = f"""You are helping import feedback written in a Word document into a structured web form.

The form collects feedback about an APM (Assessment) questionnaire. It has these fields:
{field_descriptions}

Here is the raw feedback text from the Word document:
---
{document_text}
---

Your task:
1. Read the entire document and map its content to the most appropriate form field(s).
2. Use the contributor's own words wherever possible — do not paraphrase.
3. If content spans multiple fields, split it sensibly.
4. Leave a field as an empty string "" if no content from the document fits it.
5. For "overall", you MUST choose exactly one of the four allowed values based on the overall tone. Do not leave it empty.
6. Respect character limits — truncate text (at a sentence boundary) if necessary.

Return ONLY a valid JSON object with exactly these keys:
clarity, structure, relevance, jargon, length, specific_issues, specific_positive, suggestions, overall, additional

No markdown, no explanation — just the JSON object."""

    response = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=4000,
        messages=[{"role": "user", "content": prompt}],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]

    feedback = json.loads(text)

    # Validate overall is one of the allowed values
    if feedback.get("overall") not in OVERALL_OPTIONS:
        feedback["overall"] = "Adequate - Some revisions required"

    # Enforce character limits
    for key, info in FORM_FIELDS.items():
        if info["max"] and len(feedback.get(key, "")) > info["max"]:
            feedback[key] = feedback[key][: info["max"]].rsplit(" ", 1)[0]

    return feedback


def post_submission(base_url: str, contributor_name: str, contributor_role: str,
                    contributor_email: str, feedback: dict) -> None:
    """POST the submission to the live /submit endpoint."""
    form_data = {
        "contributor_name": contributor_name,
        "contributor_role": contributor_role,
        "contributor_email": contributor_email,
        **feedback,
    }
    encoded = urllib.parse.urlencode(form_data).encode("utf-8")
    url = base_url.rstrip("/") + "/submit"
    req = urllib.request.Request(url, data=encoded, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    if not body.get("success"):
        raise RuntimeError(f"Server returned error: {body}")


def save_submission(contributor_name: str, contributor_role: str,
                    contributor_email: str, feedback: dict) -> Path:
    submissions_dir = Path(__file__).parent / "feedback_submissions"
    submissions_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().isoformat()
    safe_name = "".join(
        c for c in contributor_name if c.isalnum() or c in " _-"
    ).replace(" ", "_")
    ts_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{safe_name}_{ts_str}.json"

    submission = {
        "timestamp": timestamp,
        "contributor_name": contributor_name,
        "contributor_role": contributor_role,
        "contributor_email": contributor_email,
        "feedback": feedback,
    }

    out_path = submissions_dir / filename
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(submission, f, indent=2, ensure_ascii=False)

    return out_path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Import a Word-document feedback file into the APM feedback store."
    )
    parser.add_argument("docx_path", help="Path to the .docx file")
    parser.add_argument("--name", default="", help="Contributor's full name (overrides auto-detect)")
    parser.add_argument("--role", default="", help="Contributor's role / team")
    parser.add_argument("--email", default="", help="Contributor's email address")
    parser.add_argument("--url", default="", help="Base URL of deployed app (e.g. https://apm-feedback.onrender.com). If omitted, saves locally.")
    args = parser.parse_args()

    docx_path = args.docx_path
    if not Path(docx_path).exists():
        sys.exit(f"File not found: {docx_path}")

    # Auto-detect name from filename if not provided (e.g. "Feedback_Nguyễn Thanh Dũng.docx")
    contributor_name = args.name
    if not contributor_name:
        stem = Path(docx_path).stem  # filename without extension
        # Strip common prefixes like "MBE Feedback_", "Feedback_"
        for prefix in ("MBE Feedback_", "Feedback_", "feedback_"):
            if stem.startswith(prefix):
                stem = stem[len(prefix):]
                break
        contributor_name = stem.replace("_", " ").strip()

    print(f"Contributor : {contributor_name}")
    print(f"Role        : {args.role or '(not provided)'}")
    print(f"Email       : {args.email or '(not provided)'}")
    print(f"Reading     : {docx_path}")

    document_text = extract_text(docx_path)
    if not document_text.strip():
        sys.exit("The document appears to be empty.")

    print("Mapping content to form fields via Claude…")
    feedback = map_with_claude(document_text, contributor_name)

    print("\nMapped fields:")
    for key, value in feedback.items():
        preview = (value[:80] + "…") if len(value) > 80 else value
        print(f"  {key:20s}: {preview!r}")

    if args.url:
        print(f"\nPosting to {args.url.rstrip('/')}/submit …")
        post_submission(args.url, contributor_name, args.role, args.email, feedback)
        print("Submitted successfully. Refresh the dashboard to trigger re-analysis.")
    else:
        out_path = save_submission(contributor_name, args.role, args.email, feedback)
        print(f"\nSaved to: {out_path}")


if __name__ == "__main__":
    main()
