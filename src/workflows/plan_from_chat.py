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

from ..components.extract_form import extract_form
from ..data_loader import SUPPORTED_CITIES
from ..dates import format_age
from ..form_helpers import DEFAULTS
from ..intent import matches_only
from ..memory import recall

# Asked for before handing the form over, in the order they are asked. Not the
# fields plan_trip needs (it needs only destination and an age) but the ones
# that most change the shape of a day, and that a parent would be surprised to
# see guessed. Everything else rides on its default and is shown at the end.
REQUIRED = ("destination", "age", "wake_up", "bedtime", "naps")

# The form fields behind each question. `found` and `remembered` hold form field
# names while `skipped` and REQUIRED hold question names, and age is two fields
# behind one question, so the mapping has to be written down rather than
# rediscovered at each use. _summarise reads it too: an entry naming a question
# rather than a field matches nothing in the form and would render as a default,
# which is the one thing the provenance buckets exist to prevent.
QUESTION_FIELDS = {
    "destination": ("destination",),
    "age": ("age_years", "age_months"),
    "wake_up": ("wake_up",),
    "bedtime": ("bedtime",),
    "naps": ("naps",),
}

REQUIRED_FIELDS = frozenset(
    field for question in REQUIRED for field in QUESTION_FIELDS[question])

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

# Said before a question when memory already supplied something, because a
# parent asked one bare question ("When is their nap?") cannot tell whether the
# rest was remembered or forgotten. What exactly was remembered is shown, field
# by field and labelled by source, on the summary at the end.
RECALLED_PREFACE = "Here's what I already have for you:"

# Said when what memory supplied is cleared, so the parent gets an
# acknowledgement rather than an unexplained question.
FORGOTTEN = "Fine, I've dropped all of that. Let's take it from the top."

# Said when memory covered everything, so there is nothing to ask at all.
SAME_AS_LAST_TIME = ("Welcome back. I still have your last day out, so we can "
                     "reuse it as it is.")

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
CHANGED_CHOICE = "Something's changed"
NOTHING_CHOICE = "No, that's everything"

_YES = ("yes", "yep", "yeah", "yup", "ok", "okay", "sure", "correct", "confirm",
        "confirmed", "looks good", "that's right", "thats right", "go ahead",
        "do it", "generate", "plan it", "perfect", "great")
# Nothing to add. The "anything else" button's own label is in here, and so is
# what a parent types instead of clicking it.
_NOTHING = ("no", "nope", "nothing", "none", "nothing else", "no thanks",
            "that's everything", "thats everything", "that's all", "thats all",
            "that's it", "thats it", "all good")

# Rejecting what was remembered, which nothing in _YES or _NOTHING parses. It
# also doubles as the "unsay" this conversation otherwise lacks: without it
# there is no way to retract a field that was never asked about, because only an
# answered question can be corrected.
_CHANGED = ("something's changed", "somethings changed", "changed",
            "that's changed", "thats changed", "not right", "start over",
            "not the same", "that's wrong", "thats wrong")

# Deliberately not in there: "no" and "different". This is now tested on every
# turn, and a bare "no" is an answer to whatever was just asked, so accepting it
# here would wipe what memory supplied instead of declining one question. That
# is the mistake the ✕ Cancel matcher already had to learn.

# Naps are the one required field a child can genuinely not have, and "she
# doesn't nap anymore" is how a parent says so. Matched as a phrase inside the
# message rather than as the whole of it, because no list of whole-message
# negatives would ever catch the ways of saying this.
_NO_NAP = ("doesn't nap", "does not nap", "dont nap", "don't nap",
           "no longer nap", "stopped napping", "dropped the nap",
           "dropped her nap", "dropped his nap", "no naps", "no nap")


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


