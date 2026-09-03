"""The chat widget's endpoints: a turn, a workflow turn, and a rating.

Everything a caller sends here is theirs to inflate, so the caps below are
enforced on the way in rather than trusted. Identity is the exception that
matters most: it comes from the session and only from the session, because a
parent_id in the body would read another parent's children and saved trips.
"""

import openai
import requests
from flask import Blueprint, jsonify, request

from src.ai import rag
from src.ai.tool_agent import handle_message, run_workflow_turn
from src.ai.agents import ALLOWED_CHAT_MODELS, DEFAULT_MODEL
from src.results import save_result
from src.web import guards
from src.web.guards import (CHAT_LIMIT, CHAT_WINDOW, admin_required,
                            login_required, rate_limited)

bp = Blueprint("chat", __name__)

# Caps on what a caller may put in a chat turn. `history` is echoed back by the
# widget, so it is the caller's to inflate, and every turn of it is paid for as
# prompt tokens. A real conversation is a few short turns.
MAX_MESSAGE_CHARS = 4_000
MAX_HISTORY_TURNS = 10
MAX_HISTORY_CHARS = 4_000
# A rating carries the question and answer it is about, and they are written
# to data/results.json, which is read whole on every save.
MAX_FEEDBACK_CHARS = 8_000

def _message_context(data):
    """What the *browser* told us that the message did not: coordinates, when
    permission was already given, and whether a trip is open.

    All client-supplied, so the values are checked here rather than where they
    are used, and anything that is not a real pair of numbers becomes no
    location at all. Deliberately pure and session-free: who is asking is added
    by the route, from the session, because that is not the browser's to claim.

    Built from literal keys and never spreading `data`, which is what stops a
    client-supplied key it does not know about reaching a workflow. Keep it
    that way.
    """
    # Whether a started day is open on the page that sent this. The workflow
    # that shifts a day needs to know, so it can say "open your trip first"
    # rather than collecting a situation it cannot act on.
    context = {"on_trip": data.get("on_trip") is True}

    location = data.get("location")
    if not isinstance(location, dict):
        return context
    lat, lng = location.get("lat"), location.get("lng")
    if not (isinstance(lat, (int, float)) and isinstance(lng, (int, float))):
        return context
    if isinstance(lat, bool) or isinstance(lng, bool):
        return context
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return context
    return {**context, "lat": float(lat), "lng": float(lng)}


def _chat_context(data):
    """Everything a chat turn is given beyond the message itself.

    Identity comes from the session and only from the session: `parent_id` is
    what every recall is scoped by, so a client-supplied one would read another
    parent's children and saved trips. Read through `guards.current_parent()` rather
    than `session.get("parent_id")` raw, because SQLite reuses row ids, so a
    stale cookie can eventually name a real but different parent; the lookup
    returns None for a row that is gone.

    Our value is merged last, so it wins outright even if the browser half of
    the context ever grows a key of the same name.
    """
    parent = guards.current_parent()
    return {**_message_context(data),
            "parent_id": parent["id"] if parent else None}


def _capped_history(history):
    """The recent turns of a conversation, short enough to pay for.

    `history` is held by the widget and echoed back with every message, so its
    length and contents are the caller's to choose, and every turn is billed as
    prompt tokens. Unbounded, one request could carry a prompt of any size.

    The newest turns are kept rather than the oldest, because that is the part
    of a conversation the next answer depends on.
    """
    if not isinstance(history, list):
        return []
    turns = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        if not isinstance(turn, dict):
            continue
        content = turn.get("content")
        if not isinstance(content, str):
            continue
        turns.append({"role": "user" if turn.get("role") == "user" else "assistant",
                      "content": content[:MAX_HISTORY_CHARS]})
    return turns


