"""SQLite persistence for Travel with Tots.

A small, self-contained data layer: it owns the connection, the schema, and
first-run setup, and is kept separate from the page routes. Every connection
enables foreign-key enforcement; every write is parameterized and runs inside a
transaction.

Tables: parents -> children -> trips (each references its parent), plus a
standalone venues directory (seeded + user-submitted). Itinerary/stop tables
are intentionally deferred to a later stage.
"""

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

from . import postgres


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = _DATA_DIR / "app.db"

# What DB_PATH is when nobody has redirected it. Tests and one-off scripts point
# DB_PATH at their own file, and naming a specific SQLite file is a clear enough
# statement of intent to override the data-source dropdown: without this, running
# the suite with Supabase selected would send 300-odd writes to the live project.
_DEFAULT_DB_PATH = DB_PATH

# Why the last attempt to reach Supabase failed, or None. Read by /settings so a
# fallback to local is visible rather than silent.
LAST_BACKEND_ERROR = None



# What a unique-index violation looks like, whichever database is serving. The
# review page catches this per row so one clashing candidate cannot unwind a
# whole batch of decisions; without the Postgres class in here, that catch would
# quietly stop working the moment Supabase was selected.
INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + postgres.integrity_errors()

# Feature/flag columns on `venues` that the AI planner is allowed to filter
# candidates by -- never string-interpolate a column name that isn't in here.
# The only venue flag left as a column. It is not reportable: it follows the
# kind of place, is set at import and review, and the lunch rule reads it
# directly. The five amenities that used to sit beside it are REPORTABLE_FIELDS
# now, stored in venue_reports where a claim has an author and a date.
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
# (field name, label). A subset of REPORTABLE_FIELDS, and the names match
# add_venue's parameters. Here rather than in the workflow that first needed
# it, because the Log a Place page, the dashboard's edit form, that workflow
# and the chat agent's tool all offer the same list, and app.py should not have
# to import a vocabulary out of the demo layer to render a form.
AMENITY_OPTIONS = [
    ("has_family_room", "Family room"),
    ("has_nursing_room", "Nursing room"),
    ("stroller_accessible", "Stroller / step-free"),
]


# Venue sources trustworthy enough to plan a family's day around. Excludes
# 'user_submitted', which is whatever a parent typed in and nobody has checked.
#
# Trust has **two routes, not one**: provenance or inspection. The City is
# authoritative about its own parks, so a municipal row is trusted because of
# where it came from and is never put in front of a reviewer. Everything else
# earns it by being looked at, which is what `verified_at` records. So the gate
# this is heading for is
#
#     source = 'municipal_open_data' OR verified_at IS NOT NULL
#
# and *not* `verified_at IS NOT NULL` alone, which would demand a person
# confirm 238 parks the City already publishes. That is review as theatre, and
# a backlog nobody clears makes the queue useless for the rows that need it.
#
# "Trusted" is scoped to what the City actually publishes: name, location,
# existence. Not hours -- importers.PARK_HOURS is our judgment, because the
# City publishes none -- and not whether a place suits a toddler, which is why
# three municipal golf courses are plannable today.
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


