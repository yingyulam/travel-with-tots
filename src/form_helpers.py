"""Trip-planning form parsing and validation -- pure functions operating on
the raw form dict app.py's routes already parse, no Flask dependency."""

import json
from datetime import date

from .dates import MAX_TRIP_DAYS, compute_age, date_range, days_between, parse_date
from .geo import (DEFAULT_WALK_BUDGET_MIN, WALK_BUDGET_OPTIONS, as_point,
                  walk_budget_min)
from .data_loader import SUPPORTED_CITIES, VENUE_TYPES, interest_options
from .db import get_children

# Age is capped at this many years, 0 months.
MAX_AGE_YEARS = 5
MAX_MONTHS = 11
MAX_NAPS = 4

# Assumed nap length when a parent gives a nap time but not how long it runs,
# which is the common case both in the form and in a described day. Held here
# rather than reusing itinerary.STOP_DURATION_MIN["nap"]: that one is how long
# a nap stop occupies the schedule, this one is a guess about the child, and
# there is no reason the two must move together.
ASSUMED_NAP_DURATION_MIN = 60
NAP_DURATION_MIN_MINUTES = 15
NAP_DURATION_MAX_MINUTES = 180

# Sanity bounds on the raw "how many places" form input -- the realistic
# range for a given child's age is enforced downstream by
# itinerary.realistic_stop_count, not here.
STOP_COUNT_FORM_MIN = 1
STOP_COUNT_FORM_MAX = 6

# The form's fixed vocabularies, shared with the template so the form and the
# plan stay in sync. They live here rather than in app.py because they are form
# vocabulary, and because components need them too: anything importing app.py
# would be circular, since app.py imports the components.
# How the family gets from one stop to the next -- and only that. The old list
# was car/bus/stroller/carrier/other, which mixed two questions: how you travel
# between venues, and what you have with you at one. A stroller is not a way of
# covering three kilometres; it is what you push around a park once you arrive.
# Every family is assumed to have one, so it is not asked about here.
#
# One answer, not several. Everyone walks, so walking is the floor rather than
# one option among many, and what actually varies is the furthest you can
# comfortably get. Ticking several only ever meant "take the widest".
#
# `car` covers taxi and ride-share: a visiting family often has no car and will
# take an Uber, and their reach is a driver's.
TRANSIT_OPTIONS = [("car", "Car, taxi or ride-share"),
                   ("transit", "Public transit"),
                   ("walk", "On foot")]
TRANSIT_KEYS = [key for key, _ in TRANSIT_OPTIONS]
DEFAULT_TRANSIT = "walk"

# How long the family will spend getting to any one stop. Asked in minutes
# because that is what a parent can judge standing on a pavement with a
# stroller, and asked at all because the comfortable answer differs enormously
# between families -- the shortest option is the default, since a day that is
# too tightly packed can be widened and a day that is too spread out cannot be
# walked. The values come from geo, which is where the constraint is enforced.
WALK_BUDGET_FORM_OPTIONS = [(str(m), f"Up to {m} min") for m in WALK_BUDGET_OPTIONS]

# What the old options meant, for trips saved before this was one question.
# `bus` was the only transit answer; `stroller` and `carrier` were how a family
# on foot said so; `other` was never really an answer.
LEGACY_TRANSIT = {"bus": "transit", "stroller": "walk", "carrier": "walk"}


def normalise_transit(stored):
    """One transport mode from anything a trip or a form might hold.

    Tolerates the shape this used to have. `trips.transit` was a JSON array,
    because the form was multi-select before it became a single question about
    getting between stops -- so an old row reads as '["stroller"]' or
    '["car","bus"]'. Several modes resolve to the **widest**, which is what
    ticking several always meant. Anything unrecognisable falls back to the
    default, which is the tightest reach.
    """
    from .geo import reach_km          # local: geo imports nothing from here
    if isinstance(stored, str) and stored in TRANSIT_KEYS:
        return stored
    values = stored
    if isinstance(stored, str):
        try:
            values = json.loads(stored)
        except (TypeError, ValueError):
            values = [stored]
    if isinstance(values, str):
        values = [values]
    known = [LEGACY_TRANSIT.get(m, m) for m in (values or [])]
    known = [m for m in known if m in TRANSIT_KEYS]
    return max(known, key=reach_km) if known else DEFAULT_TRANSIT
DINING_OPTIONS = [("dine_out", "Dine out"), ("on_the_go", "Eat on the go")]
TRANSIT_NAP_OPTIONS = [
    ("yes", "Yes -- naps well in a stroller, car, or bus"),
    ("sometimes", "Sometimes -- depends on the situation"),
    ("no", "No -- needs a proper place to nap"),
]

