"""Travel with Tots -- Flask entry point.

Two pages: a planning page (``/plan``) that compares candidate plans, and an
in-trip page (``/trip``) that runs the chosen plan. All the real work lives in
the src/ package; this file just wires HTTP requests to that logic.
"""

import json
import os
from datetime import date
from functools import wraps

import openai
import requests
from dotenv import set_key
from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash

from src import rag
from src.agents import (
    ALLOWED_CHAT_MODELS,
    DEFAULT_MODEL,
    WEBSITE_CHATBOT_PROMPT_PATH,
    ask_website_chatbot,
    reload_website_chatbot_prompt,
)
from src.components.find_nearby import find_nearby as find_nearby_component
from src.components.geocode import GeocodeError, geocode, reverse_geocode
from src.components.plan_trip import plan_trip
from src.components.replan_trip import replan_trip
from src.components.search_web import WebSearchError, search_web
from src.data_loader import FEATURE_LABELS, VENUES
from src.dates import compute_age
from src.db import (
    TRIP_FIELDS,
    add_child,
    add_parent,
    add_trip,
    add_venue,
    delete_child,
    delete_trip,
    get_children,
    get_logged_venues_for_parent,
    get_parent,
    get_parent_by_email,
    get_trip_for_parent,
    get_trips_for_parent,
    init_db,
    update_child,
)
from src.form_helpers import (
    DEFAULTS,
    MAX_AGE_YEARS,
    MAX_MONTHS,
    MAX_NAPS,
    STOP_COUNT_FORM_MIN,
    STOP_COUNT_FORM_MAX,
    clamp_int,
    read_form,
    resolve_plan_child,
)
from src.interactions import (
    NEED_OPTIONS,
    SITUATION_OPTIONS,
    find_nearby,
    replan,
)
from src.itinerary import THEMES
from src.llms import run_agent
from src.models import Plan, Trip
from src.results import get_results, get_stats, save_result

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Create the SQLite tables (data/app.db) on startup if they don't exist yet.
init_db()

# Chunk + embed the knowledge base in the background; the chatbot widget
# polls /rag/status and shows a progress animation until this finishes.
rag.init_index_async()

# Transit choices and feature checkboxes, defined once and shared with the
# template so the form and the plan stay in sync.
TRANSIT_OPTIONS = ["car", "bus", "stroller", "carrier", "other"]
DINING_OPTIONS = [("dine_out", "Dine out"), ("on_the_go", "Eat on the go")]
THEME_OPTIONS = [t["label"] for t in THEMES]
TRANSIT_NAP_OPTIONS = [
    ("yes", "Yes -- naps well in a stroller, car, or bus"),
    ("sometimes", "Sometimes -- depends on the situation"),
    ("no", "No -- needs a proper place to nap"),
]

# How many times a parent can say "something's off" and get the plan
# adjusted again before we stop offering it and point at in-trip replanning.
MAX_REVISE_ROUNDS = 2
FEATURE_OPTIONS = list(FEATURE_LABELS.items())


def _current_parent():
    """The logged-in parent's row, or None if no one is logged in."""
    parent_id = session.get("parent_id")
    return get_parent(parent_id) if parent_id else None


