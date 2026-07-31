"""Travel with Tots -- Flask entry point.

Two pages: a planning page (``/plan``) that compares candidate plans, and an
in-trip page (``/trip``) that runs the chosen plan. All the real work lives in
the src/ package; this file just wires HTTP requests to that logic.
"""

import json

from flask import Flask, jsonify, redirect, render_template, request, url_for

from src.data_loader import FEATURE_LABELS, load_venues
from src.interactions import (
    NEED_OPTIONS,
    SITUATION_OPTIONS,
    find_nearby,
    replan,
)
from src.itinerary import generate_plans
from src.models import Plan, Trip

app = Flask(__name__)

# Venue data never changes at runtime, so load it once at startup.
VENUES = load_venues()

# Transit choices and feature checkboxes, defined once and shared with the
# template so the form and the plan stay in sync.
TRANSIT_OPTIONS = ["car", "bus", "stroller", "carrier", "other"]
PACE_OPTIONS = ["relaxed", "balanced", "adventurous"]
DINING_OPTIONS = [("dine_out", "Dine out"), ("on_the_go", "Eat on the go")]

# Age is capped at this many years, 0 months.
MAX_AGE_YEARS = 5
MAX_MONTHS = 11
FEATURE_OPTIONS = list(FEATURE_LABELS.items())

# Sensible defaults so the form is usable on first load.
DEFAULTS = {
    "wake_up": "07:00",
    "bedtime": "20:00",
    "nap_1": "",
    "nap_2": "",
    "age_years": "2",
    "age_months": "0",
    "destination": "Vancouver",
    "accommodation": "",
    "transit": ["stroller"],
    "pace": "balanced",
    "dining": "dine_out",
    "nap_notes": "",
    "extra_notes": "",
    "features": ["kid_friendly"],
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


def _read_form(form):
    """Normalise the raw request form into the shape the logic expects."""
    age_years, age_months = _read_age(form)
    values = {
        "wake_up": form.get("wake_up") or DEFAULTS["wake_up"],
        "bedtime": form.get("bedtime") or DEFAULTS["bedtime"],
        "nap_1": form.get("nap_1", ""),
        "nap_2": form.get("nap_2", ""),
        "age_years": age_years,
        "age_months": age_months,
        "destination": form.get("destination") or DEFAULTS["destination"],
        "accommodation": form.get("accommodation", "").strip(),
        "transit": form.getlist("transit"),
        "pace": form.get("pace") or DEFAULTS["pace"],
        "dining": form.get("dining") or DEFAULTS["dining"],
        "nap_notes": form.get("nap_notes", ""),
        "extra_notes": form.get("extra_notes", ""),
        "features": form.getlist("features"),
    }
    values["nap_times"] = [n for n in (values["nap_1"], values["nap_2"]) if n]
    return values


@app.route("/")
def home():
    """Marketing landing page."""
    return render_template("index.html")


@app.route("/plan", methods=["GET", "POST"])
def plan():
    """Planning page: the trip form and, after generating, comparable plans."""
    if request.method == "POST":
        form = _read_form(request.form)
        plans = generate_plans(VENUES, form)
        # Context carried to the in-trip page when a plan is chosen.
        trip_context = {
            "destination": form["destination"],
            "transit": form["transit"],
            "features": form["features"],
            "bedtime": form["bedtime"],
        }
    else:
        form = dict(DEFAULTS)
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

    trip = Trip(
        destination=context.get("destination", "Vancouver"),
        transit=context.get("transit", []),
        features=context.get("features", []),
        bedtime=context.get("bedtime", ""),
        original=Plan.from_dict(plan_data),
    )
    return render_template(
        "trip.html",
        trip=trip.to_dict(),
        feature_options=FEATURE_OPTIONS,
        situation_options=SITUATION_OPTIONS,
        need_options=NEED_OPTIONS,
    )


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
                          bedtime=data.get("bedtime"), nap_length=data.get("nap_length")))


@app.route("/find_nearby", methods=["POST"])
def find_nearby_route():
    """Return 1-2 venues matching an immediate need as JSON."""
    data = request.get_json(silent=True) or {}
    venues = find_nearby(data.get("need", ""), VENUES)
    return jsonify({"need": data.get("need", ""), "venues": venues})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8016, debug=True)
