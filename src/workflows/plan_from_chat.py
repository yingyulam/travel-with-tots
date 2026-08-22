"""Fill the planning form from a chat message, instead of typing it in.

A parent's message goes to the AI agent, which calls the form extractor, and the
filled form comes back for them to review. The chat bubble is the agent's
interface, so this is what already happens when someone describes a day there;
`templates/plan_from_chat.html` watches it happen.

The chain stops at the form. Building the day from it is a separate step, and
the conversation that would fill in what a description left out -- asking
whether they would rather use the form by hand, prompting for missing fields,
checking they are happy with what was read -- is the agentic-chatbot task.

One thing the page has to get right, found live: the extractor swallows its own
failures and returns nothing rather than raising, so "the extractor ran and
could not read a form" and "the agent chose a different tool" both arrive as an
absent form. Reporting them the same way hides a real failure behind a shrug,
which is why `static/plan-from-chat.js` tells them apart.

The filename says "plan" for history's sake; the workflow ends at the form.
"""

from ..components.extract_form import extract_form


def run(message: str) -> dict:
    """Read a described day into the planning form.

    Returns {"reply", "form", "found"}. `reply` is what the parent reads, so it
    names the fields that came from their own words: a form they cannot see is
    not an answer.

    This existed before, was deleted as dead code, and is back because the
    intent router now calls it. That is the whole difference: it has a caller.
    """
    result = extract_form(message)
    found = result["found"]
    if found:
        reply = ("I've filled in the planning form from that: "
                 + ", ".join(name.replace("_", " ") for name in found)
                 + ". Everything else is at its default, so check it before "
                   "building the day.")
    else:
        reply = ("I couldn't pull any planning details out of that. Try "
                 "mentioning where you are, your child's age, and the times "
                 "your day starts and ends.")
    return {"reply": reply, "form": result["form"], "found": found}


WORKFLOW = {
    "name": "Fill the form from a chat message",
    # Not 💬: the trigger group heading already carries that.
    "emoji": "📝",
    "trigger": "message",
    # Endpoint name for its test page, so /workflows can offer a "Try it" link.
    "page": "plan_from_chat_page",
    "description": (
        "A parent describes their day in their own words and the planning form "
        "fills itself in, with every field marked as either read from their "
        "description or left at its default. They check it before the day is "
        "built, which is a separate step."
    ),
    "steps": [
        {"component": "AI Agent (OpenRouter)", "built": True},
        {"component": "Form extractor", "built": True},
    ],
}