def connect_sqlite():
    """Open the local SQLite file, whichever data source is selected.

    Asked for by name rather than dispatched, because everything that reads a
    PRAGMA or runs executescript is SQLite-only: schema creation, the column
    migrations, and the read side of the clone. Naming it means those can never
    be handed a Postgres connection.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _supabase_dsn():
    """The Postgres connection string to serve from, or None to stay local.

    Conditions, cheapest first: DB_BACKEND does not force local, DB_PATH names
    the real file, Supabase is chosen (by DB_BACKEND or by the dropdown), a
    connection string is set, and psycopg is installed. Anything missing means
    SQLite, which is the path that always works.

    **DB_BACKEND overrides the dropdown, in both directions**, and each
    direction earns its keep:

    * `local` is how the suite stays offline. Sixty-odd tests call a query
      function without redirecting DB_PATH, so they read whatever database is
      live: harmless when that could only be a local file, but with Supabase
      selected they become network reads against the real project. `tests/`
      sets it, so this holds however the suite is invoked.
    * `supabase` is how a deployment pins the backend. The dropdown is stored
      in `data/data_source.json`, and a host with an ephemeral disk loses that
      file on every deploy: the app would come back up quietly reading a fresh,
      empty SQLite database it had just seeded with demo rows.
    """
    from . import supabase_sync          # imports db, so it cannot be top-level
    backend = os.environ.get("DB_BACKEND", "").strip().lower()
    if backend == supabase_sync.LOCAL:
        return None
    if Path(DB_PATH) != _DEFAULT_DB_PATH:
        return None
    if supabase_sync.SUPABASE not in (backend, supabase_sync.active_source()):
        return None
    return supabase_sync.db_url() or None


def effective_backend():
    """Which database is actually serving: "supabase" or "local".

    Not the same question as `supabase_sync.active_source()`, which reads the
    dropdown's file. The two disagree whenever DB_BACKEND is set, and on a host
    with an ephemeral disk that is the normal case: the file is gone after every
    deploy, so the dropdown reads "local" while the environment pins Supabase.
    /settings asks this instead, or it reports the setting rather than the truth.
    """
    from . import supabase_sync
    return (supabase_sync.SUPABASE if _supabase_dsn() is not None
            else supabase_sync.LOCAL)


def backend_pinned_by_env():
    """The backend DB_BACKEND forces, or None when it is not set.

    What lets /settings say the dropdown has no effect, rather than showing a
    control that silently does nothing.
    """
    from . import supabase_sync
    backend = os.environ.get("DB_BACKEND", "").strip().lower()
    return backend if backend in supabase_sync.SOURCES else None


def connect():
    """A connection to whichever database the admin selected.

    Falls back to SQLite when Supabase cannot be reached, recording why in
    LAST_BACKEND_ERROR. A page that renders local data with a warning on
    /settings beats every page 500ing, and the alternative to recording it is a
    silent switch back, which is worse than either.
    """
    global LAST_BACKEND_ERROR
    dsn = _supabase_dsn()
    if dsn is None:
        return connect_sqlite()
    try:
        conn = postgres.connect(dsn)
    except (ImportError, *postgres.unreachable_errors()) as e:
        LAST_BACKEND_ERROR = postgres.first_line(e)
        return connect_sqlite()
    LAST_BACKEND_ERROR = None
    return conn




























# The password `schema._seed_admin` used to hard-code, published in this repository.
# Kept only so a database seeded before that changed can be checked for it,
# which is the one thing a published password is still good for.
RETIRED_PASSWORD = "admin1234"

# Passwords that must never open an admin account, checked before a deploy.
# Two kinds, and the second is the one that was missed: the seeded defaults,
# and the throwaway values tests use. A test that once ran against the real
# database left `search_web_test_admin2@example.com` with the password "pw",
# and the clone carried it to Supabase, where it sat as a second admin nobody
# knew about. Checking only the seeded default would not have found it.
WEAK_PASSWORDS = (RETIRED_PASSWORD, "demo1234", "pw", "x", "test", "admin",
                  "password", "secret", "hash", "hashed", "12345678")


def admins_with_password(password):
    """Admin accounts whose password is still `password`.

    Every hash is checked, which is slow by design and fine over a handful of
    admins.
    """
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT id, email, password_hash FROM parents WHERE is_admin = 1"
        ).fetchall()
    return [dict(row) for row in rows
            if check_password_hash(row["password_hash"], password)]


def admins_with_weak_password():
    """Every admin whose password is one anybody could guess or read here.

    The pre-deploy question, asked properly. `/settings` can change the data
    source and rewrite the chatbot's prompt, so one such account is enough to
    lose the deployment.
    """
    found = {}
    for password in WEAK_PASSWORDS:
        for row in admins_with_password(password):
            found.setdefault(row["email"], password)
    return found


def list_admins():
    """Every account that can reach /settings, so the answer to "who has admin"
    is one command rather than a query somebody writes by hand."""
    with closing(connect()) as conn:
        return [dict(row) for row in conn.execute(
            "SELECT id, email, name FROM parents WHERE is_admin = 1 "
            "ORDER BY email")]


def make_admin(email):
    """Give an existing account admin rights, leaving its password alone.

    The normal way to get an admin: sign up through the app like any parent,
    choosing your own password in the form that already validates it, then run
    this. Nothing here ever sees the password, which is the point -- a script
    that sets one is a script that has one.

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

    The fallback for when signing up first is not possible: a locked-out
    deployment, or an account seeded before the password stopped being a
    constant. Prefer signup plus `make_admin`, which never handles a password.

    Works against whichever backend is selected, because it goes through the
    same connection everything else does.
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

    Exists for the seeded demo and admin logins, whose passwords are published
    in this file's own history: on a database cloned before that changed, the
    fix is to delete them rather than to pick new passwords for accounts nobody
    uses. Children, trips and reports follow via ON DELETE CASCADE.
    """
    parent = get_parent_by_email(email.strip().lower())
    if parent is None:
        return None
    _write("DELETE FROM parents WHERE id = ?", (parent["id"],))
    return parent["id"]


def _write(sql, params):
    """Run one parameterized write in its own transaction; return lastrowid."""
    with closing(connect()) as conn, conn:
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
    """Remove a child. Their saved trips are kept (owned by the parent
    account, not the child); only child_id is cleared via ON DELETE SET NULL."""
    _write("DELETE FROM children WHERE id = ?", (child_id,))


def add_trip(parent_id, child_id, **fields):
    """Insert a trip. Only known columns (TRIP_FIELDS) are accepted, so the
    column names are never user-controlled and the values stay parameterized."""
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
                    "hours_note",
                    "address", "open_time", "close_time", "min_age_months",
                    "max_age_months", "lat", "lng", "parent_id", "source_url",
                    "external_id", "verified_at", "verified_by")


def add_venue(name, *, source, venue_type=None, **fields):
    """Insert a venue. Returns its id.

    `city`, `lat` and `lng` are optional so a submission still survives a
    geocoder that is unreachable or unconfigured, but supplying them is what
    makes the row usable: without coordinates it can never be distance-ranked,
    and without a city it never matches a city query.

    `source` alone decides whether the row is searchable, since only
    VERIFIED_SOURCES are queried. A "user_submitted" row therefore stays out of
    every result however complete it is, which is the human-in-the-loop gate
    rather than a gap.

    Takes `**fields` rather than one parameter per column because the callers
    now want very different subsets: a parent's submission sets a handful, while
    an approved candidate carries hours and a citation. `venue_type`
    stays an explicit keyword because `type` shadows a builtin and every caller
    already spells it that way.
    """
    unknown = set(fields) - set(ADD_VENUE_FIELDS) - set(CANDIDATE_FEATURE_COLUMNS)
    if unknown:
        raise ValueError(f"not a venue field: {', '.join(sorted(unknown))}")
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
# EDITABLE_VENUE_FIELDS below, and deliberately so: that list is the parent's
# own edit form, where "correct my typo" must not move a venue, while
# re-submitting the whole form may well mean the parent moved the map pin.
# Still excludes source and parent_id, which are never a caller's to rewrite.
SUBMISSION_FIELDS = ("type", "setting", "neighbourhood", "city", "lat", "lng",
                     "notes", "address")


def add_or_update_submission(name, *, parent_id, **fields):
    """Store this parent's submission of `name`, replacing their own earlier
    submission of the same place instead of adding a second row. Returns the
    row id either way.

    A parent logging the same place twice is correcting it, not reporting a
    second place: they thought the first attempt had not worked, or they are
    fixing what they said. Without this the admin review queue fills with
    near-identical rows for a human to sort out, which is the state the live
    database is already in (three copies of one venue from one parent, logged
    97 and 17 seconds apart, so not a double-click either).

    Matching is scoped to this parent's own user_submitted rows, for the same
    reason update_venue's is: it must never touch a curated venue or someone
    else's submission, and venues.parent_id is nullable so id alone would not
    be enough of a guard.
    """
    unknown = set(fields) - set(SUBMISSION_FIELDS)
    if unknown:
        raise ValueError(f"not a submission field: {', '.join(sorted(unknown))}")
    with closing(connect()) as conn, conn:
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

# What a reviewer may correct on a venue the app is already planning around.
# Wider than EDITABLE_VENUE_FIELDS, which is the parent's own form: an admin
# confirming a hand-typed row is checking exactly these against a source, and a
# wrong `type` or `city` is the kind of thing they are there to catch.
#
# Excludes hours, which go through set_venue_default_hours and set_venue_hours,
# and amenities, which are reports rather than columns. Excludes source and
# parent_id, which are never a caller's to rewrite.
REVIEWABLE_VENUE_FIELDS = ("name", "type", "setting", "neighbourhood", "city",
                           "can_eat")


def update_reviewed_venue(venue_id, **fields):
    """Correct a venue already in the searchable set, from the review queue.

    Scoped to VERIFIED_SOURCES in the SQL rather than trusting the caller, the
    same way update_venue is scoped to one parent: a pending submission belongs
    to the other queue, with its own clash and missing-city checks.

    Unknown field names raise rather than being ignored, so a typo in a form
    name fails loudly instead of silently dropping an edit.
    """
    unknown = set(fields) - set(REVIEWABLE_VENUE_FIELDS)
    if unknown:
        raise ValueError(f"not reviewable: {', '.join(sorted(unknown))}")
    if not fields:
        return
    source_clause, source_params = _verified_source_clause()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    _write(f"UPDATE venues SET {assignments} WHERE id = ? AND {source_clause}",
           [*fields.values(), venue_id, *source_params])


def update_venue(venue_id, parent_id, **fields):
    """Update one of this parent's own submissions.

    Ownership and the gate are both enforced in the SQL rather than left to the
    caller: venues.parent_id is nullable, so a query keyed on id alone would
    happily rewrite a curated seed row. The source filter means a submission
    that has since been verified is no longer the parent's to edit.

    Unknown or non-editable field names raise rather than being ignored, so a
    typo fails loudly instead of silently dropping an edit.
    """
    unknown = set(fields) - set(EDITABLE_VENUE_FIELDS)
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
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
    same name and city. The queue shows it because promoting into a clash is
    exactly what idx_venues_curated_identity refuses, and the admin needs to see
    that before clicking rather than after. The live database has one (a parent
    submitted Science World, which is already curated).
    """
    with closing(connect()) as conn:
        return conn.execute("""
            SELECT v.*, p.email AS submitted_by,
                   (SELECT COUNT(*) FROM venues c
                    WHERE c.source = 'curated' AND c.name = v.name
                      AND IFNULL(c.city, '') = IFNULL(v.city, '')) AS curated_clash
            FROM venues v LEFT JOIN parents p ON p.id = v.parent_id
            WHERE v.source = 'user_submitted' AND v.rejected_at IS NULL
            ORDER BY v.created_at DESC, v.id DESC""").fetchall()


