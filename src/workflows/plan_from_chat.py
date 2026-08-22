"""Plan-from-chat workflow: describe the day instead of filling in the form.

Declaration only for now -- when this is implemented, the run function belongs
in this file. Unlike the other two, this one needs a component that does not
exist yet: something that turns a parent's description into the form's
structured fields. Note that free prose already reaches the planner through
extra_notes, so an extractor earns its place on the fields prose cannot set --
wake-up and bedtime, stop count, naps, destination, dining, transit, features.
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
        {"component": "Form extraction", "built": False},
        {"component": "Plan trips (adjustment)", "built": True},
    ],
}
