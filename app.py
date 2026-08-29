"""Travel with Tots -- Flask entry point.

Two pages: a planning page (``/plan``) that compares candidate plans, and an
in-trip page (``/trip``) that runs the chosen plan. All the real work lives in
the src/ package; this file just wires HTTP requests to that logic.
"""

import json
import os
from datetime import date, datetime, timezone
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

from src import candidates, db, rag
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
from src import osm
from src.components.plan_trip import plan_trip
from src.components.replan_trip import replan_trip
from src.components.search_web import WebSearchError, search_web
import sqlite3

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
from src.dates import compute_age, parse_date
from src.db import (
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
    clamp_int,
    read_form,
    resolve_plan_child,
)
from src.interactions import (
    MAX_REPLAN_MINUTES,
    MIN_REPLAN_MINUTES,
    NEED_OPTIONS,
    SITUATION_OPTIONS,
    replan,
)
from src.agent import handle_message
from src.models import Plan, Trip
from src.results import get_results, get_stats, save_result
from src.workflows import log_a_place, workflows_by_trigger
from src.workflows.log_a_place import AMENITY_OPTIONS

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

# Create the SQLite tables (data/app.db) on startup if they don't exist yet.
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


def _current_parent():
    """The logged-in parent's row, or None if no one is logged in."""
    parent_id = session.get("parent_id")
    return get_parent(parent_id) if parent_id else None


def _chat_context(data):
    """Everything a chat turn is given beyond the message itself.

    Identity comes from the session and only from the session: `parent_id` is
    what every recall is scoped by, so a client-supplied one would read another
    parent's children and saved trips. Read through `_current_parent()` rather
    than `session.get("parent_id")` raw, because SQLite reuses row ids, so a
    stale cookie can eventually name a real but different parent; the lookup
    returns None for a row that is gone.

    Our value is merged last, so it wins outright even if the browser half of
    the context ever grows a key of the same name.
    """
    parent = _current_parent()
    return {**_message_context(data),
            "parent_id": parent["id"] if parent else None}


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

    return render_template("dashboard.html", parent=parent, trips=trips,
                           places=places, amenity_options=AMENITY_OPTIONS)


def _settleable(check):
    """One pending hours check, with its finding worked out now.

    `finding` is stored on the row by whichever tool filed it, and a stored
    judgment goes stale the moment the rule behind it changes: every card read
    "more than one pair holds" including one where OpenStreetMap agreed with us
    exactly. It is a pure function of our hours and the string, both of which
    are here, so it is derived rather than trusted.

    `osm_week` is filled only when the string is a plain week the venue_hours
    table holds exactly. It is what turns the card into one click; a seasonal
    string leaves it empty and asks for judgment.
    """
    row = dict(check)
    ours = db.get_venue_hours([row["venue_id"]]).get(row["venue_id"])
    row["current_per_day"] = ours
    row["finding"] = osm.compare(row["current_open"], row["current_close"],
                                 row["source_says"], our_per_day=ours)
    row["osm_week"] = osm.per_day_hours(row["source_says"])
    return row


@app.route("/venues/review")
@login_required
@admin_required
def venue_review():
    """Everything a person still has to settle, grouped by the decision asked.

    Three questions, in the order they cost the database:

      Decide   is this a venue at all?      proposals + parent submissions
      Finish   it is in, but unusable       missing hours + disputed hours
      Confirm  it is in use, unchecked      the curated seed

    plus Set aside, which is an archive rather than a queue. Grouping by
    mechanism instead put two unrelated hours sections in different places and
    left the page reading as six things to do.

    Municipal imports never appear: the City is authoritative about its own
    parks, so those rows are trusted by provenance. See db.VERIFIED_SOURCES.
    """
    unverified = get_unverified_venues()
    missing_hours = get_venues_missing_hours()
    submissions = get_pending_submissions()
    pending = candidates.counts()[candidates.PENDING]
    return render_template(
        "venue_review.html",
        proposals=_reviewable_candidates(PROPOSAL_PAGE_SIZE),
        proposals_total=pending,
        page_size=PROPOSAL_PAGE_SIZE,
        rejected_candidates=[
            dict(r, source_link=_safe_url(r.get("source_url")))
            for r in candidates.load(candidates.REJECTED)],
        rejected_submissions=get_rejected_submissions(),
        hours_checks=[_settleable(c) for c in get_pending_hours_checks()],
        weekdays=list(enumerate(osm.WEEKDAYS)),
        missing_hours=[dict(row, source_link=_safe_url(row["source_url"]))
                       for row in missing_hours[:MISSING_HOURS_PAGE_SIZE]],
        missing_hours_total=len(missing_hours),
        submissions=submissions,
        # Amenities stopped being venue columns, so a submission row carries
        # none. The parent ticked them when they logged the place and they are
        # sitting in venue_reports; without this the card shows nothing.
        submission_reports=db.reported_flags([v["id"] for v in submissions]),
        unverified=[dict(v, source_link=_safe_url(v["source_url"]))
                    for v in unverified[:UNVERIFIED_PAGE_SIZE]],
        unverified_reports=db.reported_flags(
            [v["id"] for v in unverified[:UNVERIFIED_PAGE_SIZE]]),
        # So a venue with a per-day timetable is not described as "every day".
        unverified_hours=db.get_venue_hours(
            [v["id"] for v in unverified[:UNVERIFIED_PAGE_SIZE]]),
        unverified_total=len(unverified),
        flag_labels=FLAG_LABELS,
        conditional_flags=db.CONDITIONAL_ON_CAN_EAT,
        venue_types=VENUE_TYPES,
        settings=SETTINGS,
        neighbourhoods=NEIGHBOURHOODS,
        cities=CITIES)