def promote_submission(venue_id, admin_id):
    """Verify one submission into the curated set, so it starts appearing in
    plans and searches. Records who verified it and when.

    Refuses, rather than half-succeeding, when the row is not promotable:

    - no city, because get_venues_in_city matches on `city LIKE`, so a curated
      row without one is published and simultaneously invisible. That is worse
      than a clear refusal.
    - a curated venue of the same name and city already exists, which the unique
      index would reject anyway; this turns that into a message an admin can act
      on.
    """
    with closing(connect()) as conn, conn:
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
    """Set a submission aside. Recorded, not deleted.

    A reviewer can be wrong, and deleting the row would take the parent's own
    words and every venue_report about it (ON DELETE CASCADE) with it. So a
    rejection is a timestamp, and restore_submission undoes it.

    Scoped to user_submitted in the SQL, not by the caller, so an admin acting
    on a stale page cannot set aside a venue that has since been verified.
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
    with closing(connect()) as conn:
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
    """Write one open-data record into `venues`. Returns (venue_id, action),
    action being "inserted", "upgraded" or "unchanged".

    Matching is two steps, and the second is what stops an import duplicating
    the seed. The 11 parks in data/venues.json were typed in before any of this
    existed, so they carry no external_id and idx_venues_external_id cannot
    recognise them. Matching by external_id alone would insert a second
    Queen Elizabeth Park, and the curator's copy -- the one with seed_rank and
    the hours somebody chose -- would be the one the planner stopped reaching.

      1. by external_id: recognises this importer's own earlier runs, which is
         what makes it re-runnable.
      2. failing that, by name against a curated row: recognises a seeded venue
         and upgrades it in place.

    **An import fills blanks and never overwrites a value.** One rule for both
    match paths, and it is the conservative one: everything already on the row
    was either typed by the curator or corrected by an admin through
    set_venue_default_hours, and no import should be able to undo that. Only
    external_id and source_url are written unconditionally, because those are
    the provenance the row was missing and the reason to run this at all.

    The cost of that rule, stated plainly: if the City renames a park or moves
    its coordinate, a re-run will not pick the change up. Deleting the row is
    the way to take a correction. Worth it, because the alternative is an
    unattended script that can quietly overwrite a human's judgment.

    `source` and `seed_rank` are never touched on an upgrade either. `source`
    decides which queue a row sits in and which unique index guards it, so
    flipping a curated park to municipal_open_data as a side effect of an
    import would move rows between queues with nobody deciding anything.
    """
    unknown = set(fields) - set(IMPORT_FIELDS)
    if unknown:
        raise ValueError(f"not an import field: {', '.join(sorted(unknown))}")
    with closing(connect()) as conn, conn:
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

    These are in the table and invisible to the planner, by design: a venue
    whose hours we do not know cannot be scheduled (see
    data_loader._hours_for_slot and _candidate_where_clause). That is the right
    answer and a dead end at the same time, so the review page lists them and
    somebody fills the hours in.

    Community centres arrive this way every time. The City publishes their
    address, their coordinates and a link to their page, and does not publish
    when they open, so nothing but a person reading that page can finish the
    row.
    """
    source_clause, source_params = _verified_source_clause()
    with closing(connect()) as conn:
        return conn.execute(
            f"SELECT * FROM venues WHERE {source_clause} "
            "AND (open_time IS NULL OR open_time = '' "
            "     OR close_time IS NULL OR close_time = '') "
            "ORDER BY name", source_params).fetchall()


