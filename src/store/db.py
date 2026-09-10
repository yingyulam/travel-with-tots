"""Every query the app runs, against SQLite or Postgres.

SQL is written in SQLite's dialect; postgres.py translates it.
Connections come from connection.py, table creation from schema.py.

Every write is parameterised and runs in a transaction.
"""

from contextlib import closing

from werkzeug.security import check_password_hash, generate_password_hash

from . import connection


# Flag columns the planner may filter candidates by. Never interpolate a column
# name that is not in here. `can_eat` follows the kind of place and is set at
# import and review; the amenities are REPORTABLE_FIELDS instead, so a claim
# carries an author and a date.
CANDIDATE_FEATURE_COLUMNS = {"can_eat"}

# The amenities a visitor is in a position to report, and therefore the only
# ones read from venue_reports rather than off the venue row. can_eat is not
# here: it follows the kind of place and is set when a venue is added.
REPORTABLE_FIELDS = ("has_washroom", "has_family_room", "has_nursing_room",
                     "stroller_accessible", "has_highchair")

# Only worth asking where a meal can happen on site. A highchair at a park is
# not a question, and asking it is how a form loses its reader.
CONDITIONAL_ON_CAN_EAT = ("has_highchair",)

# The amenities a parent can vouch for when logging a place, as
# (field name, label). A subset of REPORTABLE_FIELDS; the names match
# add_venue's parameters. Shared by the Log a Place page, the dashboard's edit
# form, the workflow and the chat agent's tool.
AMENITY_OPTIONS = [
    ("has_family_room", "Family room"),
    ("has_nursing_room", "Nursing room"),
    ("stroller_accessible", "Stroller / step-free"),
]


# Venue sources trustworthy enough to plan a day around, by provenance
# (municipal) or inspection (curated, recorded in verified_at). Excludes
# 'user_submitted' until a reviewer promotes it. Covers a venue's existence and
# location, not its hours or its suitability for a toddler.
VERIFIED_SOURCES = ("curated", "municipal_open_data")

# Keeps the AI planner's prompt cheap: enough venues for a real choice,
# never so many the prompt balloons.
CANDIDATE_LIMIT = 18

# A neighbourhood needs at least this many matching venues before it's worth
# narrowing to (see get_candidate_venues) -- otherwise a parent without a car
# could end up with too few candidates to build a real itinerary from.
MIN_CLUSTER_SIZE = 6

TRIP_FIELDS = (
    "trip_date", "wake_up", "bedtime", "naps", "transit_nap",
    "destination", "accommodation", "accommodation_lat", "accommodation_lng",
    "transit",
    "stop_count", "dining", "preferred_lunch_time", "nap_notes",
    "extra_notes", "plan_label", "plan_json",
    "trip_group_id", "day_index",
)


# The password `schema._seed_admin` used to hard-code, published in this repository.
# Kept only so a database seeded before that changed can be checked for it,
# which is the one thing a published password is still good for.
RETIRED_PASSWORD = "admin1234"

# Passwords that must never open an admin account, checked before a deploy.
# Covers both the seeded defaults and the throwaway values tests use, since a
# test run against a real database can leave one behind and a clone carries it.
WEAK_PASSWORDS = (RETIRED_PASSWORD, "demo1234", "pw", "x", "test", "admin",
                  "password", "secret", "hash", "hashed", "12345678")


def admins_with_password(password):
    """Admin accounts whose password is still `password`.

    Every hash is checked, which is slow by design and fine over a handful of
    admins.
    """
    with closing(connection.connect()) as conn:
        rows = conn.execute(
            "SELECT id, email, password_hash FROM parents WHERE is_admin = 1"
        ).fetchall()
    return [dict(row) for row in rows
            if check_password_hash(row["password_hash"], password)]


def admins_with_weak_password():
    """Every admin whose password is a known or guessable one.

    /settings can change the data source and rewrite the chatbot's prompt, so
    one such account is enough to lose the deployment.
    """
    found = {}
    for password in WEAK_PASSWORDS:
        for row in admins_with_password(password):
            found.setdefault(row["email"], password)
    return found


def list_admins():
    """Every account that can reach /settings, so the answer to "who has admin"
    is one command rather than a query somebody writes by hand."""
    with closing(connection.connect()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id, email, name FROM parents WHERE is_admin = 1 "
            "ORDER BY email")]


def make_admin(email):
    """Give an existing account admin rights, leaving its password alone.

    Returns the parent id, or None when there is no such account.
    """
    parent = get_parent_by_email(email.strip().lower())
    if parent is None:
        return None
    _write("UPDATE parents SET is_admin = 1 WHERE id = ?", (parent["id"],))
    return parent["id"]