def _seed(context: dict | None) -> dict:
    """What the app already knows, before the parent has said anything.

    Only for a parent the *session* identified: `parent_id` reaches here from
    `_chat_context`, never from the request body, because it is what the recall
    is scoped by. An anonymous chat seeds nothing and behaves exactly as it did
    before memory existed.

    recall() does not raise, and this catches anyway: losing what we remember is
    a worse outcome than the parent retyping it, but losing the turn is worse
    than both.
    """
    parent_id = (context or {}).get("parent_id")
    if not parent_id:
        return {"form": {}, "remembered": [], "recalled": {}}
    try:
        known = recall(parent_id)
    except Exception as e:
        print(f"Recall skipped for one turn: {type(e).__name__}: {e}")
        return {"form": {}, "remembered": [], "recalled": {}}
    child = known["child"] or {}
    return {
        "form": known["form"],
        "remembered": known["remembered"],
        # Kept apart from the form: these describe where the values came from,
        # for the headings, and are not fields anything plans on.
        "recalled": {"child": child.get("name"),
                     "trip_saved_at": known["trip_saved_at"]},
    }


def _forget_all(state: dict) -> dict:
    """Drop everything memory supplied and ask for it properly.

    The parent has said the recalled day is not this day, and there is no way to
    know which part changed, so nothing recalled survives. What they told us in
    this conversation does, which is why `found` is untouched.
    """
    found, skipped, remembered = _answered(state)
    form = dict(state["form"])
    for field in remembered:
        form[field] = DEFAULTS[field]
    cleared = {**state, "form": form, "remembered": [],
               "recalled": {}, "stage": STAGE_COLLECTING, "asking": None}
    missing = _missing(found, skipped)
    answer = _ask(missing[0], cleared) if missing else _confirm(cleared)
    return {**answer, "reply": f"{FORGOTTEN}\n\n{answer['reply']}"}


def _same_as_last_time(state: dict) -> dict:
    """Memory covered every question, so there is nothing to ask.

    Left to the ordinary flow this turn would answer "plan a day" with "Is there
    anything else we need to know?", which mentions no memory and reads as a non
    sequitur. Going straight to the summary makes it a one-tap replan, and that
    summary is already the "same as last time" screen. The extras question is
    marked asked, because asking it of a wholly remembered form is noise.
    """
    ready = _confirm({**state, "asked_extras": True})
    ready["reply"] = f"{SAME_AS_LAST_TIME}\n\n{ready['reply']}"
    # The door _start() would have offered stays open: a returning parent may
    # still prefer to fill the form in themselves.
    ready["choices"] = [*ready["choices"], FORM_CHOICE]
    return ready


def _to_form() -> dict:
    """Hand over to the real form and end the conversation."""
    return {
        "reply": ("No problem, the planning form is ready when you are. "
                  "Open it from the Planning link, fill in your day, and "
                  "press Generate my day."),
        "state": None,
        "open_form": True,
    }


def _forget(question: str, form: dict, remembered: set) -> tuple[dict, set]:
    """Drop what memory supplied for one question, back to the defaults.

    Marking it skipped is not enough. The recalled value is still sitting in the
    form, so the summary would show a value the parent has just contradicted and
    the hand-off would post it.
    """
    cleared = dict(form)
    for field in QUESTION_FIELDS.get(question, ()):
        cleared[field] = DEFAULTS[field]
    return cleared, remembered - set(QUESTION_FIELDS.get(question, ()))


def _declined(message: str, field: str) -> bool:
    """Whether this answer is the parent saying there is nothing to give.

    Two shapes: the whole message is a negative, which works for any question,
    or it says the child does not nap, which only the nap question can mean.
    """
    if matches_only(message, _NOTHING):
        return True
    return field == "naps" and any(phrase in message.lower() for phrase in _NO_NAP)


