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
import sqlite3
from contextlib import closing
from pathlib import Path

from werkzeug.security import generate_password_hash


_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DB_PATH = _DATA_DIR / "app.db"
VENUES_SEED = _DATA_DIR / "venues.json"

# Age is never stored -- children keep a date of birth and age is derived.
# venues.source is constrained here because SQLite has no native ENUM type.
SCHEMA = """
CREATE TABLE IF NOT EXISTS parents (
    id            INTEGER PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    name          TEXT,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS children (
    id            INTEGER PRIMARY KEY,
    parent_id     INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    date_of_birth TEXT NOT NULL,          -- ISO 'YYYY-MM-DD'; age is computed from this
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS trips (
    id            INTEGER PRIMARY KEY,
    parent_id     INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE,
    child_id      INTEGER REFERENCES children(id) ON DELETE SET NULL,
    trip_date     TEXT,                   -- day of the outing (ISO)
    wake_up       TEXT,
    bedtime       TEXT,
    naps          TEXT,                   -- JSON array of {"start", "duration_min"}
    transit_nap   TEXT,                   -- "yes"/"sometimes"/"no": can the child nap in transit
    destination   TEXT,
    accommodation TEXT,                   -- where they are staying, in their words
    -- Where that is, when they picked it on the map. Nullable and separate from
    -- the text on purpose: the text is what a parent typed and what the AI
    -- prompt reads, these are what the planner can measure from, and a typed
    -- address that was never pinned has the first without the second.
    accommodation_lat REAL,
    accommodation_lng REAL,
    transit       TEXT,                   -- JSON array of transit modes
    stop_count    TEXT,                   -- how many places the parent asked to visit
    dining        TEXT,
    preferred_lunch_time TEXT,             -- "HH:MM": when the parent wants lunch scheduled
    nap_notes     TEXT,
    extra_notes   TEXT,
    plan_label    TEXT,                   -- label of the generated plan the parent picked
    plan_json     TEXT,                   -- full Plan.to_dict() (label, blurb, stops), so the
                                           -- saved itinerary can be reopened from the dashboard
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS venues (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL,
    type                TEXT,                -- what the place is. Descriptive only
    setting             TEXT,                -- 'indoor'/'outdoor'/'both': where a
                                             -- visit is spent. See data_loader.SETTINGS
    neighbourhood       TEXT,
    source              TEXT NOT NULL CHECK (
                            source IN ('municipal_open_data', 'user_submitted', 'curated')),
    parent_id           INTEGER REFERENCES parents(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    city                TEXT,
    can_eat             INTEGER NOT NULL DEFAULT 0,
    open_time           TEXT,
    close_time          TEXT,
    lat                 REAL,                   -- NULL until a source supplies it
    lng                 REAL,
    notes               TEXT,                   -- what a parent said about it
    address             TEXT,                   -- what the geocoder resolved
    source_url          TEXT,                   -- citation: the record or page a curator used
    external_id         TEXT,                   -- the source's own id, namespaced:
                                                -- "osm:node/123", "vanopendata:parks/17"
    verified_at         TEXT,                   -- when a human last confirmed this row
    verified_by         INTEGER REFERENCES parents(id) ON DELETE SET NULL,
    rejected_at         TEXT,                   -- set instead of deleting: see reject_submission
    rejected_by         INTEGER REFERENCES parents(id) ON DELETE SET NULL,
    seed_rank           INTEGER                 -- position in venues.json: see _seed_venues
);

-- A comparison between our stored hours and an outside source, and what a
-- person decided about it. The point is that hours change: they are entered
-- once at review and nothing else ever writes them, so without this a venue's
-- hours are frozen at whatever was typed the day it was approved.
CREATE TABLE IF NOT EXISTS venue_hours_checks (
    id          INTEGER PRIMARY KEY,
    venue_id    INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,      -- where the comparison came from, e.g. "osm"
    source_says TEXT NOT NULL,      -- the source's own words, shown to a person
    our_open    TEXT,               -- what we held when the check ran
    our_close   TEXT,
    finding     TEXT NOT NULL,      -- differs | more_detail | unverifiable
    checked_at  TEXT NOT NULL DEFAULT (datetime('now')),
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending | resolved
    decided_at  TEXT,
    decided_by  INTEGER REFERENCES parents(id) ON DELETE SET NULL
);


-- What somebody actually observed at a venue, and when. The venues table has
-- columns for these too, but they are no longer what anything reads: a claim
-- needs an author and a date, and "nobody has said" has to differ from
-- "somebody looked and there was none". See reported_flags.
CREATE TABLE IF NOT EXISTS venue_reports (
    id          INTEGER PRIMARY KEY,
    venue_id    INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    field       TEXT NOT NULL,      -- one of REPORTABLE_FIELDS
    value       INTEGER NOT NULL,   -- 1 present, 0 absent. Absence is a real report
    reported_by INTEGER REFERENCES parents(id) ON DELETE SET NULL,
    reported_at TEXT NOT NULL DEFAULT (datetime('now')),
    note        TEXT
);

"""