@app.route("/venues/review/<int:venue_id>", methods=["POST"])
@login_required
@admin_required
def venue_review_decide(venue_id):
    """Verify or discard one submission, then redirect back to the queue.

    One route with an action rather than two, because both decisions are the
    same gesture on the same row. Redirecting after the POST means a refresh
    re-reads the queue instead of repeating the decision.
    """
    action = request.form.get("action")
    if action == "approve":
        try:
            promote_submission(venue_id, _current_parent()["id"])
        except PromotionError as e:
            flash(str(e).capitalize() + ".")
        else:
            flash("Verified. It can now appear in plans and searches.")
    elif action == "reject":
        reject_submission(venue_id, _current_parent()["id"])
        flash("Set aside. It stays on file and can be restored below.")
    else:
        flash("Unknown action.")
    return redirect(url_for("venue_review"))


# How many unconfirmed venues to show at once. The backlog is 38, and a page
# offering all of them has the same problem as an oversized proposal batch:
# more than one person will work through in a sitting.
UNVERIFIED_PAGE_SIZE = 12

# Filling hours in means reading a venue's own page, so this list is shown a
# screenful at a time like the others rather than all 27 community centres at
# once.
MISSING_HOURS_PAGE_SIZE = 12

# How many proposals to put in front of a reviewer at once. A batch is meant to
# be read as a set and finished in one sitting; the queue advances on its own as
# each batch is decided, because a decided candidate is no longer pending.
PROPOSAL_PAGE_SIZE = 10

# The venue flags a reviewer can vouch for, as (field, label). Built from the
# columns the planner can actually filter on, so the form and the filters
# cannot come to offer different sets.
# In FEATURE_LABELS' order, which is presentation order, rather than sorted:
# alphabetical put "Food on site" first and "Stroller" last for no reason.
# What the review form asks about: the five reportable amenities plus can_eat.
# Built from both, because only can_eat is a column now -- the other five become
# reports authored by the reviewer on approval, and a form that stopped asking
# would quietly end amenity review altogether.
FLAG_LABELS = tuple(
    (key, label) for key, label in FEATURE_LABELS.items()
    if key in db.CANDIDATE_FEATURE_COLUMNS or key in db.REPORTABLE_FIELDS)


def _safe_url(url):
    """A candidate's citation, or None if it is not a web address.

    The URL came from a model reading the open web and is rendered as a link,
    and Jinja's escaping does not stop a javascript: href.
    """
    return url if (url or "").startswith(("http://", "https://")) else None


def _reviewable_candidates(limit=None):
    """Pending proposals, with everything the reviewer needs to judge one.

    `already_have` both warns and disables the checkbox, which is as far as a
    page-render-time check can go: the clash a reviewer creates by editing the
    name, and the stale page whose disabled checkbox simply is not submitted,
    both get past it. That is why venue_review_candidates catches the
    IntegrityError rather than relying on this.

    Beyond the row itself: whether the database already has the name, both
    citations as safe links, the domain each came from, and whether the
    discovery citation is somewhere anyone can publish. The domain is shown
    because a bare "Source" link hides what a reviewer most needs to weigh --
    one live proposal was cited to a Portland guide to Vancouver, *Washington*,
    which is obvious as `pdx.eater.com` and invisible as "Source".

    Also which fields are not values the app knows, so the form can ask rather
    than pre-answer.
    """
    have = {candidates.normalize_name(row["name"])
            for row in db.get_venues_in_city("")}
    rows = []
    for row in candidates.load(candidates.PENDING):
        row = dict(row)
        row["already_have"] = candidates.normalize_name(row["name"]) in have
        row["source_link"] = _safe_url(row.get("source_url"))
        row["source_domain"] = propose_venues.domain(row.get("source_url"))
        row["low_trust"] = propose_venues.is_low_trust(row.get("source_url"))
        row["official_link"] = _safe_url(row.get("official_url"))
        row["official_domain"] = propose_venues.domain(row.get("official_url"))
        row["unknown_values"] = _unknown_values(row)
        rows.append(row)
    return rows if limit is None else rows[:limit]


def _unknown_values(row):
    """Field names whose value is not one the app knows, in review order.

    A value outside the enum is a question for the reviewer, not an answer, so
    the form leaves the dropdown unset and this is what tells it to.
    """
    checks = (("type", VENUE_TYPES), ("setting", SETTINGS),
              ("neighbourhood", NEIGHBOURHOODS), ("city", CITIES))
    return [field for field, allowed in checks
            if (row.get(field) or "").strip()
            and row[field].strip() not in allowed]


