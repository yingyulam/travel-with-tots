"""In-trip interactions: re-planning the rest of the day and finding help now.

Both ``replan`` and ``find_nearby`` are deterministic placeholders today, but
they are kept small and self-contained so they can later become real AI /
location calls without changing their signatures or the UI that calls them.
"""

from datetime import datetime

from .form_helpers import clamp_int
from .itinerary import THEMES, stop_duration, venue_open_for

# Situations a parent taps on a chosen plan: (key, label). The "running_behind"
# key is deliberately no longer its label: the option was "Running behind", and
# renaming the key would break any stored plan label or in-flight request for
# nothing, since the behaviour is the same either way (slide the rest of the day
# later).
SITUATION_OPTIONS = [
    ("nap_happened", "Nap happened here"),
    ("running_behind", "Need to stay here longer"),
    ("skip_next", "Skip next stop"),
    ("finished_early", "Finished this stop early"),
    ("weather_rain", "It's raining"),
    ("change_theme", "Change the theme"),
]

# The note-only situation, carrying just the parent's own words (see the
# fall-through in _apply_situation). Deliberately not a button: it is submitted
# from the free-text box itself, because a chip and a textbox for the same
# request meant two controls where one would do, and the box had no submit of
# its own. It still needs a label for the AI prompt and the replan blurb.
NOTE_ONLY_SITUATION = ("something_else", "Anything else")

SITUATION_LABELS = dict(SITUATION_OPTIONS + [NOTE_ONLY_SITUATION])

# "Need something now?" buttons: (key, label). "other" reveals a text box.
NEED_OPTIONS = [
    ("restaurant", "Kid-friendly restaurant"),
    ("family_room", "Family room"),
    ("changing_table", "Changing table"),
    ("nursing_room", "Nursing room"),
    ("other", "Other"),
]

# What each "need" maps to in the venue data.
#
# No "restaurant" entry: the venue table holds attractions, so a curated match
# is impossible and find_nearby falls through to web search, which has live
# hours and current reviews. No "quiet_spot" either: nobody can reliably report
# quiet, and it changes with the hour and the weather, so a soft guess in answer
# to a specific request is worse than not offering it.
NEED_FILTERS = {
    "family_room": lambda v: v["has_family_room"],
    "changing_table": lambda v: v["has_family_room"] or v["has_nursing_room"],
    "nursing_room": lambda v: v["has_nursing_room"],
}

# Minutes past a delayed stop when the parent is "running behind".
RUNNING_BEHIND_DELAY = 45
# Short breather before moving on to the next stop after finishing one early.
FINISHED_EARLY_BUFFER = 15
# Fallback nap length when the child's usual nap length isn't known.
DEFAULT_NAP_LENGTH_MIN = 90
# Bounds on a parent-typed duration. The preset chips could only ever send a
# sane number; a free textbox can send anything, and unclamped it reaches
# arithmetic that has no guard: a negative runs the day backwards, and anything
# over a day wraps in _minutes_to_display so a stop reappears in the small hours
# and sorts ahead of the stops already done. The floor is above zero because
# every situation that reads a duration is asking "how much longer", where zero
# is not an answer.
MIN_REPLAN_MINUTES = 5
MAX_REPLAN_MINUTES = 6 * 60
# How soon after "it's raining"/"change the theme" fires the family is
# assumed to wrap up the current stop and move on -- long enough to pay up
# and get moving, short enough the day doesn't just drift through the rain
# untouched.
THEME_CHANGE_BUFFER_MIN = 30


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


def _bonus_stop(minutes, venues, features, used_names, theme_types=None, reason=None):
    """A stop to fill freed-up time: a real unused venue that's open at that
    time if one fits, else a hint to use the 'Need something now?' panel.
    If `theme_types` is given (weather/theme situations), a theme-matching
    venue is preferred before the generic activity fallback."""
    pool = list(venues or [])
    dur = stop_duration("bonus")
    open_unused = [v for v in pool if v["name"] not in used_names
                   and venue_open_for(v, minutes, dur)]
    # Prefer a theme match (if asked), then any activity, then any open match.
    pick = None
    if theme_types:
        pick = next((v for v in open_unused if v.get("type") in theme_types), None)
    pick = pick or (open_unused[0] if open_unused else None)
    if pick:
        used_names.add(pick["name"])
        return {
            "time": _minutes_to_display(minutes),
            "kind": "bonus",
            "venue": pick,
            "reason": reason or "✨ Extra stop added with the time you freed up finishing early.",
        }
    return {
        "time": _minutes_to_display(minutes),
        "kind": "bonus",
        "venue": None,
        "reason": reason or ("Freed-up time -- fit in an extra nearby stop "
                             "(try “Need something now?”)."),
    }


