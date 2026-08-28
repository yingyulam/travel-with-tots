"""Generate a few candidate day plans for the parent to pick from.

``generate_plans`` is the single entry point for all plan generation and is a
deliberate placeholder. Today it returns short, themed, deterministic plans,
but its inputs and outputs are shaped so a real LLM call can replace the body
later without touching the rest of the app. It does not read the free-text
notes yet.
"""

from datetime import datetime, timedelta

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

# Placeholder inter-stop travel buffer, in minutes, by transit mode -- how long
# to allow for getting from one stop to the next when no real distance is
# known. A real implementation would ask a routing API for the actual travel
# time between each specific pair of venues instead of this flat per-mode
# guess, same role as LEAVE_BUFFER above.
TRANSIT_BUFFER_MIN = {"car": 15, "bus": 25, "stroller": 20, "carrier": 20, "other": 20}

# Fallback lunch target when the parent didn't set a preferred lunch time.
DEFAULT_LUNCH_TARGET_MIN = 12 * 60  # noon
# How far from the target lunch time to search for an open, unused venue that
# fits the full meal block, in either direction.
LUNCH_SEARCH_RADIUS_MIN = 180


def transit_buffer_min(transit_modes):
    """Conservative minutes to allow when moving between stops, given the
    selected transit mode(s) -- the slowest selected mode sets the buffer.
    Swap this out for a real routing-API lookup later; callers only need a
    single number back, so nothing else changes."""
    return max((TRANSIT_BUFFER_MIN.get(m, TRANSIT_BUFFER_MIN["other"])
                for m in (transit_modes or [])), default=TRANSIT_BUFFER_MIN["other"])


def hhmm_to_min(text):
    """'09:00' -> minutes past midnight."""
    return int(text[:2]) * 60 + int(text[3:5])


def stop_duration(kind):
    """Assumed length (minutes) of a stop, for the open-hours check."""
    return STOP_DURATION_MIN.get(kind, 60)


def venue_hours(venue):
    """Return (open_min, close_min) for the venue, or None if unknown.

    Already resolved for the day being planned: data_loader.get_venues picks the
    right pair for the trip date (season, weekday/weekend, holiday) and puts it
    in "open"/"close", so nothing here needs to know the date.

    Both spellings are accepted because two shapes reach this. Venue dicts from
    data_loader use open/close; candidate rows from db.get_candidate_venues,
    which the AI adjuster swaps in, carry the column names. Reading only one
    meant the adjuster's own "isn't open at" check silently passed every
    swapped-in venue, since its hours looked unknown.
    """
    open_t = venue.get("open") or venue.get("open_time")
    close_t = venue.get("close") or venue.get("close_time")
    if not open_t or not close_t:
        return None
    return hhmm_to_min(open_t), hhmm_to_min(close_t)


def venue_open_for(venue, start_min, duration_min):
    """True if a stop starting at ``start_min`` fits the venue's open hours:
    at or after opening, finishing before close, and not starting within the
    closing buffer.

    A venue whose hours are unknown is **not** schedulable. It used to be
    treated as open all day, which is how an unverified venue reached a real
    family's afternoon: nobody had said it was open, and the app said it for
    them. Not knowing is a reason to leave a place out, not to include it.
    """
    hours = venue_hours(venue)
    if hours is None:
        return False
    open_min, close_min = hours
    return (start_min >= open_min
            and start_min + duration_min <= close_min
            and start_min <= close_min - CLOSING_BUFFER_MIN)


# Realistic range for how many stops a single day can hold.
MIN_STOP_COUNT = 2
MAX_STOP_COUNT = 4

# Dedicated meal (lunch) stops a "dine_out" plan gets, always in addition to
# the stop-count ceiling, never counted against it (mirrors _build_plan, which
# appends the lunch stop after the requested-count stops already exist).
MAX_MEAL_STOPS = 1

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


