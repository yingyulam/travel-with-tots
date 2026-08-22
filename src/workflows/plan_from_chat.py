"""Plan-from-chat workflow: describe the day instead of filling in the form.

Declaration only for now -- when this is implemented, the run function belongs
in this file. Every component in the chain now exists: the Form Extractor
(src/components/extract_form.py) turns a description into the form's structured
fields, and hands them to the planner the /plan form already uses. What remains
is the chaining itself, plus letting the parent review the extracted form
before a plan is built from it.
"""

WORKFLOW = {
    "name": "Plan a day from a chat message",
    "emoji": "💬",
    "trigger": "message",
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