def _open_alternative(kind, venues, features, used, start_min, duration_min):
    """An unused, feature-matched venue of the right sort that's open now."""
    pool = list(venues or [])
    if kind == "nap":
        candidates = [v for v in pool if v.get("nap_friendly")]
    elif kind == "meal":
        candidates = [v for v in pool if v.get("can_eat")]
    else:  # activity / bonus
        candidates = list(pool)
    for venue in candidates:
        if venue["name"] not in used and venue_open_for(venue, start_min, duration_min):
            return venue
    return None


def _retheme_stop(stop, theme_types, venues, features, used):
    """Swap in an open, unused venue matching `theme_types` for a plain
    activity stop, at the same time (this doesn't free or consume time,
    unlike the timing situations). Meal and nap stops are untouched --
    theme doesn't override dining/nap needs. Best-effort: if nothing
    matches, the stop is left exactly as it was rather than dropped."""
    venue = stop.get("venue")
    if stop.get("kind") != "activity" or venue is None:
        return stop
    if venue.get("type") in theme_types:
        return stop
    start = _display_to_minutes(stop["time"])
    dur = stop_duration(stop["kind"])
    pool = list(venues or [])
    candidates = [v for v in pool if v.get("type") in theme_types
                  and v["name"] not in used and venue_open_for(v, start, dur)]
    if not candidates:
        return stop
    pick = candidates[0]
    used.discard(venue["name"])
    used.add(pick["name"])
    swapped = dict(stop)
    swapped["venue"] = pick
    swapped["reason"] = "Swapped for the new theme. " + stop.get("reason", "")
    return swapped


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
            swapped["reason"] = "Swapped in -- the earlier pick was closed by then. " + stop["reason"]
            result.append(swapped)
        # else: nothing open fits the new time -> drop the stop
    return result


def _bonus_before_bedtime(start, venues, features, used, bedtime_min, **kwargs):
    """A bonus stop at ``start``, or nothing if it would run past bedtime.

    Returns a list so callers can splice it in either way. The two branches
    that add a bonus stop used to skip the bedtime check that _shift_and_cap
    applies to every other stop, so a day could end with an invented stop after
    the child was meant to be asleep.
    """
    if not _fits_before_bedtime(start, "activity", bedtime_min):
        return []
    return [_bonus_stop(start, venues, features, used, **kwargs)]


