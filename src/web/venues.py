"""The review queue: what a person still has to decide about a venue.

The one admin surface in the app, and the only path that turns a proposal into
a venue. It shared app.py with the parent-facing pages purely because
@app.route needed the app object; nothing else here is about a trip.

Every route is @admin_required. The rules that decide whether a candidate may
become a venue live in _cannot_approve, which is a pure function of a row and
the closed lists it is checked against.
"""

from datetime import datetime, timezone

import requests
from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from src.store import candidates, db
from src.clients import osm
from src.data_loader import CITIES, FEATURE_LABELS, NEIGHBOURHOODS, SETTINGS, VENUE_TYPES
from src.store.db import (PromotionError, add_venue, get_pending_hours_checks,
                    get_pending_submissions, get_rejected_submissions,
                    get_unverified_venues, get_venues_missing_hours,
                    mark_verified, promote_submission, reject_submission,
                    resolve_hours_check, restore_submission,
                    set_venue_default_hours)
from src.form_helpers import clamp_int
from src.web import guards
from src.web.guards import admin_required, login_required
from src.workflows import propose_venues

bp = Blueprint("venues", __name__)

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

# What the review form asks about: the five reportable amenities plus can_eat.
# Built from both, because only can_eat is a column now -- the other five become
# reports authored by the reviewer on approval, and a form that stopped asking
# would quietly end amenity review altogether.
FLAG_LABELS = tuple(
    (key, label) for key, label in FEATURE_LABELS.items()
    if key in db.CANDIDATE_FEATURE_COLUMNS or key in db.REPORTABLE_FIELDS)

# The fields that must hold a value from a closed list before a candidate can
# become a venue, and the list each is checked against. Neighbourhood is not
# here: it may legitimately be blank, and it is checked below only when set.
APPROVAL_ENUMS = (("type", VENUE_TYPES), ("setting", SETTINGS), ("city", CITIES))

def _grouped_reports(rows):
    """Pending reports as one card per parent per venue, the shape they arrived.

    A parent ticks several boxes and sends once, so a reviewer should see "this
    parent said these four things about this place" and settle it in one go.
    """
    grouped = {}
    for row in rows:
        key = (row["venue_id"], row["reported_by"])
        card = grouped.setdefault(key, {
            "venue_id": row["venue_id"],
            "venue_name": row["venue_name"],
            "venue_type": row["venue_type"],
            "neighbourhood": row["neighbourhood"],
            "reported_by": row["reported_by"],
            "reporter_name": row["reporter_name"] or "a parent",
            "reported_at": row["reported_at"],
            "claims": [],
        })
        card["claims"].append({"field": row["field"], "value": bool(row["value"])})
    return list(grouped.values())


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


@bp.route("/venues/review")
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
        pending_reports=_grouped_reports(db.get_pending_reports()),
        # The same labels the parent ticked, so a reviewer reads the words the
        # parent saw rather than a column name.
        amenity_labels=FEATURE_LABELS,
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


@bp.route("/venues/review/<int:venue_id>", methods=["POST"])
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
            promote_submission(venue_id, guards.current_parent()["id"])
        except PromotionError as e:
            flash(str(e).capitalize() + ".")
        else:
            flash("Verified. It can now appear in plans and searches.")
    elif action == "reject":
        reject_submission(venue_id, guards.current_parent()["id"])
        flash("Set aside. It stays on file and can be restored below.")
    else:
        flash("Unknown action.")
    return redirect(url_for("venues.venue_review"))


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
        # The prefilled week, as the per-day form wants it. Parsed here rather
        # than in the template so a string that no longer reads as a week shows
        # as no week, instead of half a timetable.
        # Partial on purpose: a half-read week prefills the form so the
        # reviewer sees which days were found, while `_cannot_approve` stops
        # it being approved until the rest are filled in.
        row["week"], accounted = osm.partial_week(row.get("hours_week") or "")
        row["missing_days"] = ([osm.WEEKDAYS[d]
                                for d in sorted(set(range(7)) - accounted)]
                               if row["week"] else [])
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