@app.route("/venues/review/candidates", methods=["POST"])
@login_required
@admin_required
def venue_review_candidates():
    """Save edits to the proposed batch, and approve or reject the ticked ones.

    One submit for the whole batch: reviewing a small batch means reading it as
    a set, and thirty separate posts is thirty chances to lose your place.

    Edits are saved for every row, not only the ticked ones, so a half-finished
    review survives. That is what makes a batch bigger than one sitting workable.
    """
    action = request.form.get("action")
    if action not in ("save", "approve", "reject"):
        flash("Unknown action.")
        return redirect(url_for("venue_review"))

    picked = set(request.form.getlist("picked"))
    # The ids that were rendered, not every pending candidate. A checkbox that
    # was never on the page comes back absent, which reads identically to
    # unticked, so iterating the whole queue would wipe the flags of everything
    # the reviewer never saw.
    on_page = set(request.form.getlist("on_page"))
    admin_id = _current_parent()["id"]
    saved = approved = rejected = 0
    refused = []

    for row in candidates.load(candidates.PENDING):
        if row["id"] not in on_page:
            continue
        edits = _candidate_edits(row["id"], request.form)
        if edits:
            candidates.update(row["id"], **edits)
            saved += 1
        if row["id"] not in picked:
            continue
        if action == "reject":
            candidates.set_status(row["id"], candidates.REJECTED, decided_by=admin_id)
            rejected += 1
        elif action == "approve":
            merged = {**row, **edits}
            missing = _cannot_approve(merged)
            if missing:
                refused.append(f"{merged.get('name') or 'a venue'} ({missing})")
                continue
            try:
                _approve_candidate(merged, admin_id)
            except sqlite3.IntegrityError:
                # idx_venues_curated_identity refuses a second curated venue
                # with the same name and city. Uncaught, this unwound the loop
                # and 500'd the whole submit: every row after this one lost its
                # edits *and* its decision, the reviewer saw no flash, and
                # re-submitting failed on the same row and lost the tail again.
                #
                # Still reachable with the badge in place, which is why it is
                # caught rather than only warned about: name and city are both
                # editable, so a reviewer can create the clash themselves after
                # the page was drawn.
                refused.append(f"{merged.get('name') or 'a venue'} "
                               "(already a curated venue in that city)")
                continue
            approved += 1

    parts = []
    if action == "save":
        parts.append(f"Saved edits to {saved} venue{'s' if saved != 1 else ''}")
    if approved:
        parts.append(f"approved {approved}")
    if rejected:
        parts.append(f"rejected {rejected}")
    for name in refused:
        parts.append(f"{name} not approved")
    flash(("; ".join(parts) or "Nothing selected") + ".")
    return redirect(url_for("venue_review"))


def _candidate_edits(candidate_id, form):
    """The reviewer's changes to one candidate, read off the batch form.

    Flags come back as present-or-absent checkboxes, so every one is written
    every time: unticking has to clear a flag, not leave the old answer standing.
    """
    checkboxes = set(db.CANDIDATE_FEATURE_COLUMNS) | set(db.REPORTABLE_FIELDS)
    edits = {}
    for field in candidates.EDITABLE:
        key = f"{candidate_id}-{field}"
        if field in checkboxes:
            edits[field] = "1" if form.get(key) else ""
        elif key in form:
            edits[field] = (form.get(key) or "").strip()
    return edits


# The fields that must hold a value from a closed list before a candidate can
# become a venue, and the list each is checked against. Neighbourhood is not
# here: it may legitimately be blank, and it is checked below only when set.
APPROVAL_ENUMS = (("type", VENUE_TYPES), ("setting", SETTINGS), ("city", CITIES))


def _cannot_approve(row):
    """Why this candidate is not ready, or "" if it is.

    Hours are required. A venue with none is treated as open all day by
    itinerary.venue_open_for, which is how a museum gets scheduled at eight in
    the evening, and deciding whether a place can be visited at a given time is
    most of what the planner does.

    So is a `type` the app recognises, and that check is newer than the rest
    because the old one only asked whether the field was non-empty. A live
    batch arrived with type "activity" on four rows, the review form rendered
    it as the selected option, and approving without touching the dropdown
    wrote it straight into venues.type -- where data_loader.is_nap_friendly
    does not fail on it, it just quietly answers False forever. A value outside
    the list has to stop here, because nothing downstream will notice it.
    """
    for field, label in (("open_time", "opening time"),
                         ("close_time", "closing time"),
                         ("type", "type"), ("setting", "setting"),
                         ("city", "city")):
        if not (row.get(field) or "").strip():
            return f"no {label}"
    for field, allowed in APPROVAL_ENUMS:
        if row[field].strip() not in allowed:
            return f"{field} {row[field].strip()!r} is not one we know"
    area = (row.get("neighbourhood") or "").strip()
    if area and area not in NEIGHBOURHOODS:
        return f"neighbourhood {area!r} is not one we know"
    return ""