# Sensible defaults so the form is usable on first load.
DEFAULTS = {
    "wake_up": "07:00",
    "bedtime": "20:00",
    "naps": [],
    "transit_nap": "sometimes",
    "age_years": "2",
    "age_months": "0",
    "destination": "Vancouver",
    # The day being planned. Decides which of a venue's hours apply,
    # so it is a planning input rather than a label on a saved trip.
    "trip_date": "",
    # The last day of the visit. Empty means a one-day trip, which is what the
    # form was before it could ask: `trip_date` alone still describes that day,
    # so every reader of a single day -- a saved row, a replan, the chat
    # extractor -- is untouched by this.
    "end_date": "",
    "accommodation": "",
    # Set only by picking the accommodation on the map. Empty means the parent
    # typed an address without pinning it, or gave none: the day still plans,
    # it just has no start and end anchor.
    "accommodation_lat": "",
    "accommodation_lng": "",
    # The tightest reach, deliberately: a clustered day is fine for a family
    # with a car, and a spread-out one is not fine for a family on foot.
    "transit": DEFAULT_TRANSIT,
    "walk_budget": str(DEFAULT_WALK_BUDGET_MIN),
    # Set only by the parent, after a plan has told them what their limit left
    # out. Never set on their behalf: quietly widening the limit is how a
    # twenty-minute walk became a three-hour one.
    "beyond_budget": False,
    "stop_count": "3",
    "dining": "dine_out",
    "preferred_lunch_time": "",
    "nap_notes": "",
    "extra_notes": "",
    "revise_feedback": "",
    "strict_schedule": False,
    "features": [],
    # Filled in by default_form(), which asks the data what kinds of place
    # exist. Every one starts ticked: see all_interests().
    "interest": [],
    "child_ids": [],
    "plan_child_id": "",
}


