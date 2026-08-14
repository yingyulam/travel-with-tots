"""AI logic for Travel with Tots, routed through OpenRouter."""

import json
import os
import time

import requests
from dotenv import load_dotenv

from . import db, rag
from .data_loader import maps_url
from .interactions import (
    DEFAULT_NAP_LENGTH_MIN, FINISHED_EARLY_BUFFER, RUNNING_BEHIND_DELAY,
    SITUATION_LABELS,
)
from .itinerary import (
    DEFAULT_LUNCH_TARGET_MIN, LUNCH_DURATION_LABEL, LUNCH_SEARCH_RADIUS_MIN,
    MAX_MEAL_STOPS, combine_themes, display_to_min, hhmm_to_min,
    min_to_display, realistic_stop_count, resolve_themes, stop_duration,
    transit_buffer_min, venue_open_for,
)

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-26b-a4b-it:free"

ALLOWED_CHAT_MODELS = {
    "google/gemma-4-26b-a4b-it:free",
    "openai/gpt-4o-mini",
    "anthropic/claude-sonnet-5",
}

# Fail fast rather than hang if OpenRouter (or a queued free-tier model)
# never responds.
REQUEST_TIMEOUT_SECONDS = 60
# Free-tier models occasionally return a 200 with only anti-idle whitespace
# padding and no actual completion under load -- a transient, one-off
# condition worth one retry before giving up.
MAX_MALFORMED_BODY_RETRIES = 1

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
WEBSITE_CHATBOT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "website_chatbot.txt")
PLANNER_PROMPT_PATH = os.path.join(PROMPTS_DIR, "planner.txt")
REPLAN_DAY_PROMPT_PATH = os.path.join(PROMPTS_DIR, "replan_day.txt")
PLAN_ADJUST_PROMPT_PATH = os.path.join(PROMPTS_DIR, "plan_adjust.txt")
_WEBSITE_CHATBOT_TEMPLATE = None
_PLANNER_TEMPLATE = None
_REPLAN_DAY_TEMPLATE = None
_PLAN_ADJUST_TEMPLATE = None

# How far a stop's time may drift from the rule-based draft's own choice
# during an adjustment -- a nudge for flow, not a re-decision.
MAX_ADJUST_NUDGE_MIN = 45
# Bedtime and nap timing are targets the day can gently orbit, not walls,
# unless the parent said their schedule is strict.
SCHEDULE_OVERRUN_MIN = 30


def _print_usage_report(model: str, usage: dict | None, elapsed: float) -> None:
    usage = usage or {}
    prompt_tokens = usage.get("prompt_tokens", "n/a")
    completion_tokens = usage.get("completion_tokens", "n/a")
    total_tokens = usage.get("total_tokens", "n/a")
    cost = usage.get("cost")
    cost_str = f"${cost:.6f}" if isinstance(cost, (int, float)) else "n/a"

    width = 44
    print(f"┌─ AI usage — {model}")
    print(f"│  input tokens    {prompt_tokens}")
    print(f"│  output tokens   {completion_tokens}")
    print(f"│  total tokens    {total_tokens}")
    print(f"│  time            {elapsed:.2f}s")
    print(f"│  cost            {cost_str}")
    print("└" + "─" * width)


def _log_request_failure(model: str, detail: str) -> None:
    print(f"┌─ AI request failed, model: {model}")
    print(f"│  {detail}")
    print("└" + "─" * 44)


def _call_openrouter(messages: list[dict], model: str, response_format: dict | None = None) -> tuple[str, dict, float]:
    """Returns (reply text, usage dict, elapsed seconds). `response_format`,
    when given, is OpenRouter's json_schema structured-output shape -- makes
    schema-valid JSON the model's actual output contract instead of only a
    prompt instruction, so a caller's own parse/validate step catches fewer
    avoidable misses."""
    api_key = os.environ["OPENROUTER_API_KEY"]

    body = {"model": model, "messages": messages, "usage": {"include": True}}
    if response_format is not None:
        body["response_format"] = response_format

    malformed_error = None
    for attempt in range(MAX_MALFORMED_BODY_RETRIES + 1):
        start = time.perf_counter()
        try:
            response = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            elapsed = time.perf_counter() - start
            response.raise_for_status()
        except requests.exceptions.RequestException as e:
            detail = e.response.text[:300] if e.response is not None else str(e)
            _log_request_failure(model, detail)
            raise

        try:
            data = response.json()
            choices = data["choices"]
        except (ValueError, KeyError, IndexError) as e:
            # Some free-tier providers occasionally return a 200 with an empty
            # or malformed body under load -- treat that as "unavailable" too,
            # not as a real bug (it isn't caught by the KeyError-means-missing
            # -API-key handler in app.py, which this used to fall into), and
            # worth one immediate retry since it's usually a one-off.
            _log_request_failure(model, f"unusable response body ({e}): {response.text[:300]!r}")
            malformed_error = e
            continue

        usage = data.get("usage") or {}
        _print_usage_report(model, usage, elapsed)
        return choices[0]["message"]["content"], usage, elapsed

    raise requests.exceptions.RequestException(
        f"OpenRouter returned an unusable response for {model}") from malformed_error


def _sum_optional(a, b):
    """Add two token counts that may each be missing (None) -- None only
    when both are, since a retry's usage data is never guaranteed."""
    if a is None and b is None:
        return None
    return (a or 0) + (b or 0)


def ask(message: str, model: str = DEFAULT_MODEL) -> str:
    """Send a message to an OpenRouter-hosted model and return the reply text."""
    reply, _, _ = _call_openrouter([{"role": "user", "content": message}], model)
    return reply


def _load_website_chatbot_template() -> str:
    with open(WEBSITE_CHATBOT_PROMPT_PATH) as f:
        return f.read()


