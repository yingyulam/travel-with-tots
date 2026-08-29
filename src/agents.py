"""AI logic for Travel with Tots, routed through OpenRouter."""

import json
import os
import re
import time

import requests
from dotenv import load_dotenv

from . import db, rag
from .data_loader import is_nap_friendly, maps_url
from .interactions import SITUATION_LABELS
from .itinerary import (
    DEFAULT_LUNCH_TARGET_MIN, LUNCH_SEARCH_RADIUS_MIN, display_to_min,
    hhmm_to_min, min_to_display, stop_duration, transit_buffer_min,
    venue_open_for,
)

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
# OpenRouter's free auto-router. Free, supports structured outputs (which the
# plan adjuster and form extractor depend on, and which gemma does not
# advertise), and because it spreads across free models it survives the
# upstream rate limiting that takes a single pinned free model offline.
# Pin nvidia/nemotron-3-super-120b-a12b:free instead for reproducible output.
DEFAULT_MODEL = "openrouter/free"

ALLOWED_CHAT_MODELS = {
    "openrouter/free",
    "nvidia/nemotron-3-super-120b-a12b:free",
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
PLAN_ADJUST_PROMPT_PATH = os.path.join(PROMPTS_DIR, "plan_adjust.txt")
REPLAN_ADJUST_PROMPT_PATH = os.path.join(PROMPTS_DIR, "replan_adjust.txt")
_WEBSITE_CHATBOT_TEMPLATE = None
_PLAN_ADJUST_TEMPLATE = None
_REPLAN_ADJUST_TEMPLATE = None

# How far a stop's time may drift from the rule-based draft's own choice
# during an adjustment -- a nudge for flow, not a re-decision.
MAX_ADJUST_NUDGE_MIN = 60
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
    print(f"┌─ AI usage -- {model}")
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


def call_openrouter(messages: list[dict], model: str, response_format: dict | None = None) -> tuple[str, dict, float]:
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
    reply, _, _ = call_openrouter([{"role": "user", "content": message}], model)
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
    reply, usage, elapsed = call_openrouter(messages, model)
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
        # .get(): a candidate carries only the amenities somebody has reported,
        # so an unreported one is absent rather than False. Listing it as a
        # feature the venue lacks would be a claim nobody made.
        tags = [key for key in ("has_family_room", "has_nursing_room",
                                 "stroller_accessible") if v.get(key)]
        blocks.append(
            f"[venue_id {v['id']}] {v['name']} -- {v['type']}, {v['neighbourhood']}\n"
            f"Hours: {v['open_time'] or '?'}-{v['close_time'] or '?'}\n"
            f"Nap-friendly: {'yes' if is_nap_friendly(v) else 'no'} | "
            f"Can eat here: {'yes' if v.get('can_eat') else 'no'}\n"
            f"Features: {', '.join(tags) or 'none'}")
    return "\n\n".join(blocks)


# A reasoning model puts its working in the reply when the provider does not
# split it out, and some models add a line of prose either side of the object
# even under a strict schema. Both arrive as content that is not quite JSON.
_THINK_BLOCK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.DOTALL)


def parse_json_reply(text: str):
    """Parse a model's reply as JSON, tolerating the wrappers models put
    around it: a ``` fence anywhere in the reply, a reasoning model's <think>
    block, and prose either side of the object.

    Raises ValueError naming what came back instead. The message matters: a
    bare "that wasn't valid JSON" costs a slow live repro to diagnose, and an
    empty reply (all the tokens went to reasoning) needs a different fix from
    a reply with unparseable content in it.
    """
    if not text or not text.strip():
        raise ValueError("the model returned an empty reply")

    cleaned = _THINK_BLOCK_RE.sub("", text).strip()
    fenced = _FENCE_RE.search(cleaned)
    if fenced:
        cleaned = fenced.group(1).strip()
    if not cleaned:
        raise ValueError("the reply was only reasoning, with no answer in it")

    try:
        return json.loads(cleaned)
    except ValueError:
        pass

    # Prose either side of the object, so take the outermost braces.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise ValueError(f"no JSON object in the reply: {cleaned[:200]!r}")
    try:
        return json.loads(cleaned[start:end + 1])
    except ValueError as e:
        raise ValueError(f"unparseable JSON in the reply: {cleaned[:200]!r}") from e


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
            if target["kind"] == "nap" and not is_nap_friendly(candidate):
                return None, (
                    f"venue_id {new_venue_id!r} isn't nap-friendly, needed for "
                    f"the nap stop at {name!r}.")
            seen_new_ids.add(new_venue_id)

        if new_time:
            try:
                new_min = display_to_min(new_time)
            except (ValueError, IndexError):
                return None, f"{new_time!r} isn't a recognizable time."
            # The replan path knows what time it is; the planning path does
            # not, hence the .get. Without this the model could move the next
            # stop to before now, and the re-sort in _apply_plan_edits would
            # then file it among the stops already done.
            if ctx.get("current_time"):
                now_min = hhmm_to_min(ctx["current_time"])
                if new_min <= now_min:
                    return None, (
                        f"{name!r} moved to {new_time}, which is at or before the "
                        f"current time {min_to_display(now_min)}.")
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


