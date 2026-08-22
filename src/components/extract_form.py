"""Form extractor component: a parent's own words into the planning form.

Self-contained, one file per component (see /components). One job: turn a free
text description into the fields the /plan form collects, so a parent can
describe their day instead of filling in boxes.

The model proposes, form_helpers.read_form decides. Every value the model
returns goes through the same read_form the /plan route uses, so the clamps,
the age cap, and the nap ceiling are enforced by the real validator rather than
reimplemented here. That also means a model emitting nonsense degrades to a
sensible default instead of reaching the planner.

The model's choice vocabularies are constrained by the JSON schema rather than
checked afterwards, because read_form deliberately does not validate transit,
dining, features, or themes against the option lists.
"""

import os

from werkzeug.datastructures import MultiDict

from ..agents import _call_openrouter, _parse_json_reply
from ..data_loader import FEATURE_LABELS
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
# auto-router. The router advertises structured outputs but picks a different
# model per request, and measured live it honoured the schema only about half
# the time, failing outright on the rest. It is faster when it works, but this
# component's failure mode is "no form at all", so a slower model that always
# answers beats a fast one that sometimes cannot. Everything else keeps using
# the router.
EXTRACTOR_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"

DINING_KEYS = [key for key, _ in DINING_OPTIONS]
TRANSIT_NAP_KEYS = [key for key, _ in TRANSIT_NAP_OPTIONS]
FEATURE_KEYS = list(FEATURE_LABELS)
THEME_LABELS = [theme["label"] for theme in THEMES]

# A bare label like "Culture" doesn't tell a model that a museum belongs to it,
# so the prompt gets each theme's own blurb alongside its label. Derived from
# THEMES so the two can't drift.
THEME_CHOICES = ", ".join(
    f"{theme['label']} ({theme['blurb'].rstrip('.').lower()})" for theme in THEMES)
FEATURE_CHOICES = ", ".join(
    f"{key} ({label.lower()})" for key, label in FEATURE_LABELS.items())

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
    "features": _enum_array(FEATURE_KEYS),
    "themes": _enum_array(THEME_LABELS),
    "dining": {"type": ["string", "null"], "enum": [*DINING_KEYS, None]},
    "transit_nap": {"type": ["string", "null"], "enum": [*TRANSIT_NAP_KEYS, None]},
    "naps": {
        "type": ["array", "null"],
        "items": {
            "type": "object",
            "properties": {
                "start": {"type": "string"},
                "duration_min": {"type": "integer"},
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
        .replace("{feature_options}", FEATURE_CHOICES)
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
    "features": set(FEATURE_KEYS),
    "themes": set(THEME_LABELS),
    "dining": set(DINING_KEYS),
    "transit_nap": set(TRANSIT_NAP_KEYS),
}


def _allowed(field, value):
    """Whether a value is in `field`'s vocabulary. Unconstrained fields pass."""
    permitted = ALLOWED_VALUES.get(field)
    return permitted is None or value in permitted


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
            data.add("nap_duration", str(nap.get("duration_min", "")))
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

    Multi-choice fields (transit, features, themes) come back empty rather than
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
    reply, _usage, elapsed = _call_openrouter(
        messages, model, EXTRACTED_FORM_RESPONSE_FORMAT)

    try:
        extracted = _parse_json_reply(reply)
    except (ValueError, AttributeError) as e:
        raise FormExtractionError("That wasn't valid JSON.") from e
    if not isinstance(extracted, dict):
        raise FormExtractionError("Expected a JSON object of form fields.")

    data = _as_form_data(extracted)
    return {
        "form": read_form(data),
        "found": _found_fields(data),
        "model": model,
        "response_time": round(elapsed, 3),
    }
