"""AI logic for Travel with Tots, routed through OpenRouter."""

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

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
KNOWLEDGE_BASE_PATH = Path(__file__).resolve().parent.parent / "data" / "knowledge_base.md"
_WEBSITE_CHATBOT_PROMPT = None


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


def _call_openrouter(messages: list[dict], model: str) -> str:
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
    _print_usage_report(model, data.get("usage"), elapsed)
    return data["choices"][0]["message"]["content"]


def ask(message: str, model: str = DEFAULT_MODEL) -> str:
    """Send a message to an OpenRouter-hosted model and return the reply text."""
    return _call_openrouter([{"role": "user", "content": message}], model)


def _load_website_chatbot_prompt() -> str:
    with open(WEBSITE_CHATBOT_PROMPT_PATH) as f:
        template = f.read()
    knowledge_base = KNOWLEDGE_BASE_PATH.read_text()
    return template.replace("{knowledge_base}", knowledge_base)


def reload_website_chatbot_prompt() -> None:
    """Force the next ask_website_chatbot call to re-read the prompt/knowledge base from disk."""
    global _WEBSITE_CHATBOT_PROMPT
    _WEBSITE_CHATBOT_PROMPT = None


def ask_website_chatbot(
    message: str, model: str = DEFAULT_MODEL, history: list[dict] | None = None
) -> str:
    """Answer a question about the Travel with Tots website, using the
    website_chatbot system prompt (with the knowledge base substituted in)
    plus any prior turns in `history`."""
    global _WEBSITE_CHATBOT_PROMPT
    if _WEBSITE_CHATBOT_PROMPT is None:
        _WEBSITE_CHATBOT_PROMPT = _load_website_chatbot_prompt()

    messages = (
        [{"role": "system", "content": _WEBSITE_CHATBOT_PROMPT}]
        + (history or [])
        + [{"role": "user", "content": message}]
    )
    return _call_openrouter(messages, model)
