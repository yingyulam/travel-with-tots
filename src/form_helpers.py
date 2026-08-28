"""Trip-planning form parsing and validation -- pure functions operating on
the raw form dict app.py's routes already parse, no Flask dependency."""

from datetime import date

from .dates import parse_date, compute_age
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
TRANSIT_OPTIONS = ["car", "bus", "stroller", "carrier", "other"]
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
    "accommodation": "",
    "transit": ["stroller"],
    "stop_count": "3",
    "dining": "dine_out",
    "preferred_lunch_time": "",
    "nap_notes": "",
    "extra_notes": "",
    "revise_feedback": "",
    "strict_schedule": False,
    "features": [],
    "themes": [],
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


def default_form():
    """A blank planning form. Separate from DEFAULTS because the trip date has
    to be today at request time, not at import time: a long-running server would
    otherwise keep offering the day it booted."""
    values = dict(DEFAULTS)
    values["trip_date"] = date.today().isoformat()
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
        "destination": form.get("destination") or DEFAULTS["destination"],
        "trip_date": parse_date(form.get("trip_date")).isoformat(),
        "accommodation": form.get("accommodation", "").strip(),
        "transit": form.getlist("transit"),
        "stop_count": str(clamp_int(form.get("stop_count"), STOP_COUNT_FORM_MIN,
                                     STOP_COUNT_FORM_MAX, int(DEFAULTS["stop_count"]))),
        "dining": form.get("dining") or DEFAULTS["dining"],
        "preferred_lunch_time": form.get("preferred_lunch_time", ""),
        "nap_notes": form.get("nap_notes", ""),
        "extra_notes": form.get("extra_notes", ""),
        "revise_feedback": form.get("revise_feedback", ""),
        "strict_schedule": form.get("strict_schedule") == "on",
        "features": form.getlist("features"),
        "themes": form.getlist("themes"),
        "child_ids": form.getlist("child_ids"),
        "plan_child_id": form.get("plan_child_id", ""),
    }
    return values


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
