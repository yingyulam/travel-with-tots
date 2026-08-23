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

import os

import requests
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from .agents import DEFAULT_MODEL, ask_website_chatbot
from .components.extract_form import FormExtractionError, extract_form
from .components.find_nearby import find_nearby as find_nearby_component
from .components.plan_trip import plan_trip
from .data_loader import SUPPORTED_CITIES
from .intent import classify_intent, log_decision
from .workflows import runnable_message_workflows

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

SYSTEM_PROMPT = (
    "You are Travel with Tots' assistant, answering in the chat bubble on the "
    "site. Use your tools rather than your own knowledge:\n"
    "- answer_faq_tool for any question about how the site works or what it "
    "can do. Pass the parent's question through unchanged, and reply with what "
    "it gives you, keeping its [Source N] markers exactly as they are.\n"
    "- extract_form_tool whenever a parent describes a day out they want, so "
    "their words become the planning form. Pass their whole description. This "
    "is the tool for a description even when it sounds like a request for a "
    "plan: the form is filled in first so the parent can check it.\n"
    "- plan_trip_tool only when they explicitly ask you to build the itinerary "
    "now. Never on a first description of a day.\n"
    "- find_nearby_tool when they need somewhere nearby right now.\n"
    "Use exactly one tool per message. Keep replies short and plain. After "
    "extract_form_tool, say which details you picked up and ask them to check "
    "the form. Never write an itinerary of your own."
)

# Errors a tool must swallow: the chat route only catches KeyError and
# OpenAIError, so anything else raised inside a tool escapes as a 500.
TOOL_ERRORS = (FormExtractionError, requests.exceptions.RequestException, KeyError)


@tool(response_format="content_and_artifact")
def answer_faq_tool(question: str) -> tuple[str, dict]:
    """Answer a question about the Travel with Tots website from its knowledge
    base, with [Source N] citations. Use for anything about how the site works,
    what it can do, or how to use a feature."""
    try:
        result = ask_website_chatbot(question)
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
    found = ", ".join(result["found"]) or "nothing"
    return f"Filled in from their words: {found}.", result


@tool(response_format="content_and_artifact")
def find_nearby_tool(need: str) -> tuple[str, dict]:
    """Find 1-2 kid-friendly venues nearby matching an immediate need.
    need must be one of: restaurant, family_room, changing_table,
    nursing_room, quiet_spot, other."""
    # The component, not interactions.find_nearby: that one is the need-matching
    # predicate, with no location narrowing and no web fallback. This tool used
    # to call it directly, so the agent answered these from the sample venue
    # list while the real chain sat unused.
    #
    # This is the safety net for a phrasing the intent classifier misses; the
    # registered workflow is the main path. Both call the same component, so
    # they cannot answer the same question two different ways.
    try:
        result = find_nearby_component(need=need, city=SUPPORTED_CITIES[0])
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
    return plan_trip(destination=destination, age_months=age_months,
                      wake_up=wake_up, bedtime=bedtime, stop_count=stop_count,
                      dining=dining)


TOOLS = [answer_faq_tool, extract_form_tool, find_nearby_tool, plan_trip_tool]


def _build_agent(model: str):
    chat = ChatOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=model,
    )
    return create_react_agent(chat, TOOLS, prompt=SYSTEM_PROMPT)


def handle_message(message: str, history: list[dict] | None = None,
                   model: str = DEFAULT_MODEL,
                   conversation: dict | None = None,
                   context: dict | None = None) -> dict:
    """One turn, routed: a workflow if the message asks for one, else the agent.

    This is the entry point for any surface that carries a message. It takes a
    plain string rather than a request, so a Telegram handler can call the same
    function the website chat does.

    Two routers coexist here, deliberately rather than accidentally. The
    classifier owns workflows and runs first; the tool-calling agent below owns
    everything else and is unchanged. The one message both could handle is a
    described day, and the classifier wins because it runs first.

    The reply always carries "workflow": the name that ran, or None. None rather
    than an absent key, so a caller can tell "no workflow matched" from "this
    response predates routing".

    `context` is what the request knew that the message did not, today the
    browser's coordinates. Every workflow is handed it; most ignore it.

    `conversation` is {"workflow", "state"} when a workflow is mid-flow. While
    one is, the classifier is skipped entirely: "two year old" and "yes" are
    answers to the question just asked, not new intents, and routing them would
    derail the conversation the parent is already in.
    """
    offered = runnable_message_workflows()
    in_flight = (conversation or {}).get("workflow")

    if in_flight:
        chosen = in_flight
        # Client-supplied, so it is checked rather than trusted. A non-dict
        # would reach the workflow as an attribute error; None just restarts.
        state = conversation.get("state")
        if not isinstance(state, dict):
            state = None
    else:
        chosen = classify_intent(message, [workflow for workflow, _ in offered])
        state = None

    run = next((r for w, r in offered if w["name"] == chosen), None)

    if run is not None:
        try:
            result = run(message, state, context)
        except TOOL_ERRORS as e:
            # A workflow that fails must not cost the parent their turn, so it
            # falls through to the agent. Logged as not-run, so the trace shows
            # the routing was right even where the execution was not.
            print(f"Workflow {chosen!r} failed, answering as the chatbot: {e}")
            log_decision(message, chosen, ran=False)
        else:
            log_decision(message, chosen, ran=True)
            # A workflow that returns a state is still talking; one that returns
            # None is finished, and the next message starts fresh at the
            # classifier.
            next_state = result.get("state")
            # The widget's keys, so a workflow reply renders like any other.
            # The usage fields are genuinely unknown here: they come from the
            # FAQ tool, which did not run.
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
                "form": result.get("form"),
                "open_form": result.get("open_form", False),
                # Places render as cards with real Maps links, so they travel
                # as data rather than as URLs written into the reply text.
                "places": result.get("places") or [],
                "source": result.get("source"),
                "ask_location": result.get("ask_location", False),
            }
    else:
        log_decision(message, None, ran=False)

    # Falling through ends any flow: the parent has moved on, and holding stale
    # state would silently resume it on their next message.
    return {**run_agent(message, history=history, model=model),
            "workflow": None, "conversation": None}


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

    result = _build_agent(model).invoke({"messages": messages})
    tool_messages = [m for m in result["messages"] if isinstance(m, ToolMessage)]

    # The FAQ tool wraps ask_website_chatbot, so when it ran its result already
    # carries the citations and usage numbers the widget expects.
    faq = _artifact_of("answer_faq_tool", tool_messages)
    # When the classifier missed and the agent answered a nearby question with
    # the tool, the places are still real records. Surfaced here so they render
    # as links exactly as the workflow's do.
    nearby = _artifact_of("find_nearby_tool", tool_messages)
    return {
        "reply": result["messages"][-1].content,
        "sources": faq.get("sources", []),
        "places": nearby.get("places", []),
        "source": nearby.get("source"),
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
