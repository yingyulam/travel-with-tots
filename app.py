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
from src.web import (account, auth, devpages, guards, lookups, settings,
                     venues)
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
app.register_blueprint(settings.bp)
app.register_blueprint(venues.bp)
app.register_blueprint(auth.bp)

# Create the SQLite tables (data/app.db) on startup if they don't exist yet.
# A no-op when the data source is Supabase: the tables are already there.
init_db()

# Chunk + embed the knowledge base in the background; the chatbot widget
# polls /rag/status and shows a progress animation until this finishes.
rag.init_index_async()

# Choice lists the template renders. The vocabularies themselves live in
# src/form_helpers.py (see TRANSIT_OPTIONS and friends); these two are derived
# from data the app already owns.
# The kinds of place a parent can ask for, read from the venues that exist so
# the form never offers something nothing can satisfy. Computed per request
# rather than at import, because an import or an approval changes it.
FEATURE_OPTIONS = list(FEATURE_LABELS.items())

# How many times a parent can say "something's off" and get the plan
# adjusted again before we stop offering it and point at in-trip replanning.
MAX_REVISE_ROUNDS = 2


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


def _chosen_model(value):
    """The model a request asked for, or the default if it asked for nothing
    the app offers. The chat widget's dropdown is the one place a parent picks
    a model, so planning and replanning read their choice from the request
    rather than each keeping a default of their own."""
    return value if value in ALLOWED_CHAT_MODELS else DEFAULT_MODEL


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


@app.route("/delete-trip/<int:trip_id>", methods=["POST"])
@login_required
def delete_trip_route(trip_id):
    """Remove one of the logged-in parent's saved plans."""
    parent = guards.current_parent()
    if get_trip_for_parent(parent["id"], trip_id) is None:
        flash("Trip not found.")
        return redirect(url_for("account.dashboard"))
    delete_trip(trip_id, parent["id"])
    return redirect(url_for("account.dashboard"))


@app.route("/log-place")
@login_required
def log_place_page():
    """The Log a Place page: pin a spot, name what's there, say what it offers.

    Parent-facing rather than an admin test page, and the Log a place workflow
    card points here: a test surface that exercises the page a parent uses
    cannot drift away from it.
    """
    # No key_set flag: it read os.environ, which is fixed when the process
    # starts, so a key added to .env afterwards left the page claiming there
    # was none. The search route reports that accurately when asked.
    #
    # `?logged=<id>` is how a just-submitted place gets shown back. Redirecting
    # here after the POST rather than rendering it directly means a refresh
    # re-reads the row instead of re-submitting the form.
    logged_id = request.args.get("logged", type=int)
    return render_template(
        "log_a_place.html", amenity_options=AMENITY_OPTIONS, form={},
        stored=_logged_place(guards.current_parent()["id"], logged_id) if logged_id else None)


@app.route("/log-place", methods=["POST"])
@login_required
def log_place():
    """Log a kid-friendly place, family room, or nursing room.

    Comes back to this page showing what was stored, rather than redirecting to
    the dashboard. The whole chain (a name, a geocode, a row) is only
    observable if its output appears where it was run, and a bare redirect gave
    no confirmation that anything had happened at all.
    """
    # Storing is opt in: only a POST carrying "store" writes a row, and
    # anything else fills the form in and stops there. That is how the chat
    # hands over a place it collected, so the parent lands on the real page
    # with their answers in place, can move the map pin, and submits
    # themselves. Same template, no second code path.
    #
    # It was the other way round, a "prefill" flag that turned storing off,
    # which made writing a venue row the default for any POST that lost the
    # flag. A submit button's name is exactly what a post loses.
    if not request.form.get("store"):
        return render_template(
            "log_a_place.html", amenity_options=AMENITY_OPTIONS, stored=None,
            form=request.form)

    parent = guards.current_parent()
    try:
        record = log_a_place.store(parent["id"], request.form)
    except ValueError as e:
        flash(str(e).capitalize() + ".")
        return redirect(url_for("log_place_page"))
    return redirect(url_for("log_place_page", logged=record["id"]))


