"""Nap-time rescue workflow: reshape the day around a nap that ran long.

Declaration only for now -- when this is implemented, the run function belongs
in this file. Every component in the chain already exists, so this one needs no
new building blocks: it links the existing replan flow to Find Nearby so a
closed stop becomes a substitution rather than a hole.
"""

WORKFLOW = {
    "name": "Nap-time rescue",
    "emoji": "😴",
    "trigger": "event",
    "description": (
        "When a nap runs long, the rest of the day shifts to fit around it. "
        "If a stop in the revised day has since closed, an open kid-friendly "
        "place nearby is substituted instead of leaving a hole in the "
        "afternoon."
    ),
    "steps": [
        {"component": "User in-trip input", "built": True},
        {"component": "Replan a trip on-the-go (adjustment)", "built": True},
        {"component": "Find nearby stops", "built": True},
    ],
}