def get_unverified_venues(limit=None):
    """Venues in the searchable set that no human has confirmed: verified_at
    IS NULL.

    Scoped to 'curated' on purpose, and that is the whole verification model in
    one line: **only hand-curated rows owe a person a look.** They are trusted
    today purely because of how they were typed in, which is the one tier where
    provenance proves nothing. Stamping verified_at as they are confirmed is
    what lets the planner's gate become "municipal, or checked" (see
    VERIFIED_SOURCES) rather than a redesign.

    Municipal rows also carry verified_at IS NULL, and are deliberately left
    that way: the City is authoritative about its own parks, so there are 238 of
    them and confirming "the City lists this park" is review as theatre.

    Excludes user_submitted rows: those are a different queue with different
    guards (see get_pending_submissions), and showing them twice would invite
    confirming one without the clash and missing-city checks.
    """
    sql = ("SELECT * FROM venues WHERE source = 'curated' "
           "AND verified_at IS NULL "
           "ORDER BY seed_rank IS NULL, seed_rank, name")
    params = []
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    with closing(connect()) as conn:
        return conn.execute(sql, params).fetchall()


def mark_verified(venue_id, admin_id, source_url=None):
    """Record that a human confirmed a venue already in the searchable set.

    Separate from promote_submission, which also changes `source`: this one only
    ever stamps, so confirming a seeded venue cannot accidentally publish
    anything. Scoped to VERIFIED_SOURCES so it cannot quietly bless a pending
    submission that belongs in the other queue.

    `source_url` is what the confirmation was checked against, and it is the
    difference between a stamp that means something and one that does not: 21 of
    the 28 hand-typed venues carry no citation at all, so without this a venue
    confirmed today against its own website is indistinguishable next year from
    one nobody ever looked at. Blank leaves whatever is already there.
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
    whole set, like reported_flags, because the planner resolves every venue's
    hours for one date and must not do that venue by venue.
    """
    if venue_ids is not None and not venue_ids:
        return {}
    sql = "SELECT venue_id, weekday, open_time, close_time FROM venue_hours"
    params = []
    if venue_ids is not None:
        sql += f" WHERE venue_id IN ({', '.join('?' * len(venue_ids))})"
        params = list(venue_ids)
    out = {}
    with closing(connect()) as conn:
        for row in conn.execute(sql, params):
            out.setdefault(row["venue_id"], {})[row["weekday"]] = (
                row["open_time"], row["close_time"])
    return out


