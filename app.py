"""Travel with Tots -- Flask entry point.

Two pages: a planning page (``/plan``) that compares candidate plans, and an
in-trip page (``/trip``) that runs the chosen plan. All the real work lives in
the src/ package; this file just wires HTTP requests to that logic.
"""

import json
import os
from datetime import date
from functools import wraps

import requests
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
    PLANNER_PROMPT_PATH,
    REPLAN_DAY_PROMPT_PATH,
    WEBSITE_CHATBOT_PROMPT_PATH,
    PlanningAgent,
    PlanningAgentError,
    ReplanningAgent,
    ReplanningAgentError,
    ask_website_chatbot,
    reload_planner_prompt,
    reload_replan_day_prompt,
    reload_website_chatbot_prompt,
)
from src.data_loader import FEATURE_LABELS, load_venues
from src.db import (
    TRIP_FIELDS,
    add_child,
    add_parent,
    add_trip,
    add_venue,
    compute_age,
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
from src.interactions import (
    NEED_OPTIONS,
    SITUATION_OPTIONS,
    find_nearby,
    replan,
)
from src.itinerary import STOP_DURATION_MIN, THEMES, generate_plans
from src.models import Plan, Trip
from src.results import get_results, get_stats, save_result

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")

# Create the SQLite tables (data/app.db) on startup if they don't exist yet.
init_db()

# Chunk + embed the knowledge base in the background; the chatbot widget
# polls /rag/status and shows a progress animation until this finishes.
rag.init_index_async()

# Venue data never changes at runtime, so load it once at startup.
VENUES = load_venues()

# Transit choices and feature checkboxes, defined once and shared with the
# template so the form and the plan stay in sync.
TRANSIT_OPTIONS = ["car", "bus", "stroller", "carrier", "other"]
PACE_OPTIONS = ["relaxed", "balanced", "adventurous"]
DINING_OPTIONS = [("dine_out", "Dine out"), ("on_the_go", "Eat on the go")]
THEME_OPTIONS = [t["label"] for t in THEMES]
TRANSIT_NAP_OPTIONS = [
    ("yes", "Yes -- naps well in a stroller, car, or bus"),
    ("sometimes", "Sometimes -- depends on the situation"),
    ("no", "No -- needs a proper place to nap"),
]

# Age is capped at this many years, 0 months.
MAX_AGE_YEARS = 5
MAX_MONTHS = 11
MAX_NAPS = 4
FEATURE_OPTIONS = list(FEATURE_LABELS.items())

# Sensible defaults so the form is usable on first load.
DEFAULTS = {
    "wake_up": "07:00",
    "bedtime": "20:00",
    "naps": [],
    "transit_nap": "sometimes",
    "age_years": "2",
    "age_months": "0",
    "destination": "Vancouver",
    "accommodation": "",
    "transit": ["stroller"],
    "pace": "balanced",
    "dining": "dine_out",
    "preferred_lunch_time": "",
    "nap_notes": "",
    "extra_notes": "",
    "features": ["kid_friendly"],
    "themes": [],
    "child_ids": [],
    "plan_child_id": "",
}


def _clamp_int(value, low, high, fallback):
    """Coerce a form value to an int within [low, high], else the fallback."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return fallback


def _read_age(form):
    """Read years/months and enforce the 5-years-0-months ceiling."""
    years = _clamp_int(form.get("age_years"), 0, MAX_AGE_YEARS,
                       int(DEFAULTS["age_years"]))
    months = _clamp_int(form.get("age_months"), 0, MAX_MONTHS, 0)
    if years == MAX_AGE_YEARS:
        months = 0  # cap is exactly 5 years, 0 months
    return str(years), str(months)


def _total_months(date_of_birth):
    """A child's age in total months, for comparing who's youngest."""
    years, months = compute_age(date_of_birth)
    return years * 12 + months


def _read_form(form):
    """Normalise the raw request form into the shape the logic expects."""
    age_years, age_months = _read_age(form)
    naps = []
    for start, duration in zip(form.getlist("nap_start"), form.getlist("nap_duration")):
        if not start:
            continue
        naps.append({
            "start": start,
            "duration_min": _clamp_int(duration, 15, 180, STOP_DURATION_MIN["nap"]),
        })
    values = {
        "wake_up": form.get("wake_up") or DEFAULTS["wake_up"],
        "bedtime": form.get("bedtime") or DEFAULTS["bedtime"],
        "naps": naps[:MAX_NAPS],
        "transit_nap": form.get("transit_nap") or DEFAULTS["transit_nap"],
        "age_years": age_years,
        "age_months": age_months,
        "destination": form.get("destination") or DEFAULTS["destination"],
        "accommodation": form.get("accommodation", "").strip(),
        "transit": form.getlist("transit"),
        "pace": form.get("pace") or DEFAULTS["pace"],
        "dining": form.get("dining") or DEFAULTS["dining"],
        "preferred_lunch_time": form.get("preferred_lunch_time", ""),
        "nap_notes": form.get("nap_notes", ""),
        "extra_notes": form.get("extra_notes", ""),
        "features": form.getlist("features"),
        "themes": form.getlist("themes"),
        "child_ids": form.getlist("child_ids"),
        "plan_child_id": form.get("plan_child_id", ""),
    }
    return values


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
    with open(PLANNER_PROMPT_PATH) as f:
        planner_prompt = f.read()
    with open(REPLAN_DAY_PROMPT_PATH) as f:
        replan_prompt = f.read()
    return render_template(
        "settings.html", knowledge_base=knowledge_base, prompt=prompt,
        planner_prompt=planner_prompt, replan_prompt=replan_prompt)


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


@app.route("/settings/planner-prompt", methods=["POST"])
@login_required
@admin_required
def save_planner_prompt():
    """Save the AI itinerary planner's system prompt."""
    content = request.form.get("content", "").replace("\r\n", "\n")
    with open(PLANNER_PROMPT_PATH, "w") as f:
        f.write(content)
    reload_planner_prompt()
    flash("Planner prompt saved.")
    return redirect(url_for("settings"))


@app.route("/settings/replan-prompt", methods=["POST"])
@login_required
@admin_required
def save_replan_prompt():
    """Save the AI replanning agent's system prompt."""
    content = request.form.get("content", "").replace("\r\n", "\n")
    with open(REPLAN_DAY_PROMPT_PATH, "w") as f:
        f.write(content)
    reload_replan_day_prompt()
    flash("Replan prompt saved.")
    return redirect(url_for("settings"))


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
    chunk_size = _clamp_int(data.get("chunk_size"), 20, 2000, rag.DEFAULT_CHUNK_SIZE)
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


def _resolve_plan_child(form, parent):
    """If the parent is logged in and has children, pick which child's age
    drives the plan (respecting any checked child_ids / plan_child_id in the
    form) and overwrite the form's age fields to match. No-op otherwise.
    Shared by /plan and /plan/ai so both read the same child."""
    children_by_id = {str(c["id"]): c for c in get_children(parent["id"])} if parent else {}
    if not children_by_id:
        return form
    checked_ids = [cid for cid in form["child_ids"] if cid in children_by_id] \
        or list(children_by_id)
    plan_child_id = form["plan_child_id"] if form["plan_child_id"] in checked_ids else None
    if not plan_child_id:
        plan_child_id = min(checked_ids,
                             key=lambda cid: _total_months(children_by_id[cid]["date_of_birth"]))
    form["child_ids"], form["plan_child_id"] = checked_ids, plan_child_id
    years, months = compute_age(children_by_id[plan_child_id]["date_of_birth"])
    form["age_years"], form["age_months"] = str(years), str(months)
    return form


@app.route("/plan", methods=["GET", "POST"])
def plan():
    """Planning page: the trip form and, after generating, comparable plans."""
    if request.method == "POST":
        form = _read_form(request.form)
    else:
        form = dict(DEFAULTS)

    _resolve_plan_child(form, _current_parent())

    if request.method == "POST":
        plans = generate_plans(VENUES, form)
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
        pace_options=PACE_OPTIONS,
        dining_options=DINING_OPTIONS,
        feature_options=FEATURE_OPTIONS,
        theme_options=THEME_OPTIONS,
        transit_nap_options=TRANSIT_NAP_OPTIONS,
        max_naps=MAX_NAPS,
    )


