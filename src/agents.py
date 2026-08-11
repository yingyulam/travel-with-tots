"""AI logic for Travel with Tots, routed through OpenRouter."""

import os
import time

import requests
from dotenv import load_dotenv

from . import rag

load_dotenv()

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DEFAULT_MODEL = "openai/gpt-oss-20b:free"

ALLOWED_CHAT_MODELS = {
    "openai/gpt-oss-20b:free",
    "openai/gpt-4o-mini",
    "anthropic/claude-sonnet-5",
}

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
WEBSITE_CHATBOT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "website_chatbot.txt")
_WEBSITE_CHATBOT_TEMPLATE = None


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
    data = response.json()
    usage = data.get("usage") or {}
    _print_usage_report(model, usage, elapsed)
    return data["choices"][0]["message"]["content"], usage, elapsed


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
