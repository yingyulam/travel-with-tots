"""AI logic for Travel with Tots, routed through OpenRouter."""

import json
import os
import time

import requests
from dotenv import load_dotenv

from . import db, rag
from .data_loader import maps_url
from .itinerary import PACE_STOPS, combine_themes, resolve_themes

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "google/gemma-4-26b-a4b-it:free"

ALLOWED_CHAT_MODELS = {
    "google/gemma-4-26b-a4b-it:free",
    "openai/gpt-4o-mini",
    "anthropic/claude-sonnet-5",
}

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
WEBSITE_CHATBOT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "website_chatbot.txt")
PLANNER_PROMPT_PATH = os.path.join(PROMPTS_DIR, "planner.txt")
_WEBSITE_CHATBOT_TEMPLATE = None
_PLANNER_TEMPLATE = None


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


def _call_openrouter(messages: list[dict], model: str) -> tuple[str, dict, float]:
    """Returns (reply text, usage dict, elapsed seconds)."""
    api_key = os.environ["OPENROUTER_API_KEY"]

    start = time.perf_counter()
    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "usage": {"include": True}},
    )
    elapsed = time.perf_counter() - start
    response.raise_for_status()
    try:
        data = response.json()
        choices = data["choices"]
    except (ValueError, KeyError, IndexError) as e:
        # Some free-tier providers occasionally return a 200 with an empty
        # or malformed body under load -- treat that as "unavailable" too,
        # not as a real bug (it isn't caught by the KeyError-means-missing
        # -API-key handler in app.py, which this used to fall into).
        raise requests.exceptions.RequestException(
            f"OpenRouter returned an unusable response for {model}") from e

    usage = data.get("usage") or {}
    _print_usage_report(model, usage, elapsed)
    return choices[0]["message"]["content"], usage, elapsed


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
        nap_times = ", ".join(t for t in (ctx["nap_1"], ctx["nap_2"]) if t) or "none"
        feeding_times = ", ".join(t for t in (ctx["feeding_1"], ctx["feeding_2"]) if t) or "none"
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
            .replace("{feeding_times}", feeding_times)
            .replace("{pace}", ctx["pace"] or "balanced")
            .replace("{extra_notes}", ctx["extra_notes"] or "none")
            .replace("{dining}", ctx["dining"] or "dine_out")
            .replace("{accommodation}", ctx["accommodation"] or "not specified")
            .replace("{nap_notes}", ctx["nap_notes"] or "none")
        )
        return [{"role": "system", "content": prompt}]

    def _parse(self, text):
        text = text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            text = text[4:] if text.startswith("json") else text
        return json.loads(text)

    def _validate(self, parsed, valid_ids, pace):
        """Returns the stop list if every stop is well-formed and cites a
        real, distinct venue_id, or None otherwise. The whole response is
        rejected (and retried) if even one stop is invalid, rather than
        silently dropping it -- so a plan never ships with fewer stops than
        its pace requires just because one citation didn't check out.

        The expected count follows PACE_STOPS, but a thin candidate list
        caps it: with enough real venues, the count must match the pace
        exactly; with fewer venues than the pace calls for, anywhere from 1
        up to however many exist is valid -- a short plan is not an error."""
        stops = parsed.get("stops") if isinstance(parsed, dict) else None
        if not isinstance(stops, list):
            return None
        expected = PACE_STOPS.get(pace, 3)
        available = len(valid_ids)
        if available >= expected:
            if len(stops) != expected:
                return None
        elif not (1 <= len(stops) <= available):
            return None
        cleaned = []
        seen_ids = set()
        for stop in stops:
            if not isinstance(stop, dict):
                return None
            venue_id = stop.get("venue_id")
            if (venue_id not in valid_ids or venue_id in seen_ids
                    or not stop.get("time") or not stop.get("reason")):
                return None
            seen_ids.add(venue_id)
            cleaned.append({
                "time": stop["time"],
                "venue_id": venue_id,
                "reason": stop["reason"],
                "is_nap": bool(stop.get("is_nap")),
            })
        return cleaned

    def generate_plan_for_themes(self, theme_labels, *, destination, age_months,
                                  nap_1, nap_2, feeding_1, feeding_2, pace,
                                  wake_up, bedtime, features, transit=None,
                                  dining=None, accommodation="", nap_notes="",
                                  extra_notes=""):
        """One plan combining the given theme(s), on demand, so a parent only
        spends a model call when they actually ask for it. `theme_labels` is
        whichever theme checkboxes were selected (falls back to all three,
        "Mixed", if empty or none matched). Returns {"label", "blurb",
        "stops", "model", "response_time"}."""
        theme = combine_themes(resolve_themes(theme_labels))

        candidates = db.get_candidate_venues(
            destination, age_months, features, transit=transit, dining=dining)
        by_id = {v["id"]: v for v in candidates}
        if not by_id:
            raise PlanningAgentError(
                "No venues are available for this destination and age yet.")

        ctx = dict(destination=destination, age_months=age_months, nap_1=nap_1,
                   nap_2=nap_2, feeding_1=feeding_1, feeding_2=feeding_2,
                   pace=pace, wake_up=wake_up, bedtime=bedtime, dining=dining,
                   accommodation=accommodation, nap_notes=nap_notes,
                   extra_notes=extra_notes)
        messages = self._build_messages(theme, candidates, ctx)
        reply, usage, elapsed = _call_openrouter(messages, self.model)

        cleaned = None
        try:
            cleaned = self._validate(self._parse(reply), set(by_id), pace)
        except (ValueError, AttributeError):
            cleaned = None

        if cleaned is None:
            # One corrective retry: show the model its own bad reply.
            expected = min(PACE_STOPS.get(pace, 3), len(by_id))
            retry_messages = messages + [
                {"role": "assistant", "content": reply},
                {"role": "user", "content": (
                    "That response was not valid. Reply again with ONLY strict "
                    f"JSON: {{\"stops\": [...]}}, {expected} stop(s), each a "
                    "distinct venue_id taken from the candidate list above -- "
                    "never invent or repeat one.")},
            ]
            reply2, usage2, elapsed2 = _call_openrouter(retry_messages, self.model)
            elapsed += elapsed2
            try:
                cleaned = self._validate(self._parse(reply2), set(by_id), pace)
            except (ValueError, AttributeError):
                cleaned = None

        if cleaned is None:
            raise PlanningAgentError(f"Couldn't build a valid {theme['label']} plan.")

        stops = []
        for stop in cleaned:
            venue = dict(by_id[stop["venue_id"]])
            venue["maps_url"] = maps_url(venue["name"], venue["city"] or "Vancouver")
            stops.append({
                "time": stop["time"],
                "kind": "nap" if stop["is_nap"] else "activity",
                "venue": venue,
                "reason": stop["reason"],
            })

        return {"label": theme["label"], "blurb": theme["blurb"], "stops": stops,
                "model": self.model, "response_time": round(elapsed, 3)}