def set_venue_hours(venue_id, by_weekday):
    """Replace a venue's per-day hours with `by_weekday`, or clear them.

    `by_weekday` is {weekday: (open, close)}. Written as a replacement rather
    than a merge, because the rows are a complete description: leaving an old
    row behind would say a venue is open on a day the new answer omits, which
    is exactly the mistake this table exists to make impossible.

    An empty mapping deletes every row, which hands the venue back to its single
    pair. That is the way out of a per-day answer somebody entered by mistake.
    """
    rows = [(venue_id, day, opens, closes)
            for day, (opens, closes) in sorted(by_weekday.items())
            if opens and closes]
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM venue_hours WHERE venue_id = ?", (venue_id,))
        if rows:
            conn.executemany(
                "INSERT INTO venue_hours (venue_id, weekday, open_time, "
                "close_time) VALUES (?, ?, ?, ?)", rows)
    return len(rows)


def set_venue_default_hours(venue_id, open_time, close_time, hours_note=None):
    """Correct a venue's usual hours, and what no timetable can hold.

    The only path by which an approved venue's hours can change. Until this
    existed they were frozen at whatever was typed the day it was approved:
    EDITABLE_VENUE_FIELDS deliberately excludes hours, so not even the parent
    who submitted a place could fix them, and nothing else wrote them.

    `hours_note` is the seasonal band, the Christmas closure, the second range
    in a day: what defeats any weekday model and belongs in words a parent
    reads. It travels with the hours because it is decided in the same breath.
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
    with closing(connect()) as conn:
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

    So the in-trip panel can show a parent what they said while it waits. Their
    own, deliberately: a pending claim is not information about the venue yet,
    it is a record of what one person reported, and showing somebody else's
    would be the unreviewed data leaking out by another door.
    """
    ids = [i for i in (venue_ids or [])]
    if not parent_id or not ids:
        return {}
    placeholders = ", ".join("?" for _ in ids)
    pending = {}
    with closing(connect()) as conn:
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

    Grouped by venue and parent in the caller: a parent ticks several boxes at
    once and each is a row, so a reviewer should be able to settle the batch
    they arrived as rather than click once per tick.
    """
    with closing(connect()) as conn:
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

    Rejected rather than deleted: "somebody said this and a reviewer disagreed"
    is worth keeping, and a deleted row would let the same claim arrive again
    looking new.
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

    `value` 0 is a real report: "I looked and there was no change table" is
    information, and the point of this table is that it differs from silence.

    `reported_by` is None only for a seed claim (see schema._migrate_seed_claims): a
    report with no author is visibly weaker than one with, and that is how the
    hand-typed flags are represented now that nobody stands behind them.

    `approved` defaults to True, which keeps every writer behaving as it did.
    Only the in-trip report route passes False: that is the one place a claim
    arrives from somebody the app has no other reason to trust, and nothing
    there reaches another parent until a reviewer agrees. A caller added later
    is trusted unless it says otherwise, so say otherwise if it is a parent.
    """
    if field not in REPORTABLE_FIELDS:
        raise ValueError(f"not a reportable field: {field}")
    if not approved:
        # One pending report per parent, per venue, per field. record_amenities
        # skips unchanged answers by comparing against the approved flags, which
        # cannot see a pending row, so a parent reporting twice would otherwise
        # stack duplicates. Same rule as record_hours_check, which replaces any
        # open check from the same source.
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

    The one way an amenity claim enters the database. `values` is
    {field: truthy} over REPORTABLE_FIELDS; anything else is ignored, so a
    caller can hand over a whole form dict. Returns how many were written.

    Only changed answers are written, following report_amenities: re-saving an
    unchanged form must not manufacture reports, because recency is what
    decides a conflict and a fresh duplicate would move a claim's date without
    anybody having looked.

    Why every writer goes through here rather than setting a column. A claim
    needs an author and a date. Review, Log a Place and the replay script all
    used to write the venues columns, which are the *weakest* layer: a
    reviewer's deliberate check was overridden by the next parent report with
    no record that anyone had checked, and a parent's own ticks about a place
    they had just visited were stored as a claim by nobody. One real row said
    `reported_by=None` with the note "Hand-typed into the seed file; never
    verified" about an amenity a logged-in parent had ticked themselves.
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

    A report by a real person outranks a seed claim of any age, and between
    real reports the newest wins. Recency rather than a vote count because
    amenities genuinely change: a change table is removed, a cafe stops keeping
    a highchair, a park washroom closes for the winter. A parent who was there
    last week knows better than three who went last year, and with a small user
    base a vote threshold would leave every field unknown forever.

    A field with no report is absent from the dict, which is what lets "nobody
    has said" differ from "somebody looked and there was none".
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
    with closing(connect()) as conn:
        for row in conn.execute(sql, params):
            flags.setdefault(row["venue_id"], {})[row["field"]] = bool(row["value"])
    return flags


