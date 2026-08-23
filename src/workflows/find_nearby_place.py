"""Find a nearby place: a parent asks the chat for what they need right now.

The chain the chat bubble could not reach before. `find_nearby_tool` answered
these messages with `interactions.find_nearby`, the deterministic placeholder,
so the agent got no location awareness, no distance ranking and no web
fallback. This runs the real component instead, and being registered means the
routing log names it rather than reading "no workflow".

One turn in the normal case: read the need from what they said, ask the
component, hand back the places. The two places it can take a second turn are
a need it cannot read, and a location it does not have yet.
"""

from .. import interactions
from ..components.find_nearby import find_nearby
from ..data_loader import SUPPORTED_CITIES

STAGE_NEED = "need"

# What each need is called in a sentence. Taken from the same NEED_OPTIONS the
# trip page's buttons are built from, so the chat and the panel cannot end up
# calling the same thing two different names. "Other" is the exception: it is a
# fine button label and a useless noun.
NEED_PHRASES = {key: label.lower() for key, label in interactions.NEED_OPTIONS}
NEED_PHRASES["other"] = "kid-friendly place"

# A chip click sends the button's own label, so those are matched first and
# exactly. Everything else goes through the keywords below.
LABEL_TO_NEED = {label.lower(): key for key, label in interactions.NEED_OPTIONS}

# Read in this order, which is the whole reason it is a tuple of pairs rather
# than a dict: "a quiet place to feed the baby" is a nursing room, not a quiet
# spot, and only the order says so. Six fixed categories with distinctive words
# is work code does, so there is no model call here.
NEED_WORDS = (
    ("nursing_room", ("nursing", "nurse", "breastfeed", "breast feed",
                      "feed the baby", "feeding the baby", "milk")),
    ("changing_table", ("changing table", "change table", "changing room",
                        "nappy", "diaper", "change the baby")),
    ("family_room", ("family room", "family washroom", "family bathroom",
                     "family toilet")),
    ("restaurant", ("restaurant", "somewhere to eat", "place to eat", "food",
                    "lunch", "dinner", "breakfast", "hungry", "cafe", "snack")),
    ("quiet_spot", ("quiet", "calm", "nap", "sleep", "rest", "meltdown",
                    "wind down", "settle")),
)

NEED_QUESTION = "Sure. What do you need right now?"
LOCATION_CHOICE = "📍 Use my location"

# Asked for rather than guessed when the words match nothing, but only once:
# asking twice for the same thing is how a conversation stops being useful.
FALLBACK_NEED = "other"


def read_need(message: str) -> str | None:
    """Which of the six needs the parent is asking for, or None if unreadable."""
    said = message.strip().lower()
    if said in LABEL_TO_NEED:
        return LABEL_TO_NEED[said]
    for need, words in NEED_WORDS:
        if any(word in said for word in words):
            return need
    return None


def _coords(context: dict | None) -> tuple:
    """The parent's coordinates, or (None, None). Checked rather than trusted:
    they come from the browser through the request body, and the component
    would take a string as a number and produce nonsense distances."""
    values = []
    for key in ("lat", "lng"):
        value = (context or {}).get(key)
        values.append(float(value) if isinstance(value, (int, float)) else None)
    lat, lng = values
    return (lat, lng) if lat is not None and lng is not None else (None, None)


def _answer(result: dict, need: str, located: bool) -> dict:
    """The reply, plus the places for the widget to render as real links.

    The places travel as data rather than as URLs written into the sentence.
    Nothing here parses prose for links, which is how the trip page does it too,
    and it keeps a web result's third-party URL out of the reply text.
    """
    places = result["places"]
    phrase = NEED_PHRASES.get(need, "kid-friendly place")
    where = "near you" if located else f"in {SUPPORTED_CITIES[0]}"

    if not places:
        reply = f"I couldn't find a {phrase} {where} right now."
    elif len(places) == 1:
        reply = f"Here's the closest {phrase} I could find {where}:"
    else:
        reply = f"Here are the closest {phrase} options I could find {where}:"

    if result["source"] == "search":
        # The same distinction the trip page draws: a web result is somewhere
        # nobody has checked, and saying so is the point of having two sources.
        reply += "\n\nThese came from a web search rather than our own list."

    return {
        "reply": reply,
        "state": None,
        "places": places,
        "source": result["source"],
        # Offered, not demanded: the answer above already stands, and sharing a
        # location only sharpens it.
        "ask_location": not located,
    }


def run(message: str, state: dict | None = None,
        context: dict | None = None) -> dict:
    """One turn. `context` carries the browser's coordinates when it has them.

    Returns {"reply", "state"} plus "places" and "source" when it found
    something, "choices" when it needs the need spelled out, and
    "ask_location" when coordinates would have made the answer better.
    """
    need = read_need(message)
    if need is None:
        if not (state and state.get("stage") == STAGE_NEED):
            return {"reply": NEED_QUESTION,
                    "state": {"stage": STAGE_NEED},
                    "choices": [label for _, label in interactions.NEED_OPTIONS]}
        # Already asked once. Anything kid-friendly beats asking again.
        need = FALLBACK_NEED

    lat, lng = _coords(context)
    # The city goes in every time. The component only consults the curated
    # table when it has a city or coordinates, and Vancouver is the only city
    # the venue data covers, so this is what makes the no-location branch work
    # at all. Coordinates then add real distance ranking on top.
    result = find_nearby(need=need, city=SUPPORTED_CITIES[0], lat=lat, lng=lng)
    return _answer(result, need, lat is not None)


WORKFLOW = {
    "name": "Find a nearby place",
    "emoji": "📍",
    "trigger": "message",
    # The component's own admin page, where the same chain runs in isolation.
    "page": "find_nearby_page",
    "description": (
        "A parent asks the chat for something they need right now and the agent "
        "hands it to Find nearby stops, which picks its own source: the curated "
        "venues, ranked by real distance when the browser has shared a location, "
        "or Web Search when nothing curated matches. Each place comes back with "
        "a Google Maps link, and the reply says which source it came from."
    ),
    "steps": [
        {"component": "AI Agent (OpenRouter)", "built": True},
        {"component": "Find nearby stops", "built": True},
        {"component": "Web Search", "built": True},
    ],
}
