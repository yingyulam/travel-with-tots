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

from . import (find_nearby_place, log_a_place, plan_from_chat, propose_venues,
               replan_on_the_go)

# Display order and label for each trigger, so /workflows can group by it.
TRIGGERS = (
    ("event", "🔁 Event-driven"),
    ("message", "💬 Message-triggered"),
    ("scheduled", "⏱️ Scheduled"),
)

# The modules, in display order, rather than just their declarations: the intent
# router needs each module's `run` as well as its `WORKFLOW`. Registered by hand
# rather than discovered, so the order is explicit and a broken import is loud.
_MODULES = (replan_on_the_go, log_a_place, plan_from_chat, find_nearby_place,
            propose_venues)

WORKFLOWS = tuple(module.WORKFLOW for module in _MODULES)


def runnable_message_workflows():
    """(workflow, run) pairs a chat message could actually trigger.

    Both halves of the filter matter. `trigger == "message"` excludes the ones
    started by something else, like the /trip situation buttons. Requiring a
    `run` excludes the declaration-only ones: offering the classifier a
    workflow with nothing behind it means it will confidently pick something
    that then cannot be executed, which is worse than answering as the chatbot.

    Every `run` here takes `(message, state=None, context=None)`. `state` is
    what that workflow returned last turn, so None begins it. `context` is what
    the request knew that the message did not, today the browser's coordinates;
    it is a third argument rather than part of `state` because a first turn has
    no state, and inventing one would read as a conversation already in
    progress.
    """
    pairs = []
    for module in _MODULES:
        workflow = module.WORKFLOW
        run = getattr(module, "run", None)
        if workflow["trigger"] == "message" and callable(run):
            pairs.append((workflow, run))
    return pairs


def workflows_by_trigger():
    """(label, workflows) pairs in TRIGGERS order, skipping empty triggers, so
    the page never renders a heading with nothing under it."""
    grouped = []
    for trigger, label in TRIGGERS:
        matching = [w for w in WORKFLOWS if w["trigger"] == trigger]
        if matching:
            grouped.append((label, matching))
    return grouped