def get_parent_by_email(email):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM parents WHERE email = ?", (email,)).fetchone()


def get_parent(parent_id):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM parents WHERE id = ?", (parent_id,)).fetchone()


def get_children(parent_id):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM children WHERE parent_id = ? ORDER BY created_at",
            (parent_id,)).fetchall()


def get_trips_for_parent(parent_id):
    """Trips with a saved itinerary owned by this parent, newest first (older
    rows saved without a plan_json have nothing to open, so they're excluded
    rather than shown as a dead link). LEFT JOIN so a trip whose child was
    since removed still shows, with child_name as NULL."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name FROM trips "
            "LEFT JOIN children ON children.id = trips.child_id "
            "WHERE trips.parent_id = ? AND trips.plan_json IS NOT NULL "
            "ORDER BY trips.created_at DESC", (parent_id,)).fetchall()


def get_trip_group(parent_id, group_id):
    """Every day of one saved trip, in order, scoped to this parent.

    Ordered by day_index rather than by date: an index cannot be ambiguous, and
    a row whose date failed to save should still hold its place rather than
    sorting to the front.
    """
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name, "
            "children.date_of_birth AS child_dob FROM trips "
            "LEFT JOIN children ON children.id = trips.child_id "
            "WHERE trips.parent_id = ? AND trips.trip_group_id = ? "
            "ORDER BY trips.day_index", (parent_id, group_id)).fetchall()


def get_trip_for_parent(parent_id, trip_id):
    """One trip by id, scoped to this parent's own trips (ownership check)."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name, "
            "children.date_of_birth AS child_dob FROM trips "
            "LEFT JOIN children ON children.id = trips.child_id "
            "WHERE trips.parent_id = ? AND trips.id = ?",
            (parent_id, trip_id)).fetchone()