def login_required(view):
    """Redirect anonymous visitors to the login page instead of the view."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if _current_parent() is None:
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Redirect logged-in non-admins away from admin-only pages. Stack under
    @login_required, which already handles anonymous visitors."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not _current_parent()["is_admin"]:
            flash("You don't have access to that page.")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)
    return wrapped


@app.context_processor
def inject_current_parent():
    """Make the logged-in parent (and their children, with computed age)
    available to every template, so the masthead auth-status link and the
    child pickers work without threading them through each render_template
    call."""
    parent = _current_parent()
    children = []
    if parent:
        for child in get_children(parent["id"]):
            years, months = compute_age(child["date_of_birth"])
            children.append({
                "id": child["id"],
                "name": child["name"],
                "gender": child["gender"],
                "date_of_birth": child["date_of_birth"],
                "age_years": years,
                "age_months": months,
            })
    return {"current_parent": parent, "current_parent_children": children}


@app.route("/")
def home():
    """Marketing landing page."""
    return render_template("index.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    """Create a parent account. Children are added afterward from the dashboard."""
    if request.method == "POST":
        form = request.form
        required = ("parent_name", "email", "password", "confirm_password")
        if any(not form.get(field, "").strip() for field in required):
            flash("Please fill in every field.")
            return render_template("signup.html", form=form)
        if form["password"] != form["confirm_password"]:
            flash("Passwords do not match.")
            return render_template("signup.html", form=form)
        email = form["email"].strip().lower()
        if get_parent_by_email(email) is not None:
            flash("An account with this email already exists.")
            return render_template("signup.html", form=form)

        parent_id = add_parent(
            email, generate_password_hash(form["password"]),
            name=form["parent_name"].strip())
        session["parent_id"] = parent_id
        return redirect(url_for("dashboard"))

    return render_template("signup.html", form={})


@app.route("/login", methods=["GET", "POST"])
def login():
    """Log an existing parent in."""
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        parent = get_parent_by_email(email)
        if parent is None or not check_password_hash(parent["password_hash"], password):
            flash("Incorrect email or password.")
            return render_template("login.html", email=email)
        session["parent_id"] = parent["id"]
        return redirect(url_for("dashboard"))

    return render_template("login.html", email="")


@app.route("/logout")
def logout():
    """Log the current parent out."""
    session.clear()
    return redirect(url_for("home"))


@app.route("/dashboard")
@login_required
def dashboard():
    """The logged-in parent's saved children, trips, and logged places."""
    parent = _current_parent()
    trips = []
    for row in get_trips_for_parent(parent["id"]):
        trip = dict(row)
        trip["plan"] = Plan.from_dict(json.loads(row["plan_json"]))
        trips.append(trip)
    places = get_logged_venues_for_parent(parent["id"])

    return render_template("dashboard.html", parent=parent, trips=trips, places=places)


@app.route("/settings")
@login_required
@admin_required
def settings():
    """Edit the chatbot's knowledge base and system prompt."""
    knowledge_base = rag.KNOWLEDGE_BASE_PATH.read_text()
    with open(WEBSITE_CHATBOT_PROMPT_PATH) as f:
        prompt = f.read()
    return render_template(
        "settings.html", knowledge_base=knowledge_base, prompt=prompt)


@app.route("/settings/knowledge-base", methods=["POST"])
@login_required
@admin_required
def save_knowledge_base():
    """Save the chatbot's knowledge base and re-chunk/re-embed it in the background."""
    content = request.form.get("content", "").replace("\r\n", "\n")
    rag.KNOWLEDGE_BASE_PATH.write_text(content)
    rag.rebuild_index(rag.get_chunk_size())
    flash("Knowledge base saved. Re-indexing in the background.")
    return redirect(url_for("settings"))


@app.route("/settings/prompt", methods=["POST"])
@login_required
@admin_required
def save_prompt():
    """Save the chatbot's system prompt."""
    content = request.form.get("content", "").replace("\r\n", "\n")
    with open(WEBSITE_CHATBOT_PROMPT_PATH, "w") as f:
        f.write(content)
    reload_website_chatbot_prompt()
    flash("Chatbot prompt saved.")
    return redirect(url_for("settings"))


@app.route("/components")
@login_required
@admin_required
def components():
    """Architecture inventory: what's real, deterministic, or still planned."""
    return render_template("components.html")


@app.route("/agent")
@login_required
@admin_required
def agent_page():
    """The AI Agent's own chat page -- isolated from the site-wide chatbot
    widget so it can be tested on its own before (if ever) replacing it."""
    return render_template("ai_agent.html")


@app.route("/agent/chat", methods=["POST"])
@login_required
@admin_required
def agent_chat_route():
    """One turn of the AI Agent (tool-calling, via LangGraph), as JSON."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    try:
        result = run_agent(message, history=data.get("history") or [])
    except KeyError:
        return jsonify({"error": "The AI Agent isn't configured yet."}), 500
    except openai.OpenAIError as e:
        print(f"AI Agent call failed: {e}")
        return jsonify({"error": "The AI Agent is unavailable right now. Please try again."}), 502
    return jsonify(result)


ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


@app.route("/search-web")
@login_required
@admin_required
def search_web_page():
    """The Web Search component's own page -- query in, results out."""
    return render_template("search_web.html", key_set=bool(os.environ.get("TAVILY_API_KEY")))


