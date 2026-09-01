"""Replan a trip component: a rule-based re-plan, then AI-smoothed.

Self-contained entry point, one file per component (see /components). The
substantial logic lives in interactions.py (rule-based situation handling)
and agents.py (AI adjustment) -- this file only composes them, so app.py's
/replan/adjust route uses this single implementation.
"""

import requests

from ..agents import DEFAULT_MODEL, ReplanningAgent, ReplanningAgentError
from ..data_loader import get_venues
from ..dates import parse_date
from ..interactions import replan
from ..plan_diff import describe_changes, summarise


def replan_trip(*, plan, situation, current_time, destination="", age_months=0,
                 features=None, transit=None, dining=None, bedtime=None,
                 minutes=None, interest=None, nap_notes="", extra_notes="",
                 trip_date=None, model=DEFAULT_MODEL) -> dict:
    """Re-plan the rest of the day: a rule-based draft (stops at/before
    current_time kept as-is, remaining stops re-decided for the situation),
    then AI-smoothed. Always returns a usable plan -- if the AI step fails,
    falls back to the unadjusted draft rather than raising, so every caller
    gets that resilience for free. Returns a plan dict ({"label", "blurb",
    "from_time", "stops"}) plus "adjusted" (did the AI step run) and
    "changed" (did it move anything).

    `model` is the one the parent picked in the chat widget's dropdown, the
    same as planning, so one choice governs every AI call the app makes."""
    # Resolved for the day being replanned, not for today. Without the date a
    # trip planned in advance had its hours resolved for whenever the parent
    # happened to press the button, so a replan onto a statutory holiday, or
    # across a season boundary, could swap in a venue whose hours were read off
    # the wrong day. The /replan route already passed a date; this path did not.
    draft = replan(plan, situation, current_time,
                    get_venues(on_date=parse_date(trip_date)), features or [],
                    bedtime=bedtime, minutes=minutes, interest=interest)
    adjusted = True
    try:
        adjustment = ReplanningAgent(model).adjust_replan(
            draft, current_time=current_time, destination=destination,
            age_months=age_months, features=features or [], transit=transit or [],
            dining=dining, bedtime=bedtime, nap_notes=nap_notes,
            extra_notes=extra_notes, situation=situation,
            # Also to the AI pass, not just the rule-based one: without these
            # it cannot tell a three-hour nap from a twenty-minute one, or know
            # what the draft was rebuilt for.
            minutes=minutes, interest=interest,
        )
        draft["stops"] = adjustment["stops"]
    except (ReplanningAgentError, requests.exceptions.RequestException, KeyError) as e:
        print(f"Replan adjustment skipped, showing the unadjusted draft: {e}")
        adjusted = False
    draft["adjusted"] = adjusted
    # Whether the AI actually moved anything. adjust_plan marks every stop it
    # edits, so no marks means it read the day and left it alone. That is the
    # adjuster agreeing with the draft, which is a good outcome and a different
    # one from the call failing, and only `adjusted` can tell those apart.
    draft["changed"] = any(stop.get("adjusted") for stop in draft["stops"])
    # What accepting this would do to the day, against the day as it stands.
    # A replan is a proposal the parent says yes or no to, and "here is a new
    # timeline, spot the difference" is not a question anybody can answer
    # standing outside a shut aquarium with a toddler.
    draft["changes"] = describe_changes(plan.get("stops") or [], draft["stops"])
    draft["change_summary"] = summarise(draft["changes"])
    return draft
