"""The parent's own page: their children, saved trips and logged places."""

import json

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from src.store.db import (AMENITY_OPTIONS, add_child, delete_child, get_children,
                    get_logged_venues_for_parent, get_trips_for_parent,
                    update_child)
from src.models import Plan
from src.web import guards
from src.web.guards import login_required

bp = Blueprint("account", __name__)

@bp.route("/dashboard")
@login_required
def dashboard():
    """The logged-in parent's saved children, trips, and logged places."""
    parent = guards.current_parent()
    trips = []
    for row in get_trips_for_parent(parent["id"]):
        trip = dict(row)
        trip["plan"] = Plan.from_dict(json.loads(row["plan_json"]))
        trips.append(trip)
    places = get_logged_venues_for_parent(parent["id"])

    return render_template("dashboard.html", parent=parent, trips=trips,
                           places=places, amenity_options=AMENITY_OPTIONS)


@bp.route("/add-child", methods=["POST"])
@login_required
def add_child_route():
    """Add another child to the logged-in parent's account."""
    parent = guards.current_parent()
    name = request.form.get("child_name", "").strip()
    date_of_birth = request.form.get("date_of_birth", "")
    if not name or not date_of_birth:
        flash("A child needs both a name and a date of birth.")
        return redirect(url_for("account.dashboard"))
    add_child(parent["id"], name, date_of_birth)
    return redirect(url_for("account.dashboard"))


@bp.route("/edit-child/<int:child_id>", methods=["POST"])
@login_required
def edit_child_route(child_id):
    """Update one of the logged-in parent's children."""
    parent = guards.current_parent()
    if child_id not in {child["id"] for child in get_children(parent["id"])}:
        flash("Child not found.")
        return redirect(url_for("account.dashboard"))
    name = request.form.get("child_name", "").strip()
    date_of_birth = request.form.get("date_of_birth", "")
    if not name or not date_of_birth:
        flash("A child needs both a name and a date of birth.")
        return redirect(url_for("account.dashboard"))
    update_child(child_id, name, date_of_birth)
    return redirect(url_for("account.dashboard"))


@bp.route("/delete-child/<int:child_id>", methods=["POST"])
@login_required
def delete_child_route(child_id):
    """Remove one of the logged-in parent's children (their saved trips are kept)."""
    parent = guards.current_parent()
    if child_id not in {child["id"] for child in get_children(parent["id"])}:
        flash("Child not found.")
        return redirect(url_for("account.dashboard"))
    delete_child(child_id)
    return redirect(url_for("account.dashboard"))