@app.route("/log-place/area", methods=["POST"])
@login_required
def log_place_area_route():
    """Coordinates to a readable area, so dropping a pin can say where it
    landed rather than showing a pair of decimals. Server-side on purpose: the
    browser's map needs no key, and the geocoding key stays out of it."""
    data = request.get_json(silent=True) or {}
    if data.get("lat") is None or data.get("lng") is None:
        return jsonify({"error": "lat and lng are required"}), 400
    try:
        location = reverse_geocode(data["lat"], data["lng"])
    except (GeocodeError, KeyError) as e:
        print(f"Logged-place area lookup failed: {e}")
        return jsonify({"error": "Couldn't name that spot."}), 502
    return jsonify({"area": location["formatted_address"] or location["city"],
                    "city": location["city"],
                    "neighbourhood": location["neighbourhood"]})








@app.route("/log-place/search", methods=["POST"])
@login_required
def log_place_search_route():
    """Name lookup for the Log a Place pin."""
    return lookups.place_search_response()


@app.route("/plan/accommodation-search", methods=["POST"])
@rate_limited(LOOKUP_LIMIT, LOOKUP_WINDOW)
def accommodation_search_route():
    """Name lookup for the accommodation pin on the planning form.

    Open to anyone, because /plan is: a parent plans a day before they have an
    account. Rate limited rather than closed, because every call is a billed
    Google Places request and the field searches as the parent types: the two
    cost guards in static/plan-accommodation.js are the client's manners, and
    this is what holds when the caller is not that client.
    """
    return lookups.place_search_response()


def _logged_place(parent_id, place_id):
    """One of this parent's own submissions, or None.

    Reuses the query the dashboard already runs, which filters on both
    parent_id and user_submitted, so a curated row can never match and no new
    db function is needed.
    """
    for place in get_logged_venues_for_parent(parent_id):
        if place["id"] == place_id:
            return place
    return None


def _owns_place(parent_id, place_id):
    return _logged_place(parent_id, place_id) is not None


@app.route("/edit-place/<int:place_id>", methods=["POST"])
@login_required
def edit_place_route(place_id):
    """Correct one of the logged-in parent's own logged places."""
    parent = guards.current_parent()
    if not _owns_place(parent["id"], place_id):
        flash("Place not found.")
        return redirect(url_for("account.dashboard"))
    name = request.form.get("name", "").strip()
    if not name:
        flash("A place needs a name.")
        return redirect(url_for("account.dashboard"))
    update_venue(
        place_id, parent["id"],
        name=name,
        type=request.form.get("venue_type", "").strip() or None,
        neighbourhood=request.form.get("neighbourhood", "").strip() or None,
        notes=request.form.get("notes", "").strip() or None)
    # Correcting their own place is another observation by the same parent, so
    # it lands as a dated report rather than overwriting a column.
    db.record_amenities(
        place_id,
        {key: bool(request.form.get(key)) for key, _ in AMENITY_OPTIONS},
        reported_by=parent["id"], note="Corrected by the parent who logged it.")
    return redirect(url_for("account.dashboard"))


@app.route("/delete-place/<int:place_id>", methods=["POST"])
@login_required
def delete_place_route(place_id):
    """Remove one of the logged-in parent's own logged places."""
    parent = guards.current_parent()
    if not _owns_place(parent["id"], place_id):
        flash("Place not found.")
        return redirect(url_for("account.dashboard"))
    delete_venue(place_id, parent["id"])
    return redirect(url_for("account.dashboard"))


