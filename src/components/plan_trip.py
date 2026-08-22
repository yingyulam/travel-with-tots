"""Plan trips component: a rule-based day draft, then AI-smoothed.

Self-contained entry point, one file per component (see /components). The
substantial logic lives in itinerary.py (rule-based draft) and agents.py
(AI adjustment) -- this file only composes them, so app.py's /plan route
and src/agent.py's plan_trip_tool both use this single implementation
instead of each having their own copy.
"""

import requests

from ..agents import PlanningAgent, PlanningAgentError
from ..data_loader import VENUES
from ..itinerary import generate_plans


def plan_trip(*, destination, age_months, wake_up="07:00", bedtime="20:00",
               stop_count=3, dining="dine_out", features=None, naps=None,
               preferred_lunch_time="", nap_notes="", extra_notes="",
               transit=None, accommodation="", strict_schedule=False,
               themes=None, transit_nap="") -> dict:
    """Build a full day plan: a rule-based draft, then AI-smoothed. Always
    returns a usable plan -- if the AI step fails, falls back to the
    unadjusted draft rather than raising, so every caller gets that
    resilience for free instead of reimplementing the try/except. Returns
    a Plan-shaped dict ({"label", "blurb", "stops", "source"}) plus
    "adjusted" (bool), so callers can decide how to present a fallback."""
    inputs = {
        "wake_up": wake_up, "bedtime": bedtime, "naps": naps or [],
        "age_years": str(age_months // 12), "age_months": str(age_months % 12),
        "destination": destination, "stop_count": stop_count,
        "features": features or [], "themes": themes or [], "dining": dining,
        "accommodation": accommodation, "preferred_lunch_time": preferred_lunch_time,
        "transit_nap": transit_nap,
    }
    plan = generate_plans(VENUES, inputs)[0]
    adjusted = True
    try:
        adjustment = PlanningAgent().adjust_plan(
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
    result = plan.to_dict()
    result["adjusted"] = adjusted
    return result
