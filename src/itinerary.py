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
# Placeholder travel time from the accommodation to the first stop. A real
# implementation would ask a routing API for this per-address.
LEAVE_BUFFER = timedelta(minutes=30)
# Default lunch block for a "dine out" meal: a well-paced 1.5 hours. Lunch is
# placed so the whole block fits before the next stop, with a short lead after
# the previous stop to get there.
LUNCH_DURATION_MIN = 90
LUNCH_LEAD_MIN = 30
LUNCH_DURATION_LABEL = "about 1.5 hours"

# Don't schedule a stop that would start within this window of a venue closing.
CLOSING_BUFFER_MIN = 30
# How long each kind of stop is assumed to take (used for the open-hours check).
STOP_DURATION_MIN = {"activity": 60, "nap": 45, "meal": LUNCH_DURATION_MIN, "bonus": 60}

# Lunch has to start somewhere in this window -- late enough that a stop or two
# can come first, but not so late it stops being "lunch".
LUNCH_WINDOW_START_MIN = 10 * 60 + 30  # 10:30am
LUNCH_WINDOW_END_MIN = 13 * 60 + 30    # 1:30pm


def _hhmm_to_min(text):
    """'09:00' -> minutes past midnight."""
    return int(text[:2]) * 60 + int(text[3:5])


def stop_duration(kind):
    """Assumed length (minutes) of a stop, for the open-hours check."""
    return STOP_DURATION_MIN.get(kind, 60)


def venue_hours(venue):
    """Return (open_min, close_min) for the venue, or None if unknown.

    Single open/close pair for now. This is the one place per-day hours would
    later plug in -- e.g. take a weekday and read venue["hours"][weekday] here,
    without changing any caller.
    """
    open_t, close_t = venue.get("open"), venue.get("close")
    if not open_t or not close_t:
        return None
    return _hhmm_to_min(open_t), _hhmm_to_min(close_t)


def venue_open_for(venue, start_min, duration_min):
    """True if a stop starting at ``start_min`` fits the venue's open hours:
    at or after opening, finishing before close, and not starting within the
    closing buffer. Venues with no hours are treated as always open."""
    hours = venue_hours(venue)
    if hours is None:
        return True
    open_min, close_min = hours
    return (start_min >= open_min
            and start_min + duration_min <= close_min
            and start_min <= close_min - CLOSING_BUFFER_MIN)


# How many stops a plan has, before age adjustment, by pace.
PACE_STOPS = {"relaxed": 2, "balanced": 3, "adventurous": 4}

# Candidate themes. Each biases activity choices toward certain venue types;
# food and nap stops aren't restricted to these types, but are sorted to
# prefer a theme match when one exists (see _build_plan).
THEMES = [
    {"label": "Outdoorsy", "types": {"park", "attraction"},
     "blurb": "Parks and fresh air, with stroller-friendly strolls."},
    {"label": "Rainy-day", "types": {"museum", "mall", "cafe"},
     "blurb": "Indoor stops that stay dry and cosy."},
    {"label": "Culture", "types": {"museum", "attraction"},
     "blurb": "Museums and sights for curious little minds."},
]


def resolve_themes(labels):
    """The THEMES entries matching `labels`, in THEMES order; falls back to
    all three ("Mixed", see combine_themes) when none were selected or none
    of the submitted labels matched a real theme."""
    selected = [t for t in THEMES if t["label"] in (labels or [])]
    return selected or list(THEMES)


def combine_themes(themes):
    """Merge one or more THEMES entries into a single theme-shaped dict, so
    a plan can draw from several themes across the day instead of exactly
    one. Same shape as a THEMES entry, so every existing theme-aware helper
    below (_matches_theme, _reason, the food/nap theme-biased sorts) works
    on it unchanged."""
    label = "Mixed" if len(themes) == len(THEMES) else ", ".join(t["label"] for t in themes)
    return {
        "label": label,
        "blurb": " ".join(t["blurb"] for t in themes),
        "types": set().union(*(t["types"] for t in themes)),
    }