@app.route("/save-trip", methods=["POST"])
@login_required
def save_trip():
    """Persist a generated plan as a trip, one per child the parent picked on
    the planning page, so it shows up on the dashboard.

    A child is optional. The day belongs to the parent, and child_id only
    records whose age shaped it: worth having, not what makes the plan real.
    Requiring one turned Save into a redirect back to /plan that saved nothing
    and said nothing, for the parent least likely to know why.
    """
    parent = guards.current_parent()
    valid_ids = {str(child["id"]) for child in get_children(parent["id"])}
    try:
        # One day or a whole visit. The in-trip page and the planning page both
        # post "plans"; "plan" is what everything older posts, and reads as a
        # visit of one rather than as a second path through here.
        posted = request.form.get("plans")
        plan_data = (json.loads(posted) if posted
                     else [json.loads(request.form.get("plan", ""))])
        trip_form = json.loads(request.form.get("trip_form", "{}"))
    except (TypeError, ValueError):
        return redirect(url_for("plan"))
    if not isinstance(plan_data, list) or not plan_data:
        return redirect(url_for("plan"))
    child_ids = [cid for cid in trip_form.get("child_ids", []) if cid in valid_ids]

    fields = {field: trip_form[field] for field in TRIP_FIELDS if field in trip_form}
    fields["transit"] = trip_form.get("transit") or DEFAULT_TRANSIT
    fields["naps"] = json.dumps(trip_form.get("naps", []))
    # What ties the days of one visit together. Generated here, never taken
    # from the post: it decides which rows are read back as one trip, and a
    # client-supplied one would let a parent staple their days onto somebody
    # else's. Every trip gets one, including a trip of one day, so reading a
    # group is never a special case.
    fields["trip_group_id"] = secrets.token_urlsafe(12)
    dates = trip_dates(trip_form)

    # [None] is one trip with nobody attached, not zero trips. child_id is
    # nullable and ON DELETE SET NULL, so the dashboard already reads a trip
    # whose child is missing; this is the same row, arrived at sooner.
    for child_id in child_ids or [None]:
        for index, plan in enumerate(plan_data):
            day = dict(fields)
            day["day_index"] = index
            day["plan_label"] = plan.get("label")
            day["plan_json"] = json.dumps(plan)
            # The day this plan is for. From the plan itself when it says, so a
            # day saved from the in-trip page keeps its own date rather than
            # the first day's.
            day["trip_date"] = (plan.get("trip_date")
                                or (dates[index] if index < len(dates) else "")
                                or date.today().isoformat())
            add_trip(parent["id"], int(child_id) if child_id else None, **day)
    return redirect(url_for("account.dashboard"))


def _planner_kwargs(form, extra_notes, model):
    """The inputs plan_days takes, read off one planning form.

    Shared by /plan and by replanning the rest of a trip, because two readings
    of the same form drift apart: the cascade would quietly plan the later days
    of a visit on defaults the parent never chose -- a different wake-up, a
    tighter travel limit -- and the plans would look wrong for no visible
    reason.

    Every field goes through DEFAULTS, because this also reads a form that
    arrived as JSON from the in-trip page rather than from read_form, and a
    missing key there should mean "what the form would have shown" rather than
    a KeyError.
    """
    def field(name):
        value = form.get(name, DEFAULTS.get(name))
        return DEFAULTS.get(name) if value is None else value

    return dict(
        destination=field("destination"),
        age_months=int(field("age_years") or 0) * 12 + int(field("age_months") or 0),
        wake_up=field("wake_up"), bedtime=field("bedtime"),
        stop_count=int(field("stop_count")), dining=field("dining"),
        naps=field("naps"), preferred_lunch_time=field("preferred_lunch_time"),
        nap_notes=field("nap_notes"), extra_notes=extra_notes,
        transit=field("transit"), accommodation=field("accommodation"),
        accommodation_lat=field("accommodation_lat"),
        accommodation_lng=field("accommodation_lng"),
        features=field("features"), strict_schedule=field("strict_schedule"),
        interest=field("interest"), transit_nap=field("transit_nap"),
        walk_budget=field("walk_budget"), beyond_budget=field("beyond_budget"),
        model=model,
    )


