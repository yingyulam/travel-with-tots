"""The shape of the database, and getting an existing one into that shape.

Everything here runs once at startup, driven by `init_db`. Nothing answers a
request, which is why it is separate from db.py.

**Startup creates tables and never venues.** The venues table is the source of
truth and rows arrive through review, so nothing here writes to it. Bootstrap a
fresh database with scripts/seed_venues.py.

Migrations are write-once and delete-never: a column added to SCHEMA is free
for a database created afterwards and needs a patch for every database created
before. So each `_ensure_*` and `_migrate_*` check stays permanently and does
nothing on any boot after the first.

The dependency runs one way: schema imports db for its connections, never the
reverse.
"""

import json
import os
from contextlib import closing

from werkzeug.security import generate_password_hash

from . import db, postgres

# db's own names are reached through the module rather than imported, so
# whichever module owns a name is the one place to patch it: db.connect_sqlite
# is db's, create_schema below is this module's.


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
    -- A day of a longer visit. One row is still one day: a five-day trip is
    -- five rows sharing a group id, ordered by day_index, which is what keeps
    -- every existing query, the dashboard and a one-day trip untouched.
    -- Nullable because every row saved before multi-day existed is a group of
    -- one, and reads as such.
    trip_group_id TEXT,
    day_index     INTEGER,
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
    seed_rank           INTEGER                 -- the curator's ordering, set only by
                                                -- scripts/seed_venues.py. NULL for rows
                                                -- that arrived through review
);

-- A comparison between our stored hours and an outside source, and what a
-- person decided about it. The point is that hours change: they are entered
-- once at review and nothing else ever writes them, so without this a venue's
-- hours are frozen at whatever was typed the day it was approved.
-- Opening hours for one day of the week, when a venue's hours are not the same
-- every day. Keyed on the weekday alone: 0 is Monday, matching date.weekday().
--
-- Two rules, and they are what make the table unambiguous:
--   * a venue with **no rows** keeps venues.open_time/close_time all week,
--     which is what most venues are and why adding this needed no migration;
--   * a venue with **any rows** is described entirely by them, so a weekday
--     with no row is closed that day.
--
-- The second rule is the reason for a table rather than columns: "closed on
-- Mondays" is the commonest real closure and a nullable column cannot say it
-- differently from "not filled in".
CREATE TABLE IF NOT EXISTS venue_hours (
    venue_id   INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    open_time  TEXT NOT NULL,
    close_time TEXT NOT NULL,
    PRIMARY KEY (venue_id, weekday)
);

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
    field       TEXT NOT NULL,      -- one of db.REPORTABLE_FIELDS
    value       INTEGER NOT NULL,   -- 1 present, 0 absent. Absence is a real report
    reported_by INTEGER REFERENCES parents(id) ON DELETE SET NULL,
    reported_at TEXT NOT NULL DEFAULT (datetime('now')),
    note        TEXT,
    -- Whether anybody has checked this. Only reports from the in-trip page are
    -- written pending: the parent standing in the building is the best source
    -- there is, but nothing they say reaches another parent until a reviewer
    -- agrees. Everything else -- a reviewer's own ticks, the municipal import,
    -- Log a Place -- is written approved and behaves as it always has.
    status      TEXT NOT NULL DEFAULT 'approved',  -- pending | approved | rejected
    decided_at  TEXT,
    decided_by  INTEGER REFERENCES parents(id) ON DELETE SET NULL
);

"""


# Indexes, kept apart from SCHEMA because they must be created after
# _ensure_columns: on a database that predates a column, an index naming it
# cannot be created until the ALTER TABLE has run. See create_schema.
INDEXES = """-- Two curated copies of one place is the duplicate the seed script skips on.
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


def create_schema(conn):
    """Bring `conn` up to the current schema.

    Tables, then columns added after a table was first created, then indexes.
    The order matters: an index naming a column can only be created once that
    column exists. Callers use this rather than executescript(SCHEMA) so they
    cannot get the order wrong.
    """
    # Before the schema runs: CREATE TABLE IF NOT EXISTS would skip the new
    # venue_hours while the old one still holds the name, and dropping after
    # would then leave no table at all.
    _drop_stale_venue_hours(conn)
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.executescript(INDEXES)


# Columns added to a table after Supabase's copy of it was created.
#
# postgres_ddl only emits CREATE TABLE IF NOT EXISTS, which does nothing to an
# existing table, so a column added to SCHEMA reaches SQLite and not Supabase.
# Add a line here and a deploy applies it. Idempotent, so it costs nothing to
# run at every boot.
POSTGRES_ADDED_COLUMNS = (
    ("trips", "trip_group_id", "TEXT"),
    ("trips", "day_index", "INTEGER"),
    ("venue_reports", "status", "TEXT NOT NULL DEFAULT 'approved'"),
    ("venue_reports", "decided_at", "TEXT"),
    ("venue_reports", "decided_by", "INTEGER"),
)


def _ensure_postgres_columns():
    """Add any column Supabase's tables are missing. Idempotent.

    Connects to Postgres directly rather than through connect(), which falls
    back to SQLite when Supabase is unreachable: ADD COLUMN IF NOT EXISTS is
    Postgres syntax, so the fallback would run the wrong dialect against the
    local file.

    Never raises. Failing here would take every page down rather than the one
    feature the column serves.
    """
    dsn = db._supabase_dsn()
    if dsn is None:
        return
    try:
        conn = postgres.connect(dsn)
    except (ImportError, *postgres.unreachable_errors()):
        return
    with closing(conn):
        for table, column, spec in POSTGRES_ADDED_COLUMNS:
            try:
                conn.execute(f"ALTER TABLE {table} "
                             f"ADD COLUMN IF NOT EXISTS {column} {spec}")
                conn.commit()
            except Exception as e:
                conn.rollback()
                print(f"Could not add {table}.{column}: "
                      f"{type(e).__name__}: {e}", flush=True)


