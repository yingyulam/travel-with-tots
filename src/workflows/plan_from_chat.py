"""Fill the planning form by talking, instead of typing it in.

A conversation, not a single extraction. Each turn the extractor reads whatever
the parent just said, that merges into the form built up so far, and the
assistant either asks for the next thing it still needs or shows the whole form
and asks them to check it. It keeps going until the required fields are there
and the parent says yes.

Two things this deliberately does not do.

It does not generate the day. `/plan` already does that, and doing it here would
mean duplicating a sixteen-argument call, showing a plan the page would then
regenerate differently (the AI adjuster is not deterministic), and carrying a
2.5-4.5KB plan around. Instead the finished form is posted to `/plan`, which
plans it exactly as it always has.

It does not decide what "complete" means from the server's rules, because there
are none: read_form({}) returns a perfectly plannable form. REQUIRED below is
this conversation's own judgement about what is worth asking for.
"""

import re

from ..components.extract_form import extract_form
from ..data_loader import SUPPORTED_CITIES
from ..form_helpers import DEFAULTS

# Asked for before handing the form over, in the order they are asked. Not the
# fields plan_trip needs (it needs only destination and an age) but the ones
# that most change the shape of a day, and that a parent would be surprised to
# see guessed. Everything else rides on its default and is shown at the end.
REQUIRED = ("destination", "age", "wake_up", "bedtime", "naps")

# What to say when one is missing. One question per turn: a question answerable
# in a sentence gets answered, a checklist gets abandoned. Naps are the
# exception and ask for two things at once, because a nap time without a length
# is half an answer and asking twice for one fact is worse than asking once for
# both.
QUESTIONS = {
    "destination": "Which city are you visiting?",
    "age": "How old is your little one?",
    "wake_up": "What time does their day usually start?",
    "bedtime": "And what time is bedtime?",
    "naps": "When is their nap, and how long does it usually last?",
}

# Buttons offered with a question whose answers are a short fixed list. Only
# the city has one, and it comes from the venue data rather than a literal, so
# the offer cannot promise a city the app has nothing to plan in.
QUESTION_CHOICES = {"destination": list(SUPPORTED_CITIES)}

# Asked once, before any of the individual questions. Everything at once, in
# one message, because a parent who can describe their day in a sentence should
# not be interviewed field by field: that is the form again, only slower. The
# questions above are what is left over, asked only for what this did not get.
OPENING_QUESTION = (
    "Tell me about your day, whatever you know:\n\n"
    f"- Which city (we cover {SUPPORTED_CITIES[0]} for now)\n"
    "- How old your little one is\n"
    "- What time their day starts, and bedtime\n"
    "- Nap time and how long it lasts, if they still nap\n\n"
    "All in one message is fine, something like: \"Vancouver, she's 2, up at 7 "
    "and bed at 7:30, naps at 1 for an hour.\""
)

# Asked once, after the required fields, because the useful things a parent
# knows about their own child are the ones no field thought to ask for. The
# answer is free text and goes wherever the extractor puts it, usually the
# notes, so there is nothing here that can be "missing".
EXTRAS_QUESTION = "Is there anything else we need to know?"

# Fields the parent never talks about, so listing them as defaults is noise.
INTERNAL = ("child_ids", "plan_child_id", "revise_feedback")

STAGE_OFFERED = "offered"
STAGE_COLLECTING = "collecting"
STAGE_EXTRAS = "extras"
STAGE_CONFIRMING = "confirming"

# The buttons offered at each stage. Named rather than written inline at the
# point they are offered, because the widget sends a button's own label back as
# the message: a label this module cannot parse is a button that does nothing.
FORM_CHOICE = "Fill out the form"
CHAT_CHOICE = "Plan through chat"
CONFIRM_CHOICE = "Yes, that's right"
NOTHING_CHOICE = "No, that's everything"

_YES = ("yes", "yep", "yeah", "yup", "ok", "okay", "sure", "correct", "confirm",
        "confirmed", "looks good", "that's right", "thats right", "go ahead",
        "do it", "generate", "plan it", "perfect", "great")
# Nothing to add. The "anything else" button's own label is in here, and so is
# what a parent types instead of clicking it.
_NOTHING = ("no", "nope", "nothing", "none", "nothing else", "no thanks",
            "that's everything", "thats everything", "that's all", "thats all",
            "that's it", "thats it", "all good")

# Naps are the one required field a child can genuinely not have, and "she
# doesn't nap anymore" is how a parent says so. Matched as a phrase inside the
# message rather than as the whole of it, because no list of whole-message
# negatives would ever catch the ways of saying this.
_NO_NAP = ("doesn't nap", "does not nap", "dont nap", "don't nap",
           "no longer nap", "stopped napping", "dropped the nap",
           "dropped her nap", "dropped his nap", "no naps", "no nap")

# Dropped before matching, so "yes please" is the same answer as "yes".
_FILLER = ("please", "thanks", "thank you")