@app.route("/search-web/run", methods=["POST"])
@login_required
@admin_required
def search_web_run_route():
    """Run a Tavily Search query, as JSON."""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        results = search_web(query)
    except KeyError:
        return jsonify({"error": "Web Search isn't configured yet -- save a Tavily API key first."}), 500
    except (WebSearchError, requests.exceptions.RequestException) as e:
        print(f"Web Search call failed: {e}")
        return jsonify({"error": "Web Search is unavailable right now. Please try again."}), 502
    return jsonify({"results": results})


@app.route("/search-web/key", methods=["POST"])
@login_required
@admin_required
def search_web_key_route():
    """Save a Tavily API key into .env and use it immediately, no restart."""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400
    set_key(ENV_PATH, "TAVILY_API_KEY", key)
    os.environ["TAVILY_API_KEY"] = key
    return jsonify({"status": "saved"})


def _resolve_location(data):
    """The place a find-nearby request is centred on: reverse-geocoded
    browser coordinates, a typed address, or (when neither was sent) nothing
    at all, so a parent who declines location sharing still gets results."""
    if data.get("lat") is not None and data.get("lng") is not None:
        return reverse_geocode(data["lat"], data["lng"])
    address = (data.get("address") or "").strip()
    if address:
        return geocode(address)
    return {"city": "", "neighbourhood": "", "formatted_address": "",
            "lat": None, "lng": None}


@app.route("/find-nearby")
@login_required
@admin_required
def find_nearby_page():
    """The Find Nearby component's own page -- a location + a need in, places out."""
    return render_template(
        "find_nearby.html", need_options=NEED_OPTIONS,
        key_set=bool(os.environ.get("GOOGLE_MAPS_API_KEY")))


@app.route("/find-nearby/run", methods=["POST"])
@login_required
@admin_required
def find_nearby_run_route():
    """Resolve a location, then find places matching a need, as JSON.

    Shared coordinates are enough on their own: geocoding only adds the place
    name, so a missing key degrades to distance-ranked results rather than an
    error. A typed address genuinely needs the geocoder, since there are no
    coordinates to fall back on."""
    data = request.get_json(silent=True) or {}
    need = (data.get("need") or "").strip()
    if not need:
        return jsonify({"error": "need is required"}), 400
    has_coords = data.get("lat") is not None and data.get("lng") is not None
    try:
        location = _resolve_location(data)
    except (GeocodeError, KeyError) as e:
        if not has_coords:
            print(f"Geocoding call failed: {e}")
            return jsonify({"error": "Couldn't look up that location. Share your "
                                     "location instead, or save a Google Maps API key "
                                     "to search by address."}), 502
        print(f"Place lookup skipped, using raw coordinates: {e}")
        location = {"city": "", "neighbourhood": "", "formatted_address": "",
                    "lat": data["lat"], "lng": data["lng"]}

    result = find_nearby_component(
        need=need, city=location["city"], neighbourhood=location["neighbourhood"],
        place_name=location["formatted_address"],
        lat=location["lat"], lng=location["lng"])
    result["location"] = location
    return jsonify(result)


@app.route("/find-nearby/key", methods=["POST"])
@login_required
@admin_required
def find_nearby_key_route():
    """Save a Google Maps API key into .env and use it immediately, no restart."""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify({"error": "key is required"}), 400
    set_key(ENV_PATH, "GOOGLE_MAPS_API_KEY", key)
    os.environ["GOOGLE_MAPS_API_KEY"] = key
    return jsonify({"status": "saved"})


@app.route("/plan-trip")
@login_required
@admin_required
def plan_trip_page():
    """The Plan Trips component's own page -- trip details in, a plan out."""
    return render_template("plan_trip.html")


@app.route("/plan-trip/run", methods=["POST"])
@login_required
@admin_required
def plan_trip_run_route():
    """Run the Plan Trips component (rule-based draft + AI smoothing), as JSON."""
    data = request.get_json(silent=True) or {}
    destination = (data.get("destination") or "").strip()
    if not destination:
        return jsonify({"error": "destination is required"}), 400
    result = plan_trip(
        destination=destination,
        age_months=clamp_int(data.get("age_months"), 0, MAX_AGE_YEARS * 12 + MAX_MONTHS, 24),
        wake_up=data.get("wake_up") or DEFAULTS["wake_up"],
        bedtime=data.get("bedtime") or DEFAULTS["bedtime"],
        stop_count=clamp_int(data.get("stop_count"), STOP_COUNT_FORM_MIN,
                              STOP_COUNT_FORM_MAX, int(DEFAULTS["stop_count"])),
        dining=data.get("dining") or DEFAULTS["dining"],
    )
    return jsonify(result)


