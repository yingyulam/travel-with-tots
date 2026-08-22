"""End-to-end use cases, each one a chain of components from /components.

One file per workflow, mirroring src/components/. A workflow is only as
reliable as the pieces it chains, which is the point: every component is
already built and tested on its own page, so a workflow adds sequencing rather
than new risk.

Each module declares its chain so /workflows can show what the parts add up to,
and a module with a `page` key has a test page where the chain really runs.
`TRIGGERS` names how a workflow starts; today the message and event triggers
already exist (the chat bubble and the /trip situation buttons), while scheduled
has no mechanism in the app at all.
"""

from .log_a_place import WORKFLOW as _log_a_place
from .nap_time_rescue import WORKFLOW as _nap_time_rescue
from .plan_from_chat import WORKFLOW as _plan_from_chat

# Display order and label for each trigger, so /workflows can group by it.
TRIGGERS = (
    ("event", "🔁 Event-driven"),
    ("message", "💬 Message-triggered"),
    ("scheduled", "⏱️ Scheduled"),
)

WORKFLOWS = (
    _nap_time_rescue,
    _log_a_place,
    _plan_from_chat,
)


def workflows_by_trigger():
    """(label, workflows) pairs in TRIGGERS order, skipping empty triggers, so
    the page never renders a heading with nothing under it."""
    grouped = []
    for trigger, label in TRIGGERS:
        matching = [w for w in WORKFLOWS if w["trigger"] == trigger]
        if matching:
            grouped.append((label, matching))
    return grouped
