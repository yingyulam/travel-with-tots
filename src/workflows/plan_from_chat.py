"""Plan-from-chat workflow: describe the day instead of filling in the form.

A parent's message goes to the AI agent, which calls the form extractor, and
the filled form comes back for them to review before any plan is built from it.
The chat bubble is the agent's interface, so this is what already happens when
someone describes a day there; `run` exists so a test page can drive the same
path directly.

One turn per message. Deciding which workflow a message wants, and asking for
whatever the description left out, are deliberately not here -- that is the
agentic-chatbot task.
"""

from ..llms import run_agent


def run(message: str, model: str | None = None) -> dict:
    """One turn: message in, agent's reply plus the extracted form out.

    Returns {"reply", "form", "found", "note", "model", "tool_calls"}. `form`
    is None when nothing was extracted, so a caller can tell that apart from an
    empty form rather than showing blank fields as if they were read from the
    message.

    `note` says why there is no form, which matters because there are two very
    different reasons: the agent chose another tool, or the extractor ran and
    could not read one (it swallows its own failures and returns nothing).
    Reporting those the same way would hide a real failure behind a shrug.
    """
    result = run_agent(message, model=model) if model else run_agent(message)
    calls = result["tool_calls"]
    names = [call["name"] for call in calls]

    extractions = [c for c in calls if c["name"] == "extract_form_tool"]
    extracted = next((c["data"] for c in extractions if c.get("data")), None)

    if extracted:
        note = None
    elif extractions:
        # It ran and came back empty, so its own message is the real reason.
        note = extractions[0]["output"]
    elif names:
        note = f"The agent used {', '.join(names)} rather than the extractor."
    else:
        note = "The agent answered directly without using a tool."

    return {
        "reply": result["reply"],
        "form": (extracted or {}).get("form"),
        "found": (extracted or {}).get("found", []),
        "note": note,
        "model": result["model"],
        "tool_calls": names,
    }

WORKFLOW = {
    "name": "Plan a day from a chat message",
    "emoji": "💬",
    "trigger": "message",
    # Endpoint name for its test page, so /workflows can offer a "Try it" link.
    "page": "plan_from_chat_page",
    "description": (
        "A parent describes their day in their own words and gets a real plan "
        "back, without filling in the form first. The agent pulls the "
        "structured fields out of that description and hands them to the "
        "existing planner, so the plan is built the same way either route."
    ),
    "steps": [
        {"component": "AI Agent (OpenRouter)", "built": True},
        {"component": "Form extractor", "built": True},
        {"component": "Plan trips (adjustment)", "built": True},
    ],
}