@bp.route("/venues/review/candidates", methods=["POST"])
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
    if action not in ("save", "approve", "reject", "repropose"):
        flash("Unknown action.")
        return redirect(url_for("venues.venue_review"))

    picked = set(request.form.getlist("picked"))
    # The ids that were rendered, not every pending candidate. A checkbox that
    # was never on the page comes back absent, which reads identically to
    # unticked, so iterating the whole queue would wipe the flags of everything
    # the reviewer never saw.
    on_page = set(request.form.getlist("on_page"))
    admin_id = guards.current_parent()["id"]
    saved = approved = rejected = reproposed = 0
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
        if action == "repropose":
            # Back through the lookups, in place. A third verdict beside approve
            # and reject, for the row that is neither wrong nor ready: a venue
            # whose site has published hours since it was proposed, or one that
            # arrived before a lookup existed. It keeps the row and its id, so
            # nothing is re-found and no rejection is recorded against a place
            # that was never turned down.
            reproposed += _repropose_candidate(
                {**row, **edits},
                pasted=request.form.get(f"{row['id']}-hours_text", "").strip())
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
            except db.INTEGRITY_ERRORS:
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
    if reproposed:
        parts.append(f"looked {reproposed} up again")
    for name in refused:
        parts.append(f"{name} not approved")
    flash(("; ".join(parts) or "Nothing selected") + ".")
    return redirect(url_for("venues.venue_review"))


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
    # The week comes back as fourteen time inputs, not as the string it is
    # stored in, so it is reassembled here. Written blank as well as filled, or
    # clearing the per-day fields could not undo a timetable a reviewer
    # disagreed with.
    #
    # Only when the form actually carried those inputs, though. Absent reads
    # identically to cleared, and a submit from anywhere that did not render
    # them would wipe a week nobody touched. The same distinction `on_page`
    # draws for the checkboxes, for the same reason.
    if any(key.startswith(f"{candidate_id}-day") for key in form):
        week = _per_day_from_form(form, prefix=f"{candidate_id}-")
        edits["hours_week"] = osm.to_week_string(week) if week else ""
    return edits


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
    # A half-read week must not be approved. Blank days mean closed, so
    # approving "Mo-Fr 09:00-17:00" would record the venue as shut at weekends
    # on the strength of a page that simply did not mention them.
    week, accounted = osm.partial_week(row.get("hours_week") or "")
    if week and len(accounted) != 7:
        short = ", ".join(osm.WEEKDAYS[d] for d in sorted(set(range(7)) - accounted))
        return f"hours still missing for {short}"

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
    # A whole week, when the reviewer left one on the row. Parsed rather than
    # trusted: the same notation OSM uses, through the same parser, so a
    # hand-edited string that no longer reads as a week is dropped instead of
    # becoming a broken timetable. The venue keeps its single pair either way.
    week = _week_worth_storing(osm.per_day_hours(row.get("hours_week") or "") or {})
    if week:
        db.set_venue_hours(venue_id, week)
    candidates.set_status(row["id"], candidates.APPROVED, decided_by=admin_id)


def _repropose_candidate(row, pasted="") -> int:
    """Run a candidate back through the agent's lookups, in place. Returns 1.

    The same work `propose_venues.enrich` does at the end of a batch, for one
    row that has already been written: the venue's own site, its hours from
    OpenStreetMap, and failing that the hours printed on that site. A candidate
    proposed before a lookup existed, or before the venue published its hours,
    gets them without being re-found or re-searched.

    Deliberately not a re-search. The row keeps its id, its citation and any
    edits already made, so this refreshes evidence rather than replacing the
    candidate: a reviewer's correction to the name is not undone by looking the
    hours up again.
    """
    # Automatic first, manual second. Text the reviewer pasted wins, because
    # they only paste when the fetch could not do it: a page too big to read, a
    # site behind a script, hours in an image's caption. The extraction is the
    # same either way -- `hours_from_page` never cared where its text came
    # from -- so a paste needs no second code path and gets the same guards.
    proposal = dict(row, open_time="", close_time="", hours_week="")
    if pasted:
        _read_pasted_hours(proposal, pasted)
    else:
        # Looked up as if nothing were known, or `enrich` skips the page read
        # for any row that already has hours, and this exists to refresh them.
        propose_venues.enrich([proposal])

    # A refresh may improve a row and must never degrade one: a site that is
    # down today must not erase hours read from it last week. So a lookup that
    # produced no hours writes none. One that did writes all of them, blanks
    # included, or a week that has since become uniform could not clear the
    # per-day string it replaces.
    read_hours = bool(proposal.get("open_time"))
    hour_fields = ("open_time", "close_time", "hours_week", "hours_note",
                   "hours_source") if read_hours else ()
    fields = {field: proposal.get(field) or ""
              for field in (*hour_fields, "official_url")
              if (proposal.get(field) or "") != (row.get(field) or "")
              and (field in hour_fields or proposal.get(field))}
    if fields:
        candidates.refresh_evidence(row["id"], **fields)
    return 1