def display_to_min(text):
    """Parse a '1:30 PM' display string into minutes past midnight."""
    dt = _parse_display(text)
    return dt.hour * 60 + dt.minute


def min_to_display(minutes):
    """Format minutes past midnight (mod 24h) as a '1:30 PM' string."""
    minutes %= 24 * 60
    return _format(datetime(1900, 1, 1, minutes // 60, minutes % 60))


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
                   f"first stop on time. (Placeholder -- real travel time coming soon.)"),
    }


def _round_to(dt, minutes=15):
    """Round a datetime to the nearest quarter hour for tidy times."""
    total = int(round((dt.hour * 60 + dt.minute) / minutes) * minutes) % (24 * 60)
    return dt.replace(hour=total // 60, minute=total % 60, second=0, microsecond=0)


def realistic_stop_count(requested, age_months):
    """Clamp a parent's requested stop count to what's realistic: at most
    MAX_STOP_COUNT, one lower for a child under 24 months, never below
    MIN_STOP_COUNT. Shared with agents.py so the AI planner enforces the
    same ceiling as this rule-based one."""
    ceiling = MAX_STOP_COUNT - 1 if age_months < 24 else MAX_STOP_COUNT
    return max(MIN_STOP_COUNT, min(ceiling, int(requested)))


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
        # Says why the slot is here (the parent told us to expect a nap then)
        # without claiming how the child sleeps. The old wording asserted "nap
        # on the go" to every parent, including one who had said their child
        # needs a proper place, and called whatever it picked "Nap-friendly"
        # even when the fallback had handed it a swimming pool.
        fit = ("somewhere a rest fits easily" if venue.get("nap_friendly")
               else "the best fit open at this hour")
        return f"Timed around the nap you expect -- a {venue['type']}, {fit}."
    return f"{theme['label']} pick: {venue['type']} in {venue['neighbourhood']}."


def _lunch_time(stops, naps, preferred_lunch_min=None):
    """A lunch start time close to ``preferred_lunch_min`` (or a noon default
    when not given) so the whole 1.5-hour block fits before the next stop
    (with a short lead after the previous one), keeping the day well-paced."""
    occupied = []
    for stop in stops:
        dt = _parse_display(stop["time"])
        occupied.append(dt.hour * 60 + dt.minute)
    occupied += [n.hour * 60 + n.minute for n in naps]

    target = preferred_lunch_min if preferred_lunch_min is not None else DEFAULT_LUNCH_TARGET_MIN
    for offset in range(0, LUNCH_SEARCH_RADIUS_MIN + 1, 15):
        for candidate in (target - offset, target + offset):
            if not 0 <= candidate < 24 * 60:
                continue
            before = max((o for o in occupied if o <= candidate), default=None)
            after = min((o for o in occupied if o > candidate), default=None)
            lead_ok = before is None or candidate - before >= LUNCH_LEAD_MIN
            block_ok = after is None or after - candidate >= LUNCH_DURATION_MIN
            if lead_ok and block_ok:
                return datetime(1900, 1, 1, candidate // 60, candidate % 60)
    return datetime(1900, 1, 1, target // 60, target % 60)


def _stop_before(stops, when):
    """The last stop starting before `when`, so a lunch block can say where to
    look. None when lunch lands before anything else."""
    earlier = [s for s in stops if _parse_display(s["time"]) <= when and s.get("venue")]
    return earlier[-1]["venue"] if earlier else None


def _lunch_stop(stops, naps, preferred_lunch_min=None):
    """A lunch to fit in -- a meal, not one of the day's stops.

    Lunch happens at a stop the day already includes, when one of them serves
    food and is open for the whole block. Eating where you already are removes
    a travel leg, which is worth more to a tired parent than a nicer lunch
    across town.

    Otherwise the block names no venue. The planner used to insert the nearest
    venue that served food, which sent a parent standing at Stanley Park to a
    mall seven kilometres south; the table holds attractions, not restaurants,
    so it has no good answer and says so. Finding somewhere is a live search
    from where they actually are, which has current hours and reviews.
    """
    when = _lunch_time(stops, naps, preferred_lunch_min)
    start_min = when.hour * 60 + when.minute

    # Only the stop the parent is actually at when lunch lands counts. Any
    # can_eat stop in the day is not the same thing: it put "you are already
    # there" against a mall the family does not reach until four in the
    # afternoon, and named it twice in one plan.
    here = _stop_before(stops, when)
    venue = here if (here and here.get("can_eat")
                     and venue_open_for(here, start_min, stop_duration("meal"))) else None

    if venue is not None:
        spot = "food court" if venue["type"] == "mall" else venue["type"]
        reason = (f"Eat at the {spot} at {venue['name']} -- you are already "
                  f"there, so no extra travel. Plan {LUNCH_DURATION_LABEL}.")
    else:
        near = _stop_before(stops, when)
        where = f" near {near['name']}" if near else " nearby"
        reason = (f"Find lunch{where} -- plan {LUNCH_DURATION_LABEL}. "
                  "Use Find nearby for somewhere open now.")
    return {
        "time": _format(when),
        "kind": "meal",
        "venue": venue,
        "reason": reason,
        "duration": LUNCH_DURATION_LABEL,
    }


# A nap stop is never checked against more than this, however long a caller
# says the nap is. Not a policy about naps -- form_helpers already clamps the
# form to 15-180 minutes -- but a guard, because generate_plans takes a plain
# dict: an implausible length would fail every venue's hours check and the nap
# stop would disappear from the day rather than fail loudly.
MAX_NAP_STOP_MIN = 180


def _nap_minutes(naps):
    """{nap start time: how long the parent said it runs}, for the ones that
    gave a usable length. A missing or unusable value is left out, so the slot
    falls back to STOP_DURATION_MIN rather than to a guess."""
    found = {}
    for nap in naps:
        if not nap.get("start"):
            continue
        try:
            minutes = int(nap.get("duration_min"))
        except (TypeError, ValueError):
            continue
        if minutes > 0:
            found[_parse(nap["start"])] = min(minutes, MAX_NAP_STOP_MIN)
    return found


def _build_plan(matches, wake, bedtime, naps, count, theme, dining,
                preferred_lunch_min=None, nap_minutes=None):
    """Build one themed plan: an ordered list of timed stops.

    ``dining`` is "dine_out" (a midday food stop) or "on_the_go" (no dedicated
    food stop -- the family eats during transit or at an activity).
    """
    def _matches_theme(venue):
        return venue["type"] in theme["types"]

    # Theme-matching venues come first, rather than being the only ones. A
    # filter here discarded every venue whose type no theme names, and ten of
    # the fourteen allowed types name no theme: aquarium, community centre,
    # farm, market, pool, library, playground, garden, beach, seawall. The
    # `or list(matches)` fallback below only rescued a day where *nothing*
    # matched, so one museum in the pool was enough to throw the other five
    # venues away -- six open venues and a request for three stops returned a
    # one-stop day.
    #
    # Sorting is what makes the type list safe to extend: a type nobody
    # remembered to map is deprioritised, never invisible. Same idiom the nap
    # pool below already uses, and Python's sort is stable, so the curator's
    # seed_rank order survives inside each group.
    activities = sorted(matches, key=lambda v: 0 if _matches_theme(v) else 1)

    # No lunch pool: lunch is taken at a stop the day already includes, or it
    # is a block with a handoff. See _lunch_stop.

    # The nap window prefers somewhere restful, it does not require it. A nap
    # is a soft constraint: a child's actual nap does not follow the plan, and
    # what happens on the day is handled by Replan on the Go ("nap happened
    # here"), not by finding a perfect venue in advance.
    #
    # This was a filter, with a fallback that only rescued a day where nothing
    # at all was nap-friendly. So a single nap-friendly venue in the pool was
    # enough to exclude every other option, and if that one venue happened to
    # be shut at nap time the whole stop silently vanished from the day.
    #
    # Nap-friendliness first, theme match second, so a rainy-day nap is still
    # an indoor mall stroll rather than an outdoor park. Stable sort, so the
    # curator's seed_rank order survives inside each group.
    naps_pool = sorted(matches, key=lambda v: (0 if v.get("nap_friendly") else 1,
                                               0 if _matches_theme(v) else 1))

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
        nearest["minutes"] = (nap_minutes or {}).get(nap)
    for slot in slots:
        if slot["kind"] is None:
            slot["kind"] = "activity"  # dining is added separately, not a stop
    slots.sort(key=lambda s: s["time"])

    pools = {"nap": naps_pool, "activity": activities}
    used = set()
    stops = []
    for slot in slots:
        start_min = slot["time"].hour * 60 + slot["time"].minute
        # A nap uses the length the parent actually gave, so a venue that
        # shuts partway through it is not offered. The flat STOP_DURATION_MIN
        # meant a thirty-minute nap and a three-hour one were checked
        # identically, and a museum closing at two could host either.
        venue = _pick(pools[slot["kind"]], used, start_min,
                      slot.get("minutes") or stop_duration(slot["kind"]))
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
        stops.append(_lunch_stop(stops, naps, preferred_lunch_min))
        stops.sort(key=lambda s: _parse_display(s["time"]))
    return stops


def generate_plans(venues, inputs):
    """Return a single candidate day plan for the parent to review.

    ``inputs`` is the normalised form dict (wake_up, bedtime, naps, stop_count,
    age_years, age_months, features, themes, ...). Returns a one-item list
    holding a ``Plan`` (label, blurb, ordered stops) that draws from whichever
    themes were selected in ``inputs["themes"]`` (or all three, "Mixed", if
    none were). Kept as a list (rather than returning the ``Plan`` directly)
    so callers that loop over "candidate plans" don't need to change.

    Placeholder logic: filter venues by the chosen features, decide how many
    stops fit the child's age and the parent's requested count, then arrange
    a plan with a food venue around midday and a nap-friendly venue during
    the nap window. The structure is what a future LLM-backed implementation
    would return, so only this function's body needs to change later.
    """
    # No amenity filtering: what a parent needs in the moment (a nursing room,
    # a change table) is answered by find_nearby where they are, not by
    # narrowing a whole day to venues someone happened to have reported on.
    matches = venues
    wake = _parse(inputs["wake_up"])
    bedtime = _parse(inputs["bedtime"])
    naps = sorted(_parse(n["start"]) for n in inputs.get("naps", []) if n.get("start"))
    nap_minutes = _nap_minutes(inputs.get("naps", []))
    total_months = int(inputs["age_years"]) * 12 + int(inputs["age_months"])
    requested_count = int(inputs["stop_count"])
    count = realistic_stop_count(requested_count, total_months)
    accommodation = inputs.get("accommodation", "")
    dining = inputs.get("dining", "dine_out")
    preferred_lunch_time = inputs.get("preferred_lunch_time") or ""
    preferred_lunch_min = hhmm_to_min(preferred_lunch_time) if preferred_lunch_time else None

    theme = combine_themes(resolve_themes(inputs.get("themes")))
    stops = _build_plan(matches, wake, bedtime, naps, count, theme, dining,
                        preferred_lunch_min, nap_minutes)
    leave = _leave_stop(accommodation, stops)
    if leave:
        stops = [leave] + stops
    blurb = theme["blurb"]
    if count != requested_count:
        blurb += (f" You asked for {requested_count} stops; we planned {count} "
                  "instead, a more realistic pace for this age.")
    return [Plan(label=theme["label"], blurb=blurb, stops=stops)]