def _approve_candidate(row, admin_id):
    """Insert an approved candidate as a verified venue, and stamp the record.

    The only path that turns a proposal into a venue, and it runs because a
    person clicked. The agent writes candidates and nothing else.
    """
    venue_id = add_venue(
        row["name"],
        source="curated",
        venue_type=row.get("type") or None,
        neighbourhood=row.get("neighbourhood") or None,
        city=row.get("city") or None,
        address=row.get("address") or None,
        open_time=row.get("open_time") or None,
        close_time=row.get("close_time") or None,
        lat=_as_float(row.get("lat")),
        lng=_as_float(row.get("lng")),
        setting=row.get("setting") or None,
        # What a single pair cannot hold, in words a parent reads. The proposer
        # has been filling this with the raw OSM string and the entry it
        # matched; approval used to drop it for want of a column.
        hours_note=row.get("hours_note") or None,
        # Whatever identity the geocoder gave us, so a re-proposal of the same
        # place is recognised rather than inserted twice. Null when the locator
        # found nothing, which idx_venues_external_id allows.
        external_id=row.get("external_id") or None,
        # The venue's own site when we found one, the page it was discovered
        # on otherwise. The official site is preferred, and the discovery URL
        # is not lost: venue_candidates.csv keeps it, and that file is the
        # durable record of where a venue came from (see src/candidates.py).
        source_url=row.get("official_url") or row.get("source_url") or None,
        verified_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        verified_by=admin_id,
        # can_eat stays a column: it follows the kind of place, nobody reports
        # it, and the lunch rule reads it directly.
        can_eat=row.get("can_eat") in ("1", 1, True))
    # The amenities the reviewer ticked, as reports authored by them. These used
    # to go into the venues columns, which is the weakest layer -- so a
    # deliberate check was overridden by the next parent report with no record
    # that anyone had ever looked.
    db.record_amenities(
        venue_id, {f: row.get(f) in ("1", 1, True) for f in db.REPORTABLE_FIELDS
                   if row.get(f) not in (None, "")},
        reported_by=admin_id, note="Checked at review.")
    candidates.set_status(row["id"], candidates.APPROVED, decided_by=admin_id)


def _hour_pair(form, open_field="open_time", close_field="close_time"):
    """A validated HH:MM pair from a form, or (None, None).

    Validated rather than trusted, because itinerary.venue_hours parses these
    with int() and no fallback: one malformed value stored on a venue makes
    every plan in that city raise. The <input type="time"> stops it in a
    browser, which means the only way to get a bad value in is a hand-made POST
    or a browser that does not support the type -- neither of which should be
    able to break the planner for everyone. Both ends or neither: half a pair
    says nothing.
    """
    opens = (form.get(open_field) or "").strip()
    closes = (form.get(close_field) or "").strip()
    if not (opens and closes):
        return None, None
    for value in (opens, closes):
        try:
            hour, minute = value.split(":")
            if not (0 <= int(hour) <= 23 and 0 <= int(minute) <= 59):
                return None, None
        except ValueError:
            return None, None
    return opens, closes


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _per_day_from_form(form, prefix=""):
    """{weekday: (open, close)} from the per-day inputs, empty when untouched.

    A day left blank is simply absent, which the table reads as closed. That is
    the only way to say "shut on Mondays", so it must not be filled in for them.

    `prefix` because the confirm list puts many venues' hours in one form, the
    way the proposal rows already do.
    """
    table = {}
    for day in range(7):
        opens, closes = _hour_pair(form, f"{prefix}day{day}_open",
                                   f"{prefix}day{day}_close")
        if opens and closes:
            table[day] = (opens, closes)
    return table


def _store_week(venue_id, week, hours_note=None):
    """Write a venue's whole week, collapsing it when every day agrees.

    A venue open the same hours all week is described by its single pair, so the
    per-day rows are deleted rather than written seven times. Only a week that
    actually varies earns rows, which keeps the table small and keeps "has rows"
    meaning "is unusual".

    The single pair is kept in step either way. Nothing plans from it once rows
    exist, but get_venues_missing_hours reads it, and a venue with a full
    timetable must not appear under "no hours at all".
    """
    if not week:
        return 0
    # Every day present *and* identical. A six-day week is not uniform however
    # alike its days are: the seventh day is a closure, and collapsing it would
    # quietly reopen the venue on the day it shuts.
    uniform = len(week) == 7 and len(set(week.values())) == 1
    usual = max(set(week.values()), key=list(week.values()).count)
    set_venue_default_hours(venue_id, usual[0], usual[1], hours_note)
    return db.set_venue_hours(venue_id, {} if uniform else week)