@bp.route("/chatbot", methods=["POST"])
@rate_limited(CHAT_LIMIT, CHAT_WINDOW)
def chatbot_route():
    """One turn of the chat bubble, as JSON.

    The bubble is the AI Agent's interface. An intent classifier looks first for
    a workflow the message is asking for and runs it; anything else falls
    through to the tool-calling agent, which answers from the knowledge base,
    reads a described day into the form, plans a day, or finds somewhere nearby.

    The routing lives in agent.handle_message rather than here, so a Telegram
    handler can reuse it. The reply carries "workflow", the name that ran or
    None, which the widget shows as a badge."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": "That message is too long. Please shorten it."}), 413

    model = data.get("model")
    if model not in ALLOWED_CHAT_MODELS:
        model = DEFAULT_MODEL

    # Only a build in progress blocks a turn, and only because it finishes in
    # seconds. Every other state degrades on its own: rag.retrieve returns
    # nothing unless the index is ready, and the prompt then says the knowledge
    # base holds no answer. Refusing on "error" instead took the whole bubble
    # down for a fault in one tool -- the workflows and the other three tools
    # never touch retrieval, and a parent replanning a day got a 503 because the
    # FAQ was broken.
    if rag.get_status()["state"] == "indexing":
        return jsonify({"error": "The knowledge base is still indexing. Please try again shortly."}), 503

    # The widget echoes back whatever workflow state it was given, so this is
    # client-controlled: anything that is not a dict is dropped rather than
    # handed to a workflow, which would reach it as an attribute error.
    conversation = data.get("conversation")
    if not isinstance(conversation, dict):
        conversation = None

    try:
        # No force_workflow: that let any caller pick which workflow their
        # message ran, on a route open to everyone, and the test pages post to
        # /workflows/<name>/run behind an admin login instead. `conversation` is
        # different -- it is how a workflow the agent started keeps the turn.
        result = handle_message(message, history=_capped_history(data.get("history")),
                                model=model, conversation=conversation,
                                context=_chat_context(data))
    except KeyError:
        return jsonify({"error": "The chatbot isn't configured yet."}), 500
    except (openai.OpenAIError, requests.exceptions.RequestException) as e:
        print(f"Chat turn failed: {e}")
        return jsonify({"error": "The chatbot is unavailable right now. Please try again."}), 502

    return jsonify(result)


@bp.route("/workflows/<name>/run", methods=["POST"])
@login_required
@admin_required
def run_workflow_route(name):
    """One turn of a named workflow, as JSON: the workflow test pages' backend.

    Their own route rather than /chatbot with a flag. /chatbot is the parent's
    front door and its orchestration is the agent's; this is a demo surface and
    its orchestration is a named workflow. One router each, and the same
    run_workflow_turn behind both, so a workflow cannot answer two ways.

    Admin-only, which force_workflow never was: it arrived in the body of a
    public route, so any caller could pick the workflow their message reached.

    Deliberately not rate limited. The Listen mode on those pages sends message
    after message on purpose, and the caller is an authenticated admin rather
    than the open internet the chat limits exist for.
    """
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > MAX_MESSAGE_CHARS:
        return jsonify({"error": "That message is too long. Please shorten it."}), 413

    model = data.get("model")
    if model not in ALLOWED_CHAT_MODELS:
        model = DEFAULT_MODEL

    # Client-controlled, so anything that is not a dict is dropped rather than
    # handed to a workflow, which would reach it as an attribute error.
    conversation = data.get("conversation")
    if not isinstance(conversation, dict):
        conversation = None

    try:
        result = run_workflow_turn(name, message, conversation=conversation,
                                   context=_chat_context(data), model=model,
                                   forced=True)
    except (openai.OpenAIError, requests.exceptions.RequestException) as e:
        print(f"Workflow turn failed: {e}")
        return jsonify({"error": "That workflow is unavailable right now."}), 502

    if result is None:
        # Said plainly rather than answered by the agent. This page exists to
        # watch one workflow run, so quietly answering as something else would
        # be the page reporting a pass on a test it never ran.
        return jsonify({"error": f"No workflow named {name!r} could run that."}), 404
    return jsonify(result)


@bp.route("/feedback", methods=["POST"])
@rate_limited(CHAT_LIMIT, CHAT_WINDOW)
def feedback_route():
    """Save a thumbs up/down rating on a chatbot response, an AI-generated
    plan, or an AI replan, as JSON.

    Both texts are truncated rather than refused: a rating is worth keeping
    even if the answer it quotes was long. They are stored in
    data/results.json, which is read whole and rewritten on every save, so
    without a cap an anonymous caller sets how much memory that costs.
    """
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()[:MAX_FEEDBACK_CHARS]
    response_text = (data.get("response") or "")[:MAX_FEEDBACK_CHARS]
    rating = data.get("rating")
    kind = data.get("kind") or "chatbot"
    if (not question or not response_text or rating not in ("up", "down")
            or kind not in ("chatbot", "plan", "replan")):
        return jsonify({"error": "question, response, and a valid rating/kind are required"}), 400

    save_result(
        question=question,
        response=response_text,
        rating=rating,
        model=data.get("model") or DEFAULT_MODEL,
        response_time=data.get("response_time"),
        input_tokens=data.get("input_tokens"),
        output_tokens=data.get("output_tokens"),
        kind=kind,
    )
    return jsonify({"status": "saved"})