def revoke_admin(email):
    """Take admin rights away. The counterpart to make_admin, and what makes a
    promotion reversible without touching the database by hand."""
    parent = get_parent_by_email(email.strip().lower())
    if parent is None:
        return None
    _write("UPDATE parents SET is_admin = 0 WHERE id = ?", (parent["id"],))
    return parent["id"]


def set_admin_password(email, password):
    """Set an account's password and make it an admin, creating it if needed.

    Prefer signup plus make_admin, which never handles a password. Works
    against whichever backend is selected.
    """
    email = email.strip().lower()
    hashed = generate_password_hash(password)
    existing = get_parent_by_email(email)
    if existing:
        _write("UPDATE parents SET password_hash = ?, is_admin = 1 WHERE id = ?",
               (hashed, existing["id"]))
        return existing["id"], "updated"
    return _write(
        "INSERT INTO parents (email, password_hash, name, is_admin) "
        "VALUES (?, ?, ?, 1)", (email, hashed, "Admin")), "created"


def delete_parent(email):
    """Remove an account and everything hanging off it.

    Children, trips and reports follow via ON DELETE CASCADE.
    """
    parent = get_parent_by_email(email.strip().lower())
    if parent is None:
        return None
    _write("DELETE FROM parents WHERE id = ?", (parent["id"],))
    return parent["id"]


def _write(sql, params):
    """Run one parameterized write in its own transaction; return lastrowid."""
    with closing(connection.connect()) as conn, conn:
        return conn.execute(sql, params).lastrowid


def add_parent(email, password_hash, name=None):
    return _write(
        "INSERT INTO parents (email, password_hash, name) VALUES (?, ?, ?)",
        (email, password_hash, name))


def add_child(parent_id, name, date_of_birth):
    return _write(
        "INSERT INTO children (parent_id, name, date_of_birth) "
        "VALUES (?, ?, ?)", (parent_id, name, date_of_birth))


def update_child(child_id, name, date_of_birth):
    _write(
        "UPDATE children SET name = ?, date_of_birth = ? WHERE id = ?",
        (name, date_of_birth, child_id))


def delete_child(child_id):
    """Remove a child.

    Their saved trips are kept (owned by the parent account, not
    the child); only child_id is cleared via ON DELETE SET NULL.
    """
    _write("DELETE FROM children WHERE id = ?", (child_id,))


def add_trip(parent_id, child_id, **fields):
    """Insert a trip.

    Only known columns (TRIP_FIELDS) are accepted, so the column
    names are never user-controlled and the values stay parameterized.
    """
    columns = ["parent_id", "child_id"] + [f for f in TRIP_FIELDS if f in fields]
    values = [parent_id, child_id] + [fields[f] for f in TRIP_FIELDS if f in fields]
    placeholders = ", ".join("?" for _ in columns)
    return _write(
        f"INSERT INTO trips ({', '.join(columns)}) VALUES ({placeholders})", values)


def delete_trip(trip_id, parent_id):
    """Remove one of this parent's saved trips (ownership enforced here)."""
    _write("DELETE FROM trips WHERE id = ? AND parent_id = ?", (trip_id, parent_id))


# Columns add_venue will set beyond `name`, `source` and the flags. Whitelisted
# so an unknown keyword fails loudly rather than being dropped, the same
# discipline update_venue uses.
ADD_VENUE_FIELDS = ("type", "setting", "neighbourhood", "city", "notes",
                    "hours_note", "address", "open_time", "close_time",
                    "lat", "lng", "parent_id", "source_url",
                    "external_id", "verified_at", "verified_by")


def reject_unknown_fields(fields, allowed, what):
    """Raise unless every name in `fields` is in `allowed`.

    Every **fields writer here takes a whitelist, so a renamed form input fails
    loudly instead of silently dropping an edit somebody made.
    """
    unknown = set(fields) - set(allowed)
    if unknown:
        raise ValueError(f"not {what}: {', '.join(sorted(unknown))}")


def add_venue(name, *, source, venue_type=None, **fields):
    """Insert a venue. Returns its id.

    `city`, `lat` and `lng` are optional so a submission survives an
    unreachable geocoder, but without coordinates the row can never be
    distance-ranked and without a city it never matches a city query.

    `source` alone decides whether the row is searchable: only VERIFIED_SOURCES
    are queried, so a "user_submitted" row stays out of every result until it
    is promoted. `venue_type` is spelled out because `type` shadows a builtin.
    """
    reject_unknown_fields(
        fields, set(ADD_VENUE_FIELDS) | set(CANDIDATE_FEATURE_COLUMNS),
        "a venue field")
    if venue_type is not None:
        fields["type"] = venue_type
    # Flags are 0/1 in SQLite, and callers pass real booleans.
    for flag in CANDIDATE_FEATURE_COLUMNS & set(fields):
        fields[flag] = int(bool(fields[flag]))
    columns = ", ".join(("name", "source") + tuple(fields))
    placeholders = ", ".join("?" for _ in range(len(fields) + 2))
    return _write(f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
                  (name, source, *fields.values()))