@app.route("/venues/hours/<int:check_id>", methods=["POST"])
@login_required
@admin_required
def venue_hours_decide(check_id):
    """Settle one hours comparison: correct our hours, or keep them.

    The tool never changes hours itself. It reports what an outside source says
    and a person decides, because half of what it finds needs judgment: a mall
    tagged as closing at half four is more likely a mis-tagged building than a
    mall that closes at half four.
    """
    admin_id = _current_parent()["id"]
    action = request.form.get("action")
    venue_id = request.form.get("venue_id", type=int)
    note = request.form.get("hours_note", "").strip() or None

    # "Take OpenStreetMap's" is offered only where the string is a plain week,
    # so the times come from osm rather than the form: the reviewer is accepting
    # what they read, not retyping seven rows.
    if action == "take_osm" and venue_id:
        week = osm.per_day_hours(request.form.get("source_says", ""))
        if not week:
            flash("Those hours are not a shape we can store. Set them by hand.")
            return redirect(url_for("venue_review"))
        rows = _store_week(venue_id, week, note)
        flash(f"Took OpenStreetMap's hours"
              + (f", {rows} days differing." if rows else ", the same all week."))
    elif action == "update" and venue_id:
        week = _per_day_from_form(request.form)
        opens, closes = _hour_pair(request.form)
        if week:
            rows = _store_week(venue_id, week, note)
            flash(f"Hours updated"
                  + (f", {rows} days differing." if rows else ", the same all week."))
        elif opens and closes:
            db.set_venue_hours(venue_id, {})
            set_venue_default_hours(venue_id, opens, closes, note)
            flash(f"Hours updated to {opens}-{closes}, the same all week.")
        else:
            flash("Both an opening and a closing time are needed, as HH:MM.")
            return redirect(url_for("venue_review"))
    else:
        flash("Kept our hours.")
    resolve_hours_check(check_id, admin_id)
    return redirect(url_for("venue_review"))


@app.route("/venues/<int:venue_id>/hours", methods=["POST"])
@login_required
@admin_required
def venue_set_hours(venue_id):
    """Give a venue the default hours it arrived without.

    An imported community centre is complete in every way except this: the City
    publishes the address, the coordinates and a link to the centre's page, and
    does not publish when it opens. Until somebody reads that page the venue
    stays out of every plan, which is the right answer to unknown hours and a
    dead end at the same time. This is the way out of it.
    """
    note = request.form.get("hours_note", "").strip() or None
    week = _per_day_from_form(request.form)
    if week:
        rows = _store_week(venue_id, week, note)
        flash("Hours set"
              + (f", {rows} days differing." if rows else ", the same all week.")
              + " It can be planned around now.")
        return redirect(url_for("venue_review"))
    opens, closes = _hour_pair(request.form)
    if not (opens and closes):
        flash("Both an opening and a closing time are needed, as HH:MM.")
        return redirect(url_for("venue_review"))
    set_venue_default_hours(venue_id, opens, closes, note)
    flash(f"Hours set to {opens}-{closes}. It can be planned around now.")
    return redirect(url_for("venue_review"))


@app.route("/venues/restore", methods=["POST"])
@login_required
@admin_required
def venue_restore():
    """Put a rejected candidate or submission back in the queue.

    A reviewer can reject the wrong row, or learn something later that changes
    the answer. Nothing here is deleted, so changing your mind costs a click.
    """
    candidate_id = request.form.get("candidate_id")
    venue_id = request.form.get("venue_id", type=int)
    if candidate_id:
        candidates.set_status(candidate_id, candidates.PENDING)
        flash("Back in the review queue.")
    elif venue_id:
        restore_submission(venue_id)
        flash("Submission back in the review queue.")
    else:
        flash("Nothing to restore.")
    return redirect(url_for("venue_review"))


@app.route("/venues/confirm", methods=["POST"])
@login_required
@admin_required
def venue_confirm_batch():
    """Record that a person checked venues the app was already planning around.

    A citation may come with each one, from that venue's own box. It is what
    makes the stamp mean something later: most of these rows have none, so
    "confirmed" would otherwise say only that somebody clicked.
    """
    admin_id = _current_parent()["id"]
    picked = request.form.getlist("picked")
    saved = sum(_save_reviewed_venue(int(vid), vid, admin_id)
                for vid in request.form.getlist("on_page"))

    cited = 0
    for venue_id in picked:
        source_url = _safe_url(request.form.get(f"{venue_id}-source_url", "").strip())
        cited += bool(source_url)
        mark_verified(int(venue_id), admin_id, source_url or None)

    parts = []
    if saved:
        parts.append(f"Saved {saved} venue{'s' if saved != 1 else ''}")
    if picked:
        parts.append(f"confirmed {len(picked)}"
                     + (f", {cited} with a citation" if cited else ""))
    flash(", ".join(parts) + "." if parts else "Nothing selected.")
    return redirect(url_for("venue_review"))


