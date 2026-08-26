"""What the app already knows about a parent, recalled so the chat need not ask.

Read only, and no Flask, so a workflow, a route or a test can all use it. One
job: turn a parent id into the durable facts worth reusing, already in the shape
the planning form speaks.

Nothing here becomes a new source of truth. An age is recomputed from a date of
birth on every call, so it cannot go stale, and the routine comes from the most
recent trip the parent actually saved. Drafts and half-finished forms are not
memory.

Two rules do most of the work:

`read_form` decides, not this module. The trip row goes through the same
validator the /plan route uses, so NULL naps, a malformed naps array, the legacy
`stop_count` values ("balanced", "adventurous") and the age cap are all handled
once, by code that already exists.

A field is only *remembered* if its stored value survived that validation
unchanged. `read_form` fills every field from DEFAULTS, so the returned form can
never say what came from memory, and a value the validator had to repair was
never the parent's choice. Presenting a clamped "balanced" as a stop count they
picked would be worse than not remembering it at all.
"""

import json
from datetime import date

from werkzeug.datastructures import MultiDict

from .dates import compute_age
from .db import get_children, get_trips_for_parent
from .form_helpers import MAX_AGE_YEARS, read_form

# How the child sleeps and eats moves every few months at these ages, and it is
# what the whole plan is shaped around, so past this the clock is asked about
# again rather than recalled. The rest of the routine decays slowly enough to be
# worth offering.
STALE_AFTER_DAYS = 90

CLOCK_FIELDS = ("wake_up", "bedtime", "preferred_lunch_time", "naps")
STABLE_FIELDS = ("destination", "accommodation", "transit", "features",
                 "dining", "stop_count", "transit_nap")

# Stored as JSON arrays, and read_form takes them as repeated form fields.
_JSON_LIST_FIELDS = ("transit", "features")

# Deliberately never recalled. Both are posted to /plan and fed into the AI
# adjuster's prompt, so remembering them would ship a months-old note ("she's on
# antibiotics") into a new model request. They are also the two fields the chat
# accumulates rather than replaces, so a remembered note would be neither the
# parent's nor memory's.
NEVER_RECALLED = ("nap_notes", "extra_notes")


def _nothing() -> dict:
    return {"child": None, "form": {}, "remembered": [], "trip_saved_at": None}


def _age(date_of_birth) -> tuple | None:
    """A child's age as (years, months), or None if the date is unusable.

    Clamped here rather than left to read_form. A date of birth in the future
    gives compute_age a negative year, and a child past the cap gives more
    months than the form allows; either way read_form would silently repair it
    and the chat would then show an age the planner does not use.
    """
    try:
        years, months = compute_age(date_of_birth)
    except (TypeError, ValueError):
        return None
    if years < 0:
        return 0, 0
    if years >= MAX_AGE_YEARS:
        return MAX_AGE_YEARS, 0
    return years, months


def _children(parent_id) -> list:
    """This parent's children as plain dicts, each with a usable age.

    Per child rather than in one pass, so one unparseable date of birth costs
    that child and not the whole recall. Plain dicts because the result travels
    through jsonify, which cannot serialise a sqlite3.Row.
    """
    found = []
    for row in get_children(parent_id):
        age = _age(row["date_of_birth"])
        if age is None:
            continue
        found.append({"id": str(row["id"]), "name": row["name"],
                      "age_years": age[0], "age_months": age[1],
                      "total_months": age[0] * 12 + age[1]})
    return found


def _pick_child(children: list, trip) -> dict | None:
    """Whose day this is: the child on the last saved trip, else the youngest.

    The last trip is the parent's own most recent choice, so it beats a rule.
    The fallback matches form_helpers.resolve_plan_child, which is what /plan
    applies to whatever this hands over, so the two cannot disagree.
    """
    if not children:
        return None
    if trip is not None and trip["child_id"] is not None:
        named = str(trip["child_id"])
        for child in children:
            if child["id"] == named:
                return child
    return min(children, key=lambda child: child["total_months"])


def _last_trip(parent_id):
    """The most recently saved trip, or None.

    get_trips_for_parent already scopes to this parent and already excludes rows
    with no saved plan, so "the last trip" means the last day they kept rather
    than the last form they touched. Re-sorted here because it orders on
    created_at alone, and a day saved for several children writes one row per
    child with an identical timestamp, which is exactly the case where a
    nondeterministic tie would pick a different child on different requests.
    """
    trips = list(get_trips_for_parent(parent_id))
    if not trips:
        return None
    return max(trips, key=lambda row: (row["created_at"] or "", row["id"]))