def reload_website_chatbot_prompt() -> None:
    """Force the next ask_website_chatbot call to re-read the prompt template from disk."""
    global _WEBSITE_CHATBOT_TEMPLATE
    _WEBSITE_CHATBOT_TEMPLATE = None


def _space_out_bullets(text: str) -> str:
    """Ensure a blank line before every '- ' bullet line. Models don't always
    follow the prompt's spacing instructions reliably, so this guarantees
    lists never render as one dense block regardless of model compliance."""
    lines = text.split("\n")
    spaced = []
    for line in lines:
        if line.strip().startswith("- ") and spaced and spaced[-1].strip() != "":
            spaced.append("")
        spaced.append(line)
    return "\n".join(spaced)


def _format_sources(sources: list[dict]) -> str:
    if not sources:
        return "No relevant information was found in the knowledge base."
    return "\n\n".join(
        f"[Source {s['index']}] ({s['section']}, similarity {s['score']:.2f})\n{s['text']}"
        for s in sources
    )


def ask_website_chatbot(
    message: str, model: str = DEFAULT_MODEL, history: list[dict] | None = None
) -> dict:
    """Answer a question about the Travel with Tots website, grounded only in
    the top chunks retrieved from the knowledge base for this question.
    Returns {"reply", "sources", "model", "response_time", "input_tokens",
    "output_tokens"}."""
    global _WEBSITE_CHATBOT_TEMPLATE
    if _WEBSITE_CHATBOT_TEMPLATE is None:
        _WEBSITE_CHATBOT_TEMPLATE = _load_website_chatbot_template()

    sources = rag.retrieve(message)
    system_prompt = _WEBSITE_CHATBOT_TEMPLATE.replace(
        "{retrieved_chunks}", _format_sources(sources))

    messages = (
        [{"role": "system", "content": system_prompt}]
        + (history or [])
        + [{"role": "user", "content": message}]
    )
    reply, usage, elapsed = _call_openrouter(messages, model)
    return {
        "reply": _space_out_bullets(reply),
        "sources": sources,
        "model": model,
        "response_time": round(elapsed, 3),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
    }


class PlanningAgentError(Exception):
    """Raised when the model's response can't be turned into a valid
    itinerary, even after one retry, or when there are no candidate venues
    to ground it on."""


def _load_planner_template() -> str:
    with open(PLANNER_PROMPT_PATH) as f:
        return f.read()


def reload_planner_prompt() -> None:
    """Force the next PlanningAgent call to re-read the prompt template from disk."""
    global _PLANNER_TEMPLATE
    _PLANNER_TEMPLATE = None


def _load_plan_adjust_template() -> str:
    with open(PLAN_ADJUST_PROMPT_PATH) as f:
        return f.read()


def reload_plan_adjust_prompt() -> None:
    """Force the next PlanningAgent.adjust_plan call to re-read the prompt template from disk."""
    global _PLAN_ADJUST_TEMPLATE
    _PLAN_ADJUST_TEMPLATE = None


def _format_venue_candidates(venues: list) -> str:
    if not venues:
        return "No venues matched this trip's destination, age, and features."
    blocks = []
    for v in venues:
        tags = [key for key in ("kid_friendly", "has_family_room", "has_nursing_room",
                                 "stroller_accessible") if v[key]]
        blocks.append(
            f"[venue_id {v['id']}] {v['name']} -- {v['category']} ({v['type']}), {v['neighbourhood']}\n"
            f"Hours: {v['open_time'] or '?'}-{v['close_time'] or '?'} | "
            f"Ages: {v['min_age_months']}-{v['max_age_months']} months\n"
            f"Nap-friendly: {'yes' if v['nap_friendly'] else 'no'} | "
            f"Can eat here: {'yes' if v['can_eat'] else 'no'}\n"
            f"Features: {', '.join(tags) or 'none'}")
    return "\n\n".join(blocks)