def _save_reviewed_venue(venue_id, prefix, admin_id):
    """Apply one confirm row's edits. Returns 1 if the row was on the page.

    Three stores, because a venue's data lives in three places: the columns a
    reviewer may correct, its hours, and its amenities as dated reports. Each
    underlying write already skips an unchanged value, so a row nobody touched
    costs a comparison rather than a spurious edit or a moved timestamp.
    """
    fields = {name: (request.form.get(f"{prefix}-{name}") or "").strip()
              for name in db.REVIEWABLE_VENUE_FIELDS
              if name != "can_eat" and f"{prefix}-{name}" in request.form}
    fields["can_eat"] = 1 if request.form.get(f"{prefix}-can_eat") else 0
    db.update_reviewed_venue(venue_id, **fields)

    note = request.form.get(f"{prefix}-hours_note", "").strip() or None
    week = _per_day_from_form(request.form, prefix=f"{prefix}-")
    if week:
        _store_week(venue_id, week, note)
    else:
        db.set_venue_hours(venue_id, {})
        opens, closes = _hour_pair(request.form, f"{prefix}-open_time",
                                   f"{prefix}-close_time")
        set_venue_default_hours(venue_id, opens, closes, note)

    db.record_amenities(
        venue_id,
        {f: bool(request.form.get(f"{prefix}-{f}")) for f in db.REPORTABLE_FIELDS},
        reported_by=admin_id, note="Checked at review.")
    return 1


@app.route("/propose-venues")
@login_required
@admin_required
def propose_venues_page():
    """The venue proposal component's own page: run a small batch, see it."""
    return render_template("propose_venues.html",
                           counts=candidates.counts(),
                           batch_size=propose_venues.DEFAULT_BATCH_SIZE)


@app.route("/propose-venues/run", methods=["POST"])
@login_required
@admin_required
def propose_venues_run_route():
    """Run one proposal batch, as JSON.

    Kept small from the page: each candidate costs a search, a model call and a
    place lookup, so a large batch belongs on the command line where nothing
    times out. scripts/propose_venues.py is that path.
    """
    data = request.get_json(silent=True) or {}
    batch_size = clamp_int(data.get("batch_size"), 1, 10,
                           propose_venues.DEFAULT_BATCH_SIZE)
    try:
        result = propose_venues.propose(batch_size=batch_size)
    except KeyError:
        return jsonify({"error": "Set TAVILY_API_KEY and OPENROUTER_API_KEY first."}), 500
    except propose_venues.ProposalError as e:
        print(f"Venue proposal failed: {e}")
        return jsonify({"error": "Couldn't reach the search or model right now."}), 502
    except requests.exceptions.RequestException as e:
        print(f"Venue proposal failed: {e}")
        return jsonify({"error": "Couldn't reach the search or model right now."}), 502
    return jsonify({**result, "counts": candidates.counts()})


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


@app.route("/workflows")
@login_required
@admin_required
def workflows():
    """End-to-end use cases, each a chain of the components above."""
    return render_template("workflows.html", trigger_groups=workflows_by_trigger())


@app.route("/agent")
@login_required
@admin_required
def agent_page():
    """The AI Agent's test page. Deliberately has no chat of its own: it uses
    the real bubble every page carries, and adds a panel showing what the agent
    actually did with each message, so what's tested here is what a parent gets.
    There is no /agent/chat any more -- that was a second implementation."""
    return render_template("ai_agent.html")


@app.route("/workflows/plan-from-chat")
@login_required
@admin_required
def plan_from_chat_page():
    """The Plan from chat workflow's test page: describe a day in the bubble,
    watch the agent turn it into the planning form."""
    return render_template("plan_from_chat.html")


@app.route("/workflows/replan-on-the-go")
@login_required
@admin_required
def replan_on_the_go_page():
    """The Replan on the go workflow's test page: say what changed in the
    bubble, watch the request it collected.

    Its own page rather than /trip: that one holds the plan and does the
    re-timing, and is where this workflow hands off to, so it cannot also be
    the surface for watching the conversation that fills the request."""
    return render_template("replan_on_the_go.html")


@app.route("/workflows/log-a-place")
@login_required
@admin_required
def log_place_from_chat_page():
    """The Log a place workflow's test page: tell the bubble about a place,
    watch the submission fill in.

    Its own page rather than /log-place: that one is the form a parent
    submits, and it is where this workflow hands off to, so it cannot also be
    the surface for watching the conversation that fills it."""
    return render_template("log_place_from_chat.html")


@app.route("/workflows/find-nearby-place")
@login_required
@admin_required
def find_nearby_place_page():
    """The Find a nearby place workflow's test page: ask the bubble for
    somewhere you need, watch the workflow answer.

    Its own page rather than the Find Nearby component's: that one calls the
    component directly, so it exercises the search without ever running the
    workflow the card names."""
    return render_template("find_nearby_place.html")


ENV_PATH = os.path.join(os.path.dirname(__file__), ".env")


@app.route("/extract-form")
@login_required
@admin_required
def extract_form_page():
    """The Form Extractor component's own page -- a description in, a form out."""
    return render_template("extract_form.html")


