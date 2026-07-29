"""Generate a single-day plan of stops.

``generate_plan`` is the single entry point for all plan generation and is a
deliberate placeholder. Today it returns a simple, deterministic plan, but its
inputs and outputs are shaped so a real LLM call can replace the body later
without touching the rest of the app. It does not parse ``nap_notes`` yet.
"""

from datetime import datetime, timedelta

from .data_loader import FEATURE_LABELS
from .filters import filter_by_features

# How far apart consecutive stops are spaced across the day.
SLOT_STEP = timedelta(hours=2)
# A slot within this window of a nap time is left free (no venue) for the nap.
NAP_WINDOW = timedelta(minutes=59)
# Slots whose hour falls in this range are treated as the midday meal.
MIDDAY_START, MIDDAY_END = 11, 14


def _parse(t):
    """Parse an 'HH:MM' string into a datetime on a fixed reference day."""
    return datetime.strptime(t, "%H:%M")


def _format(dt):
    """Format a datetime as a friendly 12-hour time, e.g. '1:30 PM'."""
    return dt.strftime("%-I:%M %p")


def _slot_times(wake, bedtime):
    """Candidate stop times, starting a slot after wake-up so the morning has
    room for breakfast and getting out the door, up to (not past) bedtime."""
    times, t = [], wake + SLOT_STEP
    while t < bedtime:
        times.append(t)
        t += SLOT_STEP
    return times


def _pick(pool, used):
    """Take the next unused venue from a pool, falling back to reuse."""
    for venue in pool:
        if venue["name"] not in used:
            used.add(venue["name"])
            return venue
    return pool[0] if pool else None


def _reason(venue, slot, kind, features):
    """One-line, deterministic explanation of why this stop was chosen."""
    if kind == "food":
        role = "A relaxed lunch around midday"
    elif slot.hour < MIDDAY_START:
        role = "An easy morning outing"
    else:
        role = "A calm afternoon activity"

    if venue is None:
        return f"{role}, but no venue matched your chosen features."

    matched = [FEATURE_LABELS[k] for k in features if venue.get(k)]
    if matched:
        return f"{role} — has your must-haves: {', '.join(matched)}."
    return f"{role} in {venue['neighbourhood']}."


def generate_plan(venues, wake_time, bedtime, nap_times, transit_modes,
                  nap_notes, features):
    """Return an ordered list of stops for the day.

    Placeholder logic: filter venues by the selected features, then lay stops
    out between wake-up and bedtime -- a food venue around midday, activities
    elsewhere -- while leaving nap windows free so the day stays nap-aware.
    Each stop carries a one-line ``reason``.

    ``transit_modes`` and ``nap_notes`` are accepted for parity with a future
    LLM-backed implementation; they are not yet used to alter the schedule.

    Each stop is a dict: {"time", "kind", "venue", "reason"}. ``venue`` may be
    None when nothing matched the selected features.
    """
    matches = filter_by_features(venues, features)
    food = [v for v in matches if v["category"] == "food"]
    activities = [v for v in matches if v["category"] == "activity"]

    wake = _parse(wake_time)
    bedtime = _parse(bedtime)
    naps = sorted(_parse(n) for n in nap_times)

    used = set()
    stops = []
    for slot in _slot_times(wake, bedtime):
        if any(abs(slot - nap) <= NAP_WINDOW for nap in naps):
            continue  # leave this window free for a nap; schedule nothing
        kind = "food" if MIDDAY_START <= slot.hour < MIDDAY_END else "activity"
        venue = _pick(food if kind == "food" else activities, used)
        stops.append({
            "time": _format(slot),
            "kind": kind,
            "venue": venue,
            "reason": _reason(venue, slot, kind, features),
        })
    return stops