def _supplied(found: set, skipped: set = (), remembered: set = ()) -> set:
    """Which required fields count as answered, by whatever route.

    Read off these three lists rather than the form, because read_form fills
    every field from DEFAULTS: a form always *has* a destination, so only the
    provenance lists can say whether it came from anywhere.
    """
    supplied = set(found) | set(skipped) | set(remembered)
    # A question is answered once any of its fields is, since age is two form
    # fields behind one question.
    supplied.update(question for question, fields in QUESTION_FIELDS.items()
                    if supplied.intersection(fields))
    return supplied


def _missing(found: set, skipped: set = (), remembered: set = ()) -> list:
    """Required fields still to ask about, in the order they are asked."""
    supplied = _supplied(found, skipped, remembered)
    return [field for field in REQUIRED if field not in supplied]


def _answered(state: dict) -> tuple[set, set, set]:
    """The three provenance lists off a state, as sets.

    One reader rather than three `set(state.get(...) or [])` incantations at
    four call sites, which is exactly where a forgotten third argument would
    quietly re-ask something already known. `.get` throughout, because every
    state written before memory existed lacks the third key.
    """
    return (set(state.get("found") or []),
            set(state.get("skipped") or []),
            set(state.get("remembered") or []))


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


# Recalled from the child's own record, so recomputed today and never stale.
# Everything else recalled comes from a saved trip and can be months old, which
# is why the two are never shown under one heading.
PROFILE_FIELDS = ("age_years", "age_months")


def _recalled_source(remembered: set) -> str:
    """Where the recalled values came from, naming only the sources actually
    used: crediting a child's record to a parent who has not added one is the
    same unverifiable claim this is meant to replace.

    Both sources are things they can already see and edit on the dashboard,
    which is the point worth making. Nothing is remembered here that the app
    holds and they cannot reach.
    """
    profile = bool(set(PROFILE_FIELDS) & set(remembered))
    trip = bool(set(remembered) - set(PROFILE_FIELDS))
    if profile and trip:
        where = "your child's details and the last day you saved, both"
    elif profile:
        where = "your child's details,"
    else:
        where = "the last day you saved,"
    return (f"That comes from {where} on your dashboard. "
            "Tell me if anything has changed.")


def _recalled_blocks(form: dict, remembered: set, recalled: dict) -> list:
    """What memory supplied, as headed blocks, one per source.

    Shared by the summary and by the preface on the turn memory is first used,
    because a claim to remember something is worth nothing unless the parent can
    see what it is. Rendered once here so the two cannot disagree about the same
    values, which was the reason the preface used not to itemise at all.
    """
    profile, trip = [], []
    for field in sorted(remembered):
        if field in INTERNAL or field not in form:
            continue
        line = f"- {field.replace('_', ' ')}: {_show(form[field])}"
        (profile if field in PROFILE_FIELDS else trip).append(line)

    blocks = []
    if profile:
        name = (recalled or {}).get("child")
        heading = f"From {name}'s details:" if name else "From your saved details:"
        blocks.append(heading + "\n" + "\n".join(profile))
    if trip:
        saved = (recalled or {}).get("trip_saved_at")
        heading = f"From your last trip{f', saved {saved}' if saved else ''}:"
        blocks.append(heading + "\n" + "\n".join(trip))
    return blocks


def _summarise(form: dict, found: set, remembered: set = (),
               recalled: dict = None) -> str:
    """The whole form, every field marked with where it came from.

    Four buckets, each rendered only when it has something. What the parent
    said, what was recalled about the child, what was recalled from their last
    day out, and what is riding on a default, so nothing reaches the planner
    unseen and nothing is presented as theirs that was not.

    `found` is checked before `remembered` on purpose: _merge adds a corrected
    field to `found` without removing it from `remembered`, so the order is what
    moves it into the right bucket rather than any bookkeeping.
    """
    # A corrected field is in both lists, so `found` winning here is what moves
    # it out of the recalled bucket. _recalled_blocks is given only what is
    # still purely memory's for the same reason.
    theirs, defaults = [], []
    still_recalled = set(remembered) - set(found)
    for field, value in form.items():
        if field in INTERNAL:
            continue
        line = f"- {field.replace('_', ' ')}: {_show(value)}"
        if field in found:
            theirs.append(line)
        elif field not in still_recalled:
            defaults.append(line)
    parts = []
    if theirs:
        parts.append("From what you told me:\n" + "\n".join(theirs))
    parts.extend(_recalled_blocks(form, still_recalled, recalled))
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


