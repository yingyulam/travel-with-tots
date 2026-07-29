"""Travel with Tots -- Flask entry point.

Serves the trip-planning form and renders a generated single-day itinerary.
All the real work lives in the src/ package; this file just wires HTTP
requests to that logic.
"""

from flask import Flask, jsonify, render_template, request

from src.ai_helper import get_suggestion
from src.data_loader import FEATURE_LABELS, load_venues
from src.itinerary import generate_plan

app = Flask(__name__)

# Venue data never changes at runtime, so load it once at startup.
VENUES = load_venues()

# Transit choices and feature checkboxes, defined once and shared with the
# template so the form and the plan stay in sync.
TRANSIT_OPTIONS = ["car", "bus", "stroller", "carrier", "other"]
FEATURE_OPTIONS = list(FEATURE_LABELS.items())

# Sensible defaults so the form is usable on first load.
DEFAULTS = {
    "wake_up": "07:00",
    "bedtime": "19:00",
    "nap_1": "12:30",
    "nap_2": "",
    "kid_age": "2",
    "destination": "Vancouver",
    "transit": ["stroller"],
    "nap_notes": "",
    "features": ["kid_friendly"],
}


def _read_form(form):
    """Normalise the raw request form into the shape the logic expects."""
    values = {
        "wake_up": form.get("wake_up") or DEFAULTS["wake_up"],
        "bedtime": form.get("bedtime") or DEFAULTS["bedtime"],
        "nap_1": form.get("nap_1", ""),
        "nap_2": form.get("nap_2", ""),
        "kid_age": form.get("kid_age") or DEFAULTS["kid_age"],
        "destination": form.get("destination") or DEFAULTS["destination"],
        "transit": form.getlist("transit"),
        "nap_notes": form.get("nap_notes", ""),
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
    if request.method == "POST":
        form = _read_form(request.form)
        itinerary = generate_plan(
            VENUES,
            wake_time=form["wake_up"],
            bedtime=form["bedtime"],
            nap_times=form["nap_times"],
            transit_modes=form["transit"],
            nap_notes=form["nap_notes"],
            features=form["features"],
        )
    else:
        form = dict(DEFAULTS)
        itinerary = None

    return render_template(
        "plan.html",
        form=form,
        itinerary=itinerary,
        transit_options=TRANSIT_OPTIONS,
        feature_options=FEATURE_OPTIONS,
    )


@app.route("/help", methods=["POST"])
def help_suggestion():
    """Return a placeholder assistant suggestion as JSON."""
    return jsonify({"suggestion": get_suggestion(request.get_json(silent=True))})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8016, debug=True)