@app.route("/replan-trip")
@login_required
@admin_required
def replan_trip_page():
    """The Replan a trip component's own page -- build a sample day, then
    re-plan it for a situation. Reuses /plan-trip/run for the first step."""
    return render_template("replan_trip.html", situation_options=SITUATION_OPTIONS,
                           theme_options=THEME_OPTIONS)


@app.route("/replan-trip/run", methods=["POST"])
@login_required
@admin_required
def replan_trip_run_route():
    """Run the Replan a trip component on a held sample plan, as JSON."""
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    situation = data.get("situation")
    current_time = data.get("current_time")
    if not plan or not situation or not current_time:
        return jsonify({"error": "plan, situation, and current_time are required"}), 400
    result = replan_trip(
        plan=plan, situation=situation, current_time=current_time,
        destination="Vancouver", age_months=24,
        minutes=data.get("minutes"), theme=data.get("theme"),
    )
    return jsonify(result)


@app.route("/rag/status")
def rag_status():
    """Poll-able indexing status, used by the chatbot widget and Chunks page."""
    return jsonify(rag.get_status())


@app.route("/chunks")
@login_required
@admin_required
def chunks():
    """List every chunk the knowledge base was split into."""
    return render_template(
        "chunks.html", chunks=rag.list_chunks(), chunk_size=rag.get_chunk_size())


@app.route("/chunks/rerun", methods=["POST"])
@login_required
@admin_required
def chunks_rerun():
    """Re-chunk and re-embed the knowledge base with a different chunk size."""
    data = request.get_json(silent=True) or {}
    chunk_size = clamp_int(data.get("chunk_size"), 20, 2000, rag.DEFAULT_CHUNK_SIZE)
    rag.rebuild_index(chunk_size)
    return jsonify({"status": "started"})


# (kind, display title) for each session shown on the Results page.
RESULT_KINDS = [("chatbot", "Chatbox"), ("plan", "Generated Plan"), ("replan", "AI Replan")]


def _results_sessions():
    return [{"kind": kind, "title": title, "results": get_results(kind), "stats": get_stats(kind)}
            for kind, title in RESULT_KINDS]


@app.route("/results")
@login_required
@admin_required
def results():
    """Every rated chatbot response, AI-generated plan, and AI replan, with
    aggregate stats per session."""
    return render_template("results.html", sessions=_results_sessions())


@app.route("/results/data")
@login_required
@admin_required
def results_data():
    """Poll-able stats + full results list per session, so the Results page
    can refresh itself in place without reloading (which would also reset
    any chatbot conversation open elsewhere on the page)."""
    return jsonify({"sessions": _results_sessions()})


@app.route("/add-child", methods=["POST"])
@login_required
def add_child_route():
    """Add another child to the logged-in parent's account."""
    parent = _current_parent()
    name = request.form.get("child_name", "").strip()
    date_of_birth = request.form.get("date_of_birth", "")
    if not name or not date_of_birth:
        flash("A child needs both a name and a date of birth.")
        return redirect(url_for("dashboard"))
    add_child(parent["id"], name, request.form.get("gender") or None, date_of_birth)
    return redirect(url_for("dashboard"))


@app.route("/edit-child/<int:child_id>", methods=["POST"])
@login_required
def edit_child_route(child_id):
    """Update one of the logged-in parent's children."""
    parent = _current_parent()
    if child_id not in {child["id"] for child in get_children(parent["id"])}:
        flash("Child not found.")
        return redirect(url_for("dashboard"))
    name = request.form.get("child_name", "").strip()
    date_of_birth = request.form.get("date_of_birth", "")
    if not name or not date_of_birth:
        flash("A child needs both a name and a date of birth.")
        return redirect(url_for("dashboard"))
    update_child(child_id, name, request.form.get("gender") or None, date_of_birth)
    return redirect(url_for("dashboard"))


