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
from .components.extract_form import FormExtractionError, extract_form
from .components.find_nearby import find_nearby as find_nearby_component
from .components.plan_trip import plan_trip
from .data_loader import SUPPORTED_CITIES
from .db import AMENITY_OPTIONS
from .intent import CANCEL_CHOICE, is_cancel, log_decision
from .interactions import (
    AMENITY_QUESTION,
    FREE_TEXT_SITUATION,
    NEED_CHIP_LABELS,
    NEED_QUESTION,
    SITUATION_CHIP_LABELS,
    SITUATION_LABELS,
    SITUATION_QUESTION,
    read_need,
    read_replan_request,
    read_situation,
)
from .workflows import runnable_message_workflows

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = (
    "You are Travel with Tots' assistant, answering in the chat bubble on the "
    "site. Use your tools rather than your own knowledge:\n"
    # No instruction about what to do with the answer, because the model never
    # sees it: this tool's reply goes straight to the parent (FINAL_ANSWER_TOOLS)
    # and the turn ends there.
    "- answer_faq_tool for any question about how the site works or what it "
    "can do. Pass the parent's question through unchanged.\n"
    "- Asking to plan a day without saying anything about it is not a question "
    "about the site: ask them for the details, and do not reach for the "
    "knowledge base.\n"
    "- extract_form_tool whenever a parent describes a day out they want, so "
    "their words become the planning form. Pass their whole description. This "
    "is the tool for a description even when it sounds like a request for a "
    "plan: the form is filled in first so the parent can check it.\n"
    "- find_nearby_tool when they need somewhere nearby right now.\n"
    "- log_place_tool when they tell you about a kid-friendly place the "
    "site is missing and want it added. Give it the place's own name, not "
    "their sentence about wanting to log one.\n"
    "- replan_tool when something has changed during a day already under way "
    "and the rest of it needs reshaping: a long nap, rain, a shut stop, "
    "running behind, skipping the next stop. Pass their words through "
    "unchanged.\n"
    "When a tool needs to know which of a fixed set of things the parent "
    "means, call it anyway rather than asking them yourself: it answers "
    "with the question and the buttons that go with it, and buttons beat "
    "a typed reply for somebody holding a toddler.\n"
    "Use exactly one tool per message. Keep replies short and plain, with no "
    "markdown: no **bold**, no #headings, no backticks. The chat shows text "
    "exactly as you write it, so those characters appear on screen. Never "
    "write an itinerary yourself: filling the form is how a day gets planned, "
    "and the planning page builds it."
)

# Errors a tool must swallow: the chat route only catches KeyError and
# OpenAIError, so anything else raised inside a tool escapes as a 500.
TOOL_ERRORS = (FormExtractionError, requests.exceptions.RequestException, KeyError)


# The model this turn was asked for. A ContextVar rather than an argument
# because a LangGraph tool's parameters are what the model fills in, and the
# model choice is not the model's to make. Per-context, so the eight threads in
# the worker cannot read each other's turn.
_TURN_MODEL = contextvars.ContextVar("turn_model", default=DEFAULT_MODEL)

# Whether a started day is open on the page this turn came from, set from the
# request's context. A ContextVar for the same reason as the model above: a
# tool's arguments are what the model fills in, and whether the parent has a
# trip open is a fact about the request, not something to let the model decide
# or a caller's message assert.
_TURN_ON_TRIP = contextvars.ContextVar("turn_on_trip", default=False)


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


@tool(response_format="content_and_artifact")
def extract_form_tool(description: str) -> tuple[str, dict]:
    """Turn a parent's description of a day out into the planning form. Use
    when they describe what they want rather than asking a question: times,
    a child's age, naps, a destination, how many places, preferences. Pass
    their description through whole, in their own words."""
    try:
        # Deliberately not passing the agent's model through: the extractor
        # needs structured-output support, which not every model offered in the
        # chat widget advertises, so it keeps its own known-good default.
        result = extract_form(description)
    except TOOL_ERRORS as e:
        return f"Couldn't read a form from that ({type(e).__name__}).", {}
    # Parent-facing, because this tool answers for itself (FINAL_ANSWER_TOOLS).
    # Underscores out: these are form field names, and "wake up" reads where
    # "wake_up" looks like code.
    found = ", ".join(f.replace("_", " ") for f in result["found"])
    picked = f"I've filled in {found}. " if found else ""
    return (f"{picked}Open the form to check it and add anything missing, then "
            "plan your day from there."), result


