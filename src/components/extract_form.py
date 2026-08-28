"""Form extractor component: a parent's own words into the planning form.

Self-contained, one file per component (see /components). One job: turn a free
text description into the fields the /plan form collects, so a parent can
describe their day instead of filling in boxes.

The model proposes, form_helpers.read_form decides. Every value the model
returns goes through the same read_form the /plan route uses, so the clamps,
the age cap, and the nap ceiling are enforced by the real validator rather than
reimplemented here. That also means a model emitting nonsense degrades to a
sensible default instead of reaching the planner.

read_form asks whether a value is well formed; `_grounded` asks whether the
parent said it. Both are needed, because a fabricated time is perfectly well
formed and in range.

The model's choice vocabularies are constrained by the JSON schema rather than
checked afterwards, because read_form deliberately does not validate transit,
dining or themes against the option lists.
"""

import os
import re

from werkzeug.datastructures import MultiDict

from ..agents import call_openrouter, parse_json_reply
from ..form_helpers import (
    DINING_OPTIONS,
    MAX_AGE_YEARS,
    MAX_NAPS,
    STOP_COUNT_FORM_MAX,
    STOP_COUNT_FORM_MIN,
    TRANSIT_NAP_OPTIONS,
    TRANSIT_OPTIONS,
    read_form,
)
from ..itinerary import THEMES

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "prompts")
EXTRACT_FORM_PROMPT_PATH = os.path.join(PROMPTS_DIR, "extract_form.txt")
_EXTRACT_FORM_TEMPLATE = None

# Pinned rather than using agents.DEFAULT_MODEL, which is OpenRouter's free
# auto-router: the router advertises structured outputs but picks a different
# model per request, and measured live it honoured the schema only about half
# the time.
#
# The pin was a free reasoning model until measurement replaced it. On the same
# description it spent 3.2k-4.5k tokens, mostly reasoning, over 25-75s, and
# found fewer fields than this model does in ~2s on ~130 tokens. Worse, when
# the account was near its free-tier ceiling the reasoning consumed the whole
# reply and `content` came back empty, so the extractor failed outright.
#
# So this is a paid non-reasoning model with real structured-output support. It
# costs about $0.0003 a call, which buys latency a parent will wait through and
# a result that does not change between identical requests.
#
# Both models used to invent a nap's length, because the schema required
# duration_min as a plain integer and strict mode left no way to say "they
# didn't say". It is nullable now and the assumption lives in
# form_helpers.ASSUMED_NAP_DURATION_MIN, which is deterministic work that was
# never the model's to do.
EXTRACTOR_MODEL = "openai/gpt-4o-mini"

DINING_KEYS = [key for key, _ in DINING_OPTIONS]
TRANSIT_NAP_KEYS = [key for key, _ in TRANSIT_NAP_OPTIONS]
THEME_LABELS = [theme["label"] for theme in THEMES]

# A bare label like "Culture" doesn't tell a model that a museum belongs to it,
# so the prompt gets each theme's own blurb alongside its label. Derived from
# THEMES so the two can't drift.
THEME_CHOICES = ", ".join(
    f"{theme['label']} ({theme['blurb'].rstrip('.').lower()})" for theme in THEMES)

# The form fields worth asking a model for. Excludes child_ids and
# plan_child_id (database ids the parent picks in the UI) and revise_feedback
# (internal UI state), none of which a description can supply.
TEXT_FIELDS = ("wake_up", "bedtime", "destination", "accommodation",
               "preferred_lunch_time", "nap_notes", "extra_notes")
COUNT_FIELDS = ("age_years", "age_months", "stop_count")


def _nullable(*types):
    """A strict-mode schema property: OpenRouter requires every field to be
    present and listed in `required`, so "not mentioned" has to be expressed
    as an explicit null rather than an omitted key."""
    return {"type": [*types, "null"]}


def _enum_array(values):
    return {"type": ["array", "null"], "items": {"type": "string", "enum": values}}


EXTRACTED_FORM_PROPERTIES = {
    **{field: _nullable("string") for field in TEXT_FIELDS},
    **{field: _nullable("integer") for field in COUNT_FIELDS},
    "strict_schedule": _nullable("boolean"),
    "transit": _enum_array(TRANSIT_OPTIONS),
    "themes": _enum_array(THEME_LABELS),
    "dining": {"type": ["string", "null"], "enum": [*DINING_KEYS, None]},
    "transit_nap": {"type": ["string", "null"], "enum": [*TRANSIT_NAP_KEYS, None]},
    "naps": {
        "type": ["array", "null"],
        "items": {
            "type": "object",
            "properties": {
                "start": {"type": "string"},
                # Nullable, so "they didn't say how long" is expressible. As a
                # plain integer the model had to invent a number to satisfy
                # strict mode, which is what it did: 15 minutes one run and an
                # hour the next, from a description that gave neither.
                "duration_min": _nullable("integer"),
            },
            "required": ["start", "duration_min"],
            "additionalProperties": False,
        },
    },
}