def _call_and_validate_edits(messages: list[dict], model: str, stops: list,
                              by_id: dict, ctx: dict, error_cls: type[Exception]):
    """Calls OpenRouter with the edits schema, validates the reply against
    `stops` (retrying once, with the model's own invalid reply and the
    specific rule it broke, on failure), and raises `error_cls` if neither
    attempt produces a valid edit list. Returns (edits, elapsed_seconds,
    input_tokens, output_tokens). Shared by adjust_plan and adjust_replan,
    which only differ in which stops the edits are validated against and
    which error to raise."""
    reply, usage, elapsed = call_openrouter(messages, model, PLAN_EDITS_RESPONSE_FORMAT)
    input_tokens = usage.get("prompt_tokens")
    output_tokens = usage.get("completion_tokens")

    cleaned, error = None, None
    try:
        parsed = parse_json_reply(reply)
        edits = parsed.get("edits") if isinstance(parsed, dict) else None
        cleaned, error = _validate_plan_edits(edits, stops, by_id, ctx)
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
        reply2, usage2, elapsed2 = call_openrouter(retry_messages, model, PLAN_EDITS_RESPONSE_FORMAT)
        elapsed += elapsed2
        input_tokens = _sum_optional(input_tokens, usage2.get("prompt_tokens"))
        output_tokens = _sum_optional(output_tokens, usage2.get("completion_tokens"))
        try:
            parsed2 = parse_json_reply(reply2)
            edits2 = parsed2.get("edits") if isinstance(parsed2, dict) else None
            cleaned, error = _validate_plan_edits(edits2, stops, by_id, ctx)
        except (ValueError, AttributeError):
            cleaned, error = None, "That wasn't valid JSON."

    if cleaned is None:
        raise error_cls(f"Couldn't build a valid set of adjustments: {error}")

    return cleaned, elapsed, input_tokens, output_tokens


