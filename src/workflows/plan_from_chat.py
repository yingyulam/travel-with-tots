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
from ..form_helpers import DEFAULTS

# Asked for before handing the form over, in the order they are asked. Not the
# fields plan_trip needs (it needs only destination and an age) but the ones
# that most change the shape of a day, and that a parent would be surprised to
# see guessed. Everything else rides on its default and is shown at the end.
REQUIRED = ("destination", "age", "wake_up", "bedtime")

# What to say when one is missing. One question per turn: a question answerable
# in a sentence gets answered, a checklist gets abandoned.
QUESTIONS = {
    "destination": "Which city are you in?",
    "age": "How old is your little one?",
    "wake_up": "What time does their day usually start?",
    "bedtime": "And what time is bedtime?",
}

# Fields the parent never talks about, so listing them as defaults is noise.
INTERNAL = ("child_ids", "plan_child_id", "revise_feedback")

STAGE_OFFERED = "offered"
STAGE_COLLECTING = "collecting"
STAGE_CONFIRMING = "confirming"

# The buttons offered at each stage. Named rather than written inline at the
# point they are offered, because the widget sends a button's own label back as
# the message: a label this module cannot parse is a button that does nothing.
FORM_CHOICE = "Fill out the form"
CHAT_CHOICE = "Plan through chat"
CONFIRM_CHOICE = "Yes, that's right"

_YES = ("yes", "yep", "yeah", "yup", "ok", "okay", "sure", "correct", "confirm",
        "confirmed", "looks good", "that's right", "thats right", "go ahead",
        "do it", "generate", "plan it", "perfect", "great")
# Dropped before matching, so "yes please" is the same answer as "yes".
_FILLER = ("please", "thanks", "thank you")


def _is_yes(message: str) -> bool:
    """True only when the whole message is an affirmation and nothing else.

    Every clause has to be one, rather than the message merely starting with a
    yes word: "yes, but make it four stops" is a correction, and accepting it
    would hand over a form the parent had just asked to change. That is also
    why the confirmation button's own label has to be checked here, since it
    reads "Yes, that's right", which is two affirmations rather than one.
    """
    text = message.lower()
    for filler in _FILLER:
        text = text.replace(filler, " ")
    parts = [part.strip(" !.?") for part in re.split(r"[,.!?]| and ", text)]
    parts = [part for part in parts if part]
    return bool(parts) and all(part in _YES for part in parts)


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


def _missing(found: set) -> list:
    """Required fields the parent has not actually supplied.

    Read off `found` rather than the form, because read_form fills every field
    from DEFAULTS: a form always *has* a destination, so only `found` can say
    whether the parent chose it.
    """
    supplied = set(found)
    # Age is two form fields but one question, so either counts as answered.
    if "age_years" in supplied or "age_months" in supplied:
        supplied.add("age")
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


def _collect(message: str, state: dict) -> dict:
    """One collecting turn: extract, merge, then ask or confirm."""
    form = state.get("form") or dict(DEFAULTS)
    found = set(state.get("found") or [])
    try:
        form, found = _merge(form, found, extract_form(message))
    except Exception as e:
        # The extractor is one model call among many in a conversation. Losing
        # one turn's words is better than losing the conversation, so the
        # parent is asked again rather than shown an error.
        print(f"Extraction skipped for one turn: {type(e).__name__}: {e}")

    missing = _missing(found)
    new_state = {"stage": STAGE_COLLECTING, "form": form, "found": sorted(found)}
    if missing:
        return {"reply": QUESTIONS[missing[0]], "state": new_state}

    new_state["stage"] = STAGE_CONFIRMING
    return {"reply": _summarise(form, found), "state": new_state,
            "choices": [CONFIRM_CHOICE]}


def run(message: str, state: dict | None = None) -> dict:
    """One turn of the form-filling conversation.

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
        begun = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        return {"reply": QUESTIONS[REQUIRED[0]], "state": begun}

    if stage == STAGE_CONFIRMING:
        if _is_yes(message):
            return {
                "reply": ("Great. I've got everything ready on the planning "
                          "page. Open it to check the form, or generate the day "
                          "straight away."),
                "state": None,
                "form": state["form"],
                "found": state.get("found") or [],
            }
        # Anything else is a correction, not a refusal.
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
