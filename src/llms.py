"""AI Agent: a LangGraph tool-calling agent over OpenRouter.

Isolated from the site-wide FAQ chatbot (src/agents.py's ask_website_chatbot)
while it's still being tested -- see the "AI Agent" card on /components. Its
tools are thin wrappers around other components (plan_trip) and existing
logic (interactions.find_nearby); nothing there changes.
"""

import os

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from . import interactions
from .components.plan_trip import plan_trip
from .data_loader import load_venues

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Scoped to just this agent (not agents.py's shared DEFAULT_MODEL, used by the
# chatbot/planner/replanner) -- picked to dodge google/gemma-4-26b-a4b-it:free's
# shared-pool rate limiting, still a free model.
AGENT_MODEL = "openai/gpt-oss-20b:free"

# Venue data never changes at runtime, loaded once, same as app.py.
VENUES = load_venues()

SYSTEM_PROMPT = (
    "You are Travel with Tots' trip-planning assistant. You can plan a day "
    "trip or find a nearby kid-friendly place using the tools you're given. "
    "If asked anything outside that, say so plainly and point the parent at "
    "the chat bubble in the corner of the site for general questions."
)


@tool
def find_nearby_tool(need: str) -> list[dict]:
    """Find 1-2 kid-friendly venues nearby matching an immediate need.
    need must be one of: restaurant, family_room, changing_table,
    nursing_room, quiet_spot."""
    return interactions.find_nearby(need, VENUES)


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


def _build_agent():
    model = ChatOpenAI(
        base_url=OPENROUTER_BASE_URL,
        api_key=os.environ["OPENROUTER_API_KEY"],
        model=AGENT_MODEL,
    )
    return create_react_agent(model, [find_nearby_tool, plan_trip_tool], prompt=SYSTEM_PROMPT)


def run_agent(message: str, history: list[dict] | None = None) -> dict:
    """Runs one turn of the AI Agent: given a free-text message and prior
    turns ({"role", "content"} dicts, the same shape the site's chatbot
    already uses), lets it decide whether to call a tool or just reply.
    Returns {"reply", "model", "tool_calls"} -- tool_calls (name + raw
    output, in call order) is the concrete proof a tool was actually
    invoked, not just something the reply claims happened; empty if the
    agent answered directly. Never logs or prints the API key."""
    messages = []
    for turn in (history or []):
        cls = HumanMessage if turn.get("role") == "user" else AIMessage
        messages.append(cls(turn.get("content", "")))
    messages.append(HumanMessage(message))

    result = _build_agent().invoke({"messages": messages})
    reply = result["messages"][-1].content
    tool_calls = [{"name": m.name, "output": m.content}
                  for m in result["messages"] if isinstance(m, ToolMessage)]
    return {"reply": reply, "model": AGENT_MODEL, "tool_calls": tool_calls}
