"""Check a finished itinerary against each venue's hours for the trip date.

The planner already builds a draft from venues that are open (itinerary._pick
calls venue_open_for), and the AI adjuster already refuses an edit that moves a
stop outside a venue's hours. This is the step that holds the *whole* plan
accountable for the *specific day*, after both of those have run, and repairs
what it finds.

It is deterministic on purpose. Comparing a stop time against stored hours is
arithmetic, and CLAUDE.md's rule is not to ask a model to do what code already
does. What a model genuinely cannot be trusted with here is the thing that
matters most: whether a venue is open on Christmas Day. So this never guesses.
A venue whose hours for the day are unknown is treated as closed, reported as
unverified, and replaced.

The gaps it reports are the useful output beyond the repair: they name exactly
which venue needs which day's hours filled in, which is what the review queue
is for.
"""

from ..dates import day_type_for, parse_date
from ..interactions import open_alternative
from ..itinerary import display_to_min, stop_duration, venue_open_for

# Why a stop did not survive, in words a parent can read.
CLOSED = "closed"
UNVERIFIED = "unverified"

# Each phrase is a clause that has to read correctly after "because", since
# that is how it reaches a parent on the trip page.
_REASONS = {
    # Only fires for a venue with a door now: a park, beach or seawall falls
    # back to its ordinary pair on a holiday, because there is nothing to lock.
    "holiday_unknown": (UNVERIFIED, "we do not know its holiday hours"),
    "missing": (UNVERIFIED, "we do not have its opening hours"),
    "default": (CLOSED, "it is closed then"),
}


def _problem(stop, venue, day_label):
    """Why this stop cannot stand, or None if it can."""
    start = display_to_min(stop["time"])
    duration = stop_duration(stop.get("kind", "activity"))
    if venue_open_for(venue, start, duration):
        return None
    kind, template = _REASONS.get(venue.get("hours_source"), _REASONS["default"])
    return {
        "time": stop["time"],
        "venue": venue["name"],
        "kind": kind,
        "why": template,
    }


def check_plan(plan, on_date=None, day_label=None):
    """Every stop whose venue cannot be visited at its scheduled time.

    Returns {"ok", "problems", "day_type"}. `on_date` is only used to
    label the report: the venues in `plan` already carry the hours resolved for
    that day by data_loader.get_venues(on_date=...), so this cannot disagree
    with what the planner saw.
    """
    on_date = parse_date(on_date) if not hasattr(on_date, "year") else on_date
    day_type = day_type_for(on_date)
    label = day_label or day_type
    problems = []
    for stop in plan.get("stops", []):
        venue = stop.get("venue")
        if not venue:
            continue
        found = _problem(stop, venue, label)
        if found:
            problems.append(found)
    return {"ok": not problems, "problems": problems,
            "day_type": day_type,
            "venues_total": 0, "venues_without_hours": 0, "note": ""}


def enforce(plan, venues, on_date=None, features=None, day_label=None):
    """Replace or drop every stop that cannot stand, and report what happened.

    Replacement uses the same open_alternative the in-trip replan uses, so a
    substitute is chosen by exactly the rules that would apply if the parent
    hit a closure on the day. A stop with no available substitute loses its
    venue rather than keeping one that is shut: a slot the parent can fill
    themselves beats a confident wrong answer.

    Never mutates `plan`. Returns (plan, report) where report adds "replaced"
    and "dropped" to what check_plan gives.
    """
    on_date = parse_date(on_date) if not hasattr(on_date, "year") else on_date
    label = day_label or day_type_for(on_date)
    stops = [dict(stop) for stop in plan.get("stops", [])]
    used = {s["venue"]["name"] for s in stops if s.get("venue")}
    replaced, dropped, problems = [], [], []

    for stop in stops:
        venue = stop.get("venue")
        if not venue:
            continue
        found = _problem(stop, venue, label)
        if not found:
            continue
        problems.append(found)
        start = display_to_min(stop["time"])
        duration = stop_duration(stop.get("kind", "activity"))
        alternative = open_alternative(stop.get("kind", "activity"), venues,
                                       features or [], used, start, duration)
        if alternative is not None:
            used.discard(venue["name"])
            used.add(alternative["name"])
            stop["venue"] = alternative
            stop["reason"] = (f"{alternative['name']} instead of "
                              f"{venue['name']}, because {found['why']}.")
            stop["hours_swapped"] = True
            replaced.append({**found, "with": alternative["name"]})
        else:
            used.discard(venue["name"])
            stop["venue"] = None
            stop["reason"] = (f"{venue['name']} was planned here, but "
                              f"{found['why']}. Nothing else is open then, so "
                              "this slot is yours to fill.")
            dropped.append(found)

    report = check_plan({"stops": stops}, on_date, label)
    report.update({"replaced": replaced, "dropped": dropped,
                   "problems": problems})
    report.update(_coverage(venues, label))
    return {**plan, "stops": stops}, report


def _coverage(venues, day_label):
    """How much of the venue set has usable hours for this day, and a sentence
    saying so when it is not most of it.

    Without this a holiday reads as a mysteriously empty day. The planner
    refuses to schedule a venue whose hours it does not know, which is right,
    but the parent is owed the reason and the admin is owed the fix.
    """
    venues = list(venues or [])
    unknown = [v for v in venues if not v.get("open") or not v.get("close")]
    note = ""
    if venues and len(unknown) == len(venues):
        note = (f"We do not have {day_label} hours for any venue yet, so there "
                "was nothing we could safely put in a day. Rather than guess, "
                "we have left it empty.")
    elif len(unknown) > len(venues) / 2:
        note = (f"We only have {day_label} hours for "
                f"{len(venues) - len(unknown)} of {len(venues)} places, so this "
                "day is thinner than usual.")
    return {"venues_total": len(venues),
            "venues_without_hours": len(unknown),
            "note": note}