def _apply_situation(situation, remaining, now, venues, features, used_names,
                     theme=None, bedtime_min=None):
    """Re-decide the stops still ahead. Returns a fresh list of stop dicts."""
    if situation in ("weather_rain", "change_theme"):
        # The family is assumed to wrap up the current stop and move on
        # within THEME_CHANGE_BUFFER_MIN -- if the next remaining stop is
        # further off than that (or there isn't one), pull a theme-matching
        # stop into that gap instead of leaving the day untouched until
        # whatever was already scheduled.
        theme_types = next((t["types"] for t in THEMES if t["label"] == theme), set())
        used = set(used_names)
        anchor = now + THEME_CHANGE_BUFFER_MIN
        if not remaining:
            return _bonus_before_bedtime(
                anchor, venues, features, used, bedtime_min,
                theme_types=theme_types,
                reason=f"✨ A {theme or 'new'}-friendly stop for the rest of the day.")
        # Meal and nap stops have their own real scheduling constraints (a
        # lunch spot, a nap window) -- pulling them earlier just because the
        # theme changed doesn't make sense, same reason _retheme_stop never
        # swaps their venue either. Only activity stops are eligible to be
        # pulled into the freed-up gap.
        shiftable = [s for s in remaining if s.get("kind") not in ("meal", "nap")]
        shift = (max(0, _display_to_minutes(shiftable[0]["time"]) - anchor)
                 if shiftable else 0)
        shifted = []
        for stop in remaining:
            moved = dict(stop)
            if shift > 0 and stop.get("kind") not in ("meal", "nap"):
                moved["time"] = _minutes_to_display(_display_to_minutes(stop["time"]) - shift)
                moved["reason"] = "Moved earlier for the theme change. " + moved.get("reason", "")
            shifted.append(moved)
        return [_retheme_stop(s, theme_types, venues, features, used) for s in shifted]

    if situation == "skip_next":
        # Drop the very next stop, but fill its slot with a different open
        # venue rather than leaving a gap in the day.
        if not remaining:
            return []
        dropped = remaining[0]
        rest = [dict(s) for s in remaining[1:]]
        bonus = _bonus_before_bedtime(_display_to_minutes(dropped["time"]),
                                       venues, features, used_names, bedtime_min)
        return bonus + rest

    if situation == "finished_early":
        # This stop wrapped up early. Keep going, just sooner: pull the rest of
        # the day earlier to use the freed time, and -- if the freed time opens a
        # slot -- fit a real extra stop into it.
        if not remaining:
            return _bonus_before_bedtime(now, venues, features, used_names, bedtime_min)
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
            # The old last slot is now free -- fit an extra stop into it.
            out += _bonus_before_bedtime(starts[-1], venues, features,
                                          used_names, bedtime_min)
        return out

    # "something_else" lands here, and so does anything unrecognised: keep the
    # rest of the day exactly as planned and let the AI adjuster act on whatever
    # the parent typed. "nap_happened" and "running_behind" never reach here,
    # being handled in replan() where the bedtime cap lives.
    return [dict(s) for s in remaining]


def _replan_minutes(minutes, default):
    """A parent-typed duration in minutes, or ``default`` when they gave none.

    Presets could only ever send a sane number, so nothing used to check this.
    A free textbox can send anything, so everything unusable lands on the
    default or the nearest bound here rather than reaching arithmetic that has
    no guard of its own. Note zero clamps up to the floor instead of falling
    back: someone who types 0 means "no extra time", which is far closer to the
    floor than to a 45 or 90 minute default they never asked for.
    """
    if minutes is None or minutes == "":
        return default
    return clamp_int(minutes, MIN_REPLAN_MINUTES, MAX_REPLAN_MINUTES, default)


def _fits_before_bedtime(start, kind, bedtime_min):
    """Whether a stop of ``kind`` starting at ``start`` ends before bedtime."""
    return bedtime_min is None or start + stop_duration(kind) <= bedtime_min


def _shift_and_cap(stops, shift, bedtime_min):
    """Move each stop later by ``shift`` minutes, dropping any whose block would
    then run past bedtime rather than pushing it past the end of the day."""
    out = []
    for stop in stops:
        start = _display_to_minutes(stop["time"]) + shift
        if not _fits_before_bedtime(start, stop["kind"], bedtime_min):
            continue
        moved = dict(stop)
        moved["time"] = _minutes_to_display(start)
        out.append(moved)
    return out


def _running_behind(remaining, delay, bedtime_min):
    """Everything ahead slides later by ``delay`` to absorb the delay, dropping
    any stops that would then run past bedtime."""
    out = _shift_and_cap(remaining, delay, bedtime_min)
    for stop in out:
        stop["reason"] = "Pushed later to catch up. " + stop["reason"]
    return out


def _nap_here(kept, remaining, bedtime_min, nap_length):
    """Child fell asleep at the current stop: extend that stop by ``nap_length``,
    push the remaining stops later to start after it, cancel any separately
    scheduled nap, and drop stops that would then run past bedtime.

    Returns ``(kept, remaining)`` rather than one list, so the caller can
    re-check opening hours on the stops ahead without exposing the stops that
    already happened to being swapped or dropped.
    """
    kept = [dict(s) for s in kept]
    # The scheduled nap is redundant now the nap has happened here.
    remaining = [s for s in remaining if s["kind"] != "nap"]

    if not kept:
        # Nothing has started yet, so there is no "here" to nap at. Cancelling
        # the scheduled nap is still right (they are asleep now), but there is
        # no current stop to extend and nothing to shift around it.
        return kept, [dict(s) for s in remaining]

    current = kept[-1]
    extended_end = _display_to_minutes(current["time"]) + nap_length
    current["reason"] = (f"😴 Nap happened here -- staying about {nap_length} min "
                         f"longer while they sleep. " + current.get("reason", "")).strip()

    out = []
    if remaining:
        # Shift just enough that the first remaining stop starts after the nap,
        # preserving the gaps between the rest.
        shift = max(0, extended_end - _display_to_minutes(remaining[0]["time"]))
        out = _shift_and_cap(remaining, shift, bedtime_min)
    return kept, out


