"""Persisted thumbs-up/down feedback on chatbot responses and AI-generated
plans, discriminated by "kind" ("chatbot" or "plan"). Records written before
this discriminator existed have no "kind" key and are treated as "chatbot"."""

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


def get_results(kind="chatbot") -> list[dict]:
    """Every rated response of the given kind, newest first."""
    return [r for r in reversed(_read_all()) if r.get("kind", "chatbot") == kind]


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