def get_candidate_venues(city, age_months=None, features=None, transit=None,
                          dining=None, near_neighbourhood=None, limit=CANDIDATE_LIMIT):
    """Verified venues in `city` (substring match). Used to ground the AI
    planning agent -- it must never reference a venue outside this list.

    `age_months` and `features` are accepted and ignored, so the callers in
    agents.py need no change. Neither ever narrowed anything: every row's age
    range was 0-60, and amenity filtering moved to find_nearby (see
    _candidate_where_clause).

    Venues carry lat/lng where a source supplied it, but only some do and
    there is still no routing API, so this planner-facing query deliberately
    keeps using neighbourhood as a coarse proxy rather than a real
    radius/travel-time filter. (components/find_nearby.py does use real
    distance, since a partial answer is fine when ranking a short list.)
    If `near_neighbourhood` is given (e.g. replanning from a known
    current stop), candidates are narrowed to that specific neighbourhood, as
    long as it has enough venues to still offer a real choice. Otherwise
    `transit` decides: without a car, candidates are narrowed to the single
    most common neighbourhood among the matches (keeping stops close
    together), again only if it has enough venues; with a car, all matching
    neighbourhoods stay in play. If `dining` is "dine_out", at least one
    venue where a meal is possible is guaranteed a slot, so there's always a
    real lunch option."""
    where, params = _candidate_where_clause(city)

    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM venues WHERE {where} ORDER BY name", params).fetchall()
        rows = _narrow_by_neighbourhood(rows, near_neighbourhood, transit)
        rows = rows[:limit]
        rows = _ensure_dining_option(conn, rows, where, params, dining, limit)
    # Plain dicts with the reports overlaid, for the same reason
    # data_loader.get_venues does it: an amenity is whatever somebody last
    # observed, not what the column claims. These rows are both described to
    # the AI planner and swapped into plans as venues, so telling it a nursing
    # room is there when a parent has since said otherwise put the stale answer
    # in front of a family twice over.
    reported = reported_flags([row["id"] for row in rows])
    return [{**dict(row), **reported.get(row["id"], {})} for row in rows]