def _parse(t):
    """Parse an 'HH:MM' string into a datetime on a fixed reference day."""
    return datetime.strptime(t, "%H:%M")


def _format(dt):
    """Format a datetime as a friendly 12-hour time, e.g. '1:30 PM'."""
    return dt.strftime("%-I:%M %p")


def _parse_display(text):
    """Parse a '1:30 PM' display string back into a datetime."""
    return datetime.strptime(text, "%I:%M %p")


def _leave_stop(accommodation, stops):
    """Placeholder 'leave by' note before the first stop.

    Stub: leaves a fixed ``LEAVE_BUFFER`` before the first stop. A real version
    would ask a routing API for the travel time from ``accommodation`` to the
    first venue and set the departure (and wake/first-stop) timing from that.
    Returns None when there is no accommodation or no stops to head to.
    """
    if not accommodation or not stops:
        return None
    leave = _round_to(_parse_display(stops[0]["time"]) - LEAVE_BUFFER)
    return {
        "time": _format(leave),
        "kind": "leave",
        "venue": None,
        "reason": (f"Leave {accommodation} by {_format(leave)} to reach your "
                   f"first stop on time. (Placeholder — real travel time coming soon.)"),
    }


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


def _pick(pool, used, start_min=None, duration_min=0):
    """Take the next unused venue that's open for the slot (swapping past ones
    that don't fit). Returns None if nothing fits, so the slot is skipped."""
    for venue in pool:
        if venue["name"] in used:
            continue
        if start_min is not None and not venue_open_for(venue, start_min, duration_min):
            continue
        used.add(venue["name"])
        return venue
    return None


def _reason(venue, kind, theme):
    """One-line, deterministic explanation of why this stop was chosen."""
    if venue is None:
        return "No venue matched your chosen features for this slot."
    if kind == "nap":
        return f"Nap-friendly {venue['type']} — nap on the go so the day keeps flowing."
    return f"{theme['label']} pick: {venue['type']} in {venue['neighbourhood']}."