@app.route("/plan", methods=["GET", "POST"])
@rate_limited(PLAN_LIMIT, PLAN_WINDOW)
def plan():
    """Planning page: the trip form and, after generating, comparable plans.

    Generating is opt in: only a POST carrying "generate" builds a day, and
    anything else fills the form in and stops there. That is how the chat
    assistant hands over a form it collected, so the parent lands on the real
    page with their answers in place and presses Generate themselves. Same
    read_form, same template, no second code path.

    It was the other way round, a "prefill" flag that turned generating off,
    which made a ten-second AI call the default for any POST that lost the
    flag. A submit button's name is exactly what a post loses: disable the
    submitter mid-submit, or serve a cached older script, and the safe action
    silently becomes the expensive one.
    """
    should_generate = request.method == "POST" and request.form.get("generate")
    if request.method == "POST":
        form = read_form(request.form)
    else:
        form = default_form()

    resolve_plan_child(form, guards.current_parent())

    # Every kind of place unticked. The form itself blocks this, so getting
    # here means a hand-made post or a page whose script did not run: say so
    # rather than guessing which of the ten kinds they meant, and rather than
    # quietly planning as though they had ticked them all.
    interest_error = should_generate and not form["interest"]
    # More days than we will lay out in one go. Refused rather than clamped:
    # planning the first week of a fortnight and saying nothing is the kind of
    # answer that reads as a bug to whoever asked for the fortnight.
    too_long = trip_too_long(form) if should_generate else None
    should_generate = should_generate and not interest_error and not too_long

    hours_report = None
    adjustment = None
    revise_count = clamp_int(request.form.get("revise_count"), 0, MAX_REVISE_ROUNDS, 0)
    is_revise = revise_count > 0
    revise_message, revise_error = None, False

    if should_generate:
        # The visible "extra_notes" box only ever holds what the parent typed
        # there; feedback from "Something's off" travels separately in
        # revise_feedback and is merged in here, just for the AI call.
        notes_for_ai = form["extra_notes"]
        if form["revise_feedback"]:
            notes_for_ai = (f"{notes_for_ai}\n{form['revise_feedback']}"
                            if notes_for_ai else form["revise_feedback"])
        # One plan per day of the visit, planned in order so no two days send
        # the family to the same place. A one-day trip is a list of one date
        # and takes the single call it always took.
        results = plan_days(
            trip_dates(form),
            **_planner_kwargs(form, notes_for_ai,
                              _chosen_model(request.form.get("model"))))
        # One entry per day: the plan itself, the date it is for, and what the
        # hours check and the travel limit had to say about that day. Kept
        # together so a card can report on its own day rather than the page
        # reporting on all of them at once.
        days = [{"plan": Plan.from_dict(r), "date": r.get("trip_date", ""),
                 "index": i, "hours": r.get("hours"),
                 "out_of_range": r.get("out_of_range") or []}
                for i, r in enumerate(results)]
        plans = [d["plan"] for d in days]
        # The whole visit, ready to post to /trip and /save-trip. Each plan
        # carries the date it is for: Plan is a day's content and Day owns the
        # calendar, so a Plan round-tripped through to_dict() has no date, and
        # three days were being saved as three copies of the first.
        #
        # Serialised here rather than in the template: Jinja's map(attribute=)
        # hands back the bound method rather than calling it.
        plans_json = [{**d["plan"].to_dict(), "trip_date": d["date"]}
                      for d in days]
        result = results[0]
        hours_report = result.get("hours")
        # Any day the travel limit thinned. One offer for the trip: a parent
        # who wants to look further wants it for the visit, not for Tuesday.
        out_of_range = [k for r in results for k in (r.get("out_of_range") or [])]
        # Whether the AI step ran, and whether it moved anything. Not shown on
        # a first generate: the parent asked for a day out, and either way they
        # got a real plan. plan.html logs this to the console instead, so it
        # stays visible while developing.
        adjustment = {"adjusted": result["adjusted"], "changed": result["changed"]}
        # A revise is the exception. The parent asked for a specific change, so
        # saying nothing would read as the button having done nothing. Three
        # outcomes, described by what happened to their plan rather than by
        # which step of ours produced it.
        if is_revise:
            if not result["adjusted"]:
                revise_message = "We couldn't update your plan this time."
                revise_error = True
            elif not result["changed"]:
                revise_message = ("This is already the best plan for your day. "
                                  "No changes needed.")
            else:
                revise_message = "Your plan has been updated."
        # The whole form is carried to the in-trip page when a plan is chosen,
        # so a plan can still be saved from there without re-asking for it.
        trip_context = form
    else:
        plans = None
        plans_json = None
        days = None
        trip_context = None
        out_of_range = None

    return render_template(
        "plan.html",
        form=form,
        plans=plans,
        hours_report=hours_report,
        adjustment=adjustment,
        out_of_range=out_of_range,
        interest_error=interest_error,
        days=days,
        plans_json=plans_json,
        too_long=too_long,
        max_trip_days=MAX_TRIP_DAYS,
        trip_context=trip_context,
        supported_cities=SUPPORTED_CITIES,
        transit_options=TRANSIT_OPTIONS,
        walk_budget_options=WALK_BUDGET_FORM_OPTIONS,
        walk_budget_by_transit=WALK_BUDGET_BY_TRANSIT,
        dining_options=DINING_OPTIONS,
        feature_options=FEATURE_OPTIONS,
        interest_options=interest_options(),
        transit_nap_options=TRANSIT_NAP_OPTIONS,
        max_naps=MAX_NAPS,
        nap_duration_min=NAP_DURATION_MIN_MINUTES,
        nap_duration_max=NAP_DURATION_MAX_MINUTES,
        revise_count=revise_count,
        can_revise_more=revise_count < MAX_REVISE_ROUNDS,
        revise_message=revise_message,
        revise_error=revise_error,
    )