def _parse_json_reply(text: str):
    """Strip a ``` fence if present, then parse strict JSON. Shared by
    PlanningAgent and ReplanningAgent, which both expect the same
    {"stops": [...]} shape back from the model."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    return json.loads(text)


# OpenRouter structured-output schema for the {"stops": [...]} shape both
# PlanningAgent and ReplanningAgent expect back -- shared since the shape is
# identical, only the semantic checks in _clean_stops/_validate differ.
STOPS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "day_stops",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "stops": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "venue_id": {"type": "integer"},
                            "time": {"type": "string"},
                            "reason": {"type": "string"},
                            "is_nap": {"type": "boolean"},
                            "is_meal": {"type": "boolean"},
                        },
                        "required": ["venue_id", "time", "reason", "is_nap", "is_meal"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["stops"],
            "additionalProperties": False,
        },
    },
}


# OpenRouter structured-output schema for the {"edits": [...]} shape
# PlanningAgent.adjust_plan() expects back -- a short list of changes
# against an already-valid draft, not a full stops array.
PLAN_EDITS_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "plan_edits",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "current_venue_name": {"type": "string"},
                            "new_venue_id": {"type": ["integer", "null"]},
                            "new_time": {"type": ["string", "null"]},
                            "reason": {"type": "string"},
                        },
                        "required": ["current_venue_name", "new_venue_id", "new_time", "reason"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["edits"],
            "additionalProperties": False,
        },
    },
}


def _clean_stops(stops: list, valid_ids: set):
    """Shared structural checks for a "stops" JSON array: every stop cites a
    real, non-repeated venue_id, has a non-empty time/reason, and is never
    both is_nap and is_meal. Returns (cleaned, None), or (None, error)
    describing the first problem found. Shared by PlanningAgent and
    ReplanningAgent, which only diverge in what they check beyond this
    (stop-count ceiling vs. anchor-time floor, meal-cap baseline, stop-
    duration lookup for spacing)."""
    cleaned = []
    seen_ids = set()
    for stop in stops:
        if not isinstance(stop, dict):
            return None, "Every stop must be a JSON object."
        venue_id = stop.get("venue_id")
        if venue_id not in valid_ids:
            return None, f"venue_id {venue_id!r} is not in the candidate list."
        if venue_id in seen_ids:
            return None, f"venue_id {venue_id!r} is used more than once."
        if not stop.get("time") or not stop.get("reason"):
            return None, "Every stop needs a non-empty \"time\" and \"reason\"."
        is_nap, is_meal = bool(stop.get("is_nap")), bool(stop.get("is_meal"))
        if is_nap and is_meal:
            return None, (
                f"The stop at {stop['time']} has both \"is_nap\" and "
                "\"is_meal\" true -- a stop is never both.")
        seen_ids.add(venue_id)
        cleaned.append({
            "time": stop["time"],
            "venue_id": venue_id,
            "reason": stop["reason"],
            "is_nap": is_nap,
            "is_meal": is_meal,
        })
    return cleaned, None


def _check_transit_spacing(cleaned: list, transit_buffer_min: int, duration_fn):
    """Shared consecutive-stop spacing check: every stop must start no
    earlier than the previous stop's end plus the transit buffer. Assumes
    `cleaned` is already sorted by time. `duration_fn(stop)` returns that
    stop's occupied minutes -- callers supply their own since PlanningAgent
    looks up a matched nap's real duration while ReplanningAgent uses a
    flat activity/meal duration; this only shares the loop, not that
    lookup, so each agent's existing behavior is unchanged. Returns an
    error string, or None if every stop is spaced correctly."""
    for prev, nxt in zip(cleaned, cleaned[1:]):
        prev_end = display_to_min(prev["time"]) + duration_fn(prev)
        next_start = display_to_min(nxt["time"])
        if next_start < prev_end + transit_buffer_min:
            return (
                f"{nxt['time']} (venue_id {nxt['venue_id']}) starts before "
                f"the previous stop ({prev['time']}, venue_id "
                f"{prev['venue_id']}, ends {min_to_display(prev_end)}) plus "
                f"the {transit_buffer_min}-minute travel buffer -- push it to at least "
                f"{min_to_display(prev_end + transit_buffer_min)}, or drop a stop.")
    return None


def _finalize_stops(cleaned: list, by_id: dict) -> list:
    """Turn validated {venue_id, time, reason, is_nap, is_meal} dicts into the
    {time, kind, venue, reason} shape the rest of the app expects. Shared by
    PlanningAgent and ReplanningAgent."""
    stops = []
    for stop in cleaned:
        venue = dict(by_id[stop["venue_id"]])
        venue["maps_url"] = maps_url(venue["name"], venue["city"] or "Vancouver")
        kind = "meal" if stop["is_meal"] else "nap" if stop["is_nap"] else "activity"
        stops.append({
            "time": stop["time"],
            "kind": kind,
            "venue": venue,
            "reason": stop["reason"],
        })
    return stops


def _format_draft_stops_for_prompt(stops: list) -> str:
    """Compact rendering of a rule-based draft's stops for the adjuster
    prompt -- distinct from _format_stops_for_prompt (ReplanningAgent's
    kept/remaining lists): a Plan-shaped stop has no venue_id to cite, so
    edits reference it by venue name instead."""
    if not stops:
        return "(no stops)"
    lines = []
    for s in stops:
        venue = s.get("venue")
        name = venue["name"] if venue else s.get("kind", "stop")
        place = f", {venue['neighbourhood']}" if venue and venue.get("neighbourhood") else ""
        lines.append(f"- {s['time']}: {name} ({s.get('kind', 'activity')}{place}) -- {s.get('reason', '')}")
    return "\n".join(lines)


def _format_schedule_flexibility(strict: bool) -> str:
    if strict:
        return ("This family's wake-up and bedtime are strict: never place or move a "
                "stop before wake-up or past bedtime, even briefly.")
    return (
        "This family's wake-up and bedtime have some flexibility: a stop may run a "
        f"little before wake-up or past bedtime (up to about {SCHEDULE_OVERRUN_MIN} "
        "minutes) if it clearly improves the day, but don't drift far.")


def _plan_stop_duration(stop: dict, ctx: dict) -> int:
    """Minutes this Plan-shaped stop ({time, kind, venue, reason}) occupies,
    mirroring PlanningAgent._stop_duration's per-kind logic for the shape
    the rule-based draft and its edited result use."""
    if stop["kind"] == "meal":
        return ctx["meal_duration_min"]
    if stop["kind"] == "nap":
        naps = ctx.get("naps") or []
        if naps:
            start = display_to_min(stop["time"])
            nearest = min(naps, key=lambda n: abs(hhmm_to_min(n["start"]) - start))
            return nearest["duration_min"]
    return ctx["activity_duration_min"]


def _apply_plan_edits(draft_plan: dict, edits: list, by_id: dict) -> list:
    """Apply already-validated edits to a copy of the draft's stops. Never
    mutates `draft_plan`. Each edit's target stop (matched by its current
    venue name -- rule-based venues carry no numeric id) gets a new venue
    and/or a new time; its "kind" is always preserved, edits change what
    or when a stop is, never what kind of stop it is."""
    stops = [dict(s) for s in draft_plan.get("stops", [])]
    index_by_name = {s["venue"]["name"]: i for i, s in enumerate(stops) if s.get("venue")}
    for edit in edits:
        idx = index_by_name.get(edit["current_venue_name"])
        if idx is None:
            continue
        stop = dict(stops[idx])
        if edit.get("new_venue_id") is not None:
            venue = dict(by_id[edit["new_venue_id"]])
            venue["maps_url"] = maps_url(venue["name"], venue.get("city") or "Vancouver")
            stop["venue"] = venue
        if edit.get("new_time"):
            stop["time"] = edit["new_time"]
        stop["reason"] = edit.get("reason") or stop["reason"]
        stop["adjusted"] = True
        stops[idx] = stop
    stops.sort(key=lambda s: display_to_min(s["time"]))
    return stops


def _validate_plan_edits(edits, draft_stops: list, by_id: dict, ctx: dict):
    """Returns (edits, None) if every edit is well-formed and the resulting
    day stays realistic, or (None, error) describing the first problem
    found. Unlike PlanningAgent/ReplanningAgent._validate, this checks
    EDITS against an already-valid draft, not a full plan built from
    scratch: each edit must reference a real stop by its current venue
    name, may change venue and/or time but never a stop's kind, and
    nudges must stay small. Bedtime and nap timing may run over by
    SCHEDULE_OVERRUN_MIN unless the parent said their schedule is strict;
    the meal count, venue open hours, and travel buffer never flex. An
    empty "edits" array is valid -- an unchanged draft is not an error."""
    if not isinstance(edits, list):
        return None, "\"edits\" must be a JSON array."
    if not edits:
        return [], None

    draft_by_name = {s["venue"]["name"]: s for s in draft_stops if s.get("venue")}
    cleaned = []
    seen_targets = set()
    seen_new_ids = set()
    for edit in edits:
        if not isinstance(edit, dict):
            return None, "Every edit must be a JSON object."
        name = edit.get("current_venue_name")
        target = draft_by_name.get(name)
        if target is None:
            return None, f"{name!r} is not a venue in the current draft."
        if name in seen_targets:
            return None, f"{name!r} is targeted by more than one edit."
        seen_targets.add(name)

        new_venue_id = edit.get("new_venue_id")
        new_time = edit.get("new_time")
        reason = edit.get("reason")
        if new_venue_id is None and not new_time:
            return None, f"The edit for {name!r} changes nothing -- omit it instead."
        if not reason:
            return None, f"The edit for {name!r} needs a non-empty \"reason\"."

        if new_venue_id is not None:
            if new_venue_id not in by_id:
                return None, f"venue_id {new_venue_id!r} is not in the candidate list."
            if new_venue_id in seen_new_ids:
                return None, f"venue_id {new_venue_id!r} is used by more than one edit."
            candidate = by_id[new_venue_id]
            if target["kind"] == "meal" and not candidate.get("can_eat"):
                return None, (
                    f"venue_id {new_venue_id!r} isn't a venue where a meal is "
                    f"possible, needed for the meal stop at {name!r}.")
            if target["kind"] == "nap" and not candidate.get("nap_friendly"):
                return None, (
                    f"venue_id {new_venue_id!r} isn't nap-friendly, needed for "
                    f"the nap stop at {name!r}.")
            seen_new_ids.add(new_venue_id)

        if new_time:
            try:
                new_min = display_to_min(new_time)
            except (ValueError, IndexError):
                return None, f"{new_time!r} isn't a recognizable time."
            original_min = display_to_min(target["time"])
            if abs(new_min - original_min) > MAX_ADJUST_NUDGE_MIN:
                return None, (
                    f"{name!r} moved from {target['time']} to {new_time}, more than "
                    f"the {MAX_ADJUST_NUDGE_MIN}-minute nudge an adjustment is allowed to make.")
            if target["kind"] == "meal":
                lunch_target = (hhmm_to_min(ctx["preferred_lunch_time"])
                                 if ctx.get("preferred_lunch_time") else DEFAULT_LUNCH_TARGET_MIN)
                if abs(new_min - lunch_target) > LUNCH_SEARCH_RADIUS_MIN:
                    return None, (
                        f"{new_time} is outside a sensible lunch window around "
                        f"{min_to_display(lunch_target)}.")

        cleaned.append({"current_venue_name": name, "new_venue_id": new_venue_id,
                         "new_time": new_time, "reason": reason})

    resulting = _apply_plan_edits({"stops": draft_stops}, cleaned, by_id)

    # These checks only hold edits accountable for the region they actually
    # touched -- the draft is already valid everywhere else by construction,
    # so an edit shouldn't get rejected for a pre-existing condition
    # elsewhere in the day that it never came near.
    overrun = 0 if ctx.get("strict_schedule") else SCHEDULE_OVERRUN_MIN
    bedtime_min = hhmm_to_min(ctx["bedtime"]) if ctx.get("bedtime") else None
    if bedtime_min is not None and resulting and resulting[-1].get("adjusted"):
        last = resulting[-1]
        last_end = display_to_min(last["time"]) + _plan_stop_duration(last, ctx)
        if last_end > bedtime_min + overrun:
            return None, (
                f"The edited day now ends at {min_to_display(last_end)}, too far "
                f"past bedtime ({ctx['bedtime']}) even with some flexibility.")

    buffer = ctx["transit_buffer_min"]
    for prev, nxt in zip(resulting, resulting[1:]):
        if not (prev.get("adjusted") or nxt.get("adjusted")):
            continue
        prev_end = display_to_min(prev["time"]) + _plan_stop_duration(prev, ctx)
        next_start = display_to_min(nxt["time"])
        if next_start < prev_end + buffer:
            return None, (
                f"After these edits, {nxt['venue']['name']} at {nxt['time']} starts "
                f"before the previous stop ends plus the {buffer}-minute travel buffer.")

    for stop in resulting:
        if not stop.get("adjusted") or not stop.get("venue"):
            continue
        start = display_to_min(stop["time"])
        dur = _plan_stop_duration(stop, ctx)
        if not venue_open_for(stop["venue"], start, dur):
            return None, f"{stop['venue']['name']} isn't open at {stop['time']}."

    return cleaned, None


class PlanningAgent:
    """Generates a day plan grounded only in real venues from the SQL venues
    table, drawing from one or more src.itinerary.THEMES entries (combined
    via combine_themes), matching the rule-based planner's themes so the two
    are directly comparable. Stop count follows the same PACE_STOPS mapping
    the rule-based planner uses, capped by however many real candidate
    venues actually exist."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model if model in ALLOWED_CHAT_MODELS else DEFAULT_MODEL

    def _build_messages(self, theme, candidates, ctx):
        global _PLANNER_TEMPLATE
        if _PLANNER_TEMPLATE is None:
            _PLANNER_TEMPLATE = _load_planner_template()
        nap_times = "; ".join(
            f"{n['start']} for about {n['duration_min']} min"
            for n in (ctx.get("naps") or [])
        ) or "none"
        prompt = (
            _PLANNER_TEMPLATE
            .replace("{theme_label}", theme["label"])
            .replace("{theme_blurb}", theme["blurb"])
            .replace("{candidate_venues}", _format_venue_candidates(candidates))
            .replace("{destination}", ctx["destination"] or "")
            .replace("{age_months}", str(ctx["age_months"]))
            .replace("{wake_up}", ctx["wake_up"] or "")
            .replace("{bedtime}", ctx["bedtime"] or "")
            .replace("{nap_times}", nap_times)
            .replace("{stop_count}", str(ctx["stop_count"]))
            .replace("{extra_notes}", ctx["extra_notes"] or "none")
            .replace("{dining}", ctx["dining"] or "dine_out")
            .replace("{accommodation}", ctx["accommodation"] or "not specified")
            .replace("{nap_notes}", ctx["nap_notes"] or "none")
            .replace("{transit}", ", ".join(ctx["transit"]) if ctx.get("transit") else "none")
            .replace("{transit_nap}", ctx["transit_nap"] or "sometimes")
            .replace("{preferred_lunch_time}", ctx["preferred_lunch_time"] or "none")
            .replace("{activity_duration_min}", str(ctx["activity_duration_min"]))
            .replace("{meal_duration_min}", str(ctx["meal_duration_min"]))
            .replace("{transit_buffer_min}", str(ctx["transit_buffer_min"]))
            .replace("{lunch_duration_label}", LUNCH_DURATION_LABEL)
        )
        return [{"role": "system", "content": prompt}]

    @staticmethod
    def _stop_duration(stop, ctx):
        """Minutes this stop occupies, mirroring itinerary.py's per-kind stop
        durations: the meal duration for a meal stop, the specific matched
        nap's own duration for a nap stop (nearest by start time), or the
        flat activity duration otherwise."""
        if stop["is_meal"]:
            return ctx["meal_duration_min"]
        if stop["is_nap"]:
            naps = ctx.get("naps") or []
            if naps:
                start = display_to_min(stop["time"])
                nearest = min(naps, key=lambda n: abs(hhmm_to_min(n["start"]) - start))
                return nearest["duration_min"]
        return ctx["activity_duration_min"]

    def _validate(self, parsed, valid_ids, ctx):
        """Returns (stops, None) if the response is well-formed and realistic,
        or (None, error) describing the first problem found. The whole
        response is rejected (and retried) if even one stop is invalid,
        rather than silently dropping it -- so a plan never ships with fewer
        stops than requested just because one citation didn't check out.

        The requested stop count (clamped to what's realistic for the
        child's age via realistic_stop_count, the same rule the rule-based
        planner uses) is a ceiling on activity/nap stops only, not a
        mandate: anywhere from 1 up to min(that ceiling, available
        candidates) is valid, whether the candidate list is thin or the
        model simply chose a shorter, more realistically-paced day --
        neither is an error. A dedicated meal stop (is_meal) is additional
        and never counts against that ceiling, mirroring the rule-based
        planner. Consecutive stops must also leave enough time for the
        previous stop's duration plus the transit buffer -- this is
        enforced here, not just requested in the prompt, since nothing else
        catches a model that ignores its own stated spacing rule."""
        stops = parsed.get("stops") if isinstance(parsed, dict) else None
        if not isinstance(stops, list) or not stops:
            return None, "\"stops\" must be a non-empty JSON array."

        cleaned, error = _clean_stops(stops, valid_ids)
        if cleaned is None:
            return None, error

        meal_stops = [s for s in cleaned if s["is_meal"]]
        max_meals = MAX_MEAL_STOPS if ctx["dining"] == "dine_out" else 0
        if len(meal_stops) > max_meals:
            return None, (
                f"Found {len(meal_stops)} \"is_meal\" stop(s); dining "
                f"{ctx['dining']!r} allows at most {max_meals}.")

        non_meal_count = len(cleaned) - len(meal_stops)
        cap = min(realistic_stop_count(ctx["stop_count"], ctx["age_months"]), len(valid_ids))
        if not (1 <= non_meal_count <= cap):
            return None, (
                f"{non_meal_count} non-meal stop(s) (the meal stop doesn't "
                f"count); requested {ctx['stop_count']} stop(s) allows 1 to {cap}.")

        cleaned.sort(key=lambda s: display_to_min(s["time"]))
        error = _check_transit_spacing(
            cleaned, ctx["transit_buffer_min"],
            lambda s: self._stop_duration(s, ctx))
        if error:
            return None, error

        return cleaned, None

    def generate_plan_for_themes(self, theme_labels, *, destination, age_months,
                                  naps=None, stop_count,
                                  wake_up, bedtime, features, transit=None,
                                  dining=None, accommodation="", nap_notes="",
                                  extra_notes="", transit_nap="",
                                  preferred_lunch_time=""):
        """One plan combining the given theme(s), on demand, so a parent only
        spends a model call when they actually ask for it. `theme_labels` is
        whichever theme checkboxes were selected (falls back to all three,
        "Mixed", if empty or none matched). Returns {"label", "blurb",
        "stops", "model", "response_time", "input_tokens", "output_tokens"}."""
        theme = combine_themes(resolve_themes(theme_labels))

        candidates = db.get_candidate_venues(
            destination, age_months, features, transit=transit, dining=dining)
        by_id = {v["id"]: v for v in candidates}
        if not by_id:
            raise PlanningAgentError(
                "No venues are available for this destination and age yet.")

        ctx = dict(destination=destination, age_months=age_months, naps=naps,
                   stop_count=stop_count, wake_up=wake_up, bedtime=bedtime, dining=dining,
                   accommodation=accommodation, nap_notes=nap_notes,
                   extra_notes=extra_notes, transit=transit, transit_nap=transit_nap,
                   preferred_lunch_time=preferred_lunch_time,
                   activity_duration_min=stop_duration("activity"),
                   meal_duration_min=stop_duration("meal"),
                   transit_buffer_min=transit_buffer_min(transit))
        messages = self._build_messages(theme, candidates, ctx)
        reply, usage, elapsed = _call_openrouter(messages, self.model, STOPS_RESPONSE_FORMAT)
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")

        cleaned, error = None, None
        try:
            cleaned, error = self._validate(_parse_json_reply(reply), set(by_id), ctx)
        except (ValueError, AttributeError):
            cleaned, error = None, "That wasn't valid JSON."

        if cleaned is None:
            # One corrective retry: show the model its own bad reply and the
            # specific rule it broke, so the single retry has a real shot.
            retry_messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": (
                    f"That response was invalid: {error} Reply again with ONLY "
                    "strict JSON in the same {\"stops\": [...]} shape, citing "
                    "distinct venue_id values from the candidate list above -- "
                    "never invent or repeat one.")},
            ]
            reply2, usage2, elapsed2 = _call_openrouter(retry_messages, self.model, STOPS_RESPONSE_FORMAT)
            elapsed += elapsed2
            input_tokens = _sum_optional(input_tokens, usage2.get("prompt_tokens"))
            output_tokens = _sum_optional(output_tokens, usage2.get("completion_tokens"))
            try:
                cleaned, error = self._validate(_parse_json_reply(reply2), set(by_id), ctx)
            except (ValueError, AttributeError):
                cleaned, error = None, "That wasn't valid JSON."

        if cleaned is None:
            raise PlanningAgentError(f"Couldn't build a valid {theme['label']} plan.")

        return {"label": theme["label"], "blurb": theme["blurb"],
                "stops": _finalize_stops(cleaned, by_id),
                "model": self.model, "response_time": round(elapsed, 3),
                "input_tokens": input_tokens, "output_tokens": output_tokens}

    def _build_adjust_messages(self, draft_stops, candidates, ctx):
        global _PLAN_ADJUST_TEMPLATE
        if _PLAN_ADJUST_TEMPLATE is None:
            _PLAN_ADJUST_TEMPLATE = _load_plan_adjust_template()
        prompt = (
            _PLAN_ADJUST_TEMPLATE
            .replace("{destination}", ctx["destination"] or "")
            .replace("{age_months}", str(ctx["age_months"]))
            .replace("{wake_up}", ctx["wake_up"] or "")
            .replace("{bedtime}", ctx["bedtime"] or "")
            .replace("{schedule_flexibility}", _format_schedule_flexibility(ctx["strict_schedule"]))
            .replace("{stop_count}", str(ctx["stop_count"]))
            .replace("{transit}", ", ".join(ctx["transit"]) if ctx.get("transit") else "none")
            .replace("{accommodation}", ctx["accommodation"] or "not specified")
            .replace("{dining}", ctx["dining"] or "dine_out")
            .replace("{preferred_lunch_time}", ctx["preferred_lunch_time"] or "none")
            .replace("{nap_notes}", ctx["nap_notes"] or "none")
            .replace("{extra_notes}", ctx["extra_notes"] or "none")
            .replace("{draft_stops}", _format_draft_stops_for_prompt(draft_stops))
            .replace("{candidate_venues}", _format_venue_candidates(candidates))
        )
        return [{"role": "system", "content": prompt}]

    def adjust_plan(self, draft_plan, *, destination, age_months, wake_up, bedtime,
                     stop_count, dining, naps=None, preferred_lunch_time="", nap_notes="",
                     extra_notes="", transit=None, accommodation="", features=None,
                     strict_schedule=False):
        """Given an already-valid rule-based draft, proposes a short list of
        edits (never a full regeneration) that smooth the day's flow and/or
        apply nap_notes/extra_notes, then applies them. Returns
        {"stops", "edits", "model", "response_time", "input_tokens",
        "output_tokens"}. Never mutates `draft_plan`."""
        draft_stops = draft_plan.get("stops", [])
        used_names = {s["venue"]["name"] for s in draft_stops if s.get("venue")}
        candidates = db.get_candidate_venues(
            destination, age_months, features, transit=transit, dining=dining)
        candidates = [v for v in candidates if v["name"] not in used_names]
        by_id = {v["id"]: v for v in candidates}

        ctx = dict(destination=destination, age_months=age_months, wake_up=wake_up,
                   bedtime=bedtime, stop_count=stop_count, dining=dining, naps=naps,
                   preferred_lunch_time=preferred_lunch_time, nap_notes=nap_notes,
                   extra_notes=extra_notes, transit=transit, accommodation=accommodation,
                   strict_schedule=strict_schedule,
                   activity_duration_min=stop_duration("activity"),
                   meal_duration_min=stop_duration("meal"),
                   transit_buffer_min=transit_buffer_min(transit))

        messages = self._build_adjust_messages(draft_stops, candidates, ctx)
        reply, usage, elapsed = _call_openrouter(messages, self.model, PLAN_EDITS_RESPONSE_FORMAT)
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")

        cleaned, error = None, None
        try:
            parsed = _parse_json_reply(reply)
            edits = parsed.get("edits") if isinstance(parsed, dict) else None
            cleaned, error = _validate_plan_edits(edits, draft_stops, by_id, ctx)
        except (ValueError, AttributeError):
            cleaned, error = None, "That wasn't valid JSON."

        if cleaned is None:
            retry_messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": (
                    f"That response was invalid: {error} Reply again with ONLY "
                    "strict JSON in the same {\"edits\": [...]} shape, or an "
                    "empty \"edits\" list if nothing actually needs to change.")},
            ]
            reply2, usage2, elapsed2 = _call_openrouter(retry_messages, self.model, PLAN_EDITS_RESPONSE_FORMAT)
            elapsed += elapsed2
            input_tokens = _sum_optional(input_tokens, usage2.get("prompt_tokens"))
            output_tokens = _sum_optional(output_tokens, usage2.get("completion_tokens"))
            try:
                parsed2 = _parse_json_reply(reply2)
                edits2 = parsed2.get("edits") if isinstance(parsed2, dict) else None
                cleaned, error = _validate_plan_edits(edits2, draft_stops, by_id, ctx)
            except (ValueError, AttributeError):
                cleaned, error = None, "That wasn't valid JSON."

        if cleaned is None:
            raise PlanningAgentError(f"Couldn't build a valid set of adjustments: {error}")

        return {"stops": _apply_plan_edits(draft_plan, cleaned, by_id), "edits": cleaned,
                "model": self.model, "response_time": round(elapsed, 3),
                "input_tokens": input_tokens, "output_tokens": output_tokens}