@app.route("/delete-child/<int:child_id>", methods=["POST"])
@login_required
def delete_child_route(child_id):
    """Remove one of the logged-in parent's children (their saved trips are kept)."""
    parent = _current_parent()
    if child_id not in {child["id"] for child in get_children(parent["id"])}:
        flash("Child not found.")
        return redirect(url_for("dashboard"))
    delete_child(child_id)
    return redirect(url_for("dashboard"))


@app.route("/delete-trip/<int:trip_id>", methods=["POST"])
@login_required
def delete_trip_route(trip_id):
    """Remove one of the logged-in parent's saved plans."""
    parent = _current_parent()
    if get_trip_for_parent(parent["id"], trip_id) is None:
        flash("Trip not found.")
        return redirect(url_for("dashboard"))
    delete_trip(trip_id, parent["id"])
    return redirect(url_for("dashboard"))


@app.route("/log-place", methods=["POST"])
@login_required
def log_place():
    """Log a kid-friendly place, family room, or nursing room."""
    parent = _current_parent()
    name = request.form.get("name", "").strip()
    if not name:
        flash("A place needs a name.")
        return redirect(url_for("dashboard"))
    add_venue(
        name,
        source="user_submitted",
        parent_id=parent["id"],
        venue_type=request.form.get("venue_type") or None,
        neighbourhood=request.form.get("neighbourhood") or None,
        kid_friendly=bool(request.form.get("kid_friendly")),
        has_family_room=bool(request.form.get("has_family_room")),
        has_nursing_room=bool(request.form.get("has_nursing_room")),
        stroller_accessible=bool(request.form.get("stroller_accessible")))
    return redirect(url_for("dashboard"))


@app.route("/save-trip", methods=["POST"])
@login_required
def save_trip():
    """Persist a generated plan as a trip for each child the logged-in parent
    picked on the planning page, so it shows up on the dashboard."""
    parent = _current_parent()
    valid_ids = {str(child["id"]) for child in get_children(parent["id"])}
    try:
        plan_data = json.loads(request.form.get("plan", ""))
        trip_form = json.loads(request.form.get("trip_form", "{}"))
    except (TypeError, ValueError):
        return redirect(url_for("plan"))
    child_ids = [cid for cid in trip_form.get("child_ids", []) if cid in valid_ids]
    if not child_ids:
        return redirect(url_for("plan"))

    fields = {field: trip_form[field] for field in TRIP_FIELDS if field in trip_form}
    fields["transit"] = json.dumps(trip_form.get("transit", []))
    fields["features"] = json.dumps(trip_form.get("features", []))
    fields["naps"] = json.dumps(trip_form.get("naps", []))
    fields["plan_label"] = plan_data.get("label")
    fields["plan_json"] = json.dumps(plan_data)
    fields["trip_date"] = date.today().isoformat()
    for child_id in child_ids:
        add_trip(parent["id"], int(child_id), **fields)
    return redirect(url_for("dashboard"))


@app.route("/plan", methods=["GET", "POST"])
def plan():
    """Planning page: the trip form and, after generating, comparable plans."""
    if request.method == "POST":
        form = read_form(request.form)
    else:
        form = dict(DEFAULTS)

    resolve_plan_child(form, _current_parent())

    revise_count = clamp_int(request.form.get("revise_count"), 0, MAX_REVISE_ROUNDS, 0)
    is_revise = revise_count > 0
    revise_message, revise_error = None, False

    if request.method == "POST":
        # The visible "extra_notes" box only ever holds what the parent typed
        # there; feedback from "Something's off" travels separately in
        # revise_feedback and is merged in here, just for the AI call.
        notes_for_ai = form["extra_notes"]
        if form["revise_feedback"]:
            notes_for_ai = (f"{notes_for_ai}\n{form['revise_feedback']}"
                            if notes_for_ai else form["revise_feedback"])
        age_months = int(form["age_years"]) * 12 + int(form["age_months"])
        result = plan_trip(
            destination=form["destination"], age_months=age_months,
            wake_up=form["wake_up"], bedtime=form["bedtime"],
            stop_count=int(form["stop_count"]), dining=form["dining"],
            naps=form["naps"], preferred_lunch_time=form["preferred_lunch_time"],
            nap_notes=form["nap_notes"], extra_notes=notes_for_ai,
            transit=form["transit"], accommodation=form["accommodation"],
            features=form["features"], strict_schedule=form["strict_schedule"],
        )
        plans = [Plan.from_dict(result)]
        if result["adjusted"]:
            if is_revise:
                revise_message = "Your plan has been updated."
        elif is_revise:
            revise_message = "Couldn't fine-tune your plan right now. Showing the plan you had."
            revise_error = True
        else:
            flash("Showing the standard plan, couldn't fine-tune it right now.")
        # The whole form is carried to the in-trip page when a plan is chosen,
        # so a plan can still be saved from there without re-asking for it.
        trip_context = form
    else:
        plans = None
        trip_context = None

    return render_template(
        "plan.html",
        form=form,
        plans=plans,
        trip_context=trip_context,
        transit_options=TRANSIT_OPTIONS,
        dining_options=DINING_OPTIONS,
        feature_options=FEATURE_OPTIONS,
        theme_options=THEME_OPTIONS,
        transit_nap_options=TRANSIT_NAP_OPTIONS,
        max_naps=MAX_NAPS,
        revise_count=revise_count,
        can_revise_more=revise_count < MAX_REVISE_ROUNDS,
        revise_message=revise_message,
        revise_error=revise_error,
    )