def _build_trip(destination, transit, bedtime, age_months, dining, days,
                 trip_date="", nap_notes="", extra_notes="", group_id=""):
    """Assemble a Trip from its days, shared by the fresh in-trip page and by
    reopening a saved itinerary from the dashboard.

    `days` is a list of Day. A one-day trip is a list of one, and takes exactly
    the same path: there is no single-day branch here to fall out of step.
    """
    return Trip(
        destination=destination or "Vancouver",
        transit=transit,
        trip_date=trip_date or (days[0].date if days else ""),
        bedtime=bedtime,
        age_months=age_months,
        dining=dining,
        nap_notes=nap_notes,
        extra_notes=extra_notes,
        group_id=group_id,
        days=days,
    )


def _day_from(plan_data, index=0, date="", accommodation="",
              accommodation_lat=None, accommodation_lng=None, trip_id=None):
    """One Day from a plan dict and where they are staying for it.

    The accommodation is passed per day even though the form asks once. That is
    the seam a different hotel on Thursday goes through, and it costs nothing
    to thread now while there is one caller per shape.
    """
    return Day(
        date=date or plan_data.get("trip_date", ""),
        index=index,
        original=Plan.from_dict(plan_data),
        accommodation=accommodation,
        accommodation_lat=accommodation_lat,
        accommodation_lng=accommodation_lng,
        trip_id=trip_id,
    )


def _trip_venue_ids(trip):
    """Every venue id anywhere in the trip: all days, all versions of each.

    Across the whole visit rather than one day, because the report panel opens
    on whichever day the parent is looking at and one round trip should arm all
    of them. Seven days of four stops is 28 ids, which is one query.
    """
    return sorted({stop["venue"]["id"]
                   for day in trip.get("days", [])
                   for plan in day.get("plans", [])
                   for stop in plan.get("stops", [])
                   if stop.get("venue") and stop["venue"].get("id")})


def _trip_venue_reports(trip):
    """{venue_id: {field: bool}} for every venue in the trip.

    So the report panel can open already showing what we hold. Without it a
    parent cannot tell "nobody has said" from "we think there is one", and
    unticking could not mean "that has gone".

    Approved only, because that is what the app holds. What this parent has
    reported and nobody has checked is a separate map: see _trip_pending_reports.
    """
    ids = _trip_venue_ids(trip)
    return db.reported_flags(ids) if ids else {}


def _trip_pending_reports(trip):
    """{venue_id: {field: bool}} of this parent's own unreviewed reports.

    So the panel can show them their tick still standing, marked as waiting,
    rather than appearing to have swallowed it. Their own only: somebody else's
    unchecked claim is exactly what the queue exists to keep out of view.
    """
    parent = guards.current_parent()
    ids = _trip_venue_ids(trip)
    if not parent or not ids:
        return {}
    return db.pending_reports_for(parent["id"], ids)