def _is_only(message: str, vocabulary: tuple) -> bool:
    """True when the whole message is that one kind of answer and nothing else.

    Every clause has to match, rather than the message merely starting with a
    matching word: "yes, but make it four stops" is a correction, and accepting
    it would hand over a form the parent had just asked to change. It is also
    why a button's own label has to parse here, since "Yes, that's right" is
    two affirmations rather than one.
    """
    text = message.lower()
    for filler in _FILLER:
        text = text.replace(filler, " ")
    parts = [part.strip(" !.?") for part in re.split(r"[,.!?]| and ", text)]
    parts = [part for part in parts if part]
    return bool(parts) and all(part in vocabulary for part in parts)


# Only one of the two ways is tested for. Anything that is not asking for the
# form carries on here, which is where the parent already is, so an unclear
# answer costs nothing: they can still leave for the form at any point.
_FORM_CHOICE = ("form", "myself", "1", "one", "first")

# The free-text fields, which accumulate instead of being replaced. Every other
# field holds one value that a later answer corrects, but a note is something a
# parent adds to: "she needs a highchair" does not retract "she hates loud
# places". The extractor only ever sees the current message, so without this the
# earlier note is silently dropped. accommodation is deliberately not here: it
# is free text, but it is a value, and saying where you are staying twice is a
# correction rather than an addition.
NOTE_FIELDS = ("nap_notes", "extra_notes")


def _append_note(existing: str, addition: str) -> str:
    """Both notes as one. Sentence-shaped fragments joined with a space read as
    prose, so no reformatting is needed. A repeat of something already in there
    is dropped, which is what saying the same thing twice deserves."""
    existing = (existing or "").strip()
    addition = (addition or "").strip()
    if not addition or addition in existing:
        return existing
    return f"{existing} {addition}".strip()


def _declined(message: str, field: str) -> bool:
    """Whether this answer is the parent saying there is nothing to give.

    Two shapes: the whole message is a negative, which works for any question,
    or it says the child does not nap, which only the nap question can mean.
    """
    if _is_only(message, _NOTHING):
        return True
    return field == "naps" and any(phrase in message.lower() for phrase in _NO_NAP)


def _supplied(found: set, skipped: set = ()) -> set:
    """Which required fields count as answered.

    Read off `found` rather than the form, because read_form fills every field
    from DEFAULTS: a form always *has* a destination, so only `found` can say
    whether the parent chose it.
    """
    supplied = set(found) | set(skipped)
    # Age is two form fields but one question, so either counts as answered.
    if "age_years" in supplied or "age_months" in supplied:
        supplied.add("age")
    return supplied


def _missing(found: set, skipped: set = ()) -> list:
    """Required fields still to ask about, in the order they are asked."""
    supplied = _supplied(found, skipped)
    return [field for field in REQUIRED if field not in supplied]


def _merge(form: dict, found: set, result: dict) -> tuple[dict, set]:
    """Fold one extraction into the form built up so far.

    Only fields the extractor reported in `found` may overwrite. A plain dict
    update would let a later turn reset an earlier one's destination back to
    its default, because every extraction returns a *complete* form.

    Notes are the exception: they are added to rather than replaced, since the
    extractor reads one message at a time and has no way to know what the
    parent already said.
    """
    merged = dict(form)
    for field in result["found"]:
        value = result["form"][field]
        if field in NOTE_FIELDS:
            value = _append_note(merged.get(field), value)
        merged[field] = value
    return merged, set(found) | set(result["found"])


def _summarise(form: dict, found: set) -> str:
    """The whole form, every field marked as theirs or a default.

    Both halves matter: they asked to see the values that came from their words,
    and to see the defaults, so nothing reaches the planner unseen.
    """
    theirs, defaults = [], []
    for field, value in form.items():
        if field in INTERNAL:
            continue
        line = f"- {field.replace('_', ' ')}: {_show(value)}"
        (theirs if field in found else defaults).append(line)
    parts = []
    if theirs:
        parts.append("From what you told me:\n" + "\n".join(theirs))
    if defaults:
        parts.append("Using defaults for the rest:\n" + "\n".join(defaults))
    parts.append("Does that look right? Say yes, or tell me what to change.")
    return "\n\n".join(parts)


def _show(value) -> str:
    if isinstance(value, list):
        if not value:
            return "(none)"
        return ", ".join(
            f"{item['start']} for {item['duration_min']} min"
            if isinstance(item, dict) else str(item) for item in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value) if value != "" else "(not set)"


def _start() -> dict:
    """The two ways to plan. Offered rather than assumed: some parents would
    rather fill the form themselves, and the chat should not railroad them."""
    return {
        "reply": ("Happy to help you plan a day. Two ways to do it:\n\n"
                  "1. Fill out the form yourself, and I'll take you there.\n"
                  "2. Plan through chat, and I'll fill the form in for you as "
                  "we talk.\n\nWhich would you prefer?"),
        "state": {"stage": STAGE_OFFERED, "form": {}, "found": []},
        "choices": [FORM_CHOICE, CHAT_CHOICE],
    }


