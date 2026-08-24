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

import re

from .. import interactions

STAGE_SITUATION = "situation"

# The situations the trip page already offers, so the chat and the buttons
# cannot drift into naming the same thing differently.
SITUATION_LABELS = [label for _, label in interactions.SITUATION_OPTIONS]
LABEL_TO_SITUATION = {label.lower(): key
                      for key, label in interactions.SITUATION_OPTIONS}

# Anything the words below do not match becomes this, carrying what they typed
# as the note. The trip page's own free-text box does exactly the same, so an
# unrecognised situation is still a real replan rather than a dead end.
FREE_TEXT_SITUATION = interactions.NOTE_ONLY_SITUATION[0]

# Read in this order, which is why it is a tuple of pairs. A nap that ran long
# usually also means running behind, and the nap is the thing that happened.
SITUATION_WORDS = (
    ("nap_happened", ("nap", "napped", "napping", "slept", "asleep",
                      "sleeping", "went down")),
    ("weather_rain", ("rain", "raining", "pouring", "wet", "downpour",
                      "drizzl")),
    ("skip_next", ("skip", "drop the next", "miss the next", "cut a stop")),
    ("finished_early", ("finished early", "done early", "early", "ahead of",
                        "quicker than")),
    ("running_behind", ("longer", "running behind", "behind", "late",
                        "delayed", "stay here", "stay put", "overran")),
    ("change_theme", ("theme", "something different", "change the day",
                      "indoors instead")),
)

# Situations the day's shape depends on a number for. The trip page asks with a
# preset or a typed number; here the number is read out of the sentence, and
# the server's own default stands when there isn't one.
TIMED_SITUATIONS = ("nap_happened", "running_behind")

SITUATION_QUESTION = (
    "Let's shift the rest of the day. What's happened? Pick one, or just tell "
    "me in your own words."
)
NO_TRIP_REPLY = (
    "I can shift a day you've already started. Open your trip from the "
    "planning page, then ask me again and I'll replan from where you are."
)

# "an hour and a half" is not worth parsing; a number with a unit is. The unit
# is required, so "3 stops left" is not three minutes. Plurals are optional and
# the longer spellings come first, or "minutes" would fail the word boundary
# after "minute" and read as nothing at all.
_MINUTES = re.compile(r"(\d+)\s*(hours?|hrs?|h|minutes?|mins?|m)\b", re.I)


def read_situation(message: str) -> str:
    """Which of the trip page's situations this message describes.

    Falls back to the free-text situation rather than None: every message is a
    replan request of some kind once the parent is in this conversation, and
    the component treats an unnamed situation as "re-time what's left, and read
    my note".
    """
    said = message.strip().lower()
    if said in LABEL_TO_SITUATION:
        return LABEL_TO_SITUATION[said]
    for situation, words in SITUATION_WORDS:
        if any(word in said for word in words):
            return situation
    return FREE_TEXT_SITUATION


def read_minutes(message: str) -> int | None:
    """How long, in minutes, if they said. None leaves the server's default.

    Clamped by `interactions._replan_minutes` downstream either way, so this
    only has to read the number, not police it.
    """
    match = _MINUTES.search(message)
    if not match:
        return None
    amount = int(match.group(1))
    return amount * 60 if match.group(2).lower().startswith(("hour", "hr", "h")) \
        else amount


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
        return {"reply": SITUATION_QUESTION,
                "state": {"stage": STAGE_SITUATION, "values": {}},
                "choices": SITUATION_LABELS}

    situation = read_situation(message)
    values = {"situation": situation}
    if situation in TIMED_SITUATIONS:
        minutes = read_minutes(message)
        if minutes:
            values["minutes"] = minutes
    # Their own words ride along whenever they typed rather than tapped: the
    # replan prompt reads the note, and a tapped label adds nothing a label
    # does not already say.
    if message.strip().lower() not in LABEL_TO_SITUATION:
        values["note"] = message.strip()
    return _ready(values)


WORKFLOW = {
    "name": "Replan on the go",
    "emoji": "🔄",
    "trigger": "message",
    "page": "replan_on_the_go_page",
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
