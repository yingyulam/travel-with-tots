"""In-trip interactions: re-planning the rest of the day and finding help now.

Both ``replan`` and ``find_nearby`` are deterministic placeholders today, but
they are kept small and self-contained so they can later become real AI /
location calls without changing their signatures or the UI that calls them.
"""

from datetime import datetime

from .filters import filter_by_features
from .itinerary import stop_duration, venue_open_for

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
# Fallback nap length when the child's usual nap length isn't known.
DEFAULT_NAP_LENGTH_MIN = 90


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


def _bonus_stop(minutes, venues, features, used_names):
    """A stop to fill freed-up time: a real unused venue that's open at that
    time if one fits, else a hint to use the 'Need something now?' panel."""
    pool = filter_by_features(venues or [], features or [])
    dur = stop_duration("bonus")
    open_unused = [v for v in pool if v["name"] not in used_names
                   and venue_open_for(v, minutes, dur)]
    # Prefer an activity for the extra outing, then fall back to any open match.
    pick = next((v for v in open_unused if v["category"] == "activity"), None)
    pick = pick or (open_unused[0] if open_unused else None)
    if pick:
        used_names.add(pick["name"])
        return {
            "time": _minutes_to_display(minutes),
            "kind": "bonus",
            "venue": pick,
            "reason": "✨ Extra stop added with the time you freed up finishing early.",
        }
    return {
        "time": _minutes_to_display(minutes),
        "kind": "bonus",
        "venue": None,
        "reason": "Freed-up time — fit in an extra nearby stop "
                  "(try “Need something now?”).",
    }


def _open_alternative(kind, venues, features, used, start_min, duration_min):
    """An unused, feature-matched venue of the right sort that's open now."""
    pool = filter_by_features(venues or [], features or [])
    if kind == "nap":
        candidates = [v for v in pool
                      if v.get("nap_friendly") and v["category"] != "food"]
    elif kind == "meal":
        candidates = sorted((v for v in pool if v.get("can_eat")),
                            key=lambda v: 0 if v["category"] == "food" else 1)
    else:  # activity / bonus
        candidates = [v for v in pool if v["category"] == "activity"]
    for venue in candidates:
        if venue["name"] not in used and venue_open_for(venue, start_min, duration_min):
            return venue
    return None


def _enforce_hours(stops, venues, features):
    """After any re-timing, keep only stops whose venue is open for the slot --
    swapping in an open alternative where possible, otherwise dropping the stop.
    Stops without a venue (leave/bonus notes) are left untouched."""
    used = {s["venue"]["name"] for s in stops if s.get("venue")}
    result = []
    for stop in stops:
        venue = stop.get("venue")
        if venue is None:
            result.append(stop)
            continue
        start = _display_to_minutes(stop["time"])
        dur = stop_duration(stop["kind"])
        if venue_open_for(venue, start, dur):
            result.append(stop)
            continue
        used.discard(venue["name"])
        alt = _open_alternative(stop["kind"], venues, features, used, start, dur)
        if alt:
            used.add(alt["name"])
            swapped = dict(stop)
            swapped["venue"] = alt
            swapped["reason"] = "Swapped in — the earlier pick was closed by then. " + stop["reason"]
            result.append(swapped)
        # else: nothing open fits the new time -> drop the stop
    return result


def _apply_situation(situation, remaining, now, venues, features, used_names):
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
        # the day earlier to use the freed time, and — if the freed time opens a
        # slot — fit a real extra stop into it.
        if not remaining:
            return [_bonus_stop(now, venues, features, used_names)]
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
            # The old last slot is now free — fit an extra stop into it.
            out.append(_bonus_stop(starts[-1], venues, features, used_names))
        return out

    # "nap_happened" is handled specially in replan() (it also extends the
    # current stop and needs the bedtime), so it never reaches here.
    return [dict(s) for s in remaining]


def _nap_here(kept, remaining, bedtime_min, nap_length):
    """Child fell asleep at the current stop: extend that stop by ``nap_length``,
    push the remaining stops later to start after it, cancel any separately
    scheduled nap, and drop stops that would then run past bedtime."""
    kept = [dict(s) for s in kept]
    # The scheduled nap is redundant now the nap has happened here.
    remaining = [s for s in remaining if s["kind"] != "nap"]

    if not kept:
        return kept + [dict(s) for s in remaining]

    current = kept[-1]
    extended_end = _display_to_minutes(current["time"]) + nap_length
    current["reason"] = (f"😴 Nap happened here — staying about {nap_length} min "
                         f"longer while they sleep. " + current.get("reason", "")).strip()

    out = []
    if remaining:
        # Shift just enough that the first remaining stop starts after the nap,
        # preserving the gaps between the rest.
        shift = max(0, extended_end - _display_to_minutes(remaining[0]["time"]))
        for stop in remaining:
            start = _display_to_minutes(stop["time"]) + shift
            # Drop anything whose block would run past bedtime.
            if bedtime_min is not None and start + stop_duration(stop["kind"]) > bedtime_min:
                continue
            moved = dict(stop)
            moved["time"] = _minutes_to_display(start)
            out.append(moved)
    return kept + out


def replan(plan, situation, current_time, venues=None, features=None,
           bedtime=None, nap_length=None):
    """Return a NEW proposed plan, re-deciding only the stops ahead of now.

    The stop happening now and everything before it are kept exactly as they
    were; only stops still ahead of ``current_time`` are re-decided based on
    ``situation``. The original plan is never mutated. ``current_time`` is an
    'HH:MM' 24-hour string from the form. ``venues``/``features`` let a
    situation fill freed time with a real, feature-matched venue that isn't
    already in the day. ``bedtime`` ('HH:MM') and ``nap_length`` (minutes) are
    used by "nap happened here" to extend the current stop and cap the day.

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

    # Venues already in the day, so a bonus stop never repeats one.
    used_names = {s["venue"]["name"] for s in stops if s.get("venue")}

    if situation == "nap_happened":
        # Extend the current stop for the nap and shift/cap the rest of the day.
        bedtime_min = _clock_to_minutes(bedtime) if bedtime else None
        length = int(nap_length) if nap_length else DEFAULT_NAP_LENGTH_MIN
        new_stops = _enforce_hours(
            _nap_here(kept, remaining, bedtime_min, length), venues, features)
    else:
        # Situations that re-time stops can push a venue past closing (or before
        # opening); re-decide only the stops ahead so their venues still fit.
        new_remaining = _enforce_hours(
            _apply_situation(situation, remaining, now, venues, features, used_names),
            venues, features)
        new_stops = kept + new_remaining
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