# Indexes, kept apart from SCHEMA because they must be created after
# _ensure_columns: on a database that predates a column, an index naming it
# cannot be created until the ALTER TABLE has run. See create_schema.
INDEXES = """-- Two curated copies of one place is the duplicate _seed_venues matches on.
-- Scoped to 'curated' because a curated venue and an imported one are allowed
-- to coexist, and because user submissions may legitimately repeat a name.
CREATE UNIQUE INDEX IF NOT EXISTS idx_venues_curated_identity
    ON venues(name, city) WHERE source = 'curated';

-- One submission per place per parent. add_or_update_submission already
-- replaces a parent's earlier submission rather than adding a second row; this
-- is what closes the gap it cannot, two simultaneous submits both finding
-- nothing and both inserting.
CREATE UNIQUE INDEX IF NOT EXISTS idx_venues_submission_identity
    ON venues(parent_id, name) WHERE source = 'user_submitted';

-- What lets a re-run import update its rows instead of duplicating them.
CREATE UNIQUE INDEX IF NOT EXISTS idx_venues_external_id
    ON venues(external_id) WHERE external_id IS NOT NULL;

-- Serves the source + city lookups in get_venues_in_city, get_candidate_venues
-- and get_logged_venues_for_parent. No index on city alone: every city match is
-- a LIKE with a leading wildcard, which can never use one.
CREATE INDEX IF NOT EXISTS idx_venues_source_city ON venues(source, city);

CREATE INDEX IF NOT EXISTS idx_venue_reports_venue ON venue_reports(venue_id, field);

-- One open check per venue per source: a re-run updates the finding rather
-- than stacking another row to dismiss.
CREATE UNIQUE INDEX IF NOT EXISTS idx_venue_hours_check_open
    ON venue_hours_checks(venue_id, source) WHERE status = 'pending';
"""

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

# The venue columns data/venues.json owns, in the order _seed_venues supplies
# them. Deliberately excludes source, parent_id and the provenance columns: a
# re-seed must never demote a row or discard a citation a human added.
SEED_FIELDS = ("type", "setting", "neighbourhood", "can_eat",
               "open_time", "close_time", "seed_rank")

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
)


def connect():
    """Open a connection with row access by name and foreign keys enforced."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create_schema(conn):
    """Bring `conn` up to the current schema: tables, then columns added after
    a table was first created, then indexes.

    The order is the point. An index that names a column can only be created
    once that column exists, and on a database predating the column that is
    _ensure_columns' job. Callers use this rather than executescript(SCHEMA)
    so they cannot get the order wrong.
    """
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.executescript(INDEXES)


def init_db():
    """Create the tables if they don't exist and seed initial data once."""
    with closing(connect()) as conn:
        create_schema(conn)
        _drop_dead_columns(conn)
        _migrate_trips_ownership(conn)
        _seed_venues(conn)
        _migrate_seed_claims(conn)
        _seed_sample_data(conn)
        _seed_admin(conn)