EXTRACTED_FORM_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "trip_form",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": EXTRACTED_FORM_PROPERTIES,
            "required": list(EXTRACTED_FORM_PROPERTIES),
            "additionalProperties": False,
        },
    },
}


class FormExtractionError(Exception):
    """Raised when the model's reply can't be read as a form at all."""


def _load_extract_form_template() -> str:
    with open(EXTRACT_FORM_PROMPT_PATH) as f:
        return f.read()


def reload_extract_form_prompt() -> None:
    """Force the next extract_form call to re-read the prompt from disk."""
    global _EXTRACT_FORM_TEMPLATE
    _EXTRACT_FORM_TEMPLATE = None


def _build_messages(description: str) -> list[dict]:
    global _EXTRACT_FORM_TEMPLATE
    if _EXTRACT_FORM_TEMPLATE is None:
        _EXTRACT_FORM_TEMPLATE = _load_extract_form_template()
    prompt = (
        _EXTRACT_FORM_TEMPLATE
        .replace("{description}", description)
        .replace("{max_naps}", str(MAX_NAPS))
        .replace("{max_age_years}", str(MAX_AGE_YEARS))
        .replace("{stop_count_min}", str(STOP_COUNT_FORM_MIN))
        .replace("{stop_count_max}", str(STOP_COUNT_FORM_MAX))
        .replace("{transit_options}", ", ".join(TRANSIT_OPTIONS))
        .replace("{dining_options}", ", ".join(DINING_KEYS))
        .replace("{transit_nap_options}", ", ".join(TRANSIT_NAP_KEYS))
        .replace("{theme_options}", THEME_CHOICES)
    )
    return [{"role": "system", "content": prompt}]


# The fixed vocabularies, kept here as well as in the schema on purpose. The
# schema tells the model what to choose from; this drops anything outside the
# list if it answers otherwise. Belt and braces, because strict-mode support
# for a nullable enum varies between providers, and because read_form
# deliberately does not validate these fields either, so nothing else would.
ALLOWED_VALUES = {
    "transit": set(TRANSIT_OPTIONS),
    "themes": set(THEME_LABELS),
    "dining": set(DINING_KEYS),
    "transit_nap": set(TRANSIT_NAP_KEYS),
}


def _allowed(field, value):
    """Whether a value is in `field`'s vocabulary. Unconstrained fields pass."""
    permitted = ALLOWED_VALUES.get(field)
    return permitted is None or value in permitted


# Digits, or the number words a parent writes instead of one: "up at seven",
# "one and a half", "a couple of places".
NUMBER_WORDS = frozenset((
    "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "half", "quarter", "couple", "few",
    "noon", "midday", "midnight"))

# Fields whose value can only have come from a number in the description. The
# prompt asks for each of these only from an explicit time or count.
NUMERIC_FIELDS = ("age_years", "age_months", "wake_up", "bedtime",
                  "preferred_lunch_time", "stop_count")

# Naps are grounded on sleep words rather than numbers, because the prompt
# deliberately allows a nap with no clock time ("naps after lunch" is 13:00).
SLEEP_WORDS = frozenset((
    "nap", "naps", "napping", "napped", "napper", "sleep", "sleeps",
    "sleeping", "slept", "asleep", "bed", "bedtime", "rest", "rests",
    "resting", "snooze", "siesta", "downtime"))

# Free text, which the prompt requires to be the parent's own wording moved
# into the right box, so an invented value shares no content word with it.
QUOTED_FIELDS = ("accommodation", "nap_notes", "extra_notes")

# Words too common to count as evidence that free text came from the parent.
_FILLER = frozenset((
    "a", "an", "and", "at", "be", "but", "for", "he", "her", "him", "his",
    "i", "in", "is", "it", "me", "my", "of", "on", "or", "our", "she",
    "the", "them", "they", "to", "us", "we", "with", "you", "your"))


def _words(text: str) -> set:
    """Lowercased word tokens, so "one" cannot be found inside "money"."""
    return set(re.findall(r"[a-z0-9]+", (text or "").lower()))


def _has_number(said: set) -> bool:
    return any(word.isdigit() or word in NUMBER_WORDS for word in said)


def _echoes(value: str, said: set) -> bool:
    """Whether free text is built from the parent's own words.

    Any overlap counts, deliberately. A stricter threshold would start
    dropping legitimate notes, and the prompt's first rule about these fields
    is that nothing the parent said may be lost. This only has to catch
    wholesale invention, which shares nothing.
    """
    mine = _words(value) - _FILLER
    return bool(mine) and bool(mine & said)