def _load_replan_day_template() -> str:
    with open(REPLAN_DAY_PROMPT_PATH) as f:
        return f.read()


def reload_replan_day_prompt() -> None:
    """Force the next ReplanningAgent call to re-read the prompt template from disk."""
    global _REPLAN_DAY_TEMPLATE
    _REPLAN_DAY_TEMPLATE = None


def _format_stops_for_prompt(stops: list) -> str:
    """Compact, human-readable rendering of a stop list for prompt context
    (kept or originally-planned remaining stops) -- distinct from
    _format_venue_candidates, which renders the citable candidate list."""
    if not stops:
        return "(none)"
    lines = []
    for s in stops:
        name = s["venue"]["name"] if s.get("venue") else s.get("kind", "stop")
        lines.append(f"- {s['time']}: {name} ({s.get('kind', 'activity')}) -- {s.get('reason', '')}")
    return "\n".join(lines)


def _format_theme_hint(theme: str | None) -> str:
    """A soft, non-mandatory theme-biasing line for "weather_rain"/
    "change_theme" -- same spirit as planner.txt's theme bias, not a hard
    filter. Empty string (no hint at all) for every other situation."""
    if not theme:
        return ""
    matched = resolve_themes([theme])
    if len(matched) != 1:
        return ""
    t = matched[0]
    return (f"Theme for the rest of the day: {t['label']} -- {t['blurb']} "
            "Bias remaining-stop venue choices toward this theme where a "
            "good candidate exists in the list below; don't force a poor "
            "fit or drop a stop just to match it.\n")


