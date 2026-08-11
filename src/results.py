"""Persisted thumbs-up/down feedback on chatbot responses."""

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
                 response_time, input_tokens, output_tokens) -> dict:
    """Append one rated chatbot response to data/results.json."""
    entry = {
        "id": uuid.uuid4().hex,
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


def get_results() -> list[dict]:
    """Every rated response, newest first."""
    return list(reversed(_read_all()))


def get_stats() -> dict:
    results = _read_all()
    up = sum(1 for r in results if r["rating"] == "up")
    down = sum(1 for r in results if r["rating"] == "down")
    total = up + down
    return {
        "up": up,
        "down": down,
        "total": total,
        "percent_positive": round(up / total * 100, 1) if total else 0,
    }
