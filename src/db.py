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
from datetime import date
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
    child_id      INTEGER NOT NULL REFERENCES children(id) ON DELETE CASCADE,
    trip_date     TEXT,                   -- day of the outing (ISO)
    wake_up       TEXT,
    bedtime       TEXT,
    nap_1         TEXT,
    nap_2         TEXT,
    feeding_1     TEXT,
    feeding_2     TEXT,
    destination   TEXT,
    accommodation TEXT,
    transit       TEXT,                   -- JSON array of transit modes
    pace          TEXT,
    dining        TEXT,
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

TRIP_FIELDS = (
    "trip_date", "wake_up", "bedtime", "nap_1", "nap_2", "feeding_1",
    "feeding_2", "destination", "accommodation", "transit", "pace", "dining",
    "features", "nap_notes", "extra_notes", "plan_label", "plan_json",
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


def _seed_venues(conn):
    """Load the bundled venues into an empty table as 'curated' source."""
    if conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0]:
        return
    venues = json.loads(VENUES_SEED.read_text(encoding="utf-8"))
    rows = [
        (v["name"], v["type"], v["neighbourhood"], int(v["kid_friendly"]),
         int(v["has_family_room"]), int(v["has_nursing_room"]),
         int(v["stroller_accessible"]), "curated", "Vancouver", v["category"],
         int(v["nap_friendly"]), int(v["can_eat"]), v["open"], v["close"])
        for v in venues
    ]
    with conn:  # single transaction for the whole seed
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
            "INSERT INTO trips (child_id, trip_date, wake_up, bedtime, nap_1, "
            "nap_2, destination, accommodation, transit, pace, dining, features, "
            "nap_notes, extra_notes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (child_id, "2026-08-01", "07:00", "20:00", "13:00", "",
             "Vancouver", "Fairmont Hotel Vancouver",
             json.dumps(["stroller", "bus"]), "balanced", "dine_out",
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
    """Remove a child; their trips cascade-delete via the FK constraint."""
    _write("DELETE FROM children WHERE id = ?", (child_id,))


def add_trip(child_id, **fields):
    """Insert a trip. Only known columns (TRIP_FIELDS) are accepted, so the
    column names are never user-controlled and the values stay parameterized."""
    columns = ["child_id"] + [f for f in TRIP_FIELDS if f in fields]
    values = [child_id] + [fields[f] for f in TRIP_FIELDS if f in fields]
    placeholders = ", ".join("?" for _ in columns)
    return _write(
        f"INSERT INTO trips ({', '.join(columns)}) VALUES ({placeholders})", values)


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


def compute_age(date_of_birth, on=None):
    """Age as (years, months) from an ISO 'YYYY-MM-DD' date of birth, on a given
    date (default today). Age is derived here, never stored."""
    dob = date.fromisoformat(date_of_birth)
    on = on or date.today()
    months = (on.year - dob.year) * 12 + (on.month - dob.month) - (on.day < dob.day)
    return months // 12, months % 12


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
    """Trips with a saved itinerary for every child of this parent, newest
    first (older rows saved without a plan_json have nothing to open, so
    they're excluded rather than shown as a dead link)."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name FROM trips "
            "JOIN children ON children.id = trips.child_id "
            "WHERE children.parent_id = ? AND trips.plan_json IS NOT NULL "
            "ORDER BY trips.created_at DESC", (parent_id,)).fetchall()


def get_trip_for_parent(parent_id, trip_id):
    """One trip by id, scoped to this parent's children (ownership check)."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name FROM trips "
            "JOIN children ON children.id = trips.child_id "
            "WHERE children.parent_id = ? AND trips.id = ?",
            (parent_id, trip_id)).fetchone()


def get_candidate_venues(city, age_months, features=None, limit=20):
    """Curated venues in `city` (substring match) whose age range covers
    `age_months`, optionally narrowed by feature tags. Used to ground the AI
    planning agent -- it must never reference a venue outside this list."""
    wanted = [f for f in (features or []) if f in CANDIDATE_FEATURE_COLUMNS]
    clauses = ["source = 'curated'", "city LIKE ?",
               "min_age_months <= ?", "max_age_months >= ?"]
    params = [f"%{city}%", age_months, age_months]
    for feature in wanted:
        clauses.append(f"{feature} = 1")
    params.append(limit)
    with closing(connect()) as conn:
        return conn.execute(
            f"SELECT * FROM venues WHERE {' AND '.join(clauses)} "
            "ORDER BY name LIMIT ?", params).fetchall()


def get_logged_venues_for_parent(parent_id):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM venues WHERE parent_id = ? AND source = 'user_submitted' "
            "ORDER BY created_at DESC", (parent_id,)).fetchall()
