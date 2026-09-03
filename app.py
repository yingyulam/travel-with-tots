"""Travel with Tots -- Flask entry point.

Two pages: a planning page (``/plan``) that compares candidate plans, and an
in-trip page (``/trip``) that runs the chosen plan. All the real work lives in
the src/ package; this file just wires HTTP requests to that logic.
"""

import json
import os
import secrets
import traceback
from contextlib import closing
from datetime import date, datetime, timezone
from functools import wraps

import openai
import requests
from dotenv import set_key
from flask import (
    Flask,
    flash,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from src import candidates, db, rag
from src.web import (account, auth, devpages, guards, lookups, places,
                     planning, settings, trip, venues)
from src.web.guards import (CHAT_LIMIT, CHAT_WINDOW, LOGIN_LIMIT,
                            LOGIN_WINDOW, LOOKUP_LIMIT, LOOKUP_WINDOW,
                            PLAN_LIMIT, PLAN_WINDOW, admin_required,
                            login_required, rate_limited)
from src.workflows import propose_venues
from src.agents import (
    ALLOWED_CHAT_MODELS,
    DEFAULT_MODEL,
    WEBSITE_CHATBOT_PROMPT_PATH,
    reload_website_chatbot_prompt,
)
from src.components.extract_form import FormExtractionError, extract_form
from src.components.find_nearby import find_nearby as find_nearby_component
from src.components.find_nearby import searchable
from src.components.geocode import (
    UNKNOWN_LOCATION,
    GeocodeError,
    resolve_location,
    reverse_geocode,
)
from src.components.place_search import PlaceSearchError, search_places
from src import osm, postgres, ratelimit, supabase_sync
from src.components.plan_trip import plan_days, plan_trip
from src.components.replan_trip import replan_trip
from src.components.search_web import WebSearchError, search_web

from src.data_loader import (
    CITIES,
    FEATURE_LABELS,
    NEIGHBOURHOODS,
    SETTINGS,
    SUPPORTED_CITIES,
    VENUE_TYPES,
    get_venues,
    interest_options,
)
from src.dates import MAX_TRIP_DAYS, compute_age, parse_date
from src.db import (
    AMENITY_OPTIONS,
    PromotionError,
    TRIP_FIELDS,
    add_child,
    add_parent,
    add_trip,
    add_venue,
    delete_child,
    delete_trip,
    delete_venue,
    get_children,
    get_logged_venues_for_parent,
    get_parent,
    get_parent_by_email,
    get_pending_hours_checks,
    get_pending_submissions,
    get_rejected_submissions,
    get_trip_for_parent,
    get_trip_group,
    get_trips_for_parent,
    get_unverified_venues,
    get_venues_missing_hours,
    init_db,
    mark_verified,
    promote_submission,
    reject_submission,
    resolve_hours_check,
    restore_submission,
    set_venue_default_hours,
    update_child,
    update_venue,
)
from src.form_helpers import (
    DEFAULT_TRANSIT,
    normalise_transit,
    DEFAULTS,
    default_form,
    DINING_OPTIONS,
    MAX_AGE_YEARS,
    MAX_MONTHS,
    MAX_NAPS,
    NAP_DURATION_MAX_MINUTES,
    NAP_DURATION_MIN_MINUTES,
    STOP_COUNT_FORM_MIN,
    STOP_COUNT_FORM_MAX,
    TRANSIT_NAP_OPTIONS,
    TRANSIT_OPTIONS,
    WALK_BUDGET_BY_TRANSIT,
    WALK_BUDGET_FORM_OPTIONS,
    clamp_int,
    read_form,
    resolve_plan_child,
    trip_dates,
    trip_too_long,
)
from src.interactions import (
    MAX_REPLAN_MINUTES,
    MIN_REPLAN_MINUTES,
    NEED_OPTIONS,
    SITUATION_OPTIONS,
    replan,
)
from src.agent import handle_message, run_workflow_turn
from src.models import Day, Plan, Trip
from src.plan_diff import describe_changes, summarise
from src.results import get_results, get_stats, save_result
from src.workflows import log_a_place, workflows_by_trigger

app = Flask(__name__)

# Signs the session cookie, which holds only session["parent_id"]. A known key
# therefore means anyone can mint a cookie naming any parent, admin included,
# with no password: authentication bypass rather than mere tampering. It used
# to fall back to a literal committed to this repo, so following the documented
# setup (cp .env.example .env, which never mentioned SECRET_KEY) shipped that
# known key. No fallback now, and no default worth having: one that works is
# one an attacker also has.
try:
    app.secret_key = os.environ["SECRET_KEY"]
except KeyError:
    raise RuntimeError(
        "SECRET_KEY is not set. It signs session cookies, so there is no safe "
        "default. Generate one and add it to .env:\n"
        "  python3 -c \"import secrets; print('SECRET_KEY=' + secrets.token_hex(32))\" >> .env"
    ) from None

# Over HTTPS the session cookie should never be sent in clear, and a deployment
# is HTTPS-only while local development is not. Off by default so `flask run` on
# http://localhost still logs you in; render.yaml turns it on.
#
# SameSite=Lax is also what stands in for CSRF tokens: a browser will not send
# this cookie on a cross-site POST, so a form on somebody else's page cannot act
# as a logged-in parent. That is why logout is a POST rather than a GET -- Lax
# *does* send the cookie on a cross-site top-level GET, so a link or an <img>
# could otherwise log a parent out.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
    # Every route reads a body somebody else sent. Unset, Flask will buffer a
    # body of any size, which on a 512MB instance is a one-request memory kill.
    # Larger than any real submit here: the biggest is the review page posting
    # a page of hours for a batch of candidates.
    MAX_CONTENT_LENGTH=256 * 1024,
)