def _render_trip(trip, saved=False, trip_form=None, trip_id=None, open_day=0):
    as_dict = trip.to_dict()
    return render_template(
        "trip.html",
        trip=as_dict,
        venue_reports=_trip_venue_reports(as_dict),
        pending_reports=_trip_pending_reports(as_dict),
        saved=saved,
        trip_form=trip_form,
        trip_id=trip_id,
        open_day=open_day,
        reportable_flags=[(key, FEATURE_LABELS[key])
                          for key in db.REPORTABLE_FIELDS],
        conditional_flags=db.CONDITIONAL_ON_CAN_EAT,
        feature_options=FEATURE_OPTIONS,
        situation_options=SITUATION_OPTIONS,
        transit_labels=dict(TRANSIT_OPTIONS),
        interest_options=interest_options(),
        need_options=NEED_OPTIONS,
        # The custom-duration inputs' min/max come from the same constants the
        # server clamps to, so the browser and the clamp cannot disagree.
        min_replan_minutes=MIN_REPLAN_MINUTES,
        max_replan_minutes=MAX_REPLAN_MINUTES,
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
        # "plan" is one day, "plans" is a whole visit. Both are accepted: the
        # planning page posts the list, and a one-day plan posted by anything
        # older -- a saved snapshot, a test, a bookmarked form -- is a list of
        # one rather than a second code path.
        posted = request.form.get("plans")
        plan_data = json.loads(posted) if posted else [json.loads(request.form.get("plan", ""))]
        context = json.loads(request.form.get("context", "{}"))
    except (ValueError, TypeError):
        return redirect(url_for("plan"))
    if not isinstance(plan_data, list) or not plan_data:
        return redirect(url_for("plan"))

    age_months = (int(context.get("age_years") or DEFAULTS["age_years"]) * 12
                  + int(context.get("age_months") or 0))
    dates = trip_dates(context)
    days = [_day_from(plan, index=i,
                      date=dates[i] if i < len(dates) else "",
                      accommodation=context.get("accommodation", ""),
                      accommodation_lat=context.get("accommodation_lat") or None,
                      accommodation_lng=context.get("accommodation_lng") or None)
            for i, plan in enumerate(plan_data)]
    trip = _build_trip(
        destination=context.get("destination"),
        transit=normalise_transit(context.get("transit")),
        bedtime=context.get("bedtime", ""),
        age_months=age_months,
        dining=context.get("dining", ""),
        days=days,
        trip_date=context.get("trip_date", ""),
        nap_notes=context.get("nap_notes", ""),
        extra_notes=context.get("extra_notes", ""),
    )
    return _render_trip(trip, trip_form=context)


@app.route("/trip/<int:trip_id>")
@login_required
def view_trip(trip_id):
    """Re-open a previously saved itinerary from the dashboard."""
    parent = guards.current_parent()
    row = get_trip_for_parent(parent["id"], trip_id)
    if row is None or not row["plan_json"]:
        flash("That saved trip doesn't have a full itinerary to show.")
        return redirect(url_for("account.dashboard"))
    if row["child_dob"]:
        years, months = compute_age(row["child_dob"])
        age_months = years * 12 + months
    else:
        age_months = int(DEFAULTS["age_years"]) * 12 + int(DEFAULTS["age_months"])
    # Every day of the same visit, when this row belongs to one. A row saved
    # before multi-day existed has no group and is a group of one.
    rows = ([row] if not row["trip_group_id"]
            else [r for r in get_trip_group(parent["id"], row["trip_group_id"])
                  if r["plan_json"]] or [row])
    days = [_day_from(json.loads(r["plan_json"]), index=i,
                      date=r["trip_date"] or "",
                      accommodation=r["accommodation"] or "",
                      accommodation_lat=r["accommodation_lat"],
                      accommodation_lng=r["accommodation_lng"],
                      trip_id=r["id"])
            for i, r in enumerate(rows)]
    trip = _build_trip(
        destination=row["destination"],
        transit=normalise_transit(row["transit"]),
        trip_date=rows[0]["trip_date"] or "",
        bedtime=row["bedtime"] or "",
        age_months=age_months,
        dining=row["dining"] or "",
        days=days,
        nap_notes=row["nap_notes"] or "",
        extra_notes=row["extra_notes"] or "",
        group_id=row["trip_group_id"] or "",
    )
    # The day the parent clicked, so reopening day three opens on day three.
    opened = next((i for i, r in enumerate(rows) if r["id"] == trip_id), 0)
    return _render_trip(trip, saved=True, trip_id=trip_id, open_day=opened)


# What a parent tells us when they say a venue was shut when they got there. It
# goes to the same queue scripts/verify_hours.py fills, so an admin settles a
# parent and OpenStreetMap in one place rather than two.
#
# They are no longer asked whether our *hours* look wrong. Hours appear only
# inside the collapsed "Why" panel on a stop, so asking a parent to check them
# was asking about data they had almost certainly never seen. What they were
# plainly told is a time to be somewhere, and whether the door was open then is
# something they can see -- so that is what is asked, and `closed_at` records the
# time, which is the part a reviewer needs in order to check anything.
PARENT_HOURS_SOURCE = "parent"
PARENT_HOURS_FINDING = "A parent found this venue closed when we sent them."


@app.route("/venues/<int:venue_id>/report", methods=["POST"])
@login_required
def report_amenities(venue_id):
    """Record what a parent saw at one stop, as JSON.

    Written pending, and a reviewer decides. The person standing in the
    building is still the best source there is for whether it has a change
    table, so this stays the easiest report in the app to file -- but an
    unchecked claim from one visitor changed what every other parent was shown,
    which is what the queue is for.

    The risk that argued against a queue is that these fields never fill, so
    the review page settles a parent's whole batch for one venue in a single
    action rather than one click per tick.

    Keyed on the venue rather than the trip, because that is what the report is
    about and because a day being run has not necessarily been saved. A
    `trip_id` in the body is still checked when it is there, so a link to
    somebody else's trip is refused rather than quietly ignored.

    The body is {"found": [field...], "shown": [field...], "hours_wrong": bool,
    "trip_id": int|null}. `shown` matters: a field the panel never offered must
    not be read as "the parent says no". A highchair is not offered at a park,
    and answering for it would invent a claim nobody made.
    """
    parent = guards.current_parent()
    data = request.get_json(silent=True) or {}
    trip_id = data.get("trip_id")
    if trip_id is not None and get_trip_for_parent(parent["id"], trip_id) is None:
        return jsonify({"error": "That trip isn't yours."}), 403

    found = set(data.get("found") or ())
    shown = [f for f in db.REPORTABLE_FIELDS if f in set(data.get("shown") or ())]
    known = db.reported_flags([venue_id]).get(venue_id, {})

    # An unticked box is not the same as "I looked and there was none": it is
    # also what a parent leaves alone. So an unticked field is only written when
    # somebody had already claimed it was there, which makes it a correction.
    values = {f: (f in found) for f in shown if f in found or f in known}
    # Held for review. The parent standing in the building is still the best
    # source there is, but nothing they say reaches another parent until a
    # reviewer agrees: see db.reported_flags, which reads approved rows only.
    written = db.record_amenities(values=values, venue_id=venue_id,
                                  reported_by=parent["id"], approved=False)

    if data.get("hours_wrong"):
        # The scheduled time, when the widget sends it. "Closed at 17:00" is
        # checkable; "reported on the 31st" is not, and that is all this used
        # to say. Trimmed and length-capped because it is client-supplied and
        # ends up rendered on the review page.
        at = str(data.get("closed_at") or "").strip()[:5]
        says = (f"Closed at {at} on {date.today()}, when the plan sent them there"
                if at else f"Reported closed on {date.today()}")
        db.record_hours_check(venue_id, PARENT_HOURS_SOURCE, source_says=says,
                              finding=PARENT_HOURS_FINDING)
        written += 1

    return jsonify({"saved": written, "message": (
        "Thank you for your contribution! Your report has been submitted and "
        "is awaiting review." if written else "Nothing new to add.")})


@app.route("/replan", methods=["POST"])
@rate_limited(PLAN_LIMIT, PLAN_WINDOW)
def replan_route():
    """Re-plan the rest of the day and return a NEW plan as JSON."""
    data = request.get_json(silent=True) or {}
    plan = data.get("plan")
    current_time = data.get("current_time")
    if not plan or not current_time:
        return jsonify({"error": "plan and current_time are required"}), 400
    return jsonify(replan(plan, data.get("situation", ""), current_time,
                          get_venues(on_date=parse_date(data.get("trip_date"))),
                          data.get("features") or [],
                          bedtime=data.get("bedtime"), minutes=data.get("minutes"),
                          interest=data.get("interest")))


@app.route("/replan/adjust", methods=["POST"])
@rate_limited(PLAN_LIMIT, PLAN_WINDOW)
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
        minutes=data.get("minutes"), interest=data.get("interest"),
        nap_notes=data.get("nap_notes", ""), extra_notes=extra_notes,
        trip_date=data.get("trip_date"),
        model=_chosen_model(data.get("model")),
    )
    return jsonify(result)