def _ask(field: str, state: dict) -> dict:
    """The next question, remembering which field it was about.

    `asking` is the only way a later turn can tell that a "no" answered *this*
    question rather than being a stray word, so it travels with the state.
    """
    asked = {**state, "stage": STAGE_COLLECTING, "asking": field}
    reply = {"reply": QUESTIONS[field], "state": asked}
    if field in QUESTION_CHOICES:
        reply["choices"] = QUESTION_CHOICES[field]
    return reply


def _confirm(state: dict) -> dict:
    """The whole form, for the parent to check before it is handed over."""
    found = set(state.get("found") or [])
    return {
        "reply": _summarise(state["form"], found),
        "state": {**state, "stage": STAGE_CONFIRMING, "asking": None},
        "choices": [CONFIRM_CHOICE],
    }


def _collect(message: str, state: dict) -> dict:
    """One collecting turn: extract, merge, then ask, or move on."""
    form = state.get("form") or dict(DEFAULTS)
    found = set(state.get("found") or [])
    skipped = set(state.get("skipped") or [])
    asking = state.get("asking")
    try:
        form, found = _merge(form, found, extract_form(message))
    except Exception as e:
        # The extractor is one model call among many in a conversation. Losing
        # one turn's words is better than losing the conversation, so the
        # parent is asked again rather than shown an error.
        print(f"Extraction skipped for one turn: {type(e).__name__}: {e}")

    # A child who has dropped their nap has no nap time to give, and repeating
    # the question until they invent one is the worst thing this could do. Any
    # question can be declined, which marks it asked and moves on.
    if asking and asking not in _supplied(found) and _declined(message, asking):
        skipped.add(asking)

    carried = {"form": form, "found": sorted(found), "skipped": sorted(skipped),
               "asked_extras": state.get("asked_extras", False)}

    missing = _missing(found, skipped)
    if missing:
        return _ask(missing[0], carried)

    if not carried["asked_extras"]:
        # Asked once, and only once: it has no answer that can be missing, so
        # nothing else would ever stop it being asked again.
        carried["asked_extras"] = True
        return {"reply": EXTRAS_QUESTION,
                "state": {**carried, "stage": STAGE_EXTRAS, "asking": None},
                "choices": [NOTHING_CHOICE]}

    return _confirm(carried)


def run(message: str, state: dict | None = None,
        context: dict | None = None) -> dict:
    """One turn of the form-filling conversation.

    `context` is part of the runnable-workflow contract and carries what the
    request knew that the message did not. Nothing here wants it: a planning
    form is about a day, not about where the parent is standing.

    No state begins it; state continues it. Returns {"reply", "state"} plus
    optionally "choices" (buttons to offer) and "form" (present only once the
    parent has confirmed, which is the signal to hand off to /plan).

    One entry point rather than a start/continue pair, because the registry
    contract `runnable_message_workflows()` looks for is a callable named `run`.
    """
    if state is None:
        return _start()

    stage = state.get("stage")

    if stage == STAGE_OFFERED:
        said = message.lower()
        if any(word in said for word in _FORM_CHOICE):
            return {
                "reply": ("No problem, the planning form is ready when you are. "
                          "Open it from the Planning link, fill in your day, and "
                          "press Generate my day."),
                "state": None,
                "open_form": True,
            }
        # The one open question, not the first of five. `asking` is None
        # because no single field owns it, so nothing here can be declined by
        # accident.
        return {"reply": OPENING_QUESTION,
                "state": {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS),
                          "found": [], "skipped": [], "asking": None,
                          "asked_extras": False}}

    if stage == STAGE_EXTRAS:
        if _is_only(message, _NOTHING):
            # Nothing to extract, and running the extractor on "no" would only
            # append it to the notes the parent is about to read.
            return _confirm(state)
        return _collect(message, {**state, "stage": STAGE_COLLECTING})

    if stage == STAGE_CONFIRMING:
        if _is_only(message, _YES):
            return {
                "reply": ("Great. I've got everything ready on the planning "
                          "page. Open it to check the form, or generate the day "
                          "straight away."),
                "state": None,
                "form": state["form"],
                "found": state.get("found") or [],
            }
        # Anything else is a correction, not a refusal. asked_extras rides
        # along in the state, so correcting cannot restart the questions.
        return _collect(message, {**state, "stage": STAGE_COLLECTING})

    return _collect(message, state)


WORKFLOW = {
    "name": "Fill the form from a chat message",
    # Not 💬: the trigger group heading already carries that.
    "emoji": "📝",
    "trigger": "message",
    # Endpoint name for its test page, so /workflows can offer a "Try it" link.
    "page": "plan_from_chat_page",
    "description": (
        "A parent says they want to plan a day and the assistant fills the "
        "planning form in with them over a few messages, showing what it read "
        "from their words and what it left at a default. The planning page "
        "still builds the day, from the form they confirmed."
    ),
    "steps": [
        {"component": "AI Agent (OpenRouter)", "built": True},
        {"component": "Form extractor", "built": True},
    ],
}