# The columns log_a_place.store owns on a submission. Wider than
# EDITABLE_VENUE_FIELDS, the parent's own edit form, because re-submitting the
# whole form may mean the parent moved the map pin. Excludes source and
# parent_id.
SUBMISSION_FIELDS = ("type", "setting", "neighbourhood", "city", "lat", "lng",
                     "notes", "address")


def add_or_update_submission(name, *, parent_id, **fields):
    """Store this parent's submission of `name`, replacing their own earlier
    submission of the same place rather than adding a second row. Returns the
    row id either way.

    Matching is scoped to this parent's own user_submitted rows:
    venues.parent_id is nullable, so id alone would not guard a curated venue
    or another parent's submission.
    """
    reject_unknown_fields(fields, SUBMISSION_FIELDS, "a submission field")
    with closing(connection.connect()) as conn, conn:
        existing = conn.execute(
            "SELECT id FROM venues WHERE parent_id = ? AND name = ? "
            "AND source = 'user_submitted'", (parent_id, name)).fetchone()
        if existing:
            assignments = ", ".join(f"{field} = ?" for field in fields)
            conn.execute(f"UPDATE venues SET {assignments} WHERE id = ?",
                         (*fields.values(), existing["id"]))
            return existing["id"]
        columns = ", ".join(("name", "source", "parent_id") + tuple(fields))
        placeholders = ", ".join("?" for _ in range(len(fields) + 3))
        return conn.execute(
            f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
            (name, "user_submitted", parent_id, *fields.values())).lastrowid


# The fields a parent may change on their own submission. Deliberately excludes
# source, parent_id and the coordinates: source is the verification gate, and
# letting an edit rewrite it would turn "correct my typo" into "publish this".
EDITABLE_VENUE_FIELDS = ("name", "type", "setting", "neighbourhood", "notes")

# What a reviewer may correct on a venue already in the searchable set. Wider
# than EDITABLE_VENUE_FIELDS, the parent's own form. Excludes hours (see
# set_venue_default_hours), amenities (reports, not columns), source and
# parent_id.
REVIEWABLE_VENUE_FIELDS = ("name", "type", "setting", "neighbourhood", "city",
                           "can_eat")


def update_reviewed_venue(venue_id, **fields):
    """Correct a venue already in the searchable set, from the review queue.

    Scoped to VERIFIED_SOURCES in the SQL, so a pending submission stays in the
    other queue. Unknown field names raise rather than silently dropping an
    edit.
    """
    reject_unknown_fields(fields, REVIEWABLE_VENUE_FIELDS, "reviewable")
    if not fields:
        return
    source_clause, source_params = _verified_source_clause()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    _write(f"UPDATE venues SET {assignments} WHERE id = ? AND {source_clause}",
           [*fields.values(), venue_id, *source_params])


def update_venue(venue_id, parent_id, **fields):
    """Update one of this parent's own submissions.

    Ownership and the source gate are enforced in the SQL: venues.parent_id is
    nullable, so a query keyed on id alone could rewrite a curated row, and a
    submission that has since been verified is no longer the parent's to edit.

    Unknown or non-editable field names raise rather than silently dropping an
    edit.
    """
    reject_unknown_fields(fields, EDITABLE_VENUE_FIELDS, "editable")
    if not fields:
        return
    assignments = ", ".join(f"{name} = ?" for name in fields)
    _write(
        f"UPDATE venues SET {assignments} "
        "WHERE id = ? AND parent_id = ? AND source = 'user_submitted'",
        (*fields.values(), venue_id, parent_id))


def delete_venue(venue_id, parent_id):
    """Remove one of this parent's own submissions. Same guards as
    update_venue, and for the same reason."""
    _write("DELETE FROM venues WHERE id = ? AND parent_id = ? "
           "AND source = 'user_submitted'", (venue_id, parent_id))


class PromotionError(Exception):
    """A submission could not be verified into the curated set."""


def get_pending_submissions():
    """Every unverified submission, newest first, for the admin review queue.

    Each row carries `curated_clash`: how many curated venues already cover the
    same name and city, which is what idx_venues_curated_identity would refuse
    on promotion.
    """
    with closing(connection.connect()) as conn:
        return conn.execute("""
            SELECT v.*, p.email AS submitted_by,
                   (SELECT COUNT(*) FROM venues c
                    WHERE c.source = 'curated' AND c.name = v.name
                      AND IFNULL(c.city, '') = IFNULL(v.city, '')) AS curated_clash
            FROM venues v LEFT JOIN parents p ON p.id = v.parent_id
            WHERE v.source = 'user_submitted' AND v.rejected_at IS NULL
            ORDER BY v.created_at DESC, v.id DESC""").fetchall()