def _start(state: dict | None = None) -> dict:
    """The two ways to plan. Offered rather than assumed: some parents would
    rather fill the form themselves, and the chat should not railroad them.

    Carries the state through rather than starting empty. The offer can be shown
    on a turn that already collected something, either recalled from memory or
    mentioned in passing ("we're staying at the Fairmont, plan me a day"), and
    an empty state here would throw it away and then ask for it again.
    """
    known = state or {}
    return {
        "reply": ("Happy to help you plan a day. Two ways to do it:\n\n"
                  "1. Fill out the form yourself, and I'll take you there.\n"
                  "2. Plan through chat, and I'll fill the form in for you as "
                  "we talk.\n\nWhich would you prefer?"),
        "state": {"stage": STAGE_OFFERED,
                  "form": known.get("form") or dict(DEFAULTS),
                  "found": sorted(known.get("found") or []),
                  "skipped": sorted(known.get("skipped") or []),
                  "remembered": sorted(known.get("remembered") or []),
                  "recalled": known.get("recalled") or {},
                  "asked_extras": known.get("asked_extras", False),
                  "asking": None},
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
    """The whole form, for the parent to check before it is handed over.

    CHANGED_CHOICE is offered whenever memory contributed, because a recalled
    value is the one kind the parent was never asked about and so has no other
    way to retract.
    """
    found, _skipped, remembered = _answered(state)
    choices = [CONFIRM_CHOICE]
    if remembered:
        choices.append(CHANGED_CHOICE)
    return {
        "reply": _summarise(state["form"], found, remembered,
                            state.get("recalled")),
        "state": {**state, "stage": STAGE_CONFIRMING, "asking": None},
        "choices": choices,
    }


def _prefaced(reply: dict) -> dict:
    """Show what memory supplied, before whatever is being asked.

    This used to say only that memory had contributed, on the reasoning that the
    summary at the end itemises everything and formatting the same values twice
    is two places to disagree. That was wrong: it left the assistant claiming to
    remember a parent's day with no way to see what it thought it knew, several
    turns before the summary. The shared renderer solves the duplication without
    the claim going unverified.

    The correction is offered here too, not only at the summary, because a
    recalled value is the one kind the parent was never asked about, so the turn
    that reveals it is the turn they need to be able to reject it.
    """
    state = reply.get("state") or {}
    found, _skipped, remembered = _answered(state)
    blocks = _recalled_blocks(state.get("form") or {}, remembered - found,
                              state.get("recalled"))
    if not blocks:
        return reply
    body = "\n\n".join([RECALLED_PREFACE, *blocks,
                        _recalled_source(remembered - found), reply["reply"]])
    return {**reply, "reply": body,
            "choices": [*(reply.get("choices") or []), CHANGED_CHOICE]}


def _collect(message: str, state: dict) -> dict:
    """One collecting turn: extract, merge, then ask, or move on."""
    form = state.get("form") or dict(DEFAULTS)
    found, skipped, remembered = _answered(state)
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
        form, remembered = _forget(asking, form, remembered)

    # A recalled nap the parent has just denied. They were never asked about it,
    # since memory answered the question, so the decline above cannot fire and
    # without this the summary would print a nap they said does not happen and
    # post it to /plan. Matched on the nap phrases only, never on a bare "no",
    # which belongs to whatever question was actually asked.
    if "naps" in remembered and any(
            phrase in message.lower() for phrase in _NO_NAP):
        skipped.add("naps")
        form, remembered = _forget("naps", form, remembered)

    carried = {"form": form, "found": sorted(found), "skipped": sorted(skipped),
               "remembered": sorted(remembered),
               "recalled": state.get("recalled") or {},
               "asked_extras": state.get("asked_extras", False)}

    missing = _missing(found, skipped, remembered)
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
        # The extractor runs on the opening message too. It used to be skipped,
        # on the reasoning that a first message is only ever an intent and the
        # call would be wasted. When a parent opens with their whole day, that
        # assumption throws all of it away and makes them type it again.
        seed = _seed(context)
        blank = {"stage": STAGE_COLLECTING,
                 "form": {**DEFAULTS, **seed["form"]},
                 "found": [], "skipped": [],
                 "remembered": seed["remembered"],
                 "recalled": seed["recalled"],
                 "asking": None, "asked_extras": False}
        opened = _collect(message, blank)
        found, skipped, remembered = _answered(opened["state"] or {})

        if found & REQUIRED_FIELDS:
            # They described their day, which is choosing chat by doing it. Skip
            # the offer and carry on from what they said. ✕ Cancel is still on
            # every turn if they wanted the form after all.
            return _prefaced(opened)

        # Nothing about the day in the message, so it really was just "plan a
        # trip". Tested against the fields this conversation asks about rather
        # than `found` being empty, because the extractor still reports themes
        # and transit modes it inferred from nothing, and those must not be able
        # to skip the offer.
        if remembered and not _missing(found, skipped, remembered):
            return _same_as_last_time(opened["state"])
        return _start(opened["state"])

    stage = state.get("stage")

    # Offered on every turn that reveals what memory supplied, so it has to
    # parse at any stage rather than only at the summary. Checked before
    # dispatch for the same reason ✕ Cancel is: a workflow should not have to
    # remember to handle it.
    if state.get("remembered") and matches_only(message, _CHANGED):
        return _forget_all(state)

    if stage == STAGE_OFFERED:
        said = message.lower()
        if any(word in said for word in _FORM_CHOICE):
            return _to_form()
        # The one open question, not the first of five. `asking` is None
        # because no single field owns it, so nothing here can be declined by
        # accident. Carried from the offered state rather than rebuilt blank,
        # which used to throw away everything memory had supplied the moment
        # the parent chose chat.
        carried = {**state, "stage": STAGE_COLLECTING, "asking": None}
        found, skipped, remembered = _answered(state)
        missing = _missing(found, skipped, remembered)
        if not missing:
            return _same_as_last_time(carried)
        if remembered:
            # Asking everything at once would list things already on file, and
            # "I've filled in what I know" followed by "how old is your little
            # one" contradicts itself. A returning parent is asked for the gaps
            # instead, one at a time, which is the fallback flow anyway.
            return _prefaced(_ask(missing[0], carried))
        return {"reply": OPENING_QUESTION, "state": carried}

    if stage == STAGE_EXTRAS:
        if matches_only(message, _NOTHING):
            # Nothing to extract, and running the extractor on "no" would only
            # append it to the notes the parent is about to read.
            return _confirm(state)
        return _collect(message, {**state, "stage": STAGE_COLLECTING})

    if stage == STAGE_CONFIRMING:
        # Both offered as buttons on a recalled summary, so both have to parse:
        # the widget sends a button's own label back as the message.
        if any(word in message.lower() for word in _FORM_CHOICE):
            return _to_form()
        if matches_only(message, _YES):
            return {
                "reply": ("Great. I've got everything ready on the planning "
                          "page. Open it to check the form, or generate the day "
                          "straight away."),
                "state": None,
                "form": state["form"],
                # Provenance travels with the hand-off, so the workflow test
                # page can still say where each value came from on the one turn
                # that has no state left to read it from.
                "found": state.get("found") or [],
                "remembered": state.get("remembered") or [],
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