def _grounded(extracted: dict, description: str) -> dict:
    """Drop values the description cannot support.

    The model proposes and read_form validates, but validation only ever asked
    whether a value was well formed and in range, never whether the parent
    said it. A fabrication passes both.

    That gap was measured, not theorised. Asked to fill this form from the
    three words "Plan a trip", the pinned model returned up to ten non-null
    fields across repeated runs, and each was lifted from an example in the
    prompt: an age of 1 year 6 months from "My 18-month-old", a 13:30 nap from
    "naps at around 1:30 pm", accommodation "our hotel in downtown Vancouver"
    word for word. Downstream that reads as fields the parent supplied, which
    is worse than a default, because nobody checks a value they are told came
    from their own words.

    Only fields whose support is decidable from the text are checked. The
    vocabulary fields (transit, themes, dining, transit_nap) are
    legitimately inferred from words they do not share, "we'll drive" meaning
    car, so there is nothing here to compare them against. They stay the
    prompt's problem.
    """
    said = _words(description)
    has_number = _has_number(said)
    kept = dict(extracted)

    for field in NUMERIC_FIELDS:
        if kept.get(field) is not None and not has_number:
            kept[field] = None

    if kept.get("naps") and not (said & SLEEP_WORDS):
        kept["naps"] = None

    # A city named in the description, or nothing. A parent who gives only a
    # neighbourhood still plans in the one city the app covers, since that is
    # the default; it is the claim that they chose it that has to go.
    destination = kept.get("destination")
    if destination and destination.lower() not in (description or "").lower():
        kept["destination"] = None

    for field in QUOTED_FIELDS:
        if kept.get(field) and not _echoes(kept[field], said):
            kept[field] = None

    return kept


def _as_form_data(extracted: dict) -> MultiDict:
    """Shape the model's reply the way read_form reads a submitted form.

    read_form expects `.get`/`.getlist`, and takes naps as parallel
    nap_start/nap_duration lists rather than a list of objects, so the naps
    array is flattened here. Nulls are dropped so read_form falls back to its
    own defaults for anything the description didn't mention, and values
    outside a field's vocabulary are dropped rather than passed on.
    """
    data = MultiDict()
    for field, value in extracted.items():
        if value is None or field == "naps":
            continue
        if field == "strict_schedule":
            # read_form looks for a checkbox's literal "on".
            if value:
                data.add("strict_schedule", "on")
        elif isinstance(value, list):
            for item in value:
                if _allowed(field, item):
                    data.add(field, item)
        elif _allowed(field, value):
            data.add(field, str(value))

    for nap in (extracted.get("naps") or [])[:MAX_NAPS]:
        if nap.get("start"):
            data.add("nap_start", nap["start"])
            # Blank when they didn't say how long, so read_form applies the
            # assumed length. Kept as a paired entry rather than omitted
            # because read_form zips the two lists positionally.
            duration = nap.get("duration_min")
            data.add("nap_duration", "" if duration is None else str(duration))
    return data


def _found_fields(data: MultiDict) -> list:
    """Which fields the description actually supplied, read off the form data
    itself rather than recomputed from the reply. That way "found" cannot
    disagree with what was really filled in, and the UI can honestly separate
    these from fields that fell back to a default."""
    # nap_start/nap_duration are how read_form takes naps; report them as the
    # one "naps" field a reader recognises. Not nap_notes, which is its own.
    paired = {"nap_start", "nap_duration"}
    return sorted({"naps" if key in paired else key for key in data})


def extract_form(description: str, model: str = EXTRACTOR_MODEL) -> dict:
    """Read a parent's description into a validated planning form.

    Returns {"form", "found", "model", "response_time"}. `form` is ready to
    hand to plan_trip or to prefill /plan; `found` lists the fields the
    description actually supplied, so the UI can distinguish those from
    defaults. Raises FormExtractionError if the reply isn't usable JSON.

    Two things to know before wiring this into a prefilled form:

    Multi-choice fields (transit, themes) come back empty rather than
    at their DEFAULTS value when the description didn't mention them, because
    that is exactly what read_form returns for a submitted form with those
    boxes unchecked. Honest, but it means a prefill wanting the form's usual
    starting checkboxes should merge this over DEFAULTS rather than use it raw.

    And `form_helpers.resolve_plan_child` later overwrites age from a chosen
    child's date of birth, while /plan doesn't render the age inputs at all for
    a logged-in parent with children, so an extracted age only reaches the
    planner for a parent without saved children.
    """
    messages = _build_messages(description)
    reply, _usage, elapsed = call_openrouter(
        messages, model, EXTRACTED_FORM_RESPONSE_FORMAT)

    try:
        extracted = parse_json_reply(reply)
    except (ValueError, AttributeError) as e:
        raise FormExtractionError("That wasn't valid JSON.") from e
    if not isinstance(extracted, dict):
        raise FormExtractionError("Expected a JSON object of form fields.")

    data = _as_form_data(_grounded(extracted, description))
    return {
        "form": read_form(data),
        "found": _found_fields(data),
        "model": model,
        "response_time": round(elapsed, 3),
    }