@tool(response_format="content_and_artifact")
def find_nearby_tool(need: str = "") -> tuple[str, dict]:
    """Find 1-2 kid-friendly venues nearby matching an immediate need.
    need should be one of: restaurant, family_room, changing_table,
    nursing_room, quiet_spot, other.

    Call this even when they have not said which they need: leave need empty
    and it will offer them the buttons to pick from. Never ask them yourself,
    and never guess a need they did not name."""
    # The component, not interactions.find_nearby: that one is the need-matching
    # predicate, with no location narrowing and no web fallback. This tool used
    # to call it directly, so the agent answered these from the sample venue
    # list while the real chain sat unused.
    #
    # This is the safety net for a phrasing the intent classifier misses; the
    # registered workflow is the main path. Both call the same component, so
    # they cannot answer the same question two different ways.
    # Asked rather than guessed when the words match nothing, with the same six
    # chips the workflow offers. A wrong guess sends a parent to a cafe when
    # they needed somewhere to feed the baby, and read_need is the one reading
    # of those words, shared with the workflow.
    known = read_need(need)
    if known is None:
        return NEED_QUESTION, {"choices": NEED_CHIP_LABELS}
    try:
        result = find_nearby_component(need=known, city=SUPPORTED_CITIES[0])
    except TOOL_ERRORS as e:
        return f"Couldn't look that up right now ({type(e).__name__}).", {}
    names = ", ".join(place["name"] for place in result["places"]) or "nothing"
    # The artifact carries the places so the caller can render real links from
    # them. Returned as content_and_artifact for exactly that reason: a plain
    # dict would be JSON-stringified into the tool message and lost.
    return f"Found {names} ({result['source']}).", result


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


@tool(response_format="content_and_artifact")
def replan_tool(situation: str) -> tuple[str, dict]:
    """Collect a request to reshape the rest of a day already under way: a nap
    that ran long, a closed stop, rain, running behind, wanting to skip the
    next stop or do something else. Pass the parent's words through unchanged.
    Only for a trip already started.

    Call this even when they have only said that something changed: pass their
    words through and it will offer them the buttons to pick from. Never ask
    them yourself."""
    # Collects, never replans, which is the same division the workflow keeps.
    # The itinerary, its versions and the current time all live on the trip
    # page, and runReplan there is the one implementation; replanning here
    # would be a second one, producing a version the page's switcher never
    # sees. So the request is handed over for one button, exactly as the
    # workflow hands it over.
    if not _TURN_ON_TRIP.get():
        # Nothing to replan without a started day, and collecting a situation
        # nobody can act on would waste the parent's turn.
        return ("I can shift a day you've already started. Open your trip from "
                "the planning page, then ask me again and I'll replan from "
                "where you are.", {})
    # The same six chips the workflow offers when the words name no particular
    # situation. Offering them is the useful thing to do for somebody who has
    # not said yet, and a tapped label reads back exactly.
    if read_situation(situation) == FREE_TEXT_SITUATION:
        return SITUATION_QUESTION, {"choices": SITUATION_CHIP_LABELS}
    request = read_replan_request(situation)
    label = SITUATION_LABELS.get(request["situation"], "Something's changed")
    return f"Collected a replan request: {label}.", {"replan_request": request}


@tool(response_format="content_and_artifact")
def log_place_tool(name: str, area: str = "", amenities: list[str] | None = None,
                    notes: str = "") -> tuple[str, dict]:
    """Collect a kid-friendly place the app is missing, so the parent can
    submit it. name is the place's own name, never a sentence about wanting to
    log one. area is a neighbourhood if they said one. notes is anything else
    they said about it.

    amenities may include has_family_room, has_nursing_room,
    stroller_accessible. Leave it out entirely if they have not said what the
    place offers, and pass an empty list once they have said it offers none of
    them."""
    # Collects and hands over, like replan_tool: the chat has no parent to
    # attach a submission to and no way to drop a map pin, and a form post has
    # both. store() stays the one writer, reached from /log-place.
    name = (name or "").strip()
    if not name:
        return "I need the place's name before I can log it.", {}
    # Absent means nobody has said yet, so the amenity chips go out, the same
    # multi-select row the workflow shows. An empty list is a real answer --
    # "none of these" -- and must not ask again, which is the difference a
    # missing argument can carry and a falsy one cannot.
    if amenities is None:
        return (AMENITY_QUESTION,
                {"choices": [label for _, label in AMENITY_OPTIONS],
                 "choose_many": True})
    values = {"name": name}
    if (area or "").strip():
        values["neighbourhood"] = area.strip()
    # Checked against the vocabulary rather than trusted. A model naming a
    # column that does not exist would otherwise reach the form as a field
    # nothing renders, the same reason propose_venues guards its enums.
    known = {key for key, _ in AMENITY_OPTIONS}
    for key in (amenities or []):
        if key in known:
            values[key] = True
    if (notes or "").strip():
        values["notes"] = notes.strip()
    return f"Collected a place to log: {name}.", {"place_form": values}


