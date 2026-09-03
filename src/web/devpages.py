"""A page per component and per workflow, where each one really runs.

The point of /components and /workflows: every piece is exercised on its own
page before a workflow chains it, so a chain adds sequencing rather than new
risk. Admin-only, and the audience is whoever is building the app rather than
a parent.

Thin by design. Each route reads a form, calls one component, and renders what
came back; the substance is in src/components and src/workflows.
"""

import os
import pathlib

import requests
from dotenv import set_key
from flask import Blueprint, jsonify, render_template, request

from src.components.extract_form import FormExtractionError, extract_form
from src.components.find_nearby import find_nearby as find_nearby_component
from src.components.find_nearby import searchable
from src.components.geocode import GeocodeError
from src.components.plan_trip import plan_trip
from src.components.replan_trip import replan_trip
from src.components.search_web import WebSearchError, search_web
from src.data_loader import interest_options
from src.form_helpers import (DEFAULTS, MAX_AGE_YEARS, MAX_MONTHS,
                              STOP_COUNT_FORM_MAX, STOP_COUNT_FORM_MIN,
                              clamp_int)
from src.interactions import NEED_OPTIONS, SITUATION_OPTIONS
from src.results import get_results, get_stats
from src.web import lookups
from src.web.guards import admin_required, login_required
from src.workflows import workflows_by_trigger

bp = Blueprint("devpages", __name__)

# Where a key typed into a component page is written: the project's own .env,
# which is the file the app reads at startup, so a key added here survives a
# restart. Resolved from this module rather than the working directory, which
# is not the project root under gunicorn.
ENV_PATH = str(pathlib.Path(__file__).resolve().parent.parent.parent / ".env")

# (kind, display title) for each session shown on the Results page.
RESULT_KINDS = [("chatbot", "Chatbox"), ("plan", "Generated Plan"),
                ("replan", "AI Replan")]

@bp.route("/components")
@login_required
@admin_required
def components():
    """Architecture inventory: what's real, deterministic, or still planned."""
    return render_template("components.html")


@bp.route("/workflows")
@login_required
@admin_required
def workflows():
    """End-to-end use cases, each a chain of the components above."""
    return render_template("workflows.html", trigger_groups=workflows_by_trigger())


@bp.route("/agent")
@login_required
@admin_required
def agent_page():
    """The AI Agent's test page. Deliberately has no chat of its own: it uses
    the real bubble every page carries, and adds a panel showing what the agent
    actually did with each message, so what's tested here is what a parent gets.
    There is no /agent/chat any more -- that was a second implementation."""
    return render_template("ai_agent.html")


@bp.route("/workflows/plan-from-chat")
@login_required
@admin_required
def plan_from_chat_page():
    """The Plan from chat workflow's test page: describe a day in the bubble,
    watch the agent turn it into the planning form."""
    return render_template("plan_from_chat.html")


@bp.route("/workflows/replan-on-the-go")
@login_required
@admin_required
def replan_on_the_go_page():
    """The Replan on the go workflow's test page: say what changed in the
    bubble, watch the request it collected.

    Its own page rather than /trip: that one holds the plan and does the
    re-timing, and is where this workflow hands off to, so it cannot also be
    the surface for watching the conversation that fills the request."""
    return render_template("replan_on_the_go.html")


@bp.route("/workflows/log-a-place")
@login_required
@admin_required
def log_place_from_chat_page():
    """The Log a place workflow's test page: tell the bubble about a place,
    watch the submission fill in.

    Its own page rather than /log-place: that one is the form a parent
    submits, and it is where this workflow hands off to, so it cannot also be
    the surface for watching the conversation that fills it."""
    return render_template("log_place_from_chat.html")


@bp.route("/workflows/find-nearby-place")
@login_required
@admin_required
def find_nearby_place_page():
    """The Find a nearby place workflow's test page: ask the bubble for
    somewhere you need, watch the workflow answer.

    Its own page rather than the Find Nearby component's: that one calls the
    component directly, so it exercises the search without ever running the
    workflow the card names."""
    return render_template("find_nearby_place.html")


@bp.route("/extract-form")
@login_required
@admin_required
def extract_form_page():
    """The Form Extractor component's own page -- a description in, a form out."""
    return render_template("extract_form.html")