def promote_submission(venue_id, admin_id):
    """Verify one submission into the curated set, recording who and when.

    Raises PromotionError rather than half-succeeding when the row has no city
    (get_venues_in_city matches on `city LIKE`, so it would be published and
    invisible at once) or when a curated venue of the same name and city
    already exists.
    """
    with closing(connection.connect()) as conn, conn:
        row = conn.execute(
            "SELECT name, city FROM venues WHERE id = ? AND source = 'user_submitted' "
            "AND rejected_at IS NULL",
            (venue_id,)).fetchone()
        if row is None:
            raise PromotionError("that submission is no longer pending")
        if not (row["city"] or "").strip():
            raise PromotionError(
                f"{row['name']} has no city, so it would never match a search. "
                "Add one before verifying it")
        clash = conn.execute(
            "SELECT 1 FROM venues WHERE source = 'curated' AND name = ? "
            "AND IFNULL(city, '') = IFNULL(?, '')",
            (row["name"], row["city"])).fetchone()
        if clash:
            raise PromotionError(
                f"{row['name']} is already a curated venue in {row['city']}. "
                "Reject this one instead, or rename it if it is a different place")
        conn.execute(
            "UPDATE venues SET source = 'curated', verified_at = datetime('now'), "
            "verified_by = ? WHERE id = ?", (admin_id, venue_id))


def reject_submission(venue_id, admin_id=None):
    """Set a submission aside. Recorded as a timestamp, not deleted, and undone
    by restore_submission.

    Scoped to user_submitted in the SQL, so an admin acting on a stale page
    cannot set aside a venue that has since been verified.
    """
    _write("UPDATE venues SET rejected_at = datetime('now'), rejected_by = ? "
           "WHERE id = ? AND source = 'user_submitted'", (admin_id, venue_id))


def restore_submission(venue_id):
    """Put a rejected submission back in the queue, for a reviewer who changed
    their mind or rejected the wrong row."""
    _write("UPDATE venues SET rejected_at = NULL, rejected_by = NULL "
           "WHERE id = ? AND source = 'user_submitted'", (venue_id,))


def get_rejected_submissions():
    """Submissions set aside, newest decision first, so they can be revisited."""
    with closing(connection.connect()) as conn:
        return conn.execute("""
            SELECT v.*, p.email AS submitted_by
            FROM venues v LEFT JOIN parents p ON p.id = v.parent_id
            WHERE v.source = 'user_submitted' AND v.rejected_at IS NOT NULL
            ORDER BY v.rejected_at DESC, v.id DESC""").fetchall()


# What an importer is allowed to write. Narrower than ADD_VENUE_FIELDS: no
# parent_id (nobody submitted these), no verified_at (no human checked them),
# and no seed_rank, which is the curator's ordering and not an import's to set.
IMPORT_FIELDS = ("type", "setting", "neighbourhood", "city", "address",
                 "lat", "lng", "open_time", "close_time", "can_eat")


def upsert_imported_venue(external_id, name, *, source, source_url, **fields):
    """Write one open-data record into `venues`.

    Returns (venue_id, action), action being "inserted", "upgraded" or
    "unchanged".

    Matching is two steps:

      1. by external_id, which recognises this importer's earlier runs and is
         what makes it re-runnable;
      2. failing that, by name against a curated row, which upgrades a
         hand-curated venue in place instead of duplicating it.

    An import fills blanks and never overwrites a value, on either path: what
    is already there was typed by a curator or corrected by an admin. Only
    external_id and source_url are written unconditionally. `source` and
    `seed_rank` are never touched on an upgrade, since `source` decides which
    queue a row sits in.

    A consequence worth knowing: a renamed or moved record is not picked up by
    a re-run. Delete the row to take such a correction.
    """
    reject_unknown_fields(fields, IMPORT_FIELDS, "an import field")
    with closing(connection.connect()) as conn, conn:
        row = conn.execute("SELECT * FROM venues WHERE external_id = ?",
                           (external_id,)).fetchone()
        action = "unchanged"
        if row is None:
            row = conn.execute(
                "SELECT * FROM venues WHERE name = ? AND source = 'curated'",
                (name,)).fetchone()
            action = "upgraded" if row else "inserted"
        if row is not None:
            # Only the columns actually empty on the row, so a value a human
            # put there survives every future run.
            blanks = {field: value for field, value in fields.items()
                      if row[field] is None or row[field] == ""}
            assignments = ", ".join(f"{field} = ?" for field in blanks)
            if assignments:
                assignments += ", "
            conn.execute(
                f"UPDATE venues SET {assignments}external_id = ?, source_url = ? "
                "WHERE id = ?",
                (*blanks.values(), external_id, source_url, row["id"]))
            return row["id"], action
        columns = ", ".join(("name", "source", "external_id", "source_url")
                            + tuple(fields))
        placeholders = ", ".join("?" for _ in range(len(fields) + 4))
        venue_id = conn.execute(
            f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
            (name, source, external_id, source_url, *fields.values())).lastrowid
        return venue_id, "inserted"