def replan(plan, situation, current_time, venues=None, features=None,
           bedtime=None, minutes=None, theme=None):
    """Return a NEW proposed plan, re-deciding only the stops ahead of now.

    The stop happening now and everything before it are kept exactly as they
    were; only stops still ahead of ``current_time`` are re-decided based on
    ``situation``. The original plan is never mutated. ``current_time`` is an
    'HH:MM' 24-hour string from the form. ``venues``/``features`` let a
    situation fill freed time with a real, feature-matched venue that isn't
    already in the day. ``bedtime`` ('HH:MM') caps the day; ``minutes`` is the
    parent-entered duration -- the nap length for "nap happened here", the
    delay for "running behind" -- clamped here rather than trusted, since it
    now comes from a free textbox as well as from presets. ``theme`` is the parent-picked target theme
    for "change_theme" (ignored otherwise -- "weather_rain" always targets
    "Rainy-day").

    This is a deterministic placeholder; a real implementation would hand the
    same inputs to an AI planner and return a plan in the same shape.
    """
    now = _clock_to_minutes(current_time)
    display_now = _minutes_to_display(now)

    # "adjusted" only ever means "this round's AI adjuster touched this
    # stop" -- strip any leftover flag from an earlier round (or the
    # original plan) before deciding what's kept vs. still ahead, so a
    # fresh replan never shows a stale "Adjusted" badge on a stop it
    # never actually touched.
    stops = [{k: v for k, v in s.items() if k != "adjusted"} for s in plan.get("stops", [])]
    kept = [dict(s) for s in stops if _display_to_minutes(s["time"]) <= now]
    remaining = [s for s in stops if _display_to_minutes(s["time"]) > now]

    # Venues already in the day, so a bonus stop never repeats one.
    used_names = {s["venue"]["name"] for s in stops if s.get("venue")}

    bedtime_min = _clock_to_minutes(bedtime) if bedtime else None

    if situation == "nap_happened":
        # Extend the current stop for the nap and shift/cap the rest of the day.
        length = _replan_minutes(minutes, DEFAULT_NAP_LENGTH_MIN)
        nap_kept, nap_remaining = _nap_here(kept, remaining, bedtime_min, length)
        # Only the stops ahead get their hours re-checked. Running the kept
        # stops through _enforce_hours too would let a stop that already
        # happened be venue-swapped or dropped, which contradicts this
        # function's own promise that earlier stops are kept as-is.
        new_stops = nap_kept + _enforce_hours(nap_remaining, venues, features)
    elif situation == "running_behind":
        # Slide the rest of the day later by the delay, capped at bedtime.
        delay = _replan_minutes(minutes, RUNNING_BEHIND_DELAY)
        new_stops = kept + _enforce_hours(
            _running_behind(remaining, delay, bedtime_min), venues, features)
    else:
        # Situations that re-time stops can push a venue past closing (or before
        # opening); re-decide only the stops ahead so their venues still fit.
        effective_theme = "Rainy-day" if situation == "weather_rain" else theme
        new_remaining = _enforce_hours(
            _apply_situation(situation, remaining, now, venues, features,
                             used_names, effective_theme, bedtime_min),
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

    A need with no entry in NEED_FILTERS returns nothing rather than falling
    back to any venue at all. Two such needs exist: "restaurant", which the
    table cannot answer because it holds attractions, and "other", which is
    free text. Returning nothing is what makes the caller escalate to web
    search, and web search can actually answer both. Answering a specific
    request with an arbitrary venue that happens to be nearby is worse than
    admitting the table does not know.
    """
    predicate = NEED_FILTERS.get(need)
    if predicate is None:
        return []
    return [v for v in venues if predicate(v)][:limit]
