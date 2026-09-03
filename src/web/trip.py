"""The in-trip page: running the day that was chosen, and changing it.

Where /plan compares candidate days, this runs one. The situation buttons and
the find-nearby panel are the two ways a day changes once it has started, and
both propose rather than apply: a parent sees the diff and accepts it.

Replanning the rest of a trip is planning again with the trip's own answers, so
the planner arguments come from src/web/planning.py rather than being described
a second time here.
"""

import json

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from src.store import db
from src.components.find_nearby import find_nearby as find_nearby_component
from src.components.find_nearby import searchable
from src.components.geocode import UNKNOWN_LOCATION, GeocodeError
from src.components.plan_trip import plan_days
from src.components.replan_trip import replan_trip
from src.data_loader import FEATURE_LABELS, get_venues, interest_options
from src.dates import MAX_TRIP_DAYS, compute_age, parse_date
from src.store.db import get_trip_for_parent, get_trip_group
from src.form_helpers import DEFAULTS, TRANSIT_OPTIONS, normalise_transit, trip_dates
from src.interactions import (MAX_REPLAN_MINUTES, MIN_REPLAN_MINUTES,
                              NEED_OPTIONS, SITUATION_OPTIONS, replan)
from src.models import Day, Plan, Trip
from src.plan_diff import describe_changes, summarise
from src.web import guards, lookups, planning
from src.web.guards import (LOOKUP_LIMIT, LOOKUP_WINDOW, PLAN_LIMIT,
                            PLAN_WINDOW, login_required, rate_limited)

bp = Blueprint("trip", __name__)

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
        feature_options=planning.FEATURE_OPTIONS,
        situation_options=SITUATION_OPTIONS,
        transit_labels=dict(TRANSIT_OPTIONS),
        interest_options=interest_options(),
        need_options=NEED_OPTIONS,
        # The custom-duration inputs' min/max come from the same constants the
        # server clamps to, so the browser and the clamp cannot disagree.
        min_replan_minutes=MIN_REPLAN_MINUTES,
        max_replan_minutes=MAX_REPLAN_MINUTES,
    )


@bp.route("/trip", methods=["GET", "POST"])
def trip():
    """In-trip page: render the chosen plan as a live, adjustable Trip.

    Reached by POSTing a chosen plan from the planning page. A direct GET (or a
    malformed submission) has no plan to show, so it returns to planning.
    """
    if request.method == "GET":
        return redirect(url_for("planning.plan"))
    try:
        # "plan" is one day, "plans" is a whole visit. Both are accepted: the
        # planning page posts the list, and a one-day plan posted by anything
        # older -- a saved snapshot, a test, a bookmarked form -- is a list of
        # one rather than a second code path.
        posted = request.form.get("plans")
        plan_data = json.loads(posted) if posted else [json.loads(request.form.get("plan", ""))]
        context = json.loads(request.form.get("context", "{}"))
    except (ValueError, TypeError):
        return redirect(url_for("planning.plan"))
    if not isinstance(plan_data, list) or not plan_data:
        return redirect(url_for("planning.plan"))

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


@bp.route("/trip/<int:trip_id>")
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


@bp.route("/replan", methods=["POST"])
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


@bp.route("/replan/adjust", methods=["POST"])
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
        model=planning._chosen_model(data.get("model")),
    )
    return jsonify(result)


@bp.route("/trip/replan-remaining", methods=["POST"])
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
                      **planning._planner_kwargs(form, form.get("extra_notes", ""),
                                        planning._chosen_model(data.get("model"))))
    proposals = []
    for was, now in zip(days, fresh):
        before = (was.get("plan") or {}).get("stops") or []
        changes = describe_changes(before, now["stops"])
        proposals.append({**now, "changes": changes,
                          "change_summary": summarise(changes)})
    return jsonify({"days": proposals})


@bp.route("/find_nearby", methods=["POST"])
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