@app.route("/trip/replan-remaining", methods=["POST"])
@rate_limited(PLAN_LIMIT, PLAN_WINDOW)
def replan_remaining_route():
    """Fresh plans for the days after one the parent has just changed.

    Only ever reached because they accepted a change and then asked for this.
    A replan on Tuesday can leave Thursday visiting somewhere Tuesday now goes,
    or free up somewhere Tuesday has dropped, and neither is something to fix
    behind their back -- so this returns proposals, with the difference spelled
    out per day, and changes nothing.

    These are whole days rebuilt rather than mid-day replans: a later day has
    not started, so plan_days is the right tool and `used_names` is how it is
    told what the days before it have taken.
    """
    data = request.get_json(silent=True) or {}
    days = data.get("days")
    if not isinstance(days, list) or not days:
        return jsonify({"error": "days are required"}), 400
    if len(days) > MAX_TRIP_DAYS:
        return jsonify({"error": f"at most {MAX_TRIP_DAYS} days"}), 400

    form = data.get("form") or {}
    # What the earlier days have spoken for, including the change just
    # accepted. Client-supplied and only a planning input: the worst a wrong
    # list can do is make a day thinner, and it is the page that knows which
    # version of each earlier day the parent settled on.
    used = [name for name in (data.get("used_names") or []) if isinstance(name, str)]

    fresh = plan_days([day.get("date") or "" for day in days], used_names=used,
                      **_planner_kwargs(form, form.get("extra_notes", ""),
                                        _chosen_model(data.get("model"))))
    proposals = []
    for was, now in zip(days, fresh):
        before = (was.get("plan") or {}).get("stops") or []
        changes = describe_changes(before, now["stops"])
        proposals.append({**now, "changes": changes,
                          "change_summary": summarise(changes)})
    return jsonify({"days": proposals})


