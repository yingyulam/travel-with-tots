"""Persisted thumbs-up/down feedback on chatbot responses, AI-generated
plans, and AI replans, discriminated by "kind" ("chatbot", "plan", or
"replan"). Records written before this discriminator existed have no "kind"
key and are treated as "chatbot". "plan"/"replan" store their raw
question/response as JSON; get_results() also attaches a human-readable
question_display/response_display for display."""

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

RESULTS_PATH = Path(__file__).resolve().parent.parent / "data" / "results.json"
_lock = threading.Lock()


def _read_all() -> list[dict]:
    if not RESULTS_PATH.exists():
        return []
    try:
        return json.loads(RESULTS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_result(*, question, response, rating, model,
                 response_time, input_tokens, output_tokens,
                 kind="chatbot") -> dict:
    """Append one rated chatbot response or AI-generated plan to
    data/results.json."""
    entry = {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "question": question,
        "response": response,
        "rating": rating,
        "model": model,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "response_time": response_time,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    with _lock:
        results = _read_all()
        results.append(entry)
        RESULTS_PATH.write_text(json.dumps(results, indent=2))
    return entry


def _format_stop(stop: dict) -> str:
    """One human-readable line for a plan/replan stop."""
    time = stop.get("time", "")
    venue = stop.get("venue")
    reason = stop.get("reason", "")
    if venue:
        place = venue.get("neighbourhood")
        name = venue.get("name", "Unknown venue")
        label = f"{name} ({place})" if place else name
    elif stop.get("kind") == "leave":
        label = f"Leave by {time}"
    elif stop.get("kind") == "bonus":
        label = "Free time, add a stop"
    else:
        label = "No matching venue"
    line = f"{time} - {label}"
    return f"{line}: {reason}" if reason else line


def _format_plan_dict(plan: dict) -> str:
    """Shared by "plan" and "replan" responses -- both are the same
    {label, blurb, stops} shape."""
    lines = [plan.get("label", "Plan")]
    if plan.get("blurb"):
        lines.append(plan["blurb"])
    lines.extend("- " + _format_stop(stop) for stop in plan.get("stops", []))
    return "\n".join(lines)


def _format_plan_question(ctx: dict) -> str:
    lines = [f"Destination: {ctx.get('destination', '?')}"]
    age = ", ".join(part for part in (
        f"{ctx['age_years']}y" if ctx.get("age_years") else "",
        f"{ctx['age_months']}m" if ctx.get("age_months") else "",
    ) if part)
    if age:
        lines.append(f"Child's age: {age}")
    if ctx.get("stop_count"):
        lines.append(f"Stops requested: {ctx['stop_count']}")
    if ctx.get("dining"):
        lines.append(f"Dining: {ctx['dining']}")
    if ctx.get("transit"):
        lines.append(f"Transit: {', '.join(ctx['transit'])}")
    if ctx.get("features"):
        lines.append(f"Features: {', '.join(ctx['features'])}")
    if ctx.get("bedtime"):
        lines.append(f"Bedtime: {ctx['bedtime']}")
    return "\n".join(lines)


def _format_replan_question(req: dict) -> str:
    lines = [f"Situation: {req.get('situation', '?')} at {req.get('current_time', '?')}"]
    if req.get("minutes"):
        lines.append(f"Duration: {req['minutes']} min")
    if req.get("destination"):
        lines.append(f"Destination: {req['destination']}")
    if req.get("age_months") is not None:
        lines.append(f"Child's age: {req['age_months']} months")
    current_plan = req.get("plan") or {}
    if current_plan.get("label"):
        lines.append(f"Replanning from: {current_plan['label']}")
    return "\n".join(lines)


def _humanize(kind: str, question: str, response: str) -> tuple[str, str]:
    """Best-effort human-readable versions of a record's raw question/
    response -- plain text already for "chatbot", JSON-encoded payloads for
    "plan"/"replan". Falls back to the raw text if parsing or the expected
    shape doesn't hold, so a malformed or pre-existing record never breaks
    the Results page."""
    if kind not in ("plan", "replan"):
        return question, response
    try:
        q_display = (_format_plan_question(json.loads(question)) if kind == "plan"
                      else _format_replan_question(json.loads(question)))
    except (ValueError, AttributeError):
        q_display = question
    try:
        r_display = _format_plan_dict(json.loads(response))
    except (ValueError, AttributeError):
        r_display = response
    return q_display, r_display


def get_results(kind="chatbot") -> list[dict]:
    """Every rated response of the given kind, newest first, with a
    human-readable question_display/response_display added on top of the
    raw question/response (kept as-is, still the source of truth on disk)."""
    matches = [r for r in reversed(_read_all()) if r.get("kind", "chatbot") == kind]
    for r in matches:
        r["question_display"], r["response_display"] = _humanize(kind, r["question"], r["response"])
    return matches


def get_stats(kind="chatbot") -> dict:
    results = [r for r in _read_all() if r.get("kind", "chatbot") == kind]
    up = sum(1 for r in results if r["rating"] == "up")
    down = sum(1 for r in results if r["rating"] == "down")
    total = up + down
    return {
        "up": up,
        "down": down,
        "total": total,
        "percent_positive": round(up / total * 100, 1) if total else 0,
    }
