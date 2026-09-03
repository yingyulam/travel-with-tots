"""AI Agent: a LangGraph tool-calling agent over OpenRouter.

This is what the site's chat bubble talks to. Its tools are thin wrappers
around components that already exist and are tested on their own pages, so a
message can be answered from the knowledge base, turned into a planning form,
planned into a day, or used to find somewhere nearby, all through one
implementation rather than two.

Every tool hands back both a short line for the model to read and the real
structured result for the caller (see `_artifact_of`), because LangGraph
otherwise JSON-stringifies a returned dict into the tool message and the
caller can only get a blob of text back.
"""

import contextvars
import os
import re

import requests
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .agents import (
    DEFAULT_MODEL,
    REQUEST_TIMEOUT_SECONDS,
    ask_website_chatbot,
)
from . import rag
# The vocabularies and readers these tools used to need are gone with them:
# collecting a need, a situation or a place's amenities is the workflow's job,
# and the workflow reads them from interactions and db directly.
from ..components.extract_form import FormExtractionError
from ..components.plan_trip import plan_trip
from .intent import CANCEL_CHOICE, is_cancel, log_decision
from ..workflows import runnable_message_workflows

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = (
    "You are Travel with Tots' assistant, answering in the chat bubble on the "
    "site. Every message is one of three things, and your only job is to tell "
    "them apart:\n"
    "1. A question about the site, how it works or what it can do: "
    "answer_faq_tool, with their question passed through unchanged. Call it "
    "every time, including when an earlier answer in this conversation looks "
    "like it covers the question -- answers come from the knowledge base, "
    "never from memory or from what you said before.\n"
    "2. A parent wanting the site to do something for them: call the tool for "
    "that task. Call it on the intent alone, before they have given any "
    "details: the tool takes no arguments and asks for what it needs, and it "
    "asks better than you can, with buttons to tap. \"I want to add a place\" "
    "and \"I want to plan a day\" are both this.\n"
    "3. Anything else -- a greeting, a thank you: answer in a sentence and "
    "call nothing.\n"
    "Use exactly one tool per message. Keep replies short and plain, with no "
    "markdown: no **bold**, no #headings, no backticks. The chat shows text "
    "exactly as you write it, so those characters appear on screen. Never "
    "write an itinerary yourself: the planning page builds those."
)


# Errors a tool must swallow: the chat route only catches KeyError and
# OpenAIError, so anything else raised inside a tool escapes as a 500.
TOOL_ERRORS = (FormExtractionError, requests.exceptions.RequestException, KeyError)


# The model this turn was asked for. A ContextVar rather than an argument
# because a LangGraph tool's parameters are what the model fills in, and the
# model choice is not the model's to make. Per-context, so the eight threads in
# the worker cannot read each other's turn.
_TURN_MODEL = contextvars.ContextVar("turn_model", default=DEFAULT_MODEL)

# What the request knew that the message did not: the browser's coordinates,
# whether a started day is open on the page, and which parent is signed in.
# Every workflow reads some of it -- find_nearby the coordinates, replan
# on_trip, plan_from_chat parent_id -- and none of it is the model's to decide
# or a caller's message to assert.
_TURN_CONTEXT = contextvars.ContextVar("turn_context", default=None)

# The parent's own words, handed to a workflow unchanged. A workflow tool takes
# no arguments at all, so this is how the message reaches it: selecting the tool
# is the only decision the model makes, and a paraphrase of the message is not
# one of them. read_situation and split_name both read the raw sentence.
_TURN_MESSAGE = contextvars.ContextVar("turn_message", default="")


@tool(response_format="content_and_artifact")
def answer_faq_tool(question: str) -> tuple[str, dict]:
    """Answer a question about the Travel with Tots website from its knowledge
    base, with [Source N] citations. Use for anything about how the site works,
    what it can do, or how to use a feature."""
    try:
        # The model the parent picked, not DEFAULT_MODEL. Omitting it meant
        # every knowledge-base answer came from `openrouter/free` however the
        # dropdown was set: the widget said "GPT-4o mini (paid)" and a free
        # model answered. Free models queue, so the visible symptom was a chat
        # turn that hung until the proxy gave up on it, while the workflows --
        # which do pass the model through -- stayed fast.
        result = ask_website_chatbot(question, model=_TURN_MODEL.get())
    except TOOL_ERRORS as e:
        return f"The knowledge base is unavailable right now ({type(e).__name__}).", {}
    return result["reply"], result