@app.route("/plan/ai", methods=["POST"])
def plan_ai():
    """One AI-assisted plan combining the selected theme(s), as JSON. On
    demand so a parent only spends a model call when they actually ask for it."""
    form = _read_form(request.form)
    _resolve_plan_child(form, _current_parent())

    model = request.form.get("ai_model")
    if model not in ALLOWED_CHAT_MODELS:
        model = DEFAULT_MODEL
    age_months = int(form["age_years"]) * 12 + int(form["age_months"])

    try:
        result = PlanningAgent(model=model).generate_plan_for_themes(
            form["themes"],
            destination=form["destination"], age_months=age_months,
            naps=form["naps"],
            pace=form["pace"], wake_up=form["wake_up"], bedtime=form["bedtime"],
            features=form["features"], transit=form["transit"],
            dining=form["dining"], accommodation=form["accommodation"],
            nap_notes=form["nap_notes"], extra_notes=form["extra_notes"],
            transit_nap=form["transit_nap"],
            preferred_lunch_time=form["preferred_lunch_time"])
    except KeyError:
        return jsonify({"error": "The AI planner isn't configured yet."}), 500
    except requests.exceptions.RequestException:
        return jsonify({"error": "The AI planner is unavailable right now. Please try again."}), 502
    except PlanningAgentError as e:
        return jsonify({"error": str(e)}), 502

    plan_obj = Plan(label=result["label"], blurb=result["blurb"],
                    stops=result["stops"], source="ai")
    return jsonify({
        "plan": plan_obj.to_dict(),
        "context": form,
        "trip_form": form,
        "model": result["model"],
        "response_time": result["response_time"],
        "input_tokens": result.get("input_tokens"),
        "output_tokens": result.get("output_tokens"),
    })


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


