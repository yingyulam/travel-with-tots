"""AI logic for Travel with Tots, routed through OpenRouter."""

import os

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
_WEBSITE_CHATBOT_PROMPT = None


def _call_openrouter(messages: list[dict], model: str) -> str:
    api_key = os.environ["OPENROUTER_API_KEY"]

    response = requests.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages},
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]["content"]


def ask(message: str, model: str = DEFAULT_MODEL) -> str:
    """Send a message to an OpenRouter-hosted model and return the reply text."""
    return _call_openrouter([{"role": "user", "content": message}], model)


def ask_website_chatbot(
    message: str, model: str = DEFAULT_MODEL, history: list[dict] | None = None
) -> str:
    """Answer a question about the Travel with Tots website, using the
    website_chatbot system prompt plus any prior turns in `history`."""
    global _WEBSITE_CHATBOT_PROMPT
    if _WEBSITE_CHATBOT_PROMPT is None:
        with open(os.path.join(PROMPTS_DIR, "website_chatbot.txt")) as f:
            _WEBSITE_CHATBOT_PROMPT = f.read()

    messages = (
        [{"role": "system", "content": _WEBSITE_CHATBOT_PROMPT}]
        + (history or [])
        + [{"role": "user", "content": message}]
    )
    return _call_openrouter(messages, model)
