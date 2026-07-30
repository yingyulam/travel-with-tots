"""Generate a few candidate day plans for the parent to pick from.

``generate_plans`` is the single entry point for all plan generation and is a
deliberate placeholder. Today it returns short, themed, deterministic plans,
but its inputs and outputs are shaped so a real LLM call can replace the body
later without touching the rest of the app. It does not read the free-text
notes yet.
"""

from datetime import datetime, timedelta

from .filters import filter_by_features
from .models import Plan

# A buffer after wake-up before the first stop (breakfast, getting out).
MORNING_BUFFER = timedelta(hours=2)
# Stops whose hour falls in this range are treated as the midday meal.
MIDDAY_START, MIDDAY_END = 11, 14

# How many stops a plan has, before age adjustment, by pace.
PACE_STOPS = {"relaxed": 2, "balanced": 3, "adventurous": 4}

# Candidate themes. Each biases activity choices toward certain venue types.
# Food and nap stops are chosen theme-independently.
THEMES = [
    {"label": "Outdoorsy", "types": {"park", "attraction"},
     "blurb": "Parks and fresh air, with stroller-friendly strolls."},
    {"label": "Rainy-day", "types": {"museum", "mall", "cafe"},
     "blurb": "Indoor stops that stay dry and cosy."},
    {"label": "Culture", "types": {"museum", "attraction"},
     "blurb": "Museums and sights for curious little minds."},
]


def _parse(t):
    """Parse an 'HH:MM' string into a datetime on a fixed reference day."""
    return datetime.strptime(t, "%H:%M")


def _format(dt):
    """Format a datetime as a friendly 12-hour time, e.g. '1:30 PM'."""
    return dt.strftime("%-I:%M %p")


def _round_to(dt, minutes=15):
    """Round a datetime to the nearest quarter hour for tidy times."""
    total = int(round((dt.hour * 60 + dt.minute) / minutes) * minutes) % (24 * 60)
    return dt.replace(hour=total // 60, minute=total % 60, second=0, microsecond=0)


def _stop_count(pace, age_years, age_months):
    """Choose 2-4 stops: fewer for younger/relaxed, more for older/adventurous."""
    count = PACE_STOPS.get(pace, 3)
    total_months = int(age_years) * 12 + int(age_months)
    if total_months < 24:
        count -= 1
    elif total_months >= 48:
        count += 1
    return max(2, min(4, count))


def _plan_times(wake, bedtime, count):
    """Space ``count`` stop times across the day, with the first stop about
    ``MORNING_BUFFER`` after wake-up (breakfast, getting out the door)."""
    start = wake + MORNING_BUFFER
    if start >= bedtime:
        start = wake
    span = bedtime - start
    # First stop lands at ``start``; the rest spread out toward bedtime,
    # leaving a gap before bedtime rather than ending exactly on it.
    return [_round_to(start + span * (i / count)) for i in range(count)]


def _pick(pool, used):
    """Take the next unused venue from a pool, falling back to reuse."""
    for venue in pool:
        if venue["name"] not in used:
            used.add(venue["name"])
            return venue
    return pool[0] if pool else None


def _reason(venue, kind, theme):
    """One-line, deterministic explanation of why this stop was chosen."""
    if venue is None:
        return "No venue matched your chosen features for this slot."
    if kind == "nap":
        return f"Nap-friendly {venue['type']} — nap on the go so the day keeps flowing."
    if kind == "food":
        return f"A relaxed bite around midday in {venue['neighbourhood']}."
    return f"{theme['label']} pick: {venue['type']} in {venue['neighbourhood']}."


def _build_plan(matches, wake, bedtime, naps, count, theme):
    """Build one themed plan: an ordered list of timed stops."""
    activities = [v for v in matches
                  if v["category"] == "activity" and v["type"] in theme["types"]]
    activities = activities or [v for v in matches if v["category"] == "activity"]
    food = [v for v in matches if v["category"] == "food"]
    naps_pool = [v for v in matches if v.get("nap_friendly")] or activities

    # Lay out the stop times, then anchor naps: each nap retimes its nearest
    # still-unassigned stop so a nap-friendly venue lands in the nap window.
    # Leave at least one non-nap stop so the day still has an outing.
    slots = [{"time": t, "kind": None} for t in _plan_times(wake, bedtime, count)]
    for nap in [n for n in naps if wake <= n <= bedtime][:count - 1]:
        free = [s for s in slots if s["kind"] is None]
        if not free:
            break
        nearest = min(free, key=lambda s: abs(s["time"] - nap))
        nearest["time"], nearest["kind"] = nap, "nap"
    for slot in slots:
        if slot["kind"] is None:
            midday = MIDDAY_START <= slot["time"].hour < MIDDAY_END
            slot["kind"] = "food" if midday else "activity"
    slots.sort(key=lambda s: s["time"])

    pools = {"nap": naps_pool, "food": food, "activity": activities}
    used = set()
    stops = []
    for slot in slots:
        venue = _pick(pools[slot["kind"]], used)
        stops.append({
            "time": _format(slot["time"]),
            "kind": slot["kind"],
            "venue": venue,
            "reason": _reason(venue, slot["kind"], theme),
        })
    return stops


def generate_plans(venues, inputs):
    """Return a short list of candidate day plans for the parent to pick from.

    ``inputs`` is the normalised form dict (wake_up, bedtime, nap_times, pace,
    age_years, age_months, features, ...). Returns a list of ``Plan`` objects,
    one per theme, each with a ``label``, a ``blurb``, and ordered ``stops``.

    Placeholder logic: filter venues by the chosen features, decide how many
    stops fit the child's age and pace, then arrange one plan per theme with a
    food venue around midday and a nap-friendly venue during the nap window.
    The structure is what a future LLM-backed implementation would return, so
    only this function's body needs to change later.
    """
    matches = filter_by_features(venues, inputs["features"])
    wake = _parse(inputs["wake_up"])
    bedtime = _parse(inputs["bedtime"])
    naps = sorted(_parse(n) for n in inputs["nap_times"])
    count = _stop_count(inputs["pace"], inputs["age_years"], inputs["age_months"])

    return [
        Plan(
            label=theme["label"],
            blurb=theme["blurb"],
            stops=_build_plan(matches, wake, bedtime, naps, count, theme),
        )
        for theme in THEMES
    ]