def _verified_source_clause():
    """SQL fragment and params restricting a query to VERIFIED_SOURCES.
    Parameterized rather than interpolated, so adding a source can never
    become a SQL-injection seam."""
    placeholders = ", ".join("?" for _ in VERIFIED_SOURCES)
    return f"source IN ({placeholders})", list(VERIFIED_SOURCES)


def _candidate_where_clause(city):
    """WHERE clause and params for a verified-venue lookup: city substring match.

    No amenity filtering. What a parent needs in the moment is answered by
    find_nearby where they are, and narrowing a whole day to venues someone
    happened to have reported on would return almost nothing once the table is
    honest about what it does not know.

    No age range either: min_age_months/max_age_months were 0-60 on every row
    ever written, so the clause never excluded anything. Age paces the day
    (itinerary.realistic_stop_count), it does not filter venues.

    Hours, though. A venue with no open/close pair is not schedulable at all
    (data_loader._hours_for_slot returns unknown, and the validator refuses
    anything unknown), so offering it as a candidate spends one of
    CANDIDATE_LIMIT slots on a stop that can only ever be replaced. That
    matters now that an import can add rows faster than anyone fills hours in:
    27 hourless community centres would crowd out most of an 18-venue budget.
    They surface in get_venues_missing_hours instead, where they can be fixed.
    """
    source_clause, source_params = _verified_source_clause()
    return (f"{source_clause} AND city LIKE ? AND open_time IS NOT NULL "
            "AND open_time != '' AND close_time IS NOT NULL "
            "AND close_time != ''"), source_params + [f"%{city}%"]


def get_venue_types_in_use():
    """The venue types at least one searchable venue actually has.

    Read from the table rather than hardcoded, so the plan form never offers a
    kind of place there is nothing behind, and a type starts being offered the
    moment a venue uses it. Same idea as data_loader.SUPPORTED_CITIES: offer
    what the data can support.
    """
    source_clause, source_params = _verified_source_clause()
    with closing(connect()) as conn:
        return {row["type"] for row in conn.execute(
            f"SELECT DISTINCT type FROM venues WHERE {source_clause} "
            "AND type IS NOT NULL AND type != ''", source_params)}


def get_venues_in_city(city):
    """Every verified venue in `city` (substring match, same as
    get_candidate_venues). Deliberately unfiltered beyond the city: callers
    decide what "matching" means -- see components/find_nearby.py, which
    applies interactions.NEED_FILTERS so need semantics live in one place."""
    source_clause, source_params = _verified_source_clause()
    with closing(connect()) as conn:
        return conn.execute(
            f"SELECT * FROM venues WHERE {source_clause} AND city LIKE ? "
            "ORDER BY name", source_params + [f"%{city}%"]).fetchall()


def _narrow_by_neighbourhood(rows, near_neighbourhood, transit):
    """Narrow to a single neighbourhood's rows when that still leaves enough
    for a real choice (MIN_CLUSTER_SIZE) -- to the specific neighbourhood
    given (replanning from a known current stop), or otherwise to whichever
    matched neighbourhood has the most venues, but only without a car (car
    access removes the need to keep stops close together). Falls back to
    every matched row whenever narrowing wouldn't leave enough."""
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
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM venues WHERE parent_id = ? AND source = 'user_submitted' "
            "ORDER BY created_at DESC", (parent_id,)).fetchall()
