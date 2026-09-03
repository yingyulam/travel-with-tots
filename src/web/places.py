"""Places a parent logged themselves, and what they tell us about a venue.

Parent-facing rather than an admin test surface: the Log a place workflow card
points at this page, so a chain that logs a place cannot drift away from the
page a parent uses to do it by hand.

A parent never writes a venue directly. Logging one creates a submission for
the review queue in src/web/venues.py, and reporting an amenity creates a
dated report rather than overwriting a column.
"""

from datetime import date

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from src.store import db
from src.components.geocode import GeocodeError, reverse_geocode
from src.store.db import (AMENITY_OPTIONS, delete_venue, get_logged_venues_for_parent,
                    get_trip_for_parent, update_venue)
from src.web import guards, lookups
from src.web.guards import login_required
from src.workflows import log_a_place

bp = Blueprint("places", __name__)

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

@bp.route("/log-place")
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


@bp.route("/log-place", methods=["POST"])
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
        return redirect(url_for("places.log_place_page"))
    return redirect(url_for("places.log_place_page", logged=record["id"]))


@bp.route("/log-place/area", methods=["POST"])
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


@bp.route("/log-place/search", methods=["POST"])
@login_required
def log_place_search_route():
    """Name lookup for the Log a Place pin."""
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


@bp.route("/edit-place/<int:place_id>", methods=["POST"])
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


@bp.route("/delete-place/<int:place_id>", methods=["POST"])
@login_required
def delete_place_route(place_id):
    """Remove one of the logged-in parent's own logged places."""
    parent = guards.current_parent()
    if not _owns_place(parent["id"], place_id):
        flash("Place not found.")
        return redirect(url_for("account.dashboard"))
    delete_venue(place_id, parent["id"])
    return redirect(url_for("account.dashboard"))


@bp.route("/venues/<int:venue_id>/report", methods=["POST"])
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
