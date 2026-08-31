"""Reading a message for the two things code can decide, and logging routing.

There was a classifier here: a message plus the offered workflows in, one
workflow name out. The agent's tool selection is that decision now -- its tools
are generated from the same registry -- so a second AI call to make it again
was a second router, a second vocabulary to drift, and latency on the critical
path of every message.

What stayed is what does not need a model. Leaving a workflow is not a
judgement, and a parent backing out should not wait on a network call.
"""

import json
import re
from datetime import datetime, timezone
from pathlib import Path



# Every decision, one JSON line, so a crash cannot lose the earlier ones. The
# app has no logging framework and results.json is the wrong home: it is written
# only when someone clicks a rating, and it rewrites the whole file with no lock.
INTENT_LOG_PATH = Path(__file__).resolve().parent.parent / "data" / "intents.jsonl"

# Leaving a workflow. Read here rather than by the model above: a parent backing
# out should not wait on a network call, and "cancel" is not a judgement.
CANCEL_WORDS = ("cancel", "stop", "quit", "exit", "never mind", "nevermind",
                "forget it", "forget this", "no thanks", "not now",
                "leave it", "start over", "something else", "go back",
                "do something else", "talk about something else")

# The button offered while a workflow is running. Named here, beside the words
# that recognise it, because the widget renders whatever label the server sends:
# a label this module cannot parse would be a button that does nothing, which is
# a bug this project has already shipped once.
CANCEL_CHOICE = "✕ Cancel"

# Dropped before matching, so "yes please" is the same answer as "yes".
FILLER_WORDS = ("please", "thanks", "thank you")

# What people put in front of backing out. Measured, not guessed: "actually
# never mind" is the obvious way to say this and a whole-message match had no
# room for the "actually". Dropped only for cancels, because "ok" on its own is
# a yes to a different question, and here it correctly leaves nothing behind.
CANCEL_FILLER = FILLER_WORDS + (
    "actually", "sorry", "ok", "okay", "oh", "well", "hmm", "um",
    "i think", "i want to", "i'd like to", "id like to", "let's", "lets",
    "can we", "could we", "we should", "just")

# Trimmed from the ends of a clause, so an emoji on a button label does not stop
# the label parsing as the words it contains.
_EDGE = re.compile(r"^[^\w']+|[^\w']+$")


def matches_only(message: str, vocabulary: tuple,
                 filler: tuple = FILLER_WORDS) -> bool:
    """True when the whole message is that one kind of answer and nothing else.

    Every clause has to match, rather than the message merely starting with a
    matching word: "yes, but make it four stops" is a correction, and accepting
    it would hand over a form the parent had just asked to change. Shared with
    the workflows, which read yes and no answers exactly the same way.
    """
    text = message.lower()
    for word in filler:
        text = text.replace(word, " ")
    parts = [_EDGE.sub("", part) for part in re.split(r"[,.!?]| and ", text)]
    parts = [part for part in parts if part]
    return bool(parts) and all(part in vocabulary for part in parts)


def is_cancel(message: str) -> bool:
    """Whether this message is the parent asking to leave the workflow.

    Whole-message only, the same rule as every other answer here: "stop by the
    park at 3" is a description of a day, not a request to abandon one.

    CANCEL_CHOICE has to satisfy this, since the button sends its own label.
    Nothing here enforces that, so a test does.
    """
    return matches_only(message, CANCEL_WORDS, CANCEL_FILLER)


def log_decision(message: str, workflow: str | None, ran: bool,
                 forced: bool = False, tool: str | None = None) -> None:
    """Append one routing decision. Never raises: losing a log line must not
    cost the parent their reply.

    `forced` marks a turn an admin test page directed, which never went near
    the classifier. This file is what routing accuracy is measured from, so
    without the flag test traffic would silently corrupt every measurement.

    `tool` is the agent's answer to the same question `workflow` answers, and
    the two are deliberately separate keys rather than one field holding either
    kind of name. /chatbot routes by tool now and /workflows/<name>/run by
    workflow, so a line says which router made the decision by which key is
    filled, and a count of one never has to guess at the other.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "message": message,
        "workflow": workflow,
        "tool": tool,
        "ran": ran,
        "forced": forced,
    }
    try:
        INTENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(INTENT_LOG_PATH, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError as e:
        print(f"Couldn't log the routing decision: {e}")