def _is_stale(trip) -> bool:
    """Whether the clock fields on this trip are too old to offer."""
    saved = trip["trip_date"] or (trip["created_at"] or "")[:10]
    try:
        return (date.today() - date.fromisoformat(saved)).days > STALE_AFTER_DAYS
    except (TypeError, ValueError):
        # An unreadable date is not evidence of freshness.
        return True


def _candidates(trip, child) -> MultiDict:
    """The trip and child as raw form data, ready for read_form.

    None keys are dropped rather than passed on, mirroring
    components/extract_form._as_form_data: read_form does `.strip()` on
    accommodation and `or DEFAULTS[...]` on the rest, so a NULL column would
    either raise or silently become a default that then looks remembered.
    """
    data = MultiDict()
    fields = STABLE_FIELDS if (trip is None or _is_stale(trip)) \
        else STABLE_FIELDS + CLOCK_FIELDS

    if trip is not None:
        for field in fields:
            if field == "naps":
                for nap in _naps(trip["naps"]):
                    data.add("nap_start", nap["start"])
                    data.add("nap_duration", nap["duration_min"])
                continue
            value = trip[field]
            if value is None or value == "":
                continue
            if field in _JSON_LIST_FIELDS:
                for item in _json_list(value):
                    data.add(field, item)
            else:
                data.add(field, str(value))

    if child is not None:
        data.add("age_years", str(child["age_years"]))
        data.add("age_months", str(child["age_months"]))
        # Name the child, do not just state their age. /plan recomputes the age
        # from plan_child_id on every branch and defaults to the youngest, so an
        # age handed over without a child attached is quietly replaced.
        data.add("plan_child_id", child["id"])
    return data


def _naps(stored) -> list:
    """Naps from a trip's JSON column, or none at all.

    NULL means "we do not know", not "no naps", and it is the common case: most
    saved rows have it empty. Anything that is not a list of objects with a real
    start is not a nap, so it is dropped rather than handed to read_form, which
    zips the two lists positionally and would pair a nap with the wrong length.
    """
    try:
        naps = json.loads(stored) if stored else []
    except (TypeError, ValueError):
        return []
    if not isinstance(naps, list):
        return []
    return [{"start": nap["start"],
             "duration_min": "" if nap.get("duration_min") is None
                             else str(nap["duration_min"])}
            for nap in naps
            if isinstance(nap, dict) and nap.get("start")]


def _json_list(stored) -> list:
    try:
        items = json.loads(stored)
    except (TypeError, ValueError):
        return []
    return [item for item in items if isinstance(item, str)] \
        if isinstance(items, list) else []


def _survived(field, raw: MultiDict, validated: dict) -> bool:
    """Whether read_form kept what was stored, which is what makes it a memory.

    A repaired value is not something the parent chose. The legacy stop_count
    "balanced" clamps to 3, and offering that back as their answer is the
    failure this exists to prevent.
    """
    if field == "naps":
        return len(validated["naps"]) == len(raw.getlist("nap_start"))
    if field in _JSON_LIST_FIELDS:
        return validated[field] == raw.getlist(field)
    return str(validated[field]) == raw.get(field)


def recall(parent_id) -> dict:
    """The durable facts worth reusing for this parent.

    Returns {"child", "form", "remembered", "trip_saved_at"}. `form` holds only
    what memory can supply, in DEFAULTS' own shapes; `remembered` names those
    fields, because the form alone cannot say which of its values came from
    anywhere. `child` is whose day it is, with an age computed just now.

    Never raises and never asks for anything: an anonymous chat passes no id and
    gets an empty recall, and a database that will not answer costs the parent
    their memory rather than their reply. handle_message only catches
    FormExtractionError, RequestException and KeyError, so a sqlite3 error would
    otherwise reach the route as a 500.
    """
    if not parent_id:
        return _nothing()
    try:
        children = _children(parent_id)
        trip = _last_trip(parent_id)
        child = _pick_child(children, trip)
        raw = _candidates(trip, child)
        validated = read_form(raw)
    except Exception as e:                                  # noqa: BLE001
        print(f"Recall skipped: {type(e).__name__}: {e}")
        return _nothing()

    remembered = sorted(
        field for field in set(raw) | ({"naps"} if "nap_start" in raw else set())
        if field not in ("nap_start", "nap_duration", "plan_child_id")
        and field not in NEVER_RECALLED
        and _survived(field, raw, validated))

    form = {field: validated[field] for field in remembered}
    if child is not None:
        # Carried but not listed: these are plumbing, and the chat names the
        # child in words instead. plan_from_chat.INTERNAL already hides them.
        form["plan_child_id"] = child["id"]

    return {"child": child, "form": form, "remembered": remembered,
            "trip_saved_at": trip["trip_date"] if trip is not None else None}
