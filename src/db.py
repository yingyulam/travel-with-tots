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
    gender        TEXT,
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
    nap_1         TEXT,                   -- unused; kept for old saved trips, see naps below
    nap_2         TEXT,
    naps          TEXT,                   -- JSON array of {"start", "duration_min"}
    transit_nap   TEXT,                   -- "yes"/"sometimes"/"no": can the child nap in transit
    feeding_1     TEXT,                   -- unused; kept for old saved trips
    feeding_2     TEXT,
    destination   TEXT,
    accommodation TEXT,
    transit       TEXT,                   -- JSON array of transit modes
    stop_count    TEXT,                   -- how many places the parent asked to visit
    dining        TEXT,
    preferred_lunch_time TEXT,             -- "HH:MM": when the parent wants lunch scheduled
    features      TEXT,                   -- JSON array of feature keys
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
    type                TEXT,
    neighbourhood       TEXT,
    kid_friendly        INTEGER NOT NULL DEFAULT 0,
    has_family_room     INTEGER NOT NULL DEFAULT 0,
    has_nursing_room    INTEGER NOT NULL DEFAULT 0,
    stroller_accessible INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL CHECK (
                            source IN ('municipal_open_data', 'user_submitted', 'curated')),
    parent_id           INTEGER REFERENCES parents(id) ON DELETE CASCADE,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    city                TEXT,
    category            TEXT,
    nap_friendly        INTEGER NOT NULL DEFAULT 0,
    can_eat             INTEGER NOT NULL DEFAULT 0,
    open_time           TEXT,
    close_time          TEXT,
    min_age_months      INTEGER NOT NULL DEFAULT 0,
    max_age_months      INTEGER NOT NULL DEFAULT 60
);
"""

# Feature/flag columns on `venues` that the AI planner is allowed to filter
# candidates by -- never string-interpolate a column name that isn't in here.
CANDIDATE_FEATURE_COLUMNS = {
    "kid_friendly", "has_family_room", "has_nursing_room",
    "stroller_accessible", "nap_friendly", "can_eat",
}

# Keeps the AI planner's prompt cheap: enough venues for a real choice,
# never so many the prompt balloons.
CANDIDATE_LIMIT = 18

# A neighbourhood needs at least this many matching venues before it's worth
# narrowing to (see get_candidate_venues) -- otherwise a parent without a car
# could end up with too few candidates to build a real itinerary from.
MIN_CLUSTER_SIZE = 6

TRIP_FIELDS = (
    "trip_date", "wake_up", "bedtime", "naps", "transit_nap",
    "destination", "accommodation", "transit",
    "stop_count", "dining", "preferred_lunch_time", "features", "nap_notes",
    "extra_notes", "plan_label", "plan_json",
)


def connect():
    """Open a connection with row access by name and foreign keys enforced."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create the tables if they don't exist and seed initial data once."""
    with closing(connect()) as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _migrate_trips_ownership(conn)
        _seed_venues(conn)
        _backfill_venue_details(conn)
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
            conn.execute("ALTER TABLE venues ADD COLUMN category TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN nap_friendly INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE venues ADD COLUMN can_eat INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE venues ADD COLUMN open_time TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN close_time TEXT")
            conn.execute("ALTER TABLE venues ADD COLUMN min_age_months INTEGER NOT NULL DEFAULT 0")
            conn.execute("ALTER TABLE venues ADD COLUMN max_age_months INTEGER NOT NULL DEFAULT 60")


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
                features      TEXT,
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
                preferred_lunch_time, features, nap_notes, extra_notes,
                plan_label, plan_json, created_at)
            SELECT t.id, c.parent_id, t.child_id, t.trip_date, t.wake_up,
                t.bedtime, t.nap_1, t.nap_2, t.naps, t.transit_nap, t.feeding_1,
                t.feeding_2, t.destination, t.accommodation, t.transit,
                t.stop_count, t.dining, t.preferred_lunch_time, t.features,
                t.nap_notes, t.extra_notes, t.plan_label, t.plan_json,
                t.created_at
            FROM trips_old t JOIN children c ON c.id = t.child_id
        """)
        conn.execute("DROP TABLE trips_old")


def _seed_venues(conn):
    """Insert any bundled venues not already present (matched by name) as
    'curated' source. Idempotent: safe to run on every startup, so adding
    more entries to the seed file always reaches the database on the next
    run instead of only ever seeding once on a completely empty table."""
    existing = {row[0] for row in conn.execute("SELECT name FROM venues").fetchall()}
    venues = json.loads(VENUES_SEED.read_text(encoding="utf-8"))
    rows = [
        (v["name"], v["type"], v["neighbourhood"], int(v["kid_friendly"]),
         int(v["has_family_room"]), int(v["has_nursing_room"]),
         int(v["stroller_accessible"]), "curated", "Vancouver", v["category"],
         int(v["nap_friendly"]), int(v["can_eat"]), v["open"], v["close"])
        for v in venues if v["name"] not in existing
    ]
    if not rows:
        return
    with conn:  # single transaction for the whole batch
        conn.executemany(
            "INSERT INTO venues (name, type, neighbourhood, kid_friendly, "
            "has_family_room, has_nursing_room, stroller_accessible, source, "
            "city, category, nap_friendly, can_eat, open_time, close_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)


def _backfill_venue_details(conn):
    """Fill in city/category/hours/nap/eat on curated rows that predate those
    columns, matched by name against the bundled seed file. Idempotent: rows
    that already have a city are left untouched, so this is safe to run on
    every startup."""
    pending = conn.execute(
        "SELECT id, name FROM venues WHERE source = 'curated' AND city IS NULL"
    ).fetchall()
    if not pending:
        return
    by_name = {v["name"]: v for v in json.loads(VENUES_SEED.read_text(encoding="utf-8"))}
    with conn:
        for row in pending:
            v = by_name.get(row["name"])
            if not v:
                continue
            conn.execute(
                "UPDATE venues SET city = ?, category = ?, nap_friendly = ?, "
                "can_eat = ?, open_time = ?, close_time = ? WHERE id = ?",
                ("Vancouver", v["category"], int(v["nap_friendly"]),
                 int(v["can_eat"]), v["open"], v["close"], row["id"]))


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
            "INSERT INTO children (parent_id, name, gender, date_of_birth) "
            "VALUES (?, ?, ?, ?)",
            (parent_id, "Sam", "male", "2023-05-10")).lastrowid
        conn.execute(
            "INSERT INTO trips (parent_id, child_id, trip_date, wake_up, bedtime, "
            "nap_1, nap_2, destination, accommodation, transit, stop_count, dining, "
            "features, nap_notes, extra_notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (parent_id, child_id, "2026-08-01", "07:00", "20:00", "13:00", "",
             "Vancouver", "Fairmont Hotel Vancouver",
             json.dumps(["stroller", "bus"]), "3", "dine_out",
             json.dumps(["kid_friendly", "has_nursing_room"]),
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


def add_child(parent_id, name, gender, date_of_birth):
    return _write(
        "INSERT INTO children (parent_id, name, gender, date_of_birth) "
        "VALUES (?, ?, ?, ?)", (parent_id, name, gender, date_of_birth))


def update_child(child_id, name, gender, date_of_birth):
    _write(
        "UPDATE children SET name = ?, gender = ?, date_of_birth = ? WHERE id = ?",
        (name, gender, date_of_birth, child_id))


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


def add_venue(name, *, source, venue_type=None, neighbourhood=None,
              kid_friendly=False, has_family_room=False,
              has_nursing_room=False, stroller_accessible=False,
              parent_id=None):
    return _write(
        "INSERT INTO venues (name, type, neighbourhood, kid_friendly, "
        "has_family_room, has_nursing_room, stroller_accessible, source, "
        "parent_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (name, venue_type, neighbourhood, int(kid_friendly), int(has_family_room),
         int(has_nursing_room), int(stroller_accessible), source, parent_id))


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


def get_candidate_venues(city, age_months, features=None, transit=None,
                          dining=None, near_neighbourhood=None, limit=CANDIDATE_LIMIT):
    """Curated venues in `city` (substring match) whose age range covers
    `age_months`, narrowed by feature tags. Used to ground the AI planning
    agent -- it must never reference a venue outside this list.

    There's no real geodata anywhere in this app (no lat/lng, no routing
    API), so location is a coarse proxy instead of a real radius/travel-time
    filter. If `near_neighbourhood` is given (e.g. replanning from a known
    current stop), candidates are narrowed to that specific neighbourhood, as
    long as it has enough venues to still offer a real choice. Otherwise
    `transit` decides: without a car, candidates are narrowed to the single
    most common neighbourhood among the matches (keeping stops close
    together), again only if it has enough venues; with a car, all matching
    neighbourhoods stay in play. If `dining` is "dine_out", at least one
    venue where a meal is possible is guaranteed a slot, so there's always a
    real lunch option."""
    where, params = _candidate_where_clause(city, age_months, features)

    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT * FROM venues WHERE {where} ORDER BY name", params).fetchall()
        rows = _narrow_by_neighbourhood(rows, near_neighbourhood, transit)
        rows = rows[:limit]
        rows = _ensure_dining_option(conn, rows, where, params, dining, limit)
        return rows


def _candidate_where_clause(city, age_months, features):
    """WHERE clause and params for a curated-venue lookup: city substring
    match, age range coverage, and any requested feature tags."""
    wanted = [f for f in (features or []) if f in CANDIDATE_FEATURE_COLUMNS]
    clauses = ["source = 'curated'", "city LIKE ?",
               "min_age_months <= ?", "max_age_months >= ?"]
    params = [f"%{city}%", age_months, age_months]
    for feature in wanted:
        clauses.append(f"{feature} = 1")
    return " AND ".join(clauses), params


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

    if "car" not in (transit or []) and rows:
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