def _build_trip(destination, transit, features, bedtime, age_months, dining, plan_data,
                 nap_notes="", extra_notes=""):
    """Assemble a Trip around a chosen plan, shared by the fresh in-trip page
    and reopening a saved itinerary from the dashboard."""
    return Trip(
        destination=destination or "Vancouver",
        transit=transit,
        features=features,
        bedtime=bedtime,
        age_months=age_months,
        dining=dining,
        nap_notes=nap_notes,
        extra_notes=extra_notes,
        original=Plan.from_dict(plan_data),
    )


def _render_trip(trip, saved=False, trip_form=None):
    return render_template(
        "trip.html",
        trip=trip.to_dict(),
        saved=saved,
        trip_form=trip_form,
        feature_options=FEATURE_OPTIONS,
        situation_options=SITUATION_OPTIONS,
        need_options=NEED_OPTIONS,
    )


@app.route("/trip", methods=["GET", "POST"])
def trip():
    """In-trip page: render the chosen plan as a live, adjustable Trip.

    Reached by POSTing a chosen plan from the planning page. A direct GET (or a
    malformed submission) has no plan to show, so it returns to planning.
    """
    if request.method == "GET":
        return redirect(url_for("plan"))
    try:
        plan_data = json.loads(request.form.get("plan", ""))
        context = json.loads(request.form.get("context", "{}"))
    except (ValueError, TypeError):
        return redirect(url_for("plan"))

    age_months = (int(context.get("age_years") or DEFAULTS["age_years"]) * 12
                  + int(context.get("age_months") or 0))
    trip = _build_trip(
        destination=context.get("destination"),
        transit=context.get("transit", []),
        features=context.get("features", []),
        bedtime=context.get("bedtime", ""),
        age_months=age_months,
        dining=context.get("dining", ""),
        plan_data=plan_data,
        nap_notes=context.get("nap_notes", ""),
        extra_notes=context.get("extra_notes", ""),
    )
    return _render_trip(trip, trip_form=context)


@app.route("/trip/<int:trip_id>")
@login_required
def view_trip(trip_id):
    """Re-open a previously saved itinerary from the dashboard."""
    parent = _current_parent()
    row = get_trip_for_parent(parent["id"], trip_id)
    if row is None or not row["plan_json"]:
        flash("That saved trip doesn't have a full itinerary to show.")
        return redirect(url_for("dashboard"))
    if row["child_dob"]:
        years, months = compute_age(row["child_dob"])
        age_months = years * 12 + months
    else:
        age_months = int(DEFAULTS["age_years"]) * 12 + int(DEFAULTS["age_months"])
    trip = _build_trip(
        destination=row["destination"],
        transit=json.loads(row["transit"] or "[]"),
        features=json.loads(row["features"] or "[]"),
        bedtime=row["bedtime"] or "",
        age_months=age_months,
        dining=row["dining"] or "",
        plan_data=json.loads(row["plan_json"]),
        nap_notes=row["nap_notes"] or "",
        extra_notes=row["extra_notes"] or "",
    )
    return _render_trip(trip, saved=True)