# Caps on what a caller may put in a chat turn. `history` is echoed back by the
# widget, so it is the caller's to inflate, and every turn of it is paid for as
# prompt tokens. A real conversation is a few short turns.
MAX_MESSAGE_CHARS = 4_000
MAX_HISTORY_TURNS = 10
MAX_HISTORY_CHARS = 4_000
# A rating carries the question and answer it is about, and they are written
# to data/results.json, which is read whole on every save.
MAX_FEEDBACK_CHARS = 8_000

# Each blueprint owns one subject's routes. Registered here rather than
# discovered, so the set is explicit and a broken import is loud.
app.register_blueprint(account.bp)
app.register_blueprint(devpages.bp)
app.register_blueprint(places.bp)
app.register_blueprint(planning.bp)
app.register_blueprint(trip.bp)
app.register_blueprint(settings.bp)
app.register_blueprint(venues.bp)
app.register_blueprint(auth.bp)

# Create the SQLite tables (data/app.db) on startup if they don't exist yet.
# A no-op when the data source is Supabase: the tables are already there.
init_db()

# Chunk + embed the knowledge base in the background; the chatbot widget
# polls /rag/status and shows a progress animation until this finishes.
rag.init_index_async()



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


@app.errorhandler(Exception)
def _json_endpoints_answer_json(error):
    """Keep a JSON endpoint answering JSON, even when it fails.

    The chat widget, the planner and find-nearby all parse every reply. A Flask
    HTML error page therefore surfaced as the browser's own parse message --
    Safari says "The string did not match the expected pattern" -- which told a
    parent nothing and told the log nothing either. The routes catch the errors
    they expect; this is for the ones nobody predicted, which are precisely the
    ones worth seeing.

    HTML requests keep Flask's normal behaviour, so an ordinary page still gets
    an ordinary error page.
    """
    unexpected = not isinstance(error, HTTPException)
    if unexpected:
        traceback.print_exc()
    if not request.is_json:
        return error if isinstance(error, HTTPException) else ("Server error", 500)
    if unexpected:
        return jsonify({"error": "Something went wrong on the server."}), 500
    return jsonify({"error": error.description}), error.code


@app.after_request
def _security_headers(response):
    """Headers a browser needs in order to defend the page.

    No Content-Security-Policy yet: the templates carry inline handlers and
    styles, so a useful policy would need 'unsafe-inline', which is a policy
    that mostly is not one. Worth doing properly rather than for show.
    """
    # This app has no reason to be framed, and framing is how a click on an
    # invisible overlay becomes a click on "Delete trip".
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Stop a browser guessing a content type: a stored venue note sniffed as
    # HTML would run as HTML.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # A trip page's URL says where a family is going. Do not hand it to every
    # site they click through to.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.context_processor
def inject_current_parent():
    """Make the logged-in parent (and their children, with computed age)
    available to every template, so the masthead auth-status link and the
    child pickers work without threading them through each render_template
    call."""
    parent = guards.current_parent()
    children = []
    if parent:
        for child in get_children(parent["id"]):
            years, months = compute_age(child["date_of_birth"])
            children.append({
                "id": child["id"],
                "name": child["name"],
                "date_of_birth": child["date_of_birth"],
                "age_years": years,
                "age_months": months,
            })
    return {"current_parent": parent, "current_parent_children": children}


# What the chat widget's dropdown offers. Free first, then the default, so the
# order reads cheapest-first and the checked one is the one that answers.
CHAT_MODEL_LABELS = {
    "nvidia/nemotron-3-super-120b-a12b:free": "Nemotron 3 Super (free)",
    "openai/gpt-4o-mini": "GPT-4o mini (paid)",
}


@app.context_processor
def inject_chat_models():
    """The models the widget may offer, from the server's own allowed set.

    The dropdown used to be a hand-written list in the template, and it drifted:
    it still defaulted to `openrouter/free` after the server default had moved,
    so every page load selected a model nobody had chosen. Rendering it from
    ALLOWED_CHAT_MODELS means adding or removing one is a single edit.
    """
    offered = [m for m in CHAT_MODEL_LABELS if m in ALLOWED_CHAT_MODELS]
    return {"chat_models": offered,
            "chat_model_labels": CHAT_MODEL_LABELS,
            "default_chat_model": DEFAULT_MODEL}


@app.route("/")
def home():
    """Marketing landing page."""
    return render_template("index.html")


























































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


@app.route("/chatbot", methods=["POST"])
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


@app.route("/workflows/<name>/run", methods=["POST"])
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


@app.route("/feedback", methods=["POST"])
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8016, debug=True)
