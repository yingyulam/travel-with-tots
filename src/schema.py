"""The shape of the database, and getting an existing one into that shape.

Everything here runs once, at startup, driven by `init_db`. Nothing in it
answers a request, which is why it is not in db.py: that module is imported by
23 files to run queries, and none of them need the 500 lines below.

Migrations are the bulk of it, and they are write-once, delete-never. A column
added to SCHEMA is free for a database created afterwards and needs a patch for
every database created before, so each `_ensure_*` and `_migrate_*` check stays
here permanently while doing nothing on any boot after the first. See
`_migrate_trips_ownership` for the shape of a real one: deleting a child used
to destroy their saved trips, and the fix had to repair existing databases as
well as define the new ones correctly.

SQL still lives in exactly two modules, this one and db.py, and the dependency
runs one way: schema imports db for its connections, never the reverse.
"""

import json
import os
from contextlib import closing

from werkzeug.security import generate_password_hash

from src import db, postgres

# db's own names are reached through the module rather than imported, so
# whichever module owns a name is the one place to patch it: db.connect_sqlite
# is db's, _seed_venues below is this module's.

VENUES_SEED = db._DATA_DIR / "venues.json"


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
    seed_rank           INTEGER                 -- position in venues.json: see _seed_venues
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


# The venue columns data/venues.json owns, in the order _seed_venues supplies
# them. Deliberately excludes source, parent_id and the provenance columns: a
# re-seed must never demote a row or discard a citation a human added.
SEED_FIELDS = ("type", "setting", "neighbourhood", "can_eat",
               "open_time", "close_time", "seed_rank")


def create_schema(conn):
    """Bring `conn` up to the current schema: tables, then columns added after
    a table was first created, then indexes.

    The order is the point. An index that names a column can only be created
    once that column exists, and on a database predating the column that is
    _ensure_columns' job. Callers use this rather than executescript(SCHEMA)
    so they cannot get the order wrong.
    """
    # Before the schema runs: CREATE TABLE IF NOT EXISTS would skip the new
    # venue_hours while the old one still holds the name, and dropping after
    # would then leave no table at all.
    _drop_stale_venue_hours(conn)
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.executescript(INDEXES)


# Columns added to a table after Supabase's copy of it was already created.
#
# postgres_ddl only emits CREATE TABLE IF NOT EXISTS, which does nothing to a
# table that exists, so a column added to SCHEMA reached SQLite and never
# reached Supabase. That is not a theoretical gap: adding venue_reports.status
# took the deployed planner, the nearby search, the trip page and the review
# page down at once, because reported_flags selects on it and get_venues calls
# reported_flags for every plan.
#
# Postgres has ADD COLUMN IF NOT EXISTS, so this is idempotent and costs
# nothing to run at every boot. Add a line here with the column, and a deploy
# applies it.
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
    Postgres syntax that SQLite does not have, so the fallback would run the
    wrong dialect against the local file. Unreachable means there is nothing to
    migrate here anyway.

    Never raises. A boot that cannot reach the database has bigger problems than
    this, and failing here would take every page down rather than the one
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
    """Create the tables if they don't exist and seed initial data once.

    On Supabase the tables were created by the SQL on /settings, and the
    SQLite migration machinery below cannot run there: it is PRAGMA table_info
    and ALTER TABLE the whole way down. What does run there is
    _ensure_postgres_columns, which adds any column added to SCHEMA since that
    copy was made.
    """
    if db._supabase_dsn() is not None:
        _ensure_postgres_columns()
        return
    with closing(db.connect_sqlite()) as conn:
        create_schema(conn)
        _drop_dead_columns(conn)
        _migrate_trips_ownership(conn)
        _seed_venues(conn)
        _migrate_seed_claims(conn)
        _seed_sample_data(conn)
        _seed_admin(conn)


def _drop_stale_venue_hours(conn):
    """Remove the (season, day_type) hours table so the per-weekday one can
    take its name.

    That table was created, never written to by anything, and left behind when
    the slot model was dropped. It carries no rows to lose, and the check is on
    its shape rather than its name so this cannot eat the new one.
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
    fields = [f for f in db.REPORTABLE_FIELDS if f in columns]
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


DEFAULT_ADMIN_EMAIL = "admin@travelwithtots.app"


def _seed_admin(conn):
    """Create the first admin account, from ADMIN_PASSWORD in the environment.

    The password used to be the literal "admin1234", which is the same mistake
    SECRET_KEY used to make and for the same reason: a default that works is one
    an attacker also has, and this one is published in the repository next to
    the app it opens. It grants /settings, which can change the data source and
    rewrite the chatbot's prompt, and every component page that spends API
    budget.

    No fallback, so a deployment cannot come up with an account somebody else
    knows the password to. Without ADMIN_PASSWORD there is simply no admin, and
    the message below says how to make one. Idempotent: skipped once any admin
    exists, so setting the variable does not reset a password already chosen.
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