def get_venues_missing_hours():
    """Verified venues with no default open/close pair.

    A venue without hours cannot be scheduled, so these are in the table and
    invisible to the planner. The review page lists them to be filled in.
    """
    source_clause, source_params = _verified_source_clause()
    with closing(connection.connect()) as conn:
        return conn.execute(
            f"SELECT * FROM venues WHERE {source_clause} "
            "AND (open_time IS NULL OR open_time = '' "
            "     OR close_time IS NULL OR close_time = '') "
            "ORDER BY name", source_params).fetchall()


def get_unverified_venues(limit=None):
    """Venues in the searchable set that no human has confirmed.

    Scoped to 'curated', which is the tier trusted only because of how it was
    typed in. Municipal rows are left unverified deliberately, since the City
    is authoritative about its own parks; user_submitted rows belong to the
    other queue (see get_pending_submissions).
    """
    sql = ("SELECT * FROM venues WHERE source = 'curated' "
           "AND verified_at IS NULL "
           "ORDER BY seed_rank IS NULL, seed_rank, name")
    params = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with closing(connection.connect()) as conn:
        return conn.execute(sql, params).fetchall()


def mark_verified(venue_id, admin_id, source_url=None):
    """Record that a human confirmed a venue already in the searchable set.

    Only stamps verified_at and verified_by; unlike promote_submission it never
    changes `source`. Scoped to VERIFIED_SOURCES. `source_url` is what the
    confirmation was checked against, and blank leaves whatever is already
    there.
    """
    source_clause, source_params = _verified_source_clause()
    if source_url:
        _write(f"UPDATE venues SET source_url = ? WHERE id = ? AND {source_clause}",
               [source_url, venue_id, *source_params])
    _write(
        "UPDATE venues SET verified_at = datetime('now'), verified_by = ? "
        f"WHERE id = ? AND {source_clause}",
        [admin_id, venue_id, *source_params])


def get_venue_hours(venue_ids=None):
    """{venue_id: {weekday: (open, close)}} for venues that vary by day.

    A venue absent from the result keeps one pair all week. One query for a
    whole set, because the planner resolves hours for every venue on one date.
    """
    if venue_ids is not None and not venue_ids:
        return {}
    sql = "SELECT venue_id, weekday, open_time, close_time FROM venue_hours"
    params = []
    if venue_ids is not None:
        sql += f" WHERE venue_id IN ({', '.join('?' * len(venue_ids))})"
        params = list(venue_ids)
    out = {}
    with closing(connection.connect()) as conn:
        for row in conn.execute(sql, params):
            out.setdefault(row["venue_id"], {})[row["weekday"]] = (
                row["open_time"], row["close_time"])
    return out


def set_venue_hours(venue_id, by_weekday):
    """Replace a venue's per-day hours with `by_weekday`, or clear them.

    `by_weekday` is {weekday: (open, close)}. A replacement rather than a
    merge: the rows are a complete description, so a leftover row would claim
    the venue opens on a day the new answer omits. An empty mapping deletes
    every row and hands the venue back to its single pair.
    """
    rows = [(venue_id, day, opens, closes)
            for day, (opens, closes) in sorted(by_weekday.items())
            if opens and closes]
    with closing(connection.connect()) as conn, conn:
        conn.execute("DELETE FROM venue_hours WHERE venue_id = ?", (venue_id,))
        if rows:
            conn.executemany(
                "INSERT INTO venue_hours (venue_id, weekday, open_time, "
                "close_time) VALUES (?, ?, ?, ?)", rows)
    return len(rows)


def set_venue_default_hours(venue_id, open_time, close_time, hours_note=None):
    """Correct a venue's usual hours, and the note no timetable can hold.

    The only path by which an approved venue's hours change, since
    EDITABLE_VENUE_FIELDS excludes them. `hours_note` carries seasonal bands
    and closures, in words a parent reads.
    """
    _write("UPDATE venues SET open_time = ?, close_time = ?, hours_note = ? "
           "WHERE id = ?",
           (open_time or None, close_time or None, hours_note or None, venue_id))