@app.route("/extract-form/run", methods=["POST"])
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
    """A request body's location, resolved. The resolving itself lives in the
    Geocode component, so this page, the trip panel and the chat workflow all
    centre on the same place given the same coordinates."""
    return resolve_location(lat=data.get("lat"), lng=data.get("lng"),
                            address=data.get("address") or "")


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

    # Same default as the chat and the trip panel: a location that resolved to
    # nothing means the city this app covers, not the whole web.
    where = searchable(location)
    result = find_nearby_component(
        need=need, city=where["city"], neighbourhood=where["neighbourhood"],
        place_name=where["formatted_address"],
        lat=where["lat"], lng=where["lng"])
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
                           interest_options=interest_options())


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
        minutes=data.get("minutes"), interest=data.get("interest"),
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
    add_child(parent["id"], name, date_of_birth)
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
    update_child(child_id, name, date_of_birth)
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
        stored=_logged_place(_current_parent()["id"], logged_id) if logged_id else None)


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

    parent = _current_parent()
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


@app.route("/place-search")
@login_required
@admin_required
def place_search_page():
    """The Place Search component's own page: a query in, candidates out.

    Isolated from Log a Place on purpose. When a submission comes back with the
    wrong address, this is how you tell a bad search result from a bad form.
    """
    return render_template("place_search.html")


def _place_search_response():
    """Find a place by name, as JSON, for whichever map is asking.

    Biased toward wherever that map is currently looking, so "the library"
    resolves to a nearby one rather than a famous namesake. Server-side, so the
    Google key stays out of the browser even though the maps themselves need
    none. Two pages and a component test page share this; they differ only in
    who is allowed to call them, which is what stays on the routes.
    """
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        places = search_places(query, lat=data.get("lat"), lng=data.get("lng"))
    except KeyError:
        return jsonify({"error": "Searching by name needs a Google Maps API key."}), 503
    except PlaceSearchError as e:
        print(f"Place search failed: {e}")
        return jsonify({"error": "Couldn't search for that right now."}), 502
    return jsonify({"query": query, "places": places})


@app.route("/place-search/run", methods=["POST"])
@login_required
@admin_required
def place_search_run_route():
    """Run the Place Search component, as JSON."""
    return _place_search_response()


@app.route("/log-place/search", methods=["POST"])
@login_required
def log_place_search_route():
    """Name lookup for the Log a Place pin."""
    return _place_search_response()


@app.route("/plan/accommodation-search", methods=["POST"])
def accommodation_search_route():
    """Name lookup for the accommodation pin on the planning form.

    Open to anyone, because /plan is: a parent plans a day before they have an
    account. That is the same exposure /plan already carries, which spends an
    AI call per generate against no login.
    """
    return _place_search_response()


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
    parent = _current_parent()
    if not _owns_place(parent["id"], place_id):
        flash("Place not found.")
        return redirect(url_for("dashboard"))
    name = request.form.get("name", "").strip()
    if not name:
        flash("A place needs a name.")
        return redirect(url_for("dashboard"))
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
    return redirect(url_for("dashboard"))


@app.route("/delete-place/<int:place_id>", methods=["POST"])
@login_required
def delete_place_route(place_id):
    """Remove one of the logged-in parent's own logged places."""
    parent = _current_parent()
    if not _owns_place(parent["id"], place_id):
        flash("Place not found.")
        return redirect(url_for("dashboard"))
    delete_venue(place_id, parent["id"])
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
    fields["transit"] = trip_form.get("transit") or DEFAULT_TRANSIT
    fields["naps"] = json.dumps(trip_form.get("naps", []))
    fields["plan_label"] = plan_data.get("label")
    fields["plan_json"] = json.dumps(plan_data)
    fields["trip_date"] = trip_form.get("trip_date") or date.today().isoformat()
    for child_id in child_ids:
        add_trip(parent["id"], int(child_id), **fields)
    return redirect(url_for("dashboard"))