def _read_pasted_hours(proposal, pasted) -> None:
    """Fill a proposal's hours from text a reviewer pasted, in place.

    The manual half of the fallback, and deliberately the same three guards the
    automatic half gets: every time must appear in the pasted text, seasonal
    detail goes to `hours_note`, and a day the text never covered is left for
    the reviewer rather than guessed. Pasting does not make a claim more true.

    The text itself is not stored. It is input, not evidence: the official URL
    is the provenance, and the reviewer's own confirmation is what makes the
    hours trusted.
    """
    week, note, missing = propose_venues.hours_from_page(
        proposal.get("name") or "this venue", pasted)
    if note:
        proposal["hours_note"] = f"{note} (from what you pasted, unconfirmed)"
    if not week:
        flash("Couldn't read opening hours from that text. "
              "Paste the part of the page that lists the days and times.")
        return
    pairs = set(week.values())
    if osm.is_uniform_week(week):
        proposal["open_time"], proposal["close_time"] = pairs.pop()
    else:
        proposal["hours_week"] = osm.to_week_string(
            week, closed=set(range(7)) - missing - set(week))
        usual = max(pairs, key=list(week.values()).count)
        proposal["open_time"], proposal["close_time"] = usual
    where = propose_venues.domain(proposal.get("official_url")) or "the site"
    proposal["hours_source"] = f"pasted from {where}"
    if missing:
        short = ", ".join(osm.WEEKDAYS[d] for d in sorted(missing))
        flash(f"Read {len(week)} day{'s' if len(week) != 1 else ''} for "
              f"{proposal.get('name')}. Still missing: {short}. Fill those in "
              "below, or clear the week if the venue is not open then.")


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


def _week_worth_storing(week):
    """`week`, or {} when the venue's single pair already says it.

    Every day present *and* identical means the pair carries the whole answer,
    and seven identical rows would make "this venue has rows" stop meaning
    "this venue is unusual". A six-day week is never uniform however alike its
    days are: the seventh is a closure, and dropping it would reopen the venue
    on the day it shuts.

    Shared by approval and the review forms, because the rule was written twice
    and approval got it wrong: a reviewer filling in a uniform week on a
    candidate had seven identical rows written for it.
    """
    return {} if osm.is_uniform_week(week) else week


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
    usual = max(set(week.values()), key=list(week.values()).count)
    set_venue_default_hours(venue_id, usual[0], usual[1], hours_note)
    return db.set_venue_hours(venue_id, _week_worth_storing(week))


@bp.route("/venues/hours/<int:check_id>", methods=["POST"])
@login_required
@admin_required
def venue_hours_decide(check_id):
    """Settle one hours comparison: correct our hours, or keep them.

    The tool never changes hours itself. It reports what an outside source says
    and a person decides, because half of what it finds needs judgment: a mall
    tagged as closing at half four is more likely a mis-tagged building than a
    mall that closes at half four.
    """
    admin_id = guards.current_parent()["id"]
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
            return redirect(url_for("venues.venue_review"))
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
            return redirect(url_for("venues.venue_review"))
    else:
        flash("Kept our hours.")
    resolve_hours_check(check_id, admin_id)
    return redirect(url_for("venues.venue_review"))


@bp.route("/venues/<int:venue_id>/reports/settle", methods=["POST"])
@login_required
@admin_required
def settle_venue_reports(venue_id):
    """Approve or reject one parent's unreviewed reports about one venue.

    A batch rather than a row, because that is how they arrive: a parent ticks
    several boxes and sends once. One decision per tick is the friction that
    argued against having a queue here at all, so the queue settles what the
    parent actually did.
    """
    admin_id = guards.current_parent()["id"]
    reporter = request.form.get("reported_by", type=int)
    approved = request.form.get("decision") == "approve"
    db.settle_reports_for(venue_id, reporter, approved=approved, admin_id=admin_id)
    flash("Report approved and applied." if approved
          else "Report rejected; nothing changed.")
    return redirect(url_for("venues.venue_review"))


@bp.route("/venues/<int:venue_id>/hours", methods=["POST"])
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
        return redirect(url_for("venues.venue_review"))
    opens, closes = _hour_pair(request.form)
    if not (opens and closes):
        flash("Both an opening and a closing time are needed, as HH:MM.")
        return redirect(url_for("venues.venue_review"))
    set_venue_default_hours(venue_id, opens, closes, note)
    flash(f"Hours set to {opens}-{closes}. It can be planned around now.")
    return redirect(url_for("venues.venue_review"))


@bp.route("/venues/restore", methods=["POST"])
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
    return redirect(url_for("venues.venue_review"))


@bp.route("/venues/confirm", methods=["POST"])
@login_required
@admin_required
def venue_confirm_batch():
    """Record that a person checked venues the app was already planning around.

    A citation may come with each one, from that venue's own box. It is what
    makes the stamp mean something later: most of these rows have none, so
    "confirmed" would otherwise say only that somebody clicked.
    """
    admin_id = guards.current_parent()["id"]
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
    return redirect(url_for("venues.venue_review"))


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


@bp.route("/propose-venues")
@login_required
@admin_required
def propose_venues_page():
    """The venue proposal component's own page: run a small batch, see it."""
    return render_template("propose_venues.html",
                           counts=candidates.counts(),
                           batch_size=propose_venues.DEFAULT_BATCH_SIZE)


@bp.route("/propose-venues/run", methods=["POST"])
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