@tool
def plan_trip_tool(destination: str, age_months: int, wake_up: str = "07:00",
                    bedtime: str = "20:00", stop_count: int = 3,
                    dining: str = "dine_out") -> dict:
    """Plan a full day trip for a young child: builds a rule-based draft day
    (venues, a meal stop, a nap-friendly stop) then lets AI smooth it.
    destination is a city name. age_months is the child's age in months.
    stop_count is how many places to visit, 2-5 is typical. dining is
    "dine_out" or "on_the_go"."""
    # The model the parent picked, same as answer_faq_tool. Without it, planning
    # a day *through the chat* fell back to DEFAULT_MODEL while planning the
    # same day from the form honoured the dropdown: one feature, two answers,
    # and nothing said which had happened.
    return plan_trip(destination=destination, age_months=age_months,
                      wake_up=wake_up, bedtime=bedtime, stop_count=stop_count,
                      dining=dining, model=_TURN_MODEL.get())


def _slug(name: str) -> str:
    """A workflow name as a tool name. LangChain allows [a-zA-Z0-9_-] only."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _workflow_tool(workflow: dict, _run):
    """One tool that starts one workflow, and does nothing else.

    It takes no arguments. Selecting it is the only decision the model makes:
    which task this message is. Everything after that -- the questions, the
    chips, the state, the cancelling, the completion -- belongs to the workflow,
    which is what those four are built to do and what a tool cannot do at all.

    That last part is the bug this exists to fix. A tool needs arguments and a
    conversation starts with none, so log_place_tool(name) could not be called
    before a name existed: measured, three of four bare intents ("I want to add
    a place", "I want to log a place we're missing", "I want to plan a day")
    called no tool and were answered as conversation instead of starting
    anything.

    The message and the request context arrive by ContextVar rather than as
    parameters, so the model cannot paraphrase either.
    """
    name = workflow["name"]

    @tool(_slug(name), description=workflow["description"],
          response_format="content_and_artifact")
    def start() -> tuple[str, dict]:
        result = run_workflow_turn(name, _TURN_MESSAGE.get(),
                                   context=_TURN_CONTEXT.get())
        if result is None:
            return f"{name} could not run that.", {}
        return result["reply"], result

    return start


# The agent's capabilities: the knowledge base, and one starter per workflow.
# Generated from the registry rather than listed, so the tools and the
# workflows are the same set and cannot drift -- adding a workflow adds the
# capability. Choosing among these *is* the intent identification step; there
# is no second classifier, and no second vocabulary for one to disagree with.
#
# plan_trip_tool is deliberately absent. A day built in the chat is a day
# outside the planner: no version switcher, no situation buttons, no
# replanning, and a second itinerary the moment the parent presses Plan my day.
TOOLS = [answer_faq_tool] + [
    _workflow_tool(workflow, run) for workflow, run in runnable_message_workflows()
]


# Tools whose output is already the answer a parent should read, so the model
# is not asked to word it again. Only the FAQ qualifies: it hands back
# ask_website_chatbot's reply, grounded in retrieved chunks and carrying
# [Source N] citations. Every other tool returns a terse line for the model to
# work from -- "Filled in from their words: destination, age_years." is not
# something to show anybody -- and their final turn earns its cost.
#
# Rewriting the FAQ answer cost a whole round trip and put the citations at the
# mercy of a prompt asking the model to leave them alone. Returning it verbatim
# is both cheaper and the only way the markers are guaranteed intact.
# A workflow's reply is its own too, and for a stronger reason than the FAQ's:
# the workflow wrote the question, and the chips underneath answer that exact
# wording. Letting the model reword "What do you need right now?" would put
# different words above the same six buttons every turn.
FINAL_ANSWER_TOOLS = frozenset(
    {"answer_faq_tool"} | {_slug(w["name"]) for w, _ in runnable_message_workflows()})


def _build_agent(model: str, stop_after_tools: bool = False):
    """The tool-calling agent, with a bound on how long it may wait.

    `timeout` and `max_retries` are the point. Left alone, langchain hands the
    OpenAI SDK its defaults -- 600 seconds and two retries -- so a stalled call
    can occupy a worker for far longer than any web request should, and the
    caller never learns why: gunicorn kills the worker first and the proxy
    answers with its own error page. That is exactly how this surfaced in
    production, as a 502 after two minutes with an empty body.

    The same 60 seconds `agents.ask_openrouter` already uses, so both paths to a
    model give up at the same point rather than one of them hanging.
    """
    chat = ChatOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=model,
        timeout=REQUEST_TIMEOUT_SECONDS,
        max_retries=1,
    )
    # interrupt_after stops the graph the moment a tool returns, before the
    # model is asked to word an answer from it. Verified to need no
    # checkpointer, which is what makes it usable here: this agent is built
    # fresh per turn and holds no thread.
    return create_react_agent(
        chat, TOOLS, prompt=SYSTEM_PROMPT,
        interrupt_after=["tools"] if stop_after_tools else None)


def handle_message(message: str, history: list[dict] | None = None,
                   model: str = DEFAULT_MODEL,
                   conversation: dict | None = None,
                   context: dict | None = None) -> dict:
    """One turn. A running workflow owns it; otherwise the agent decides.

    The entry point for any surface that carries a message. It takes a plain
    string rather than a request, so a Telegram handler can call the same
    function the website chat does.

    Two paths, and which one is taken costs nothing to decide:

    A workflow that is mid-conversation owns every message until it finishes or
    the parent cancels. That check is first and it is not a model call: "yes"
    and "Vancouver" are answers to the question just asked, and asking a model
    to re-classify them is how a flow gets derailed.

    Otherwise the agent runs, once, and its tool selection *is* the intent
    decision -- there is no separate classifier, and its tools are generated
    from the workflow registry, so the two cannot disagree about what exists.
    A workflow tool starts a workflow and does nothing else; the workflow then
    owns the conversation by the paragraph above.

    `context` is what the request knew that the message did not: coordinates,
    whether a day is open on the page, which parent is signed in. It reaches
    workflows through a ContextVar rather than as tool arguments, because a
    tool's arguments are the model's to fill in and none of this is.
    """
    if (conversation or {}).get("workflow"):
        # The workflow is the source of truth while it is running. No agent, no
        # classifier, no model call to decide anything.
        reply = run_workflow_turn(conversation["workflow"], message,
                                  conversation=conversation, context=context,
                                  model=model)
        if reply is not None:
            return reply
        # Only reachable if the workflow was unregistered mid-conversation or
        # raised. The flow is over either way, so the agent answers afresh
        # rather than the parent losing their turn.

    # Set once for the whole turn, so a tool the model chooses to call answers
    # with the model the parent chose, on the parent's own words, with what the
    # request knew. None of the three is the model's to decide.
    _TURN_MODEL.set(model)
    _TURN_MESSAGE.set(message)
    _TURN_CONTEXT.set(context)

    result = run_agent(message, history=history, model=model)
    # What handled this message, in the file routing accuracy is measured from.
    # A workflow tool has already logged its own name from run_workflow_turn, so
    # logging here too would count one message twice.
    tools = [call["name"] for call in result["tool_calls"]]
    if not result.get("workflow"):
        log_decision(message, None, ran=bool(tools),
                     tool=tools[0] if tools else None)
    return {"workflow": None, "conversation": None, **result}


def _cancelled(in_flight: str, model: str) -> dict:
    """Leaving a flow, in the widget's shape."""
    return {
        "reply": ("No problem, I've stopped there. What else can I help "
                  "you with?"),
        "sources": [], "model": model, "response_time": None,
        "input_tokens": None, "output_tokens": None, "tool_calls": [],
        "workflow": None, "conversation": None, "choices": None,
        "form": None, "open_form": False, "places": [], "source": None,
        "ask_location": False, "cancelled": in_flight,
    }


def run_workflow_turn(name: str, message: str, *, conversation: dict | None = None,
                      context: dict | None = None, model: str = DEFAULT_MODEL,
                      forced: bool = False) -> dict | None:
    """One turn of the named workflow, in the widget's shape, or None.

    None is "this did not run": the name is not on offer, or the workflow
    raised. Both are logged, and both leave the caller to answer another way --
    /chatbot falls through to the agent, the workflow route says so plainly.

    Extracted from handle_message so a workflow has one implementation whether
    it was reached by the classifier or asked for by name. A second copy behind
    the workflow route is exactly the duplication the route exists to avoid.

    An in-flight conversation outranks `name`: mid-flow, "yes" is an answer to
    the question just asked, not a request to start something.
    """
    offered = runnable_message_workflows()
    in_flight = (conversation or {}).get("workflow")

    if in_flight and is_cancel(message):
        # Checked before dispatch, so it works for every workflow rather than
        # each one having to remember. Without it a workflow was a room with no
        # door: the only ways out were finishing it or ending the chat. Logged
        # against the workflow that was in play with ran=False, which is
        # exactly what happened.
        log_decision(message, in_flight, ran=False)
        return _cancelled(in_flight, model)

    if in_flight:
        chosen = in_flight
        # Client-supplied, so it is checked rather than trusted. A non-dict
        # would reach the workflow as an attribute error; None just restarts.
        state = conversation.get("state")
        if not isinstance(state, dict):
            state = None
    else:
        chosen, state = name, None

    run = next((r for w, r in offered if w["name"] == chosen), None)
    if run is None:
        return None

    try:
        result = run(message, state, context)
    except TOOL_ERRORS as e:
        # A workflow that fails must not cost the parent their turn. Logged as
        # not-run, so the trace shows the routing was right even where the
        # execution was not.
        print(f"Workflow {chosen!r} failed, answering as the chatbot: {e}")
        log_decision(message, chosen, ran=False, forced=forced)
        return None

    log_decision(message, chosen, ran=True, forced=forced)
    # A workflow that returns a state is still talking; one that returns None is
    # finished, and the next message starts fresh.
    next_state = result.get("state")
    # The widget's keys, so a workflow reply renders like any other. The usage
    # fields are genuinely unknown here: they come from the FAQ tool, which did
    # not run.
    return {
        "reply": result["reply"],
        "sources": [],
        "model": model,
        "response_time": None,
        "input_tokens": None,
        "output_tokens": None,
        "tool_calls": [],
        "workflow": chosen,
        "workflow_result": result,
        "conversation": ({"workflow": chosen, "state": next_state}
                         if next_state else None),
        "choices": result.get("choices"),
        # Whether those choices are exclusive. A place has several features at
        # once, so its chips collect instead of sending.
        "choose_many": result.get("choose_many", False),
        "form": result.get("form"),
        # The same idea as `form`, for a different page: a collected place,
        # posted to /log-place rather than /plan.
        "place_form": result.get("place_form"),
        # A confirmed replan, for the in-trip page to act on. It holds the plan
        # and its versions, so it does the re-timing.
        "replan_request": result.get("replan_request"),
        "open_form": result.get("open_form", False),
        # Places render as cards with real Maps links, so they travel as data
        # rather than as URLs written into the reply text.
        "places": result.get("places") or [],
        "source": result.get("source"),
        "ask_location": result.get("ask_location", False),
        # Offered on every turn a workflow stays open, so leaving is always one
        # tap away. Sent from here rather than built in the widget, so the label
        # the parent clicks and the words this side recognises are the same
        # string.
        "cancel_choice": CANCEL_CHOICE if next_state else None,
    }


def _artifact_of(name: str, tool_messages: list) -> dict:
    """The structured result of the named tool, or {} if it wasn't called.

    Reads `artifact` rather than `content`: a tool returning a dict has it
    JSON-stringified into `content` by LangGraph, so the artifact is the only
    place the real thing survives.
    """
    for message in tool_messages:
        if message.name == name and isinstance(getattr(message, "artifact", None), dict):
            return message.artifact
    return {}


# One [Source N] marker, with any space in front of it so removing the marker
# does not leave a gap before the full stop.
_CITATION = re.compile(r"\s*\[Source (\d+)\]")


def _only_earned_citations(reply: str, sources: list) -> str:
    """Drop any [Source N] the retrieved sources do not actually contain.

    The widget renders every marker as a button and looks the number up in
    `sources`, so a marker with nothing behind it is a dead chip reading
    "Source details unavailable". Worse than dead: it presents a claim as
    cited when nothing was retrieved to support it, which is the one thing
    this whole retrieval path exists to prevent.

    It happens when the model answers a follow-up from the conversation rather
    than calling the FAQ tool, and copies a marker out of its own earlier
    answer. Rare -- four reproductions of the reported exchange all retrieved
    properly -- and a prompt cannot rule it out, so the markers are checked
    against the sources instead of the model being asked again.
    """
    real = {str(source["index"]) for source in (sources or [])}
    return _CITATION.sub(
        lambda m: m.group(0) if m.group(1) in real else "", reply)


def _knowledge_base_answer(message: str, model: str) -> dict | None:
    """A grounded answer when the agent skipped the tool but should not have.

    Measured: on the third turn of a conversation the model stopped calling
    answer_faq_tool entirely, 4 times out of 4, because two knowledge-base
    answers were already sitting in the transcript and it reasoned it knew the
    subject. The answers were right and completely ungrounded, which is the
    guarantee this whole path exists to give. Naming the failure mode in the
    prompt took it from 4 in 4 to 1 in 4, and a prompt is a request.

    What decides whether a message is about the site is retrieval itself, not
    the model and not a second classifier: MIN_SIMILARITY already separates the
    two cleanly, with off-topic queries topping out around 0.11 and real ones
    starting around 0.31. So a turn that used no tool is offered to the
    knowledge base, and kept only if the knowledge base has something to say.
    Nothing retrieved means the direct answer stands, which is what "hello"
    should get.
    """
    try:
        if not rag.retrieve(message):
            return None
        answer = ask_website_chatbot(message, model=model)
    except TOOL_ERRORS:
        # Retrieval is a best-effort improvement here; the agent already has a
        # reply, and losing it to a blip would be a worse turn than an
        # ungrounded one.
        return None
    return answer


def _asked_a_question(message) -> bool:
    """Whether a tool came back asking rather than answering.

    A tool that cannot proceed until the parent picks something returns the
    question plus the chips to answer it, in the widget's own `choices` shape.
    Its wording is then the reply: letting the model paraphrase "What do you
    need right now?" would put different words above the same six buttons every
    time, and those words are the label the buttons answer.
    """
    return bool((getattr(message, "artifact", None) or {}).get("choices"))


def run_agent(message: str, history: list[dict] | None = None,
              model: str = DEFAULT_MODEL) -> dict:
    """Runs one turn: given a free-text message and prior turns ({"role",
    "content"} dicts, the shape the chat widget already sends), lets the agent
    decide which tool to call, if any.

    Returns the same keys the chat widget already consumes ("reply",
    "sources", "model", "response_time", "input_tokens", "output_tokens") so
    citations keep rendering and a rating keeps recording model and timing,
    plus "tool_calls" (name, text output, and structured data per call, in
    order) as the concrete proof of what actually ran. Never logs the API key.
    """
    messages = []
    for turn in (history or []):
        cls = HumanMessage if turn.get("role") == "user" else AIMessage
        messages.append(cls(turn.get("content", "")))
    messages.append(HumanMessage(message))

    # Stop at the tool, then decide whether the model still has work to do.
    # Three outcomes: no tool was called and the answer is already written; the
    # tool that ran writes its own answers (the FAQ), so we are finished; or the
    # tool handed back a working note, and the model is resumed to turn it into
    # a reply. Resumed on the accumulated messages, so the tool is not run twice.
    result = _build_agent(model, stop_after_tools=True).invoke(
        {"messages": messages})
    last = result["messages"][-1]
    if (isinstance(last, ToolMessage) and last.name not in FINAL_ANSWER_TOOLS
            and not _asked_a_question(last)):
        result = _build_agent(model).invoke({"messages": result["messages"]})

    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    if not tool_messages:
        grounded = _knowledge_base_answer(message, model)
        if grounded is not None:
            return {
                "reply": grounded["reply"],
                "sources": grounded["sources"],
                "places": [], "source": None, "replan_request": None,
                "form": None, "place_form": None,
                "choices": None, "choose_many": False,
                "model": model,
                "response_time": grounded.get("response_time"),
                "input_tokens": grounded.get("input_tokens"),
                "output_tokens": grounded.get("output_tokens"),
                # Named as the tool that answered, because it did: the widget's
                # badge and data/intents.jsonl both read this.
                "tool_calls": [{"name": "answer_faq_tool",
                                "output": grounded["reply"], "data": grounded}],
            }

    # The FAQ tool wraps ask_website_chatbot, so when it ran its result already
    # carries the citations and usage numbers the widget expects.
    faq = _artifact_of("answer_faq_tool", tool_messages)
    # A workflow tool's artifact is the workflow's own reply, already in the
    # widget's shape. Passed through whole so the conversation, the chips, the
    # form and the cancel option all survive, and so the next turn comes back
    # in flight and never reaches the agent again.
    started = next((m.artifact for m in tool_messages
                    if (getattr(m, "artifact", None) or {}).get("workflow")), {})
    if started:
        return {**started, "reply": started["reply"]}
    # Whichever tool asked, if one did. Chips travel in the same keys a
    # workflow's do, so the widget draws them without knowing which side of the
    # app produced the question.
    asked = next((m.artifact for m in tool_messages if _asked_a_question(m)), {})
    return {
        "choices": asked.get("choices"),
        # Not exclusive: several amenities can be true of one place, so that
        # row collects instead of sending on the first tap.
        "choose_many": asked.get("choose_many", False),
        "reply": _only_earned_citations(result["messages"][-1].content,
                                        faq.get("sources", [])),
        "sources": faq.get("sources", []),
        "model": model,
        "response_time": faq.get("response_time"),
        "input_tokens": faq.get("input_tokens"),
        "output_tokens": faq.get("output_tokens"),
        "tool_calls": [
            {"name": m.name, "output": m.content,
             "data": getattr(m, "artifact", None)}
            for m in tool_messages
        ],
    }