@app.route("/replan/ai", methods=["POST"])
def replan_ai_route():
    """AI-assisted alternative to /replan, on demand. Returns a NEW plan as
    JSON -- callers must store it separately, never in place of the plan
    that was sent in."""
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    situation = data.get("situation")
    current_time = data.get("current_time")
    if not plan or not situation or not current_time:
        return jsonify({"error": "plan, situation, and current_time are required"}), 400

    model = data.get("model")
    if model not in ALLOWED_CHAT_MODELS:
        model = DEFAULT_MODEL

    try:
        result = ReplanningAgent(model=model).replan_day(
            situation, plan,
            current_time=current_time,
            destination=data.get("destination", ""),
            age_months=int(data.get("age_months") or 0),
            features=data.get("features") or [],
            transit=data.get("transit") or [],
            dining=data.get("dining"),
            bedtime=data.get("bedtime"),
            minutes=data.get("minutes"),
            theme=data.get("theme"),
            nap_notes=data.get("nap_notes", ""),
            extra_notes=data.get("extra_notes", ""),
        )
    except KeyError:
        return jsonify({"error": "The AI replanner isn't configured yet."}), 500
    except requests.exceptions.RequestException:
        return jsonify({"error": "The AI replanner is unavailable right now. Please try again."}), 502
    except ReplanningAgentError as e:
        return jsonify({"error": str(e)}), 502

    return jsonify(result)


@app.route("/find_nearby", methods=["POST"])
def find_nearby_route():
    """Return 1-2 venues matching an immediate need as JSON."""
    data = request.get_json(silent=True) or {}
    venues = find_nearby(data.get("need", ""), VENUES)
    return jsonify({"need": data.get("need", ""), "venues": venues})


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