def clamp_int(value, low, high, fallback):
    """Coerce a form value to an int within [low, high], else the fallback."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _read_age(form):
    """Read years/months and enforce the 5-years-0-months ceiling."""
    years = clamp_int(form.get("age_years"), 0, MAX_AGE_YEARS,
                       int(DEFAULTS["age_years"]))
    months = clamp_int(form.get("age_months"), 0, MAX_MONTHS, 0)
    if years == MAX_AGE_YEARS:
        months = 0  # cap is exactly 5 years, 0 months
    return str(years), str(months)


def _total_months(date_of_birth):
    """A child's age in total months, for comparing who's youngest."""
    years, months = compute_age(date_of_birth)
    return years * 12 + months


def all_interests():
    """Every kind of place the form offers, which is how it starts out.

    Ticking all of them and ticking none of them plan the same day -- a lean
    towards everything is not a lean -- so this is a change of question rather
    than of behaviour. The old form asked "any particular kind of place?" and
    treated blank as "a mix", which meant a parent who answered nothing got a
    rule they were never shown, and a parent who ticked two got the other eight
    anyway with nothing saying so.
    """
    return list(interest_options())


def default_form():
    """A blank planning form. Separate from DEFAULTS because the trip date has
    to be today at request time, not at import time: a long-running server would
    otherwise keep offering the day it booted. Same for the kinds of place,
    which come from the venues that exist."""
    values = dict(DEFAULTS)
    values["trip_date"] = date.today().isoformat()
    values["interest"] = all_interests()
    return values


def read_form(form):
    """Normalise the raw request form into the shape the logic expects."""
    age_years, age_months = _read_age(form)
    naps = []
    for start, duration in zip(form.getlist("nap_start"), form.getlist("nap_duration")):
        if not start:
            continue
        naps.append({
            "start": start,
            "duration_min": clamp_int(duration, NAP_DURATION_MIN_MINUTES,
                                       NAP_DURATION_MAX_MINUTES,
                                       ASSUMED_NAP_DURATION_MIN),
        })
    values = {
        "wake_up": form.get("wake_up") or DEFAULTS["wake_up"],
        "bedtime": form.get("bedtime") or DEFAULTS["bedtime"],
        "naps": naps[:MAX_NAPS],
        "transit_nap": form.get("transit_nap") or DEFAULTS["transit_nap"],
        "age_years": age_years,
        "age_months": age_months,
        # Only a city the app can actually plan. A select does not stop a
        # hand-made post or a stale page, which is the same gap that was closed
        # for `interest` and then for `transit`. An unsupported value falls back
        # to the default rather than raising, since it is a value we offered a
        # closed list for and there is exactly one sensible answer.
        "destination": (form.get("destination") if form.get("destination")
                        in SUPPORTED_CITIES else DEFAULTS["destination"]),
        "trip_date": parse_date(form.get("trip_date")).isoformat(),
        "end_date": _read_end_date(form),
        "accommodation": form.get("accommodation", "").strip(),
        # Kept as strings, like every other form value, so the dict round-trips
        # through a hidden field unchanged. as_point() is what turns them into
        # something to measure from, and drops anything that is not a real
        # coordinate rather than carrying it into the planner.
        **_accommodation_point(form),
        # One value, validated. It used to be an unchecked getlist, so any
        # string got through -- the same gap that was closed for `interest`.
        "transit": (form.get("transit") if form.get("transit") in TRANSIT_KEYS
                    else DEFAULT_TRANSIT),
        # walk_budget_min is the same guard the planner uses, so a hand-made
        # post asking for 500 minutes gets the default rather than a number
        # nobody was offered.
        "walk_budget": str(walk_budget_min(form.get("walk_budget"))),
        "beyond_budget": form.get("beyond_budget") in ("on", "1", "true"),
        "stop_count": str(clamp_int(form.get("stop_count"), STOP_COUNT_FORM_MIN,
                                     STOP_COUNT_FORM_MAX, int(DEFAULTS["stop_count"]))),
        "dining": form.get("dining") or DEFAULTS["dining"],
        "preferred_lunch_time": form.get("preferred_lunch_time", ""),
        "nap_notes": form.get("nap_notes", ""),
        "extra_notes": form.get("extra_notes", ""),
        "revise_feedback": form.get("revise_feedback", ""),
        "strict_schedule": form.get("strict_schedule") == "on",
        "features": form.getlist("features"),
        # An unrecognised kind is dropped rather than carried, so a stale form
        # or a hand-made post cannot ask for something nothing can satisfy.
        "interest": _read_interest(form),
        "child_ids": form.getlist("child_ids"),
        "plan_child_id": form.get("plan_child_id", ""),
    }
    return values


# The rendered form carries this so an empty `interest` can be read correctly.
# Unticked checkboxes are not submitted, so "the parent cleared every box" and
# "this post never had the field" arrive identically; only the form that showed
# the boxes can tell them apart.
INTEREST_OFFERED_FIELD = "interest_offered"


def _read_interest(form):
    """The kinds of place asked for, from a post that may not have asked.

    Empty only when the parent actively cleared every box. A post that never
    showed them -- the chat hand-off, a component call, a test -- gets all of
    them, which is what the form itself would have shown.
    """
    picked = [k for k in form.getlist("interest") if k in VENUE_TYPES]
    if picked or form.get(INTEREST_OFFERED_FIELD):
        return picked
    return all_interests()


def _read_end_date(form):
    """The last day of the visit, never earlier than the first.

    Empty when it was not asked for or matches the start, so a one-day trip
    keeps posting exactly what it posted before this existed.
    """
    start = parse_date(form.get("trip_date")).isoformat()
    end = form.get("end_date")
    if not end:
        return ""
    resolved = parse_date(end, default=parse_date(start)).isoformat()
    return "" if resolved <= start else resolved


def trip_dates(form):
    """The days a plan covers, as ISO strings. Always at least one."""
    return date_range(form.get("trip_date"), form.get("end_date"))


def trip_too_long(form):
    """How many days were asked for, when that is more than we will plan.

    None when the range is fine. Named rather than clamped: silently planning
    the first week of a fortnight is the kind of answer that looks like a bug
    to the person who asked for the fortnight.
    """
    asked = days_between(form.get("trip_date"), form.get("end_date"))
    return asked if asked > MAX_TRIP_DAYS else None


def _accommodation_point(form):
    """The map pin's coordinates, or empty strings when there is no pin.

    Both or neither: half a coordinate cannot be measured from, and storing one
    would leave a trip that looks pinned and is not.
    """
    point = as_point(form.get("accommodation_lat"), form.get("accommodation_lng"))
    if point is None:
        return {"accommodation_lat": "", "accommodation_lng": ""}
    return {"accommodation_lat": str(point["lat"]),
            "accommodation_lng": str(point["lng"])}


def resolve_plan_child(form, parent):
    """If the parent is logged in and has children, pick which child's age
    drives the plan (respecting any checked child_ids / plan_child_id in the
    form) and overwrite the form's age fields to match. No-op otherwise.
    Used by /plan to decide whose age the candidate plans are built around."""
    children_by_id = {str(c["id"]): c for c in get_children(parent["id"])} if parent else {}
    if not children_by_id:
        return form
    checked_ids = [cid for cid in form["child_ids"] if cid in children_by_id] \
        or list(children_by_id)
    plan_child_id = form["plan_child_id"] if form["plan_child_id"] in checked_ids else None
    if not plan_child_id:
        plan_child_id = min(checked_ids,
                             key=lambda cid: _total_months(children_by_id[cid]["date_of_birth"]))
    form["child_ids"], form["plan_child_id"] = checked_ids, plan_child_id
    years, months = compute_age(children_by_id[plan_child_id]["date_of_birth"])
    form["age_years"], form["age_months"] = str(years), str(months)
    return form
