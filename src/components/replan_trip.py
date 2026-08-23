"""Replan a trip component: a rule-based re-plan, then AI-smoothed.

Self-contained entry point, one file per component (see /components). The
substantial logic lives in interactions.py (rule-based situation handling)
and agents.py (AI adjustment) -- this file only composes them, so app.py's
/replan/adjust route uses this single implementation.
"""

import requests

from ..agents import DEFAULT_MODEL, ReplanningAgent, ReplanningAgentError
from ..data_loader import VENUES
from ..interactions import replan


def replan_trip(*, plan, situation, current_time, destination="", age_months=0,
                 features=None, transit=None, dining=None, bedtime=None,
                 minutes=None, theme=None, nap_notes="", extra_notes="",
                 model=DEFAULT_MODEL) -> dict:
    """Re-plan the rest of the day: a rule-based draft (stops at/before
    current_time kept as-is, remaining stops re-decided for the situation),
    then AI-smoothed. Always returns a usable plan -- if the AI step fails,
    falls back to the unadjusted draft rather than raising, so every caller
    gets that resilience for free. Returns a plan dict ({"label", "blurb",
    "from_time", "stops"}) plus "adjusted" (did the AI step run) and
    "changed" (did it move anything).

    `model` is the one the parent picked in the chat widget's dropdown, the
    same as planning, so one choice governs every AI call the app makes."""
    draft = replan(plan, situation, current_time, VENUES, features or [],
                    bedtime=bedtime, minutes=minutes, theme=theme)
    adjusted = True
    try:
        adjustment = ReplanningAgent(model).adjust_replan(
            draft, current_time=current_time, destination=destination,
            age_months=age_months, features=features or [], transit=transit or [],
            dining=dining, bedtime=bedtime, nap_notes=nap_notes,
            extra_notes=extra_notes, situation=situation,
            # Also to the AI pass, not just the rule-based one: without these
            # it cannot tell a three-hour nap from a twenty-minute one, or know
            # which theme the draft was rebuilt for.
            minutes=minutes, theme=theme,
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
    return draft
