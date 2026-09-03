"""The planning page: the trip form, and the days it generates.

Generating is opt in. Only a POST carrying "generate" builds a day, which is
what lets the chat assistant hand over a form it collected without paying for a
plan the parent has not asked for yet.

_chosen_model and _planner_kwargs live here rather than in src/web/trip.py
because replanning mid-trip is planning again with the trip's own answers, so
the two share one description of what the planner is given. The dependency runs
trip -> planning and not back.
"""

import json
import secrets
from datetime import date

from flask import (Blueprint, flash, redirect, render_template, request,
                   url_for)

from src.ai.agents import ALLOWED_CHAT_MODELS, DEFAULT_MODEL
from src.components.plan_trip import plan_days
from src.data_loader import FEATURE_LABELS, SUPPORTED_CITIES, interest_options
from src.dates import MAX_TRIP_DAYS
from src.db import TRIP_FIELDS, add_trip, delete_trip, get_children, get_trip_for_parent
from src.form_helpers import (DEFAULTS, DEFAULT_TRANSIT, DINING_OPTIONS,
                              MAX_NAPS, NAP_DURATION_MAX_MINUTES,
                              NAP_DURATION_MIN_MINUTES, TRANSIT_NAP_OPTIONS,
                              TRANSIT_OPTIONS, WALK_BUDGET_BY_TRANSIT,
                              WALK_BUDGET_FORM_OPTIONS, clamp_int, default_form,
                              read_form, resolve_plan_child, trip_dates,
                              trip_too_long)
from src.models import Plan
from src.web import guards, lookups
from src.web.guards import (LOOKUP_LIMIT, LOOKUP_WINDOW, PLAN_LIMIT,
                            PLAN_WINDOW, login_required, rate_limited)

bp = Blueprint("planning", __name__)

# The kinds of place a parent can ask for, read from the venues that exist so
# the form never offers something nothing can satisfy.
FEATURE_OPTIONS = list(FEATURE_LABELS.items())

# How many times a parent can say "something's off" and get the plan
# adjusted again before we stop offering it and point at in-trip replanning.
MAX_REVISE_ROUNDS = 2

def _chosen_model(value):
    """The model a request asked for, or the default if it asked for nothing
    the app offers. The chat widget's dropdown is the one place a parent picks
    a model, so planning and replanning read their choice from the request
    rather than each keeping a default of their own."""
    return value if value in ALLOWED_CHAT_MODELS else DEFAULT_MODEL


@bp.route("/delete-trip/<int:trip_id>", methods=["POST"])
@login_required
def delete_trip_route(trip_id):
    """Remove one of the logged-in parent's saved plans."""
    parent = guards.current_parent()
    if get_trip_for_parent(parent["id"], trip_id) is None:
        flash("Trip not found.")
        return redirect(url_for("account.dashboard"))
    delete_trip(trip_id, parent["id"])
    return redirect(url_for("account.dashboard"))


@bp.route("/plan/accommodation-search", methods=["POST"])
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


@bp.route("/save-trip", methods=["POST"])
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
        return redirect(url_for("planning.plan"))
    if not isinstance(plan_data, list) or not plan_data:
        return redirect(url_for("planning.plan"))
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


@bp.route("/plan", methods=["GET", "POST"])
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