class PlanningAgent:
    """Smooths an already-valid rule-based day plan (src/itinerary.py's
    generate_plans) by proposing a short list of edits -- never a full
    regeneration. See adjust_plan()."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model if model in ALLOWED_CHAT_MODELS else DEFAULT_MODEL

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
            .replace("{transit}", ctx.get("transit") or "none")
            .replace("{accommodation}", ctx["accommodation"] or "not specified")
            .replace("{dining}", ctx["dining"] or "dine_out")
            .replace("{preferred_lunch_time}", ctx["preferred_lunch_time"] or "none")
            .replace("{transit_nap}", ctx.get("transit_nap") or "sometimes")
            .replace("{nap_notes}", ctx["nap_notes"] or "none")
            .replace("{extra_notes}", ctx["extra_notes"] or "none")
            .replace("{draft_stops}", _format_draft_stops_for_prompt(draft_stops))
            .replace("{candidate_venues}", _format_venue_candidates(candidates))
        )
        return [{"role": "system", "content": prompt}]

    def adjust_plan(self, draft_plan, *, destination, age_months, wake_up, bedtime,
                     stop_count, dining, naps=None, preferred_lunch_time="", nap_notes="",
                     extra_notes="", transit=None, accommodation="", features=None,
                     strict_schedule=False, transit_nap=""):
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
                   strict_schedule=strict_schedule, transit_nap=transit_nap,
                   activity_duration_min=stop_duration("activity"),
                   meal_duration_min=stop_duration("meal"),
                   transit_buffer_min=transit_buffer_min(transit))

        messages = self._build_adjust_messages(draft_stops, candidates, ctx)
        cleaned, elapsed, input_tokens, output_tokens = _call_and_validate_edits(
            messages, self.model, draft_stops, by_id, ctx, PlanningAgentError)

        return {"stops": _apply_plan_edits(draft_plan, cleaned, by_id), "edits": cleaned,
                "model": self.model, "response_time": round(elapsed, 3),
                "input_tokens": input_tokens, "output_tokens": output_tokens}


def _load_replan_adjust_template() -> str:
    with open(REPLAN_ADJUST_PROMPT_PATH) as f:
        return f.read()


def reload_replan_adjust_prompt() -> None:
    """Force the next ReplanningAgent.adjust_replan call to re-read the prompt template from disk."""
    global _REPLAN_ADJUST_TEMPLATE
    _REPLAN_ADJUST_TEMPLATE = None


# What the parent actually asked for, in words, for the two situations that
# carry a number or a list of interests. Neither reached the adjuster before: it was told
# "nap happened here" without being told the nap was three hours, so it was free
# to nudge stops back inside its 60-minute allowance and undo the request; and
# for a change of plan it never learned what was now wanted, so a "better fit"
# swap could put an outdoor venue back into a day moved indoors for rain.
def _duration_asked(situation: str, minutes) -> str:
    if situation == "nap_happened" and minutes:
        return f"The nap is expected to last about {minutes} minutes."
    if situation == "running_behind" and minutes:
        return (f"They are staying about {minutes} minutes longer than planned, "
                "and the draft has already slid the rest of the day to match. "
                "Do not pull stops back to their original times.")
    return "They did not give a duration."


def _change_asked(situation: str, interest) -> str:
    if situation == "weather_rain":
        return ("It started raining, so the rest of the day has been moved under "
                "indoors. Any venue you swap in must work in the rain.")
    if situation == "change_interest" and interest:
        kinds = ", ".join(interest) if not isinstance(interest, str) else interest
        return (f"They now want {kinds}, and the draft has been changed to match. "
                "Any venue you swap in should be one of those kinds of place.")
    return "No change of plan was asked for."


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


class ReplanningAgentError(Exception):
    """Raised when the model's replanned response can't be validated, even
    after one retry, or when there are no candidate venues to ground it on."""


class ReplanningAgent:
    """Smooths an already-valid rule-based replan draft (interactions.replan)
    by proposing a short list of edits to the stops still ahead -- never a
    full regeneration. See adjust_replan()."""

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model if model in ALLOWED_CHAT_MODELS else DEFAULT_MODEL

    def _build_replan_adjust_messages(self, situation, kept, remaining, candidates, ctx):
        global _REPLAN_ADJUST_TEMPLATE
        if _REPLAN_ADJUST_TEMPLATE is None:
            _REPLAN_ADJUST_TEMPLATE = _load_replan_adjust_template()
        prompt = (
            _REPLAN_ADJUST_TEMPLATE
            .replace("{situation_label}", SITUATION_LABELS.get(situation, situation))
            .replace("{current_time}", ctx["current_time"] or "")
            .replace("{destination}", ctx["destination"] or "")
            .replace("{age_months}", str(ctx["age_months"]))
            .replace("{bedtime}", ctx["bedtime"] or "")
            .replace("{transit}", ctx.get("transit") or "none")
            .replace("{dining}", ctx["dining"] or "none")
            .replace("{nap_notes}", ctx.get("nap_notes") or "none")
            .replace("{extra_notes}", ctx.get("extra_notes") or "none")
            .replace("{duration_asked}", _duration_asked(situation, ctx.get("minutes")))
            .replace("{change_asked}", _change_asked(situation, ctx.get("interest")))
            .replace("{kept_stops}", _format_stops_for_prompt(kept))
            .replace("{remaining_stops}", _format_draft_stops_for_prompt(remaining))
            .replace("{candidate_venues}", _format_venue_candidates(candidates))
        )
        return [{"role": "system", "content": prompt}]

    def adjust_replan(self, draft_plan, *, current_time, destination, age_months,
                       features=None, transit=None, dining=None, bedtime=None,
                       nap_notes="", extra_notes="", situation="",
                       minutes=None, interest=None):
        """Given an already-valid rule-based replan draft, proposes a short
        list of edits (never a full regeneration) to the stops still ahead
        of `current_time`, mirroring PlanningAgent.adjust_plan() for the
        planning page. Stops at or before `current_time` ("kept") are never
        passed to the edit validator/applier, so they're structurally
        impossible to target -- no separate "locked stops" check needed.
        Returns {"stops", "edits", "model", "response_time", "input_tokens",
        "output_tokens"}. Never mutates `draft_plan`."""
        # "adjusted" only ever means "this round's AI adjuster touched this
        # stop" -- strip any leftover flag from an earlier round.
        stops = [{k: v for k, v in s.items() if k != "adjusted"}
                 for s in draft_plan.get("stops", [])]
        now = hhmm_to_min(current_time)
        kept = [s for s in stops if display_to_min(s["time"]) <= now]
        remaining = [s for s in stops if display_to_min(s["time"]) > now]

        near_neighbourhood = None
        for s in reversed(kept):
            if s.get("venue"):
                near_neighbourhood = s["venue"]["neighbourhood"]
                break

        used_names = {s["venue"]["name"] for s in stops if s.get("venue")}
        candidates = db.get_candidate_venues(
            destination, age_months, features, transit=transit, dining=dining,
            near_neighbourhood=near_neighbourhood)
        candidates = [v for v in candidates if v["name"] not in used_names]
        by_id = {v["id"]: v for v in candidates}

        ctx = dict(destination=destination, age_months=age_months,
                   current_time=current_time, bedtime=bedtime, dining=dining,
                   nap_notes=nap_notes, extra_notes=extra_notes, transit=transit,
                   minutes=minutes, interest=interest,
                   strict_schedule=False,
                   activity_duration_min=stop_duration("activity"),
                   meal_duration_min=stop_duration("meal"),
                   transit_buffer_min=transit_buffer_min(transit))

        messages = self._build_replan_adjust_messages(situation, kept, remaining, candidates, ctx)
        cleaned, elapsed, input_tokens, output_tokens = _call_and_validate_edits(
            messages, self.model, remaining, by_id, ctx, ReplanningAgentError)

        adjusted_remaining = _apply_plan_edits({"stops": remaining}, cleaned, by_id)
        new_stops = kept + adjusted_remaining
        new_stops.sort(key=lambda s: display_to_min(s["time"]))
        return {"stops": new_stops, "edits": cleaned,
                "model": self.model, "response_time": round(elapsed, 3),
                "input_tokens": input_tokens, "output_tokens": output_tokens}