def record_hours_check(venue_id, source, source_says, finding,
                        our_open=None, our_close=None):
    """Note that an outside source disagrees with our hours, for a person to
    settle. Replaces any open check from the same source for the same venue, so
    re-running the tool refreshes a finding instead of stacking duplicates."""
    _write("DELETE FROM venue_hours_checks WHERE venue_id = ? AND source = ? "
           "AND status = 'pending'", (venue_id, source))
    return _write(
        "INSERT INTO venue_hours_checks (venue_id, source, source_says, "
        "finding, our_open, our_close) VALUES (?, ?, ?, ?, ?, ?)",
        (venue_id, source, source_says, finding, our_open, our_close))


def get_pending_hours_checks():
    """Open hours comparisons, with the venue they concern."""
    with closing(connection.connect()) as conn:
        return conn.execute("""
            SELECT c.*, v.name, v.type, v.neighbourhood,
                   v.open_time AS current_open, v.close_time AS current_close,
                   v.hours_note AS current_note
            FROM venue_hours_checks c JOIN venues v ON v.id = c.venue_id
            WHERE c.status = 'pending'
            ORDER BY c.checked_at DESC, c.id DESC""").fetchall()


def resolve_hours_check(check_id, admin_id=None):
    """Close a comparison, whether the hours were changed or kept."""
    _write("UPDATE venue_hours_checks SET status = 'resolved', "
           "decided_at = datetime('now'), decided_by = ? WHERE id = ?",
           (admin_id, check_id))


def pending_reports_for(parent_id, venue_ids):
    """{venue_id: {field: bool}} of this parent's own unreviewed reports.

    Their own only: a pending claim is a record of what one person reported,
    not yet information about the venue.
    """
    ids = [i for i in (venue_ids or [])]
    if not parent_id or not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    pending = {}
    with closing(connection.connect()) as conn:
        rows = conn.execute(
            f"SELECT venue_id, field, value FROM venue_reports "
            f"WHERE status = 'pending' AND reported_by = ? "
            f"AND venue_id IN ({placeholders}) ORDER BY reported_at, id",
            [parent_id, *ids])
        for row in rows:
            pending.setdefault(row["venue_id"], {})[row["field"]] = bool(row["value"])
    return pending


def get_pending_reports():
    """Unreviewed amenity reports, with the venue and the parent behind each.

    Grouped by venue and parent in the caller, so a reviewer settles the batch
    a parent submitted rather than clicking once per tick.
    """
    with closing(connection.connect()) as conn:
        return conn.execute("""
            SELECT r.*, v.name AS venue_name, v.type AS venue_type,
                   v.neighbourhood, p.name AS reporter_name
            FROM venue_reports r
            JOIN venues v ON v.id = r.venue_id
            LEFT JOIN parents p ON p.id = r.reported_by
            WHERE r.status = 'pending'
            ORDER BY r.reported_at DESC, r.id DESC""").fetchall()


def settle_report(report_id, approved, admin_id=None):
    """Approve or reject one unreviewed report.

    Rejected rather than deleted, so the same claim cannot arrive again looking
    new.
    """
    _write("UPDATE venue_reports SET status = ?, decided_at = datetime('now'), "
           "decided_by = ? WHERE id = ? AND status = 'pending'",
           ("approved" if approved else "rejected", admin_id, report_id))


def settle_reports_for(venue_id, parent_id, approved, admin_id=None):
    """Settle every pending report one parent made about one venue, at once."""
    _write("UPDATE venue_reports SET status = ?, decided_at = datetime('now'), "
           "decided_by = ? WHERE venue_id = ? AND reported_by IS ? "
           "AND status = 'pending'",
           ("approved" if approved else "rejected", admin_id, venue_id, parent_id))


def add_report(venue_id, field, value, reported_by=None, note=None,
               approved=True):
    """Record that somebody observed an amenity at a venue, or its absence.

    `value` 0 is a real report: "I looked and there was none" differs from
    silence. `reported_by` None marks a claim with no author, which ranks below
    any real report. `approved` defaults to True; the in-trip route passes
    False so a parent's claim waits for a reviewer.
    """
    if field not in REPORTABLE_FIELDS:
        raise ValueError(f"not a reportable field: {field}")
    if not approved:
        # One pending report per parent, per venue, per field. record_amenities
        # compares against approved flags, which cannot see a pending row, so
        # without this a second report would stack.
        _write("DELETE FROM venue_reports WHERE venue_id = ? AND field = ? "
               "AND reported_by IS ? AND status = 'pending'",
               (venue_id, field, reported_by))
    return _write(
        "INSERT INTO venue_reports (venue_id, field, value, reported_by, note, "
        "status) VALUES (?, ?, ?, ?, ?, ?)",
        (venue_id, field, int(bool(value)), reported_by, note,
         "approved" if approved else "pending"))