@bp.route("/extract-form/run", methods=["POST"])
@login_required
@admin_required
def extract_form_run_route():
    """Read a description into a planning form, as JSON. Reports which fields
    the description actually supplied so the page can separate those from
    fields that fell back to a default."""
    data = request.get_json(silent=True) or {}
    description = (data.get("description") or "").strip()
    if not description:
        return jsonify({"error": "description is required"}), 400
    try:
        result = extract_form(description)
    except KeyError:
        return jsonify({"error": "The form extractor isn't configured yet."}), 500
    except requests.exceptions.RequestException as e:
        print(f"Form extraction call failed: {e}")
        return jsonify({"error": "The form extractor is unavailable right now. Please try again."}), 502
    except FormExtractionError as e:
        print(f"Form extraction returned an unusable reply: {e}")
        return jsonify({"error": "Couldn't read a form out of that description."}), 502
    return jsonify(result)


@bp.route("/search-web")
@login_required
@admin_required
def search_web_page():
    """The Web Search component's own page -- query in, results out."""
    return render_template("search_web.html", key_set=bool(os.environ.get("TAVILY_API_KEY")))


@bp.route("/search-web/run", methods=["POST"])
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


@bp.route("/search-web/key", methods=["POST"])
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


@bp.route("/find-nearby")
@login_required
@admin_required
def find_nearby_page():
    """The Find Nearby component's own page -- a location + a need in, places out."""
    return render_template(
        "find_nearby.html", need_options=NEED_OPTIONS,
        key_set=bool(os.environ.get("GOOGLE_MAPS_API_KEY")))


@bp.route("/find-nearby/run", methods=["POST"])
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
        location = lookups.resolve_body_location(data)
    except (GeocodeError, KeyError) as e:
        if not has_coords:
            print(f"Geocoding call failed: {e}")
            return jsonify({"error": "Couldn't look up that location. Share your "
                                     "location instead, or save a Google Maps API key "
                                     "to search by address."}), 502
        print(f"Place lookup skipped, using raw coordinates: {e}")
        location = {"city": "", "neighbourhood": "", "formatted_address": "",
                    "lat": data["lat"], "lng": data["lng"]}

    # Same default as the chat and the trip panel: a location that resolved to
    # nothing means the city this app covers, not the whole web.
    where = searchable(location)
    result = find_nearby_component(
        need=need, city=where["city"], neighbourhood=where["neighbourhood"],
        place_name=where["formatted_address"],
        lat=where["lat"], lng=where["lng"])
    result["location"] = location
    return jsonify(result)


@bp.route("/find-nearby/key", methods=["POST"])
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


@bp.route("/plan-trip")
@login_required
@admin_required
def plan_trip_page():
    """The Plan Trips component's own page -- trip details in, a plan out."""
    return render_template("plan_trip.html")


@bp.route("/plan-trip/run", methods=["POST"])
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


@bp.route("/replan-trip")
@login_required
@admin_required
def replan_trip_page():
    """The Replan a trip component's own page -- build a sample day, then
    re-plan it for a situation. Reuses /plan-trip/run for the first step."""
    return render_template("replan_trip.html", situation_options=SITUATION_OPTIONS,
                           interest_options=interest_options())


@bp.route("/replan-trip/run", methods=["POST"])
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
        minutes=data.get("minutes"), interest=data.get("interest"),
    )
    return jsonify(result)


def _results_sessions():
    return [{"kind": kind, "title": title, "results": get_results(kind), "stats": get_stats(kind)}
            for kind, title in RESULT_KINDS]


@bp.route("/results")
@login_required
@admin_required
def results():
    """Every rated chatbot response, AI-generated plan, and AI replan, with
    aggregate stats per session."""
    return render_template("results.html", sessions=_results_sessions())


@bp.route("/results/data")
@login_required
@admin_required
def results_data():
    """Poll-able stats + full results list per session, so the Results page
    can refresh itself in place without reloading (which would also reset
    any chatbot conversation open elsewhere on the page)."""
    return jsonify({"sessions": _results_sessions()})


@bp.route("/place-search")
@login_required
@admin_required
def place_search_page():
    """The Place Search component's own page: a query in, candidates out.

    Isolated from Log a Place on purpose. When a submission comes back with the
    wrong address, this is how you tell a bad search result from a bad form.
    """
    return render_template("place_search.html")


@bp.route("/place-search/run", methods=["POST"])
@login_required
@admin_required
def place_search_run_route():
    """Run the Place Search component, as JSON."""
    return lookups.place_search_response()