def _ensure_columns(conn):
    """Add columns introduced after a table was first created -- SQLite has no
    'ADD COLUMN IF NOT EXISTS', so existing databases need a manual patch."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trips)")}
    if "plan_json" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN plan_json TEXT")
    if "feeding_1" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN feeding_1 TEXT")
            conn.execute("ALTER TABLE trips ADD COLUMN feeding_2 TEXT")
    if "transit_nap" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN transit_nap TEXT")
    if "preferred_lunch_time" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN preferred_lunch_time TEXT")
    if "naps" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN naps TEXT")
    if "accommodation_lat" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN accommodation_lat REAL")
            conn.execute("ALTER TABLE trips ADD COLUMN accommodation_lng REAL")
    if "pace" in existing and "stop_count" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips RENAME COLUMN pace TO stop_count")

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(parents)")}
    if "is_admin" not in existing:
        with conn:
            conn.execute("ALTER TABLE parents ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(venues)")}
    if "city" not in existing:
        with conn:
            conn.execute("ALTER TABLE venues ADD COLUMN city TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN can_eat INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE venues ADD COLUMN open_time TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN close_time TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN min_age_months INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE venues ADD COLUMN max_age_months INTEGER NOT NULL DEFAULT 60")
    if "lat" not in existing:
        with conn:
            conn.execute("ALTER TABLE venues ADD COLUMN lat REAL")
            conn.execute("ALTER TABLE venues ADD COLUMN lng REAL")
    if "notes" not in existing:
        with conn:
            # What a parent says about a place in their own words, and the
            # address the geocoder resolved. Both are for the admin who has to
            # decide whether the submission is real: the address was previously
            # computed and then dropped for want of anywhere to put it.
            conn.execute("ALTER TABLE venues ADD COLUMN notes TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN address TEXT")
    if "source_url" not in existing:
        with conn:
            # Provenance, so a venue can cite where it came from and who checked
            # it. Nothing writes these yet: the admin review page and the
            # importers that will are still to come. Every one is nullable
            # because the seeded rows have no answer for any of them, and
            # verified_by additionally has to be, since SQLite only allows
            # ADD COLUMN with a REFERENCES clause when the default is NULL.
            conn.execute("ALTER TABLE venues ADD COLUMN source_url TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN external_id TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN verified_at TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN verified_by INTEGER "
                         "REFERENCES parents(id) ON DELETE SET NULL")
            conn.execute("ALTER TABLE venues ADD COLUMN seed_rank INTEGER")
    if "hours_note" not in existing:
        with conn:
            # What a single open/close pair cannot hold, in words a parent
            # reads: "Closed Mondays September to May". This replaced a
            # venue_hours table keyed on (season, day_type) that never held a
            # row and could not express a closed weekday anyway. The candidate
            # store has carried the same field for a while, filled with the raw
            # OpenStreetMap string and the entry it matched; approval used to
            # throw it away for want of anywhere to put it.
            conn.execute("ALTER TABLE venues ADD COLUMN hours_note TEXT")
    if "setting" not in existing:
        with conn:
            # Where a visit is spent. The one fact `type` provably cannot
            # carry: `attraction` is a legitimate residual and its eight
            # venues split four indoor, four outdoor. Nullable, because a
            # venue nobody has assessed must read as unknown rather than as
            # either answer.
            conn.execute("ALTER TABLE venues ADD COLUMN setting TEXT")
    if "has_washroom" not in existing:
        with conn:
            # For a potty-training toddler a washroom decides whether a park
            # works at all, and a highchair decides whether eating at a stop
            # does. Both are reported, never guessed.
            conn.execute("ALTER TABLE venues ADD COLUMN has_washroom "
                         "INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE venues ADD COLUMN has_highchair "
                         "INTEGER NOT NULL DEFAULT 0")
    if "rejected_at" not in existing:
        with conn:
            # Rejecting a submission used to delete it. A reviewer can be wrong,
            # and a deleted row takes its parent's own words and every report
            # about it with it, so a rejection is recorded instead.
            conn.execute("ALTER TABLE venues ADD COLUMN rejected_at TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN rejected_by INTEGER "
                         "REFERENCES parents(id) ON DELETE SET NULL")


def _drop_dead_columns(conn):
    """Remove columns that ask a question the data cannot answer.

    - `venues.category` was 'food' or 'activity'. The table holds attractions
      only, so it is a tautology; `can_eat` marks the ones with food, which is
      all the planner ever read it for.
    - `venues.kid_friendly` was true on 37 of 38 rows. It is the criterion for
      being in this table at all, not an attribute of a venue, so it is enforced
      where venues enter instead.
    - `venues.nap_friendly` is derived from `type` now
      (data_loader.is_nap_friendly), because all but one of the rows that had it
      were a park or a mall.
    - `venues.min_age_months`/`max_age_months` were 0 and 60 on every row ever
      written, so the age clause never excluded anything. Age paces the day.
    - `children.gender` was collected, stored, and read back only by the form
      that collected it. Personal data about a child that changes no output.
    - `trips.nap_1`/`nap_2`/`feeding_1`/`feeding_2` were kept "for old saved
      trips"; no trip ever carried a value in one. `trips.features` has nothing
      left to filter.

    Guarded per column and idempotent, like the additions above. Needs SQLite
    3.35+ for DROP COLUMN.
    """
    for table, column in (
            ("venues", "category"),
            ("venues", "kid_friendly"),
            ("venues", "nap_friendly"),
            ("venues", "min_age_months"),
            ("venues", "max_age_months"),
            # Amenities live in venue_reports, which is the only place a claim
            # can carry an author and a date. These columns were the base layer
            # underneath the reports, and being INTEGER NOT NULL DEFAULT 0 they
            # could not express "nobody has said" -- so every venue asserted the
            # absence of every amenity nobody had looked at. Every value they
            # held was already duplicated as a report before this ran.
            ("venues", "has_washroom"),
            ("venues", "has_family_room"),
            ("venues", "has_nursing_room"),
            ("venues", "stroller_accessible"),
            ("venues", "has_highchair"),
            ("children", "gender"),
            ("trips", "nap_1"), ("trips", "nap_2"),
            ("trips", "feeding_1"), ("trips", "feeding_2"),
            ("trips", "features")):
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column in existing:
            with conn:
                conn.execute(f"ALTER TABLE {table} DROP COLUMN {column}")


def _migrate_trips_ownership(conn):
    """Older databases have trips.child_id as NOT NULL with ON DELETE CASCADE,
    so a saved plan is really owned by the child, not the account -- deleting
    a child silently destroys their trips too. SQLite can't ALTER a column's
    constraints in place, so this rebuilds the table with parent_id as the
    real owner and child_id as an optional, SET-NULL reference, backfilling
    parent_id from each trip's current child. Idempotent: skipped once the
    table already has parent_id."""
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(trips)")}
    if "parent_id" in existing:
        return
    with conn:
        conn.execute("ALTER TABLE trips RENAME TO trips_old")
        conn.execute("""
            CREATE TABLE trips (
                id            INTEGER PRIMARY KEY,
                parent_id     INTEGER NOT NULL REFERENCES parents(id) ON DELETE CASCADE,
                child_id      INTEGER REFERENCES children(id) ON DELETE SET NULL,
                trip_date     TEXT,
                wake_up       TEXT,
                bedtime       TEXT,
                nap_1         TEXT,
                nap_2         TEXT,
                naps          TEXT,
                transit_nap   TEXT,
                feeding_1     TEXT,
                feeding_2     TEXT,
                destination   TEXT,
                accommodation TEXT,
                transit       TEXT,
                stop_count    TEXT,
                dining        TEXT,
                preferred_lunch_time TEXT,
                nap_notes     TEXT,
                extra_notes   TEXT,
                plan_label    TEXT,
                plan_json     TEXT,
                created_at    TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            INSERT INTO trips (id, parent_id, child_id, trip_date, wake_up,
                bedtime, nap_1, nap_2, naps, transit_nap, feeding_1, feeding_2,
                destination, accommodation, transit, stop_count, dining,
                preferred_lunch_time, nap_notes, extra_notes,
                plan_label, plan_json, created_at)
            SELECT t.id, c.parent_id, t.child_id, t.trip_date, t.wake_up,
                t.bedtime, t.nap_1, t.nap_2, t.naps, t.transit_nap, t.feeding_1,
                t.feeding_2, t.destination, t.accommodation, t.transit,
                t.stop_count, t.dining, t.preferred_lunch_time,
                t.nap_notes, t.extra_notes, t.plan_label, t.plan_json,
                t.created_at
            FROM trips_old t JOIN children c ON c.id = t.child_id
        """)
        conn.execute("DROP TABLE trips_old")


def _seed_venues(conn):
    """Copy data/venues.json into the venues table as 'curated' rows.

    Runs on every startup: new entries are inserted, entries already there
    (matched by name) are updated. So a hand edit to the seed file reaches an
    existing database instead of only ever landing on a fresh one, which is
    what makes venues.json the seed of record for curated venues.

    A null coordinate in the seed never overwrites one already in the table, so
    a geocoding pass (scripts/geocode_venues.py) is not undone by the next boot.

    **Hours are filled, never overwritten,** for the same reason and a sharper
    one. They have a second writer now: set_venue_default_hours, driven by
    scripts/verify_hours.py comparing us against OpenStreetMap and a person
    deciding. This function used to write them unconditionally on every startup,
    so it silently reverted those decisions -- and it really happened. The
    Vancouver Aquarium was corrected from 09:30 to 10:00 through the review
    page, after OSM showed the app was sending families half an hour before it
    opens, and the next boot put 09:30 back. Nobody was told.

    Between a static file and a decision somebody made against outside
    evidence, the decision wins. A curator who wants to change hours has the
    review page, which is the path that exists and carries a citation.

    Matching is scoped to curated rows. Comparing against every row instead let
    a parent's submission of an existing name block the seed entry entirely.
    """
    venues = json.loads(VENUES_SEED.read_text(encoding="utf-8"))
    # Hours join lat/lng in the fill-only group, so they are dropped from the
    # unconditional assignments and handled with COALESCE below.
    overwritten = tuple(f for f in SEED_FIELDS
                        if f not in ("open_time", "close_time"))
    assignments = ", ".join(f"{field} = ?" for field in overwritten)
    columns = ", ".join(("name", "source", "city") + SEED_FIELDS + ("lat", "lng"))
    placeholders = ", ".join("?" for _ in range(len(SEED_FIELDS) + 5))
    with conn:  # single transaction for the whole batch
        for rank, v in enumerate(venues):
            values = (v["type"], v["setting"], v["neighbourhood"],
                      int(v["can_eat"]), v["open"], v["close"], rank)
            coords = (v.get("lat"), v.get("lng"))
            existing = conn.execute(
                "SELECT id FROM venues WHERE name = ? AND source = 'curated'",
                (v["name"],)).fetchone()
            if existing:
                keep = tuple(values[i] for i, f in enumerate(SEED_FIELDS)
                             if f not in ("open_time", "close_time"))
                conn.execute(
                    f"UPDATE venues SET {assignments}, "
                    "open_time = COALESCE(open_time, ?), "
                    "close_time = COALESCE(close_time, ?), "
                    "lat = COALESCE(?, lat), lng = COALESCE(?, lng) WHERE id = ?",
                    keep + (v["open"], v["close"]) + coords + (existing["id"],))
            else:
                conn.execute(
                    f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
                    (v["name"], "curated", "Vancouver") + values + coords)


def _migrate_seed_claims(conn):
    """Move the hand-typed amenity flags into venue_reports as claims by nobody.

    Those values were typed in for a demo and never verified, yet the app has
    been asserting them: 11 venues claimed a nursing room and 14 a family room,
    on nobody's authority and with no way for a parent to correct them.

    Recording them as reports with `reported_by = NULL` keeps today's plans
    working while making the claim's weight visible, and a single real report
    now supersedes one. Idempotent: skipped once any report exists.
    """
    if conn.execute("SELECT COUNT(*) FROM venue_reports").fetchone()[0]:
        return
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(venues)")}
    fields = [f for f in REPORTABLE_FIELDS if f in columns]
    if not fields:
        return
    rows = conn.execute(
        f"SELECT id, {', '.join(fields)} FROM venues").fetchall()
    with conn:
        for row in rows:
            for field in fields:
                if row[field]:
                    conn.execute(
                        "INSERT INTO venue_reports (venue_id, field, value, "
                        "reported_by, note) VALUES (?, ?, 1, NULL, ?)",
                        (row["id"], field,
                         "Hand-typed into the seed file; never verified."))


def _seed_sample_data(conn):
    """Insert one demo parent -> child -> trip when there are no parents yet, so
    the tables have something to browse. Idempotent: skipped once data exists."""
    if conn.execute("SELECT COUNT(*) FROM parents").fetchone()[0]:
        return
    with conn:  # one transaction for the three linked rows
        parent_id = conn.execute(
            "INSERT INTO parents (email, password_hash, name) VALUES (?, ?, ?)",
            ("demo@travelwithtots.app", generate_password_hash("demo1234"),
             "Demo Parent")).lastrowid
        child_id = conn.execute(
            "INSERT INTO children (parent_id, name, date_of_birth) "
            "VALUES (?, ?, ?)",
            (parent_id, "Sam", "2023-05-10")).lastrowid
        conn.execute(
            "INSERT INTO trips (parent_id, child_id, trip_date, wake_up, bedtime, "
            "destination, accommodation, transit, stop_count, dining, "
            "nap_notes, extra_notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (parent_id, child_id, "2026-08-01", "07:00", "20:00",
             "Vancouver", "Fairmont Hotel Vancouver",
             json.dumps(["stroller", "bus"]), "3", "dine_out",
             "Naps well in the stroller.", "Loves parks and open space."))


def _seed_admin(conn):
    """Insert a default admin account once, so someone can log in to the
    settings page. Idempotent: skipped once any admin account exists."""
    if conn.execute("SELECT COUNT(*) FROM parents WHERE is_admin = 1").fetchone()[0]:
        return
    with conn:
        conn.execute(
            "INSERT INTO parents (email, password_hash, name, is_admin) "
            "VALUES (?, ?, ?, 1)",
            ("admin@travelwithtots.app", generate_password_hash("admin1234"), "Admin"))


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


def mark_verified(venue_id, admin_id):
    """Record that a human confirmed a venue already in the searchable set.

    Separate from promote_submission, which also changes `source`: this one only
    ever stamps, so confirming a seeded venue cannot accidentally publish
    anything. Scoped to VERIFIED_SOURCES so it cannot quietly bless a pending
    submission that belongs in the other queue.
    """
    source_clause, source_params = _verified_source_clause()
    _write(
        "UPDATE venues SET verified_at = datetime('now'), verified_by = ? "
        f"WHERE id = ? AND {source_clause}",
        [admin_id, venue_id, *source_params])


def set_venue_default_hours(venue_id, open_time, close_time):
    """Correct a venue's default hours.

    The only path by which an approved venue's hours can change. Until this
    existed they were frozen at whatever was typed the day it was approved:
    EDITABLE_VENUE_FIELDS deliberately excludes hours, so not even the parent
    who submitted a place could fix them, and nothing else wrote them.
    """
    _write("UPDATE venues SET open_time = ?, close_time = ? WHERE id = ?",
           (open_time or None, close_time or None, venue_id))


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
                   v.open_time AS current_open, v.close_time AS current_close
            FROM venue_hours_checks c JOIN venues v ON v.id = c.venue_id
            WHERE c.status = 'pending'
            ORDER BY c.checked_at DESC, c.id DESC""").fetchall()