# plan_trip_tool is deliberately not here. A day built in the chat is a day
# outside the planner: no version switcher, no situation buttons, no replanning,
# and a second itinerary for the same trip the moment the parent presses Plan my
# day. The chat fills the form and hands it over; /plan builds the day. The tool
# is kept for the component test page it was written for.
TOOLS = [answer_faq_tool, extract_form_tool, find_nearby_tool,
         replan_tool, log_place_tool]


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
# extract_form_tool is here for a different reason from the FAQ's. Left to word
# its own reply, the model wrote the itinerary out in the chat instead: a day
# with no version switcher, no situation buttons and no replanning, and a second
# one generated the moment the parent pressed Plan my day. Asking it not to did
# not hold. Not writing the reply is the only way it cannot.
FINAL_ANSWER_TOOLS = frozenset({"answer_faq_tool", "extract_form_tool"})


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
                   context: dict | None = None) -> dict:
    """One turn, answered by the agent, which chooses the tool.

    The entry point for any surface that carries a message. It takes a plain
    string rather than a request, so a Telegram handler can call the same
    function the website chat does.

    One router, and it is the agent's tool selection. A classifier used to run
    first and hand matching messages to a workflow, which meant two routers in
    series on every message, three of the four tools duplicating a workflow,
    and a message answerable two ways depending on which router saw it. The
    workflows are still here and still run, on /workflows/<name>/run, where
    they are what a test page is testing rather than a second front door.

    `context` is what the request knew that the message did not: the browser's
    coordinates, and whether a started day is open on the page. Both reach the
    tools through the ContextVars below rather than as arguments, because a
    tool's arguments are the model's to fill in and neither of these is the
    model's to decide.

    "workflow" and "conversation" are still in the reply, always None. The
    widget reads both, and a missing key is not the same as an answered one.
    """
    # Set once for the whole turn, so a tool the model chooses to call answers
    # with the model the parent chose. Tools take their arguments from the
    # model, and which model to use is not a decision the model should make.
    _TURN_MODEL.set(model)
    # Read only by replan_tool. From the request rather than the message, so
    # "replan my day" typed on a page with no trip open is refused the same way
    # the workflow refuses it, instead of collecting a request nothing can act
    # on.
    _TURN_ON_TRIP.set(bool((context or {}).get("on_trip")))

    result = run_agent(message, history=history, model=model)
    # What handled this message, in the file routing accuracy is measured from.
    # The classifier used to write that line; the agent's tool choice is the
    # same decision, made by the thing that now makes it. Logged after the turn
    # rather than before, because the choice is only known once it is made.
    tools = [call["name"] for call in result["tool_calls"]]
    log_decision(message, None, ran=bool(tools), tool=tools[0] if tools else None)
    return {**result, "workflow": None, "conversation": None}


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

    # The FAQ tool wraps ask_website_chatbot, so when it ran its result already
    # carries the citations and usage numbers the widget expects.
    faq = _artifact_of("answer_faq_tool", tool_messages)
    # When the classifier missed and the agent answered a nearby question with
    # the tool, the places are still real records. Surfaced here so they render
    # as links exactly as the workflow's do.
    nearby = _artifact_of("find_nearby_tool", tool_messages)
    # A collected replan, for the in-trip page to act on with one button. Same
    # key the workflow returns, so the widget draws the same button whichever
    # path collected it.
    replan = _artifact_of("replan_tool", tool_messages)
    logged = _artifact_of("log_place_tool", tool_messages)
    # The planning form the extractor read out of their words. Surfaced under
    # the same key the workflow used, so the widget draws the same handoff card
    # it always did. Without this the form was extracted and then dropped: the
    # parent got a paragraph describing their day back instead of a form to
    # check, which is the one thing this tool exists to avoid.
    extracted = _artifact_of("extract_form_tool", tool_messages)
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
        "places": nearby.get("places", []),
        "source": nearby.get("source"),
        "replan_request": replan.get("replan_request"),
        "form": extracted.get("form"),
        # A collected place, for the widget to hand to /log-place, which
        # is the one path that writes one. Same key the workflow returns.
        "place_form": logged.get("place_form"),
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