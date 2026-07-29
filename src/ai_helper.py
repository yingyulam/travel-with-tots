"""Placeholder for an AI-powered assistant.

No LLM is wired up yet. This function returns a generic, canned suggestion
but has the shape a real backend would take (it receives the plan context and
returns a text tip), so it can be swapped for a real model call later.
"""


def get_suggestion(context=None):
    """Return a generic parenting-on-the-go tip.

    ``context`` (the generated itinerary and form inputs) is accepted but
    ignored for now. A real implementation would send it to an LLM and return
    the model's response.
    """
    return (
        "Tip: keep the plan loose. Little ones set the pace, so treat each "
        "stop as optional. Pack snacks and a spare outfit, scout the nearest "
        "nursing or family room when you arrive, and don't be afraid to swap "
        "an activity for extra downtime if the day gets long."
    )