@app.route("/replan", methods=["POST"])
def replan_route():
    """Re-plan the rest of the day and return a NEW plan as JSON."""
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    current_time = data.get("current_time")
    if not plan or not current_time:
        return jsonify({"error": "plan and current_time are required"}), 400
    return jsonify(replan(plan, data.get("situation", ""), current_time,
                          VENUES, data.get("features") or [],
                          bedtime=data.get("bedtime"), minutes=data.get("minutes"),
                          theme=data.get("theme")))


@app.route("/replan/adjust", methods=["POST"])
def replan_adjust_route():
    """Re-plan the rest of the day (rule-based), then let the AI adjuster
    smooth it -- the same draft-then-adjust pattern /plan uses. Returns a NEW
    plan as JSON, with "adjusted" noting whether the AI step actually ran;
    callers must store it separately from the plan sent in."""
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    situation = data.get("situation")
    current_time = data.get("current_time")
    if not plan or not situation or not current_time:
        return jsonify({"error": "plan, situation, and current_time are required"}), 400

    # A note typed for this one replan (e.g. "we're leaving now, find
    # something indoor nearby") is merged in just for the AI call -- never
    # stored back into the trip's own extra_notes.
    extra_notes = data.get("extra_notes", "")
    replan_note = data.get("replan_note", "")
    if replan_note:
        extra_notes = f"{extra_notes}\n{replan_note}" if extra_notes else replan_note

    result = replan_trip(
        plan=plan, situation=situation, current_time=current_time,
        destination=data.get("destination", ""), age_months=int(data.get("age_months") or 0),
        features=data.get("features") or [], transit=data.get("transit") or [],
        dining=data.get("dining"), bedtime=data.get("bedtime"),
        minutes=data.get("minutes"), theme=data.get("theme"),
        nap_notes=data.get("nap_notes", ""), extra_notes=extra_notes,
    )
    return jsonify(result)


@app.route("/find_nearby", methods=["POST"])
def find_nearby_route():
    """Venues matching an immediate need as JSON, narrowed to the parent's
    location when the browser shared it. Location is optional on purpose: a
    parent who declines the permission prompt still gets the original
    location-blind results rather than an error."""
    data = request.get_json(silent=True) or {}
    need = data.get("need", "")
    try:
        location = _resolve_location(data)
    except (GeocodeError, KeyError) as e:
        # Naming the place is a nicety; the coordinates are the useful part,
        # so a missing or failing geocoder must not throw them away.
        print(f"Find-nearby place lookup skipped, using raw coordinates: {e}")
        location = {"city": "", "neighbourhood": "", "formatted_address": "",
                    "lat": data.get("lat"), "lng": data.get("lng")}

    if location["city"] or location["lat"] is not None:
        result = find_nearby_component(
            need=need, city=location["city"],
            neighbourhood=location["neighbourhood"],
            place_name=location["formatted_address"],
            lat=location["lat"], lng=location["lng"])
        return jsonify({"need": need, "venues": result["places"],
                        "source": result["source"], "location": location})

    return jsonify({"need": need, "venues": find_nearby(need, VENUES),
                    "source": "curated", "location": None})


@app.route("/chatbot", methods=["POST"])
def chatbot_route():
    """Answer a question about how the website works, as JSON."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400

    model = data.get("model")
    if model not in ALLOWED_CHAT_MODELS:
        model = DEFAULT_MODEL

    if rag.get_status()["state"] != "ready":
        return jsonify({"error": "The knowledge base is still indexing. Please try again shortly."}), 503

    try:
        result = ask_website_chatbot(message, model=model, history=data.get("history") or [])
    except KeyError:
        return jsonify({"error": "The chatbot isn't configured yet."}), 500
    except requests.exceptions.RequestException:
        return jsonify({"error": "The chatbot is unavailable right now. Please try again."}), 502

    return jsonify(result)


@app.route("/feedback", methods=["POST"])
def feedback_route():
    """Save a thumbs up/down rating on a chatbot response, an AI-generated
    plan, or an AI replan, as JSON."""
    data = request.get_json(silent=True) or {}
    question = (data.get("question") or "").strip()
    response_text = data.get("response") or ""
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