def init_db():
    """Create the tables if they don't exist, and seed the demo account once.

    Creates no venues: those arrive through review, and the table is the source
    of truth. Bootstrap a fresh database with scripts/seed_venues.py.

    On Supabase the tables were created by the SQL on /settings and the SQLite
    migration machinery cannot run there, so only _ensure_postgres_columns
    does.
    """
    if db._supabase_dsn() is not None:
        _ensure_postgres_columns()
        return
    with closing(db.connect_sqlite()) as conn:
        create_schema(conn)
        _drop_dead_columns(conn)
        _migrate_trips_ownership(conn)
        _seed_sample_data(conn)
        _seed_admin(conn)


def _drop_stale_venue_hours(conn):
    """Remove the old (season, day_type) hours table so the per-weekday one can
    take its name.

    Matched on the table's shape rather than its name, so this cannot drop the
    current one.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(venue_hours)")}
    if "season" in columns:
        with conn:
            conn.execute("DROP TABLE venue_hours")


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
    if "trip_group_id" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips ADD COLUMN trip_group_id TEXT")
            conn.execute("ALTER TABLE trips ADD COLUMN day_index INTEGER")
    if "pace" in existing and "stop_count" not in existing:
        with conn:
            conn.execute("ALTER TABLE trips RENAME COLUMN pace TO stop_count")

    existing = {row["name"] for row in conn.execute("PRAGMA table_info(parents)")}
    if "is_admin" not in existing:
        with conn:
            conn.execute("ALTER TABLE parents ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")

    # `existing` is empty on a database old enough to predate the table itself,
    # where CREATE TABLE has not run yet and there is nothing to alter.
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(venue_reports)")}
    if existing and "status" not in existing:
        with conn:
            # Existing rows are live today and stay live: this gates what
            # happens from here, rather than retroactively withdrawing what
            # parents have already contributed.
            conn.execute("ALTER TABLE venue_reports ADD COLUMN status TEXT "
                         "NOT NULL DEFAULT 'approved'")
            conn.execute("ALTER TABLE venue_reports ADD COLUMN decided_at TEXT")
            conn.execute("ALTER TABLE venue_reports ADD COLUMN decided_by INTEGER "
                         "REFERENCES parents(id) ON DELETE SET NULL")

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
            # What a parent says in their own words, and the address the
            # geocoder resolved. Both are for the admin deciding whether the
            # submission is real.
            conn.execute("ALTER TABLE venues ADD COLUMN notes TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN address TEXT")
    if "source_url" not in existing:
        with conn:
            # Provenance: where a venue came from and who checked it. All
            # nullable, and verified_by has to be, since SQLite only allows
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
            # reads: "Closed Mondays September to May".
            conn.execute("ALTER TABLE venues ADD COLUMN hours_note TEXT")
    if "setting" not in existing:
        with conn:
            # Where a visit is spent, which `type` cannot carry. Nullable, so
            # a venue nobody has assessed reads as unknown rather than as
            # either answer.
            conn.execute("ALTER TABLE venues ADD COLUMN setting TEXT")
    if "rejected_at" not in existing:
        with conn:
            # Rejecting a submission used to delete it. A reviewer can be wrong,
            # and a deleted row takes its parent's own words and every report
            # about it with it, so a rejection is recorded instead.
            conn.execute("ALTER TABLE venues ADD COLUMN rejected_at TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN rejected_by INTEGER "
                         "REFERENCES parents(id) ON DELETE SET NULL")


def _drop_dead_columns(conn):
    """Remove columns nothing reads, listed below.

    Guarded per column and idempotent, like the additions in _ensure_columns.
    Needs SQLite 3.35+ for DROP COLUMN.
    """
    for table, column in (
            ("venues", "category"),
            ("venues", "kid_friendly"),
            ("venues", "nap_friendly"),
            ("venues", "min_age_months"),
            ("venues", "max_age_months"),
            # Amenities live in venue_reports, the only place a claim can carry
            # an author and a date. As INTEGER NOT NULL DEFAULT 0 these columns
            # could not express "nobody has said".
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
    """Rebuild `trips` so a saved plan belongs to the account, not the child.

    child_id becomes an optional SET NULL reference and parent_id the real
    owner, backfilled from each trip's current child. SQLite cannot ALTER a
    column's constraints in place, hence the rebuild. Idempotent: skipped once
    the table has parent_id.
    """
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


DEFAULT_ADMIN_EMAIL = "admin@travelwithtots.app"


def _seed_admin(conn):
    """Create the first admin account, from ADMIN_PASSWORD in the environment.

    No fallback password: without ADMIN_PASSWORD there is simply no admin, and
    the printed message says how to make one. An admin grants /settings, which
    can change the data source and rewrite the chatbot's prompt.

    Idempotent: skipped once any admin exists, so setting the variable does not
    reset a password already chosen.
    """
    if conn.execute("SELECT COUNT(*) FROM parents WHERE is_admin = 1").fetchone()[0]:
        return
    password = os.environ.get("ADMIN_PASSWORD", "")
    if not password:
        print("No admin account: set ADMIN_EMAIL and ADMIN_PASSWORD in .env, "
              "or run scripts/set_admin.py")
        return
    email = os.environ.get("ADMIN_EMAIL", DEFAULT_ADMIN_EMAIL).strip().lower()
    with conn:
        conn.execute(
            "INSERT INTO parents (email, password_hash, name, is_admin) "
            "VALUES (?, ?, ?, 1)", (email, generate_password_hash(password), "Admin"))
    print(f"Created the admin account {email}")