def _lunch_time(stops, naps):
    """A midday start for lunch so the whole 1.5-hour block fits before the next
    stop (with a short lead after the previous one), keeping the day well-paced."""
    occupied = []
    for stop in stops:
        dt = _parse_display(stop["time"])
        occupied.append(dt.hour * 60 + dt.minute)
    occupied += [n.hour * 60 + n.minute for n in naps]

    target = 12 * 60  # aim for a noon lunch
    max_offset = max(target - LUNCH_WINDOW_START_MIN, LUNCH_WINDOW_END_MIN - target)
    for offset in range(0, max_offset + 1, 15):
        for candidate in (target - offset, target + offset):
            if not LUNCH_WINDOW_START_MIN <= candidate <= LUNCH_WINDOW_END_MIN:
                continue
            before = max((o for o in occupied if o <= candidate), default=None)
            after = min((o for o in occupied if o > candidate), default=None)
            lead_ok = before is None or candidate - before >= LUNCH_LEAD_MIN
            block_ok = after is None or after - candidate >= LUNCH_DURATION_MIN
            if lead_ok and block_ok:
                return datetime(1900, 1, 1, candidate // 60, candidate % 60)
    return datetime(1900, 1, 1, target // 60, target % 60)


def _lunch_stop(food_pool, used, stops, naps):
    """A midday lunch to fit in -- a meal, not one of the day's stops.

    Picks a place you can eat at (restaurant, cafe, or a mall food court) that
    is open for the whole lunch block at the chosen time. Returns None if
    nothing suitable is open.
    """
    when = _lunch_time(stops, naps)
    start_min = when.hour * 60 + when.minute
    venue = _pick(food_pool, used, start_min, stop_duration("meal"))
    if venue is None:
        return None
    spot = "food court" if venue["type"] == "mall" else venue["type"]
    return {
        "time": _format(when),
        "kind": "meal",
        "venue": venue,
        "reason": (f"Lunch break at this {spot} in {venue['neighbourhood']} "
                   f"— plan {LUNCH_DURATION_LABEL}. Fit it in around your stops."),
        "duration": LUNCH_DURATION_LABEL,
    }


def _build_plan(matches, wake, bedtime, naps, count, theme, dining):
    """Build one themed plan: an ordered list of timed stops.

    ``dining`` is "dine_out" (a midday food stop) or "on_the_go" (no dedicated
    food stop — the family eats during transit or at an activity).
    """
    def _matches_theme(venue):
        return venue["type"] in theme["types"]

    activities = [v for v in matches
                  if v["category"] == "activity" and _matches_theme(v)]
    activities = activities or [v for v in matches if v["category"] == "activity"]

    # Lunch spots: anything you can eat at -- restaurants, cafes, and a mall
    # food court. Prefer venues that fit the theme (a cosy cafe on a rainy day),
    # then real food venues over a food court.
    food = sorted((v for v in matches if v.get("can_eat")),
                  key=lambda v: (0 if _matches_theme(v) else 1,
                                 0 if v["category"] == "food" else 1))

    # Nap spots are stroller/carrier rests at parks, gardens or a mall stroll --
    # never a dining venue. Theme-matching spots come first, so a rainy-day nap
    # is an indoor mall stroll rather than an outdoor park (with parks as a
    # graceful fallback so a nap is never dropped for lack of a themed spot).
    nap_candidates = [v for v in matches
                      if v.get("nap_friendly") and v["category"] != "food"]
    naps_pool = sorted(nap_candidates,
                       key=lambda v: 0 if _matches_theme(v) else 1) or activities

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
            slot["kind"] = "activity"  # dining is added separately, not a stop
    slots.sort(key=lambda s: s["time"])

    pools = {"nap": naps_pool, "activity": activities}
    used = set()
    stops = []
    for slot in slots:
        start_min = slot["time"].hour * 60 + slot["time"].minute
        venue = _pick(pools[slot["kind"]], used, start_min, stop_duration(slot["kind"]))
        if venue is None:
            continue  # nothing open fits this slot -> skip it
        stops.append({
            "time": _format(slot["time"]),
            "kind": slot["kind"],
            "venue": venue,
            "reason": _reason(venue, slot["kind"], theme),
        })

    # Dining out adds a lunch to fit in around midday -- a meal, not one of the
    # day's stops, so it never displaces an activity.
    if dining == "dine_out":
        lunch = _lunch_stop(food, used, stops, naps)
        if lunch:
            stops.append(lunch)
            stops.sort(key=lambda s: _parse_display(s["time"]))
    return stops


def generate_plans(venues, inputs):
    """Return a single candidate day plan for the parent to review.

    ``inputs`` is the normalised form dict (wake_up, bedtime, nap_times, pace,
    age_years, age_months, features, themes, ...). Returns a one-item list
    holding a ``Plan`` (label, blurb, ordered stops) that draws from whichever
    themes were selected in ``inputs["themes"]`` (or all three, "Mixed", if
    none were). Kept as a list (rather than returning the ``Plan`` directly)
    so callers that loop over "candidate plans" don't need to change.

    Placeholder logic: filter venues by the chosen features, decide how many
    stops fit the child's age and pace, then arrange a plan with a food venue
    around midday and a nap-friendly venue during the nap window. The
    structure is what a future LLM-backed implementation would return, so
    only this function's body needs to change later.
    """
    matches = filter_by_features(venues, inputs["features"])
    wake = _parse(inputs["wake_up"])
    bedtime = _parse(inputs["bedtime"])
    naps = sorted(_parse(n) for n in inputs["nap_times"])
    count = _stop_count(inputs["pace"], inputs["age_years"], inputs["age_months"])
    accommodation = inputs.get("accommodation", "")
    dining = inputs.get("dining", "dine_out")

    theme = combine_themes(resolve_themes(inputs.get("themes")))
    stops = _build_plan(matches, wake, bedtime, naps, count, theme, dining)
    leave = _leave_stop(accommodation, stops)
    if leave:
        stops = [leave] + stops
    return [Plan(label=theme["label"], blurb=theme["blurb"], stops=stops)]