@app.route("/find_nearby", methods=["POST"])
@rate_limited(LOOKUP_LIMIT, LOOKUP_WINDOW)
def find_nearby_route():
    """Venues matching an immediate need as JSON, narrowed to the parent's
    location when the browser shared it. Location is optional on purpose: a
    parent who declines the permission prompt still gets the original
    location-blind results rather than an error."""
    data = request.get_json(silent=True) or {}
    need = data.get("need", "")
    try:
        location = lookups.resolve_body_location(data)
    except (GeocodeError, KeyError) as e:
        # Naming the place is a nicety; the coordinates are the useful part,
        # so a missing or failing geocoder must not throw them away.
        print(f"Find-nearby place lookup skipped, using raw coordinates: {e}")
        location = {**UNKNOWN_LOCATION,
                    "lat": data.get("lat"), "lng": data.get("lng")}

    # One call, whether or not anything resolved: `searchable` supplies the city
    # this app covers when nothing did. It used to be two branches, and the
    # no-location one returned find_nearby(VENUES) with a hardcoded "curated",
    # having consulted nothing but the sample list and never the web.
    where = searchable(location)
    result = find_nearby_component(
        need=need, city=where["city"], neighbourhood=where["neighbourhood"],
        place_name=where["formatted_address"],
        lat=where["lat"], lng=where["lng"],
        # How the family gets between stops, which is what decides how far a
        # lunch stop may reasonably be. Absent for every other need, which
        # ignores it.
        transit=data.get("transit") or "",
        # The stop they are standing at, so a Maps handoff can be anchored on
        # it when the browser shared no location. Without it the only fallback
        # left is the city, which is not a place anyone eats lunch.
        near_place=data.get("near_place") or "")
    return jsonify({"need": need, "venues": result["places"],
                    "source": result["source"],
                    # Set only for lunch: where to look for what the venue table
                    # cannot hold. None for every other need.
                    "maps_search_url": result["maps_search_url"],
                    "location": location if location["lat"] is not None
                    or location["city"] else None})


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
