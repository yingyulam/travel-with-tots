"""Intent routing: which workflow, if any, is this message asking for?

One job. A message plus the workflows on offer in, one workflow name or "none"
out. It does not run anything: `agent.handle_message` decides what to do with
the answer, so this stays testable without touching a workflow.

The answer is constrained by a strict JSON schema whose enum is the offered
names plus "none", rather than parsed out of prose. And it is checked again
afterwards, because strict-mode enum support varies by provider: a name that
was never offered becomes "none" rather than being dispatched. Same belt and
braces as extract_form.py's ALLOWED_VALUES.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .agents import call_openrouter, parse_json_reply

PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
INTENT_PROMPT_PATH = os.path.join(PROMPTS_DIR, "intent.txt")
_INTENT_TEMPLATE = None

# Every decision, one JSON line, so a crash cannot lose the earlier ones. The
# app has no logging framework and results.json is the wrong home: it is written
# only when someone clicks a rating, and it rewrites the whole file with no lock.
INTENT_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "intents.jsonl"

# Pinned, and deliberately not the free auto-router. This call sits on the
# critical path of every single message, so latency here is latency a parent
# feels on every turn. Measured in this project: a free reasoning model took
# 25-75s, this one about 2s. The answer is one word, so the cost is negligible.
INTENT_MODEL = "openai/gpt-4o-mini"

NO_WORKFLOW = "none"


def _load_template() -> str:
    with open(INTENT_PROMPT_PATH) as f:
        return f.read()


def reload_intent_prompt() -> None:
    """Force the next call to re-read the prompt from disk."""
    global _INTENT_TEMPLATE
    _INTENT_TEMPLATE = None


def _response_format(names: list[str]) -> dict:
    """Constrain the answer to the offered names plus "none"."""
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "intent",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "workflow": {"type": "string", "enum": [*names, NO_WORKFLOW]},
                },
                "required": ["workflow"],
                "additionalProperties": False,
            },
        },
    }


def _describe(workflows: list[dict]) -> str:
    """The menu the model chooses from, built from the registry so adding a
    workflow needs no prompt edit."""
    return "\n".join(
        f"- {w['name']}: {w['description']}" for w in workflows) or "(none available)"


def classify_intent(message: str, workflows: list[dict],
                    model: str = INTENT_MODEL) -> str:
    """The name of the workflow this message asks for, or "none".

    Returns "none" rather than raising when the model is unreachable or answers
    with something unusable: a router that fails closed falls through to the
    chatbot, which is a worse answer than the workflow but a better one than an
    error.
    """
    if not workflows:
        return NO_WORKFLOW

    global _INTENT_TEMPLATE
    if _INTENT_TEMPLATE is None:
        _INTENT_TEMPLATE = _load_template()
    prompt = (_INTENT_TEMPLATE
              .replace("{message}", message)
              .replace("{workflows}", _describe(workflows)))
    names = [w["name"] for w in workflows]

    try:
        reply, _usage, _elapsed = call_openrouter(
            [{"role": "system", "content": prompt}], model, _response_format(names))
        chosen = parse_json_reply(reply).get("workflow")
    except Exception as e:
        # Deliberately broad: this is a routing hint, and no failure to obtain
        # one justifies failing the parent's message. The transport and the
        # parser each raise their own family, and a new one appearing here
        # should still fall through to the chatbot rather than 500.
        print(f"Intent classification skipped: {type(e).__name__}: {e}")
        return NO_WORKFLOW

    # Checked again rather than trusted: a name that was never offered is not a
    # routing decision, it is a hallucination, and dispatching on it would call
    # something the parent never asked for.
    return chosen if chosen in names else NO_WORKFLOW


def log_decision(message: str, workflow: str | None, ran: bool) -> None:
    """Append one routing decision. Never raises: losing a log line must not
    cost the parent their reply."""
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "workflow": workflow,
        "ran": ran,
    }
    try:
        INTENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(INTENT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"Couldn't log the routing decision: {e}")