@app.route("/plan", methods=["GET", "POST"])
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

    resolve_plan_child(form, _current_parent())

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
        age_months = int(form["age_years"]) * 12 + int(form["age_months"])
        result = plan_trip(
            destination=form["destination"], age_months=age_months,
            trip_date=form["trip_date"],
            wake_up=form["wake_up"], bedtime=form["bedtime"],
            stop_count=int(form["stop_count"]), dining=form["dining"],
            naps=form["naps"], preferred_lunch_time=form["preferred_lunch_time"],
            nap_notes=form["nap_notes"], extra_notes=notes_for_ai,
            transit=form["transit"], accommodation=form["accommodation"],
            accommodation_lat=form["accommodation_lat"],
            accommodation_lng=form["accommodation_lng"],
            features=form["features"], strict_schedule=form["strict_schedule"],
            interest=form["interest"], transit_nap=form["transit_nap"],
            model=_chosen_model(request.form.get("model")),
        )
        plans = [Plan.from_dict(result)]
        hours_report = result.get("hours")
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
        trip_context = None

    return render_template(
        "plan.html",
        form=form,
        plans=plans,
        hours_report=hours_report,
        adjustment=adjustment,
        trip_context=trip_context,
        supported_cities=SUPPORTED_CITIES,
        transit_options=TRANSIT_OPTIONS,
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


def _build_trip(destination, transit, bedtime, age_months, dining, plan_data,
                 trip_date="", nap_notes="", extra_notes=""):
    """Assemble a Trip around a chosen plan, shared by the fresh in-trip page
    and reopening a saved itinerary from the dashboard."""
    return Trip(
        destination=destination or "Vancouver",
        transit=transit,
        trip_date=trip_date,
        bedtime=bedtime,
        age_months=age_months,
        dining=dining,
        nap_notes=nap_notes,
        extra_notes=extra_notes,
        original=Plan.from_dict(plan_data),
    )


def _trip_venue_reports(trip):
    """{venue_id: {field: bool}} for every venue in the day.

    So the report panel can open already showing what we hold. Without it a
    parent cannot tell "nobody has said" from "we think there is one", and
    unticking could not mean "that has gone".
    """
    ids = {stop["venue"]["id"]
           for plan in trip.get("plans", [])
           for stop in plan.get("stops", [])
           if stop.get("venue") and stop["venue"].get("id")}
    return db.reported_flags(sorted(ids)) if ids else {}


def _render_trip(trip, saved=False, trip_form=None, trip_id=None):
    as_dict = trip.to_dict()
    return render_template(
        "trip.html",
        trip=as_dict,
        venue_reports=_trip_venue_reports(as_dict),
        saved=saved,
        trip_form=trip_form,
        trip_id=trip_id,
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
        plan_data = json.loads(request.form.get("plan", ""))
        context = json.loads(request.form.get("context", "{}"))
    except (ValueError, TypeError):
        return redirect(url_for("plan"))

    age_months = (int(context.get("age_years") or DEFAULTS["age_years"]) * 12
                  + int(context.get("age_months") or 0))
    trip = _build_trip(
        destination=context.get("destination"),
        transit=normalise_transit(context.get("transit")),
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
        transit=normalise_transit(row["transit"]),
        trip_date=row["trip_date"] or "",
        bedtime=row["bedtime"] or "",
        age_months=age_months,
        dining=row["dining"] or "",
        plan_data=json.loads(row["plan_json"]),
        nap_notes=row["nap_notes"] or "",
        extra_notes=row["extra_notes"] or "",
    )
    return _render_trip(trip, saved=True, trip_id=trip_id)


# What a parent tells us when they tick "the opening hours look wrong". It goes
# to the same queue scripts/verify_hours.py fills, so an admin settles a parent
# and OpenStreetMap in one place rather than two.
PARENT_HOURS_SOURCE = "parent"
PARENT_HOURS_FINDING = "A parent at the venue said our hours look wrong."


@app.route("/venues/<int:venue_id>/report", methods=["POST"])
@login_required
def report_amenities(venue_id):
    """Record what a parent saw at one stop, as JSON.

    Here rather than behind a review queue, and here rather than on an admin
    page, because the person standing in the building is the best source there
    is for whether it has a change table. A queue in front of this would mean
    these fields never fill.

    Keyed on the venue rather than the trip, because that is what the report is
    about and because a day being run has not necessarily been saved. A
    `trip_id` in the body is still checked when it is there, so a link to
    somebody else's trip is refused rather than quietly ignored.

    The body is {"found": [field...], "shown": [field...], "hours_wrong": bool,
    "trip_id": int|null}. `shown` matters: a field the panel never offered must
    not be read as "the parent says no". A highchair is not offered at a park,
    and answering for it would invent a claim nobody made.
    """
    parent = _current_parent()
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
    written = db.record_amenities(values=values, venue_id=venue_id,
                                  reported_by=parent["id"])

    if data.get("hours_wrong"):
        db.record_hours_check(venue_id, PARENT_HOURS_SOURCE,
                              source_says=f"Reported from a trip on {date.today()}",
                              finding=PARENT_HOURS_FINDING)
        written += 1

    return jsonify({"saved": written, "message": (
        f"Thanks, noted {written} thing{'s' if written != 1 else ''}."
        if written else "Nothing new to add.")})


@app.route("/replan", methods=["POST"])
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
        lat=where["lat"], lng=where["lng"])
    return jsonify({"need": need, "venues": result["places"],
                    "source": result["source"],
                    "location": location if location["lat"] is not None
                    or location["city"] else None})


@app.route("/chatbot", methods=["POST"])
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

    model = data.get("model")
    if model not in ALLOWED_CHAT_MODELS:
        model = DEFAULT_MODEL

    # The FAQ tool reads from the index, so the agent is only fully useful once
    # it's built. Kept as a hard gate rather than a warning, same as before.
    if rag.get_status()["state"] != "ready":
        return jsonify({"error": "The knowledge base is still indexing. Please try again shortly."}), 503

    # The widget echoes back whatever workflow state it was given, so this is
    # client-controlled: anything that is not a dict is dropped rather than
    # handed to a workflow, which would reach it as an attribute error.
    conversation = data.get("conversation")
    if not isinstance(conversation, dict):
        conversation = None

    try:
        result = handle_message(message, history=data.get("history") or [],
                                model=model, conversation=conversation,
                                context=_chat_context(data),
                                force_workflow=data.get("force_workflow"))
    except KeyError:
        return jsonify({"error": "The chatbot isn't configured yet."}), 500
    except (openai.OpenAIError, requests.exceptions.RequestException) as e:
        print(f"Chat turn failed: {e}")
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