def record_amenities(venue_id, values, reported_by, note=None, approved=True):
    """Write reports for the amenities in `values`, from one author.

    Returns how many were written.

    `values` is {field: truthy} over REPORTABLE_FIELDS and anything else is
    ignored, so a caller can hand over a whole form dict. Only changed answers
    are written, because recency decides a conflict and a duplicate would move
    a claim's date without anybody having looked.

    The only way an amenity claim enters the database, so every claim carries
    an author and a date.
    """
    known = reported_flags([venue_id]).get(venue_id, {})
    written = 0
    for field in REPORTABLE_FIELDS:
        if field not in values:
            continue
        value = bool(values[field])
        if field in known and known[field] == value and not note:
            continue
        add_report(venue_id, field, value, reported_by=reported_by, note=note,
                   approved=approved)
        written += 1
    return written


def reported_flags(venue_ids=None):
    """{venue_id: {field: bool}} from the reports. Newest report per field wins.

    A report with an author outranks an authorless claim of any age; between
    real reports the newest wins, because amenities genuinely change. A field
    with no report is absent, which is what lets "nobody has said" differ from
    "somebody looked and there was none".
    """
    # Approved only. This is the whole enforcement of "a parent's in-trip
    # report waits for a reviewer": the planner, the nearby search, the review
    # page and the in-trip panel every one of them read the flags through here,
    # so nothing can show an unchecked claim by forgetting to filter.
    sql = ("SELECT venue_id, field, value, reported_by, reported_at "
           "FROM venue_reports WHERE status = 'approved'")
    params = []
    if venue_ids is not None:
        ids = list(venue_ids)
        if not ids:
            return {}
        sql += f" AND venue_id IN ({', '.join('?' for _ in ids)})"
        params = ids
    # Weakest first, so a later row simply overwrites: seed claims before real
    # reports, and older reports before newer.
    sql += " ORDER BY reported_by IS NOT NULL, reported_at, id"
    flags = {}
    with closing(connection.connect()) as conn:
        for row in conn.execute(sql, params):
            flags.setdefault(row["venue_id"], {})[row["field"]] = bool(row["value"])
    return flags


def get_parent_by_email(email):
    with closing(connection.connect()) as conn:
        return conn.execute(
            "SELECT * FROM parents WHERE email = ?", (email,)).fetchone()


def get_parent(parent_id):
    with closing(connection.connect()) as conn:
        return conn.execute(
            "SELECT * FROM parents WHERE id = ?", (parent_id,)).fetchone()


def get_children(parent_id):
    with closing(connection.connect()) as conn:
        return conn.execute(
            "SELECT * FROM children WHERE parent_id = ? ORDER BY created_at",
            (parent_id,)).fetchall()


def get_trips_for_parent(parent_id):
    """Trips with a saved itinerary owned by this parent, newest first (older
    rows saved without a plan_json have nothing to open, so they're excluded
    rather than shown as a dead link). LEFT JOIN so a trip whose child was
    since removed still shows, with child_name as NULL."""
    with closing(connection.connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name FROM trips "
            "LEFT JOIN children ON children.id = trips.child_id "
            "WHERE trips.parent_id = ? AND trips.plan_json IS NOT NULL "
            "ORDER BY trips.created_at DESC", (parent_id,)).fetchall()


def get_trip_group(parent_id, group_id):
    """Every day of one saved trip, in order, scoped to this parent.

    Ordered by day_index rather than date, so a row whose date failed to save
    keeps its place.
    """
    with closing(connection.connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name, "
            "children.date_of_birth AS child_dob FROM trips "
            "LEFT JOIN children ON children.id = trips.child_id "
            "WHERE trips.parent_id = ? AND trips.trip_group_id = ? "
            "ORDER BY trips.day_index", (parent_id, group_id)).fetchall()


def get_trip_for_parent(parent_id, trip_id):
    """One trip by id, scoped to this parent's own trips (ownership check)."""
    with closing(connection.connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name, "
            "children.date_of_birth AS child_dob FROM trips "
            "LEFT JOIN children ON children.id = trips.child_id "
            "WHERE trips.parent_id = ? AND trips.id = ?",
            (parent_id, trip_id)).fetchone()


