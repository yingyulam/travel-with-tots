"""Plan trips component: a rule-based day draft, then AI-smoothed.

Self-contained entry point, one file per component (see /components). The
substantial logic lives in itinerary.py (rule-based draft) and agents.py
(AI adjustment) -- this file only composes them, so app.py's /plan route
and src/agent.py's plan_trip_tool both use this single implementation
instead of each having their own copy.
"""

import requests

from ..agents import DEFAULT_MODEL, PlanningAgent, PlanningAgentError
from ..data_loader import get_venues
from ..components.validate_hours import enforce
from ..dates import parse_date
from ..itinerary import generate_plans


def plan_trip(*, destination, age_months, trip_date=None, wake_up="07:00", bedtime="20:00",
               stop_count=3, dining="dine_out", features=None, naps=None,
               preferred_lunch_time="", nap_notes="", extra_notes="",
               transit=None, accommodation="", accommodation_lat=None,
               accommodation_lng=None, strict_schedule=False,
               interest=None, transit_nap="", model=DEFAULT_MODEL) -> dict:
    """Build a full day plan: a rule-based draft, then AI-smoothed. Always
    returns a usable plan -- if the AI step fails, falls back to the
    unadjusted draft rather than raising, so every caller gets that
    resilience for free instead of reimplementing the try/except. Returns
    a Plan-shaped dict ({"label", "blurb", "stops", "source"}) plus
    "adjusted" (bool: did the AI step run at all) and "changed"
    (bool: did it move anything), so callers can tell an adjuster that
    agreed with the draft from one that failed.

    `model` is the one the parent picked in the chat widget's dropdown, so the
    day is smoothed by whichever model they chose rather than by a default they
    cannot see. It is the whole cost of the call: the rule-based draft below
    takes well under a millisecond, and everything after it is the model."""
    inputs = {
        "wake_up": wake_up, "bedtime": bedtime, "naps": naps or [],
        "age_years": str(age_months // 12), "age_months": str(age_months % 12),
        "destination": destination, "stop_count": stop_count,
        "features": features or [], "interest": interest or [], "dining": dining,
        "accommodation": accommodation, "preferred_lunch_time": preferred_lunch_time,
        "transit_nap": transit_nap,
        # How far apart consecutive stops may sit. This was reaching the AI
        # prompt but not the draft, so the mode a parent picked was inert on
        # the one plan they actually see.
        "transit": transit,
        # Where they are staying, when they pinned it on the map: the first
        # stop is chosen from here and the last is chosen to get back to it.
        "accommodation_lat": accommodation_lat,
        "accommodation_lng": accommodation_lng,
    }
    # The day being planned decides which of each venue's hours apply, so it is
    # resolved once here rather than threaded through the planner.
    on_date = parse_date(trip_date)
    venues = get_venues(on_date=on_date)
    plan = generate_plans(venues, inputs)[0]
    adjusted = True
    try:
        adjustment = PlanningAgent(model).adjust_plan(
            plan.to_dict(), destination=destination, age_months=age_months,
            wake_up=wake_up, bedtime=bedtime, stop_count=stop_count, dining=dining,
            naps=naps, preferred_lunch_time=preferred_lunch_time,
            nap_notes=nap_notes, extra_notes=extra_notes, transit=transit,
            accommodation=accommodation, features=features, strict_schedule=strict_schedule,
            transit_nap=transit_nap,
        )
        plan.stops = adjustment["stops"]
    except (PlanningAgentError, requests.exceptions.RequestException, KeyError) as e:
        print(f"Plan adjustment skipped, showing the unadjusted draft: {e}")
        adjusted = False
    # Last word on hours, after both the draft and the AI adjuster. The draft
    # only used venues open at their slot, and the adjuster refuses an edit that
    # moves one outside its hours, but neither is accountable for the finished
    # plan as a whole: an adjuster failure leaves the draft, a retry can land a
    # different set of edits, and no earlier step reports *why* a venue could
    # not be used. Nothing reaches a parent without passing this.
    checked, hours_report = enforce(plan.to_dict(), venues, on_date,
                                    features=features)
    plan.stops = checked["stops"]

    result = plan.to_dict()
    result["hours"] = hours_report
    result["adjusted"] = adjusted
    # Whether the AI actually moved anything. adjust_plan marks every stop it
    # edits, so no marks means it read the day and left it alone. That is the
    # adjuster agreeing with the draft, which is a good outcome and a different
    # one from the call failing, and only `adjusted` can tell those apart.
    result["changed"] = any(stop.get("adjusted") for stop in result["stops"])
    return result
