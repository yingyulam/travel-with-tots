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
    destination   TEXT,
    accommodation TEXT,
    transit       TEXT,                   -- JSON array of transit modes
    pace          TEXT,
    dining        TEXT,
    features      TEXT,                   -- JSON array of feature keys
    nap_notes     TEXT,
    extra_notes   TEXT,
    plan_label    TEXT,                   -- label of the generated plan the parent picked
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
    created_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

TRIP_FIELDS = (
    "trip_date", "wake_up", "bedtime", "nap_1", "nap_2", "destination",
    "accommodation", "transit", "pace", "dining", "features",
    "nap_notes", "extra_notes", "plan_label",
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
        _seed_venues(conn)
        _seed_sample_data(conn)


def _seed_venues(conn):
    """Load the bundled venues into an empty table as 'curated' source."""
    if conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0]:
        return
    venues = json.loads(VENUES_SEED.read_text(encoding="utf-8"))
    rows = [
        (v["name"], v["type"], v["neighbourhood"], int(v["kid_friendly"]),
         int(v["has_family_room"]), int(v["has_nursing_room"]),
         int(v["stroller_accessible"]), "curated")
        for v in venues
    ]
    with conn:  # single transaction for the whole seed
        conn.executemany(
            "INSERT INTO venues (name, type, neighbourhood, kid_friendly, "
            "has_family_room, has_nursing_room, stroller_accessible, source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)


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
    """All trips for every child of this parent, newest first."""
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT trips.*, children.name AS child_name FROM trips "
            "JOIN children ON children.id = trips.child_id "
            "WHERE children.parent_id = ? "
            "ORDER BY trips.created_at DESC", (parent_id,)).fetchall()


def get_logged_venues_for_parent(parent_id):
    with closing(connect()) as conn:
        return conn.execute(
            "SELECT * FROM venues WHERE parent_id = ? AND source = 'user_submitted' "
            "ORDER BY created_at DESC", (parent_id,)).fetchall()