def get_candidate_venues(city, age_months=None, features=None, transit=None,
                          dining=None, near_neighbourhood=None, limit=CANDIDATE_LIMIT):
    """Verified venues in `city`, for grounding the AI planner.

    Substring match on city. The planner must never reference a venue outside
    this list.

    `age_months` and `features` are accepted and ignored.

    Narrowing, in order: to `near_neighbourhood` if given and it holds at least
    MIN_CLUSTER_SIZE venues; otherwise, without a car, to the most common
    neighbourhood among the matches under the same threshold; with a car, every
    matching neighbourhood stays in play. If `dining` is "dine_out", one venue
    where a meal is possible is guaranteed a slot.
    """
    where, params = _candidate_where_clause(city)

    with closing(connection.connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM venues WHERE {where} ORDER BY name", params).fetchall()
        rows = _narrow_by_neighbourhood(rows, near_neighbourhood, transit)
        rows = rows[:limit]
        rows = _ensure_dining_option(conn, rows, where, params, dining, limit)
    # Reports overlaid, as data_loader.get_venues does: an amenity is whatever
    # somebody last observed, not what a column claims. These rows are both
    # described to the planner and swapped into plans as venues.
    reported = reported_flags([row["id"] for row in rows])
    return [{**dict(row), **reported.get(row["id"], {})} for row in rows]


def _verified_source_clause():
    """SQL fragment and params restricting a query to VERIFIED_SOURCES.
    Parameterized rather than interpolated, so adding a source can never
    become a SQL-injection seam."""
    placeholders = ", ".join("?" for _ in VERIFIED_SOURCES)
    return f"source IN ({placeholders})", list(VERIFIED_SOURCES)


def _candidate_where_clause(city):
    """WHERE clause and params for a verified-venue lookup.

    Matches `city` as a substring, and requires an open/close pair.

    Hours are required because a venue without them is not schedulable, so
    offering one would spend a CANDIDATE_LIMIT slot on a stop that could only
    be replaced. Those venues surface in get_venues_missing_hours instead.

    No amenity or age filtering: find_nearby answers what a parent needs in the
    moment, and age paces the day rather than filtering venues.
    """
    source_clause, source_params = _verified_source_clause()
    return (f"{source_clause} AND city LIKE ? AND open_time IS NOT NULL "
            "AND open_time != '' AND close_time IS NOT NULL "
            "AND close_time != ''"), source_params + [f"%{city}%"]


def get_venue_types_in_use():
    """The venue types at least one searchable venue actually has.

    Read from the table so the plan form never offers a kind of place there is
    nothing behind.
    """
    source_clause, source_params = _verified_source_clause()
    with closing(connection.connect()) as conn:
        return {row["type"] for row in conn.execute(
            f"SELECT DISTINCT type FROM venues WHERE {source_clause} "
            "AND type IS NOT NULL AND type != ''", source_params)}


def get_venues_in_city(city):
    """Every verified venue in `city` (substring match, same as
    get_candidate_venues). Deliberately unfiltered beyond the city: callers
    decide what "matching" means -- see components/find_nearby.py, which
    applies interactions.NEED_FILTERS so need semantics live in one place."""
    source_clause, source_params = _verified_source_clause()
    with closing(connection.connect()) as conn:
        return conn.execute(
            f"SELECT * FROM venues WHERE {source_clause} AND city LIKE ? "
            "ORDER BY name", source_params + [f"%{city}%"]).fetchall()


def _narrow_by_neighbourhood(rows, near_neighbourhood, transit):
    """Narrow `rows` to a single neighbourhood, when enough venues remain.

    Keeps at least MIN_CLUSTER_SIZE venues: narrows to `near_neighbourhood` if
    given, otherwise to the most common one and only without a car. Falls back
    to every row when narrowing would leave too few.
    """
    if near_neighbourhood is not None:
        narrowed = [row for row in rows if row["neighbourhood"] == near_neighbourhood]
        return narrowed if len(narrowed) >= MIN_CLUSTER_SIZE else rows

    if transit != "car" and rows:
        by_neighbourhood = {}
        for row in rows:
            by_neighbourhood.setdefault(row["neighbourhood"], []).append(row)
        top_neighbourhood = max(by_neighbourhood, key=lambda n: len(by_neighbourhood[n]))
        clustered = by_neighbourhood[top_neighbourhood]
        return clustered if len(clustered) >= MIN_CLUSTER_SIZE else rows

    return rows


def _ensure_dining_option(conn, rows, where, params, dining, limit):
    """If dining is "dine_out" and none of `rows` can host a meal, swap in
    one more query's best can_eat match so there's always a real lunch
    option -- unqualified, `rows` is returned unchanged."""
    if dining != "dine_out" or any(row["can_eat"] for row in rows):
        return rows
    lunch_row = conn.execute(
        f"SELECT * FROM venues WHERE {where} AND can_eat = 1 "
        "ORDER BY name LIMIT 1", params).fetchone()
    if not lunch_row:
        return rows
    return rows[:limit - 1] + [lunch_row]


def get_logged_venues_for_parent(parent_id):
    with closing(connection.connect()) as conn:
        return conn.execute(
            "SELECT * FROM venues WHERE parent_id = ? AND source = 'user_submitted' "
            "ORDER BY created_at DESC", (parent_id,)).fetchall()
