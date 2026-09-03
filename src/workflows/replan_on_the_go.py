"""Replan on the go: reshape the rest of the day when something changes.

Was "Nap-time rescue", which named one situation out of seven. A long nap is
the commonest reason a day stops fitting, but a closed museum, rain, or simply
wanting to stay put are the same request, and the replan component already
handles all of them.

Declaration only until now. What it collects is what the trip page's own
situation buttons collect: which situation, how long, and anything worth adding
in words. What it does *not* do is run the replan. The trip page holds the plan,
its versions, and the current time, and `runReplan` there is the one
implementation; doing it here would mean a second one, and a new version that
never reached the page's version switcher. So the confirmed request is handed to
that page, the way the planning chat hands its form to /plan.
"""

from .. import interactions

STAGE_SITUATION = "situation"

# Read from interactions, not defined here. The chat agent needs the identical
# reading and must not import from workflows/, so the one implementation lives
# beside the vocabulary it reads. Re-exported under the old names because this
# module's own tests and page name them.
SITUATION_LABELS = interactions.SITUATION_CHIP_LABELS
LABEL_TO_SITUATION = interactions.LABEL_TO_SITUATION
FREE_TEXT_SITUATION = interactions.FREE_TEXT_SITUATION
SITUATION_WORDS = interactions.SITUATION_WORDS
TIMED_SITUATIONS = interactions.TIMED_SITUATIONS
read_situation = interactions.read_situation
read_minutes = interactions.read_minutes

SITUATION_QUESTION = interactions.SITUATION_QUESTION
NO_TRIP_REPLY = (
    "I can shift a day you've already started. Open your trip from the "
    "planning page, then ask me again and I'll replan from where you are."
)

def _ready(values: dict) -> dict:
    """The situation read back, and the request handed over for one button.

    The button is the confirmation, so there is no separate confirming turn.
    An earlier draft had both, which meant tapping "Replan now" only to be
    shown a second Replan button: two controls for one decision.
    """
    label = interactions.SITUATION_LABELS.get(values["situation"],
                                              "Something's changed")
    lines = [f"- what happened: {label}"]
    if values.get("minutes"):
        lines.append(f"- how long: {values['minutes']} minutes")
    if values.get("note"):
        lines.append(f"- in your words: {values['note']}")
    return {
        "reply": ("Got it.\n\n" + "\n".join(lines)
                  + "\n\nReplan from where you are now?"),
        # The conversation is over: pressing the button is an action on the
        # trip page, not another message.
        "state": None,
        "replan_request": values,
    }


def run(message: str, state: dict | None = None,
        context: dict | None = None) -> dict:
    """One turn. `context` carries whether a trip is open on this page.

    Returns {"reply", "state"} plus "choices" while asking what happened, then
    "replan_request" with no state, which is the signal for the widget to offer
    one Replan button and for the trip page to act when it is pressed.
    """
    stage = (state or {}).get("stage")

    if stage is None:
        # Nothing to replan without a started day, and collecting a situation
        # we cannot act on would waste the parent's turn.
        if not (context or {}).get("on_trip"):
            return {"reply": NO_TRIP_REPLY, "state": None}
        # The opening message often already says what happened. Only a
        # *specific* situation counts: read_situation falls back to free text
        # for anything it does not recognise, so "we need to replan" would
        # otherwise skip straight past the six chips, which are the useful
        # thing to offer someone who has not said yet.
        if read_situation(message) != FREE_TEXT_SITUATION:
            return run(message, {"stage": STAGE_SITUATION, "values": {}}, context)
        return {"reply": SITUATION_QUESTION,
                "state": {"stage": STAGE_SITUATION, "values": {}},
                "choices": SITUATION_LABELS}

    return _ready(interactions.read_replan_request(message))


WORKFLOW = {
    "name": "Replan on the go",
    "emoji": "🔄",
    "trigger": "message",
    "page": "devpages.replan_on_the_go_page",
    "description": (
        "Something changes mid-trip, a nap that ran long, a closed stop, rain, "
        "or simply wanting to stay put, and the parent says so in the chat. "
        "The assistant reads which situation it is and hands the request to "
        "the in-trip page, which re-times the rest of the day and keeps the "
        "original to compare against."
    ),
    "steps": [
        {"component": "AI Agent (OpenRouter)", "built": True},
        {"component": "Replan a trip on-the-go (adjustment)", "built": True},
        {"component": "Find nearby stops", "built": True},
    ],
}