class ReplanningAgentError(Exception):
    """Raised when the model's replanned response can't be validated, even
    after one retry, or when there are no candidate venues to ground it on."""


class ReplanningAgent:
    """AI-backed alternative to interactions.replan(): re-decides only the
    stops still ahead of current_time, given a situation, grounded in venues
    freshly queried near the last kept stop. Never mutates current_plan --
    always returns a brand-new plan dict for the caller to store separately."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model if model in ALLOWED_CHAT_MODELS else DEFAULT_MODEL

    def _build_messages(self, situation, kept, remaining, candidates, ctx):
        global _REPLAN_DAY_TEMPLATE
        if _REPLAN_DAY_TEMPLATE is None:
            _REPLAN_DAY_TEMPLATE = _load_replan_day_template()
        prompt = (
            _REPLAN_DAY_TEMPLATE
            .replace("{situation}", situation)
            .replace("{situation_label}", SITUATION_LABELS.get(situation, situation))
            .replace("{theme_hint}", _format_theme_hint(ctx.get("theme")))
            .replace("{destination}", ctx["destination"] or "")
            .replace("{age_months}", str(ctx["age_months"]))
            .replace("{current_time}", ctx["current_time"] or "")
            .replace("{anchor_time}", min_to_display(ctx["anchor_min"]))
            .replace("{bedtime}", ctx["bedtime"] or "")
            .replace("{dining}", ctx["dining"] or "none")
            .replace("{already_meal_count}", str(ctx["already_meals"]))
            .replace("{nap_notes}", ctx.get("nap_notes") or "none")
            .replace("{extra_notes}", ctx.get("extra_notes") or "none")
            .replace("{kept_stops}", _format_stops_for_prompt(kept))
            .replace("{remaining_stops}", _format_stops_for_prompt(remaining))
            .replace("{candidate_venues}", _format_venue_candidates(candidates))
            .replace("{activity_duration_min}", str(ctx["activity_duration_min"]))
            .replace("{meal_duration_min}", str(ctx["meal_duration_min"]))
            .replace("{transit_buffer_min}", str(ctx["transit_buffer_min"]))
        )
        return [{"role": "system", "content": prompt}]

    def _validate(self, parsed, valid_ids, ctx):
        """Returns (stops, None) if well-formed and realistic, or (None,
        error). Shares PlanningAgent._validate's structural checks (real,
        non-duplicate venue_id, non-empty time/reason, is_nap/is_meal
        mutual exclusivity) via _clean_stops, and its transit-buffer spacing
        loop via _check_transit_spacing, plus one check specific to
        replanning (nothing may start before the situation's anchor time)
        and a meal cap adjusted for meals already in `kept`. Deliberately
        does NOT enforce a stop-count ceiling (meaningless for "however many
        stops remain") or bedtime/hours (prompt-only, same as PlanningAgent
        today). An empty "stops" array is valid -- a shorter remaining day
        is not an error."""
        stops = parsed.get("stops") if isinstance(parsed, dict) else None
        if not isinstance(stops, list):
            return None, "\"stops\" must be a JSON array."
        if not stops:
            return [], None

        cleaned, error = _clean_stops(stops, valid_ids)
        if cleaned is None:
            return None, error

        new_meals = sum(1 for s in cleaned if s["is_meal"])
        max_meals = MAX_MEAL_STOPS if ctx["dining"] == "dine_out" else 0
        if ctx["already_meals"] + new_meals > max_meals:
            return None, (
                f"Found {new_meals} new \"is_meal\" stop(s) on top of "
                f"{ctx['already_meals']} already today; dining "
                f"{ctx['dining']!r} allows at most {max_meals} total.")

        cleaned.sort(key=lambda s: display_to_min(s["time"]))
        if display_to_min(cleaned[0]["time"]) < ctx["anchor_min"]:
            return None, (
                f"{cleaned[0]['time']} (venue_id {cleaned[0]['venue_id']}) "
                f"starts before {min_to_display(ctx['anchor_min'])}, the "
                "earliest this situation allows.")

        error = _check_transit_spacing(
            cleaned, ctx["transit_buffer_min"],
            lambda s: ctx["meal_duration_min"] if s["is_meal"] else ctx["activity_duration_min"])
        if error:
            return None, error

        return cleaned, None

    def replan_day(self, situation, current_plan, *, current_time, destination,
                    age_months, features=None, transit=None, dining=None,
                    bedtime=None, minutes=None, theme=None,
                    nap_notes="", extra_notes=""):
        """Returns a NEW plan dict: {"label", "blurb", "from_time", "stops",
        "source", "model", "response_time", "input_tokens", "output_tokens"}.
        `current_plan` is never modified -- callers must store the result as
        an additional version, never in place of it. `theme` is the
        parent-picked target theme for "change_theme" (ignored otherwise --
        "weather_rain" always targets "Rainy-day"). `nap_notes`/`extra_notes`
        are the same free-text fields PlanningAgent uses, so a replan can
        also account for sleep habits or preferences the parent already
        described, not just the structured situation/theme."""
        stops = current_plan.get("stops", [])
        now = hhmm_to_min(current_time)
        kept = [dict(s) for s in stops if display_to_min(s["time"]) <= now]
        remaining = [s for s in stops if display_to_min(s["time"]) > now]

        near_neighbourhood = None
        for s in reversed(kept):
            if s.get("venue"):
                near_neighbourhood = s["venue"]["neighbourhood"]
                break

        candidates = db.get_candidate_venues(
            destination, age_months, features, transit=transit, dining=dining,
            near_neighbourhood=near_neighbourhood)
        used_names = {s["venue"]["name"] for s in kept if s.get("venue")}
        candidates = [v for v in candidates if v["name"] not in used_names]
        by_id = {v["id"]: v for v in candidates}
        if not by_id:
            raise ReplanningAgentError(
                "No venues are available near the current stop yet.")

        anchor_min = now
        if situation == "nap_happened":
            anchor_min = now + (int(minutes) if minutes else DEFAULT_NAP_LENGTH_MIN)
        elif situation == "running_behind":
            anchor_min = now + (int(minutes) if minutes else RUNNING_BEHIND_DELAY)
        elif situation == "finished_early":
            anchor_min = now + FINISHED_EARLY_BUFFER
        # skip_next: the freed time starts right now, no extra buffer.

        effective_theme = "Rainy-day" if situation == "weather_rain" else theme

        already_meals = sum(1 for s in kept if s.get("kind") == "meal")
        ctx = dict(destination=destination, age_months=age_months,
                   current_time=current_time, bedtime=bedtime, dining=dining,
                   anchor_min=anchor_min, already_meals=already_meals,
                   theme=effective_theme, nap_notes=nap_notes, extra_notes=extra_notes,
                   activity_duration_min=stop_duration("activity"),
                   meal_duration_min=stop_duration("meal"),
                   transit_buffer_min=transit_buffer_min(transit))

        messages = self._build_messages(situation, kept, remaining, candidates, ctx)
        reply, usage, elapsed = _call_openrouter(messages, self.model, STOPS_RESPONSE_FORMAT)
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")

        cleaned, error = None, None
        try:
            cleaned, error = self._validate(_parse_json_reply(reply), set(by_id), ctx)
        except (ValueError, AttributeError):
            cleaned, error = None, "That wasn't valid JSON."

        if cleaned is None:
            retry_messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": (
                    f"That response was invalid: {error} Reply again with ONLY "
                    "strict JSON in the same {\"stops\": [...]} shape, citing "
                    "distinct venue_id values from the candidate list above -- "
                    "never invent or repeat one.")},
            ]
            reply2, usage2, elapsed2 = _call_openrouter(retry_messages, self.model, STOPS_RESPONSE_FORMAT)
            elapsed += elapsed2
            input_tokens = _sum_optional(input_tokens, usage2.get("prompt_tokens"))
            output_tokens = _sum_optional(output_tokens, usage2.get("completion_tokens"))
            try:
                cleaned, error = self._validate(_parse_json_reply(reply2), set(by_id), ctx)
            except (ValueError, AttributeError):
                cleaned, error = None, "That wasn't valid JSON."

        if cleaned is None:
            raise ReplanningAgentError("Couldn't build a valid replanned day.")

        display_now = min_to_display(now)
        label = current_plan.get("label", "Plan")
        new_stops = kept + _finalize_stops(cleaned, by_id)
        new_stops.sort(key=lambda s: display_to_min(s["time"]))
        return {
            "label": f"{label} · AI replan from {display_now}",
            "blurb": (f"AI-replanned after “{SITUATION_LABELS.get(situation, situation)}” "
                      f"at {display_now}. Earlier stops kept as-is."),
            "from_time": display_now,
            "stops": new_stops,
            "source": "ai",
            "model": self.model,
            "response_time": round(elapsed, 3),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        }
