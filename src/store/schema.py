"""The shape of the database, and getting an existing one into that shape.

Everything here runs once at startup, driven by `init_db`. Nothing answers a
request, which is why it is separate from db.py.

**Startup creates tables and never venues.** The venues table is the source of
truth and rows arrive through review, so nothing here writes to it. Bootstrap a
fresh database with scripts/seed_venues.py.

SCHEMA is the single definition: a database gets exactly the tables written
there. A column added to it reaches Supabase only if it is also listed in
POSTGRES_ADDED_COLUMNS.

The dependency runs one way: schema imports db for its connections, never the
reverse.
"""

import json
import os
from contextlib import closing

from werkzeug.security import generate_password_hash

from . import connection, db, postgres

# Names are reached through their module rather than imported, so whichever
# module owns a name is the one place to patch it: connect_sqlite is
# connection's, create_schema below is this module's.


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
    seed_rank           INTEGER,                -- the curator's ordering, set only by
                                                -- scripts/seed_venues.py. NULL for rows
                                                -- that arrived through review
    hours_note          TEXT                    -- what a single open/close pair cannot
                                                -- hold: "Closed Mondays September to May"
);

-- Opening hours for one weekday, for venues whose hours are not the same every
-- day. Keyed on the weekday alone: 0 is Monday, matching date.weekday().
--
-- Two rules make the table unambiguous:
--   * a venue with no rows keeps venues.open_time/close_time all week;
--   * a venue with any rows is described entirely by them, so a weekday with
--     no row is closed that day.
--
-- The second rule is why this is a table rather than columns: a nullable
-- column cannot say "closed on Mondays" differently from "not filled in".
CREATE TABLE IF NOT EXISTS venue_hours (
    venue_id   INTEGER NOT NULL REFERENCES venues(id) ON DELETE CASCADE,
    weekday    INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    open_time  TEXT NOT NULL,
    close_time TEXT NOT NULL,
    PRIMARY KEY (venue_id, weekday)
);

-- A comparison between our stored hours and an outside source, and what a
-- person decided about it. Hours are entered once at review and nothing else
-- writes them, so without this they stay frozen at whatever was typed then.
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


# Indexes, kept apart from SCHEMA so they are always created after the tables.
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
    """Create the tables and then the indexes, in that order."""
    conn.executescript(SCHEMA)
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
    dsn = connection._supabase_dsn()
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

    On Supabase the tables were created by the SQL on /settings, so only
    _ensure_postgres_columns runs there.
    """
    if connection._supabase_dsn() is not None:
        _ensure_postgres_columns()
        return
    with closing(connection.connect_sqlite()) as conn:
        create_schema(conn)
        _seed_sample_data(conn)
        _seed_admin(conn)


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
