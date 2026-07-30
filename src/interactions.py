"""In-trip interactions: re-planning the rest of the day and finding help now.

Both ``replan`` and ``find_nearby`` are deterministic placeholders today, but
they are kept small and self-contained so they can later become real AI /
location calls without changing their signatures or the UI that calls them.
"""

from datetime import datetime

# Situation buttons shown on a chosen plan: (key, label).
SITUATION_OPTIONS = [
    ("nap_happened", "Nap happened here"),
    ("running_behind", "Running behind"),
    ("skip_next", "Skip next stop"),
    ("finished_early", "Finished this stop early"),
]
SITUATION_LABELS = dict(SITUATION_OPTIONS)

# "Need something now?" buttons: (key, label). "other" reveals a text box.
NEED_OPTIONS = [
    ("restaurant", "Kid-friendly restaurant"),
    ("family_room", "Family room"),
    ("changing_table", "Changing table"),
    ("nursing_room", "Nursing room"),
    ("quiet_spot", "Quiet spot"),
    ("other", "Other"),
]

# What each "need" maps to in the venue data.
NEED_FILTERS = {
    "restaurant": lambda v: v["category"] == "food" and v["kid_friendly"],
    "family_room": lambda v: v["has_family_room"],
    "changing_table": lambda v: v["has_family_room"] or v["has_nursing_room"],
    "nursing_room": lambda v: v["has_nursing_room"],
    "quiet_spot": lambda v: v.get("nap_friendly"),
}

# Minutes past a delayed stop when the parent is "running behind".
RUNNING_BEHIND_DELAY = 45
# Short breather before moving on to the next stop after finishing one early.
FINISHED_EARLY_BUFFER = 15


def _display_to_minutes(text):
    """'1:15 PM' -> minutes past midnight."""
    dt = datetime.strptime(text, "%I:%M %p")
    return dt.hour * 60 + dt.minute


def _clock_to_minutes(text):
    """'13:15' (24h form field) -> minutes past midnight."""
    dt = datetime.strptime(text, "%H:%M")
    return dt.hour * 60 + dt.minute


def _minutes_to_display(minutes):
    """Minutes past midnight -> '1:15 PM'."""
    minutes %= 24 * 60
    return datetime(2000, 1, 1, minutes // 60, minutes % 60).strftime("%-I:%M %p")


def _apply_situation(situation, remaining, now):
    """Re-decide the stops still ahead. Returns a fresh list of stop dicts."""
    if situation == "skip_next":
        # Drop the very next stop; leave the rest of the day as planned.
        return [dict(s) for s in remaining[1:]]

    if situation == "running_behind":
        # Everything ahead slides later to absorb the delay.
        out = []
        for stop in remaining:
            shifted = dict(stop)
            shifted["time"] = _minutes_to_display(
                _display_to_minutes(stop["time"]) + RUNNING_BEHIND_DELAY)
            shifted["reason"] = "Pushed later to catch up. " + stop["reason"]
            out.append(shifted)
        return out

    if situation == "finished_early":
        # This stop wrapped up early. Keep going, just sooner: pull the rest of
        # the day earlier to use the freed time, and offer a bonus stop with
        # whatever time that opens up at the end.
        if not remaining:
            return [{
                "time": _minutes_to_display(now),
                "kind": "bonus",
                "venue": None,
                "reason": "Finished early with time to spare — add a nearby stop "
                          "from “Need something now?”.",
            }]
        starts = [_display_to_minutes(s["time"]) for s in remaining]
        shift = max(0, starts[0] - (now + FINISHED_EARLY_BUFFER))
        out = []
        for stop in remaining:
            moved = dict(stop)
            moved["time"] = _minutes_to_display(_display_to_minutes(stop["time"]) - shift)
            out.append(moved)
        out[0] = dict(out[0])
        out[0]["reason"] = "Moved up after finishing early. " + out[0]["reason"]
        if shift > 0:
            # The old last slot is now free — suggest fitting an extra stop in.
            out.append({
                "time": _minutes_to_display(starts[-1]),
                "kind": "bonus",
                "venue": None,
                "reason": "Freed-up time — fit in an extra nearby stop "
                          "(try “Need something now?”).",
            })
        return out

    if situation == "nap_happened":
        # The nap just happened, so drop the next planned nap and carry on.
        out, dropped = [], False
        for stop in remaining:
            if not dropped and stop["kind"] == "nap":
                dropped = True
                continue
            out.append(dict(stop))
        if out:
            out[0] = dict(out[0])
            out[0]["reason"] = "After the nap: " + out[0]["reason"]
        return out

    return [dict(s) for s in remaining]


def replan(plan, situation, current_time):
    """Return a NEW proposed plan, re-deciding only the stops ahead of now.

    The stop happening now and everything before it are kept exactly as they
    were; only stops still ahead of ``current_time`` are re-decided based on
    ``situation``. The original plan is never mutated. ``current_time`` is an
    'HH:MM' 24-hour string from the form.

    This is a deterministic placeholder; a real implementation would hand the
    same inputs to an AI planner and return a plan in the same shape.
    """
    now = _clock_to_minutes(current_time)
    display_now = _minutes_to_display(now)

    # Keep everything up to and including the stop happening now; only stops
    # still ahead of the current time get re-decided.
    stops = plan.get("stops", [])
    kept = [dict(s) for s in stops if _display_to_minutes(s["time"]) <= now]
    remaining = [s for s in stops if _display_to_minutes(s["time"]) > now]

    new_stops = kept + _apply_situation(situation, remaining, now)
    new_stops.sort(key=lambda s: _display_to_minutes(s["time"]))

    label = plan.get("label", "Plan")
    return {
        "label": f"{label} · from {display_now}",
        "blurb": (f"Re-planned after “{SITUATION_LABELS.get(situation, situation)}” "
                  f"at {display_now}. Earlier stops kept as-is."),
        "from_time": display_now,
        "stops": new_stops,
    }


def find_nearby(need, venues, limit=2):
    """Return 1-2 venues matching a ``need`` key (e.g. 'nursing_room').

    Deterministic placeholder: filters the sample venue data. A real version
    would query a location service around the parent's current position.
    """
    predicate = NEED_FILTERS.get(need, lambda v: v["kid_friendly"])
    return [v for v in venues if predicate(v)][:limit]