def resolve_hours_check(check_id, admin_id=None):
    """Close a comparison, whether the hours were changed or kept."""
    _write("UPDATE venue_hours_checks SET status = 'resolved', "
           "decided_at = datetime('now'), decided_by = ? WHERE id = ?",
           (admin_id, check_id))


def add_report(venue_id, field, value, reported_by=None, note=None):
    """Record that somebody observed an amenity at a venue, or its absence.

    `value` 0 is a real report: "I looked and there was no change table" is
    information, and the point of this table is that it differs from silence.

    `reported_by` is None only for a seed claim (see _migrate_seed_claims): a
    report with no author is visibly weaker than one with, and that is how the
    hand-typed flags are represented now that nobody stands behind them.
    """
    if field not in REPORTABLE_FIELDS:
        raise ValueError(f"not a reportable field: {field}")
    return _write(
        "INSERT INTO venue_reports (venue_id, field, value, reported_by, note) "
        "VALUES (?, ?, ?, ?, ?)",
        (venue_id, field, int(bool(value)), reported_by, note))


def record_amenities(venue_id, values, reported_by, note=None):
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
        add_report(venue_id, field, value, reported_by=reported_by, note=note)
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
    sql = ("SELECT venue_id, field, value, reported_by, reported_at "
           "FROM venue_reports")
    params = []
    if venue_ids is not None:
        ids = list(venue_ids)
        if not ids:
            return {}
        sql += f" WHERE venue_id IN ({', '.join('?' for _ in ids)})"
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
