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
    LUNCH_DURATION_LABEL, MAX_MEAL_STOPS, PACE_STOPS, combine_themes,
    display_to_min, hhmm_to_min, min_to_display, resolve_themes,
    stop_duration, transit_buffer_min,
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
_WEBSITE_CHATBOT_TEMPLATE = None
_PLANNER_TEMPLATE = None
_REPLAN_DAY_TEMPLATE = None


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


def _call_openrouter(messages: list[dict], model: str) -> tuple[str, dict, float]:
    """Returns (reply text, usage dict, elapsed seconds)."""
    api_key = os.environ["OPENROUTER_API_KEY"]

    malformed_error = None
    for attempt in range(MAX_MALFORMED_BODY_RETRIES + 1):
        start = time.perf_counter()
        try:
            response = requests.post(
                OPENROUTER_URL,
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages, "usage": {"include": True}},
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
            .replace("{pace}", ctx["pace"] or "balanced")
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

    def _parse(self, text):
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        return json.loads(text)

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
        stops than its pace requires just because one citation didn't check
        out.

        The pace's stop count is a ceiling on activity/nap stops only, not a
        mandate: anywhere from 1 up to min(pace count, available candidates)
        is valid, whether the candidate list is thin or the model simply
        chose a shorter, more realistically-paced day -- neither is an
        error. A dedicated meal stop (is_meal) is additional and never
        counts against that ceiling, mirroring the rule-based planner.
        Consecutive stops must also leave enough time for the previous
        stop's duration plus the transit buffer -- this is enforced here,
        not just requested in the prompt, since nothing else catches a
        model that ignores its own stated spacing rule."""
        stops = parsed.get("stops") if isinstance(parsed, dict) else None
        if not isinstance(stops, list) or not stops:
            return None, "\"stops\" must be a non-empty JSON array."

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

        meal_stops = [s for s in cleaned if s["is_meal"]]
        max_meals = MAX_MEAL_STOPS if ctx["dining"] == "dine_out" else 0
        if len(meal_stops) > max_meals:
            return None, (
                f"Found {len(meal_stops)} \"is_meal\" stop(s); dining "
                f"{ctx['dining']!r} allows at most {max_meals}.")

        non_meal_count = len(cleaned) - len(meal_stops)
        cap = min(PACE_STOPS.get(ctx["pace"], 3), len(valid_ids))
        if not (1 <= non_meal_count <= cap):
            return None, (
                f"{non_meal_count} non-meal stop(s) (the meal stop doesn't "
                f"count); pace {ctx['pace']!r} allows 1 to {cap}.")

        cleaned.sort(key=lambda s: display_to_min(s["time"]))
        buffer = ctx["transit_buffer_min"]
        for prev, nxt in zip(cleaned, cleaned[1:]):
            prev_end = display_to_min(prev["time"]) + self._stop_duration(prev, ctx)
            next_start = display_to_min(nxt["time"])
            if next_start < prev_end + buffer:
                return None, (
                    f"{nxt['time']} (venue_id {nxt['venue_id']}) starts before "
                    f"the previous stop ({prev['time']}, venue_id "
                    f"{prev['venue_id']}, ends {min_to_display(prev_end)}) plus "
                    f"the {buffer}-minute travel buffer -- push it to at least "
                    f"{min_to_display(prev_end + buffer)}, or drop a stop.")

        return cleaned, None

    def generate_plan_for_themes(self, theme_labels, *, destination, age_months,
                                  naps=None, pace,
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
                   pace=pace, wake_up=wake_up, bedtime=bedtime, dining=dining,
                   accommodation=accommodation, nap_notes=nap_notes,
                   extra_notes=extra_notes, transit=transit, transit_nap=transit_nap,
                   preferred_lunch_time=preferred_lunch_time,
                   activity_duration_min=stop_duration("activity"),
                   meal_duration_min=stop_duration("meal"),
                   transit_buffer_min=transit_buffer_min(transit))
        messages = self._build_messages(theme, candidates, ctx)
        reply, usage, elapsed = _call_openrouter(messages, self.model)
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")

        cleaned, error = None, None
        try:
            cleaned, error = self._validate(self._parse(reply), set(by_id), ctx)
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
            reply2, usage2, elapsed2 = _call_openrouter(retry_messages, self.model)
            elapsed += elapsed2
            input_tokens = _sum_optional(input_tokens, usage2.get("prompt_tokens"))
            output_tokens = _sum_optional(output_tokens, usage2.get("completion_tokens"))
            try:
                cleaned, error = self._validate(self._parse(reply2), set(by_id), ctx)
            except (ValueError, AttributeError):
                cleaned, error = None, "That wasn't valid JSON."

        if cleaned is None:
            raise PlanningAgentError(f"Couldn't build a valid {theme['label']} plan.")

        return {"label": theme["label"], "blurb": theme["blurb"],
                "stops": _finalize_stops(cleaned, by_id),
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
            .replace("{kept_stops}", _format_stops_for_prompt(kept))
            .replace("{remaining_stops}", _format_stops_for_prompt(remaining))
            .replace("{candidate_venues}", _format_venue_candidates(candidates))
            .replace("{activity_duration_min}", str(ctx["activity_duration_min"]))
            .replace("{meal_duration_min}", str(ctx["meal_duration_min"]))
            .replace("{transit_buffer_min}", str(ctx["transit_buffer_min"]))
        )
        return [{"role": "system", "content": prompt}]

    def _parse(self, text):
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        return json.loads(text)

    def _validate(self, parsed, valid_ids, ctx):
        """Returns (stops, None) if well-formed and realistic, or (None,
        error). Mirrors PlanningAgent._validate's structural checks (real,
        non-duplicate venue_id, non-empty time/reason, is_nap/is_meal
        mutual exclusivity, transit-buffer spacing between consecutive new
        stops), plus one check specific to replanning (nothing may start
        before the situation's anchor time) and a meal cap adjusted for
        meals already in `kept`. Deliberately does NOT enforce a pace
        ceiling (meaningless for "however many stops remain") or bedtime/
        hours (prompt-only, same as PlanningAgent today). An empty "stops"
        array is valid -- a shorter remaining day is not an error."""
        stops = parsed.get("stops") if isinstance(parsed, dict) else None
        if not isinstance(stops, list):
            return None, "\"stops\" must be a JSON array."
        if not stops:
            return [], None

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

        buffer = ctx["transit_buffer_min"]
        for prev, nxt in zip(cleaned, cleaned[1:]):
            prev_duration = (ctx["meal_duration_min"] if prev["is_meal"]
                             else ctx["activity_duration_min"])
            prev_end = display_to_min(prev["time"]) + prev_duration
            next_start = display_to_min(nxt["time"])
            if next_start < prev_end + buffer:
                return None, (
                    f"{nxt['time']} (venue_id {nxt['venue_id']}) starts before "
                    f"the previous stop ({prev['time']}, venue_id "
                    f"{prev['venue_id']}, ends {min_to_display(prev_end)}) plus "
                    f"the {buffer}-minute travel buffer -- push it to at least "
                    f"{min_to_display(prev_end + buffer)}, or drop a stop.")

        return cleaned, None

    def replan_day(self, situation, current_plan, *, current_time, destination,
                    age_months, features=None, transit=None, dining=None,
                    bedtime=None, minutes=None, theme=None):
        """Returns a NEW plan dict: {"label", "blurb", "from_time", "stops",
        "source", "model", "response_time", "input_tokens", "output_tokens"}.
        `current_plan` is never modified -- callers must store the result as
        an additional version, never in place of it. `theme` is the
        parent-picked target theme for "change_theme" (ignored otherwise --
        "weather_rain" always targets "Rainy-day")."""
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
                   theme=effective_theme,
                   activity_duration_min=stop_duration("activity"),
                   meal_duration_min=stop_duration("meal"),
                   transit_buffer_min=transit_buffer_min(transit))

        messages = self._build_messages(situation, kept, remaining, candidates, ctx)
        reply, usage, elapsed = _call_openrouter(messages, self.model)
        input_tokens = usage.get("prompt_tokens")
        output_tokens = usage.get("completion_tokens")

        cleaned, error = None, None
        try:
            cleaned, error = self._validate(self._parse(reply), set(by_id), ctx)
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
            reply2, usage2, elapsed2 = _call_openrouter(retry_messages, self.model)
            elapsed += elapsed2
            input_tokens = _sum_optional(input_tokens, usage2.get("prompt_tokens"))
            output_tokens = _sum_optional(output_tokens, usage2.get("completion_tokens"))
            try:
                cleaned, error = self._validate(self._parse(reply2), set(by_id), ctx)
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
