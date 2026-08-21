import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src import db


def _insert_venue(conn, name, *, city="Vancouver", neighbourhood="Downtown",
                   min_age_months=0, max_age_months=60, can_eat=False, **flags):
    columns = {"kid_friendly": 0, "has_family_room": 0, "has_nursing_room": 0,
               "stroller_accessible": 0, "nap_friendly": 0}
    columns.update({k: int(v) for k, v in flags.items()})
    conn.execute(
        "INSERT INTO venues (name, city, neighbourhood, min_age_months, "
        "max_age_months, can_eat, source, kid_friendly, has_family_room, "
        "has_nursing_room, stroller_accessible, nap_friendly) "
        "VALUES (?, ?, ?, ?, ?, ?, 'curated', ?, ?, ?, ?, ?)",
        (name, city, neighbourhood, min_age_months, max_age_months, int(can_eat),
         columns["kid_friendly"], columns["has_family_room"],
         columns["has_nursing_room"], columns["stroller_accessible"],
         columns["nap_friendly"]))


class GetCandidateVenuesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def test_filters_by_city_and_age_range(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "In Range", min_age_months=0, max_age_months=24)
            _insert_venue(conn, "Too Old For This Kid", min_age_months=36, max_age_months=60)
            _insert_venue(conn, "Wrong City", city="Toronto")
        names = {v["name"] for v in db.get_candidate_venues("Vancouver", age_months=12)}
        self.assertEqual(names, {"In Range"})

    def test_filters_by_requested_feature(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Has Nursing Room", has_nursing_room=1)
            _insert_venue(conn, "No Nursing Room", has_nursing_room=0)
        names = {v["name"] for v in
                 db.get_candidate_venues("Vancouver", age_months=12, features=["has_nursing_room"])}
        self.assertEqual(names, {"Has Nursing Room"})

    def test_narrows_to_near_neighbourhood_when_large_enough(self):
        with closing(db.connect()) as conn, conn:
            for i in range(db.MIN_CLUSTER_SIZE):
                _insert_venue(conn, f"Downtown {i}", neighbourhood="Downtown")
            _insert_venue(conn, "Elsewhere", neighbourhood="Elsewhere")
        rows = db.get_candidate_venues(
            "Vancouver", age_months=12, near_neighbourhood="Downtown")
        self.assertTrue(all(v["neighbourhood"] == "Downtown" for v in rows))

    def test_ignores_near_neighbourhood_when_too_small(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Only One Downtown", neighbourhood="Downtown")
            _insert_venue(conn, "Elsewhere", neighbourhood="Elsewhere")
        rows = db.get_candidate_venues(
            "Vancouver", age_months=12, near_neighbourhood="Downtown")
        names = {v["name"] for v in rows}
        self.assertEqual(names, {"Only One Downtown", "Elsewhere"})

    def test_clusters_to_largest_neighbourhood_without_a_car(self):
        with closing(db.connect()) as conn, conn:
            for i in range(db.MIN_CLUSTER_SIZE):
                _insert_venue(conn, f"Big {i}", neighbourhood="Big")
            _insert_venue(conn, "Small", neighbourhood="Small")
        rows = db.get_candidate_venues("Vancouver", age_months=12, transit=["stroller"])
        self.assertTrue(all(v["neighbourhood"] == "Big" for v in rows))

    def test_keeps_all_neighbourhoods_with_a_car(self):
        with closing(db.connect()) as conn, conn:
            for i in range(db.MIN_CLUSTER_SIZE):
                _insert_venue(conn, f"Big {i}", neighbourhood="Big")
            _insert_venue(conn, "Small", neighbourhood="Small")
        rows = db.get_candidate_venues("Vancouver", age_months=12, transit=["car"])
        neighbourhoods = {v["neighbourhood"] for v in rows}
        self.assertEqual(neighbourhoods, {"Big", "Small"})

    def test_dine_out_guarantees_a_can_eat_venue_even_past_the_limit(self):
        # Alphabetically last, so the initial [:limit] slice cuts it off --
        # only the dine_out fallback query can still surface it.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "No Food A", can_eat=False)
            _insert_venue(conn, "No Food B", can_eat=False)
            _insert_venue(conn, "Zz Has Food", can_eat=True)
        rows = db.get_candidate_venues("Vancouver", age_months=12, dining="dine_out", limit=2)
        self.assertEqual(len(rows), 2)
        self.assertTrue(any(v["can_eat"] for v in rows))

    def test_dine_out_leaves_rows_alone_when_already_satisfied(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "No Food", can_eat=False)
            _insert_venue(conn, "Has Food", can_eat=True)
        rows = db.get_candidate_venues("Vancouver", age_months=12, dining="dine_out")
        self.assertEqual({v["name"] for v in rows}, {"No Food", "Has Food"})

    def test_on_the_go_does_not_force_a_can_eat_venue(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "No Food", can_eat=False)
        rows = db.get_candidate_venues("Vancouver", age_months=12, dining="on_the_go")
        self.assertEqual([v["name"] for v in rows], ["No Food"])

    def test_user_submitted_venues_are_never_planned_around(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Reviewed", kid_friendly=True)
            conn.execute(
                "INSERT INTO venues (name, city, neighbourhood, source, kid_friendly) "
                "VALUES ('Unreviewed', 'Vancouver', 'Downtown', 'user_submitted', 1)")
        rows = db.get_candidate_venues("Vancouver", age_months=12)
        self.assertEqual([v["name"] for v in rows], ["Reviewed"])


class EnsureColumnsMigrationTest(unittest.TestCase):
    """The migration path had no coverage: every other test builds its schema
    with executescript(db.SCHEMA), which already has every column, so
    _ensure_columns never ran. This starts from a pre-lat/lng venues table."""

    OLD_VENUES_SCHEMA = """
    CREATE TABLE venues (
        id                  INTEGER PRIMARY KEY,
        name                TEXT NOT NULL,
        type                TEXT,
        neighbourhood       TEXT,
        kid_friendly        INTEGER NOT NULL DEFAULT 0,
        has_family_room     INTEGER NOT NULL DEFAULT 0,
        has_nursing_room    INTEGER NOT NULL DEFAULT 0,
        stroller_accessible INTEGER NOT NULL DEFAULT 0,
        source              TEXT NOT NULL,
        parent_id           INTEGER,
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
    CREATE TABLE parents (id INTEGER PRIMARY KEY, email TEXT, password_hash TEXT,
                          name TEXT, is_admin INTEGER NOT NULL DEFAULT 0,
                          created_at TEXT);
    CREATE TABLE children (id INTEGER PRIMARY KEY, parent_id INTEGER, name TEXT,
                           gender TEXT, date_of_birth TEXT, created_at TEXT);
    CREATE TABLE trips (id INTEGER PRIMARY KEY, parent_id INTEGER, child_id INTEGER,
                        plan_json TEXT, feeding_1 TEXT, feeding_2 TEXT,
                        transit_nap TEXT, preferred_lunch_time TEXT, naps TEXT,
                        stop_count TEXT, created_at TEXT);
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            conn.executescript(self.OLD_VENUES_SCHEMA)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _venue_columns(self):
        with closing(db.connect()) as conn:
            return {row["name"] for row in conn.execute("PRAGMA table_info(venues)")}

    def test_adds_lat_lng_to_an_older_database(self):
        self.assertNotIn("lat", self._venue_columns())
        with closing(db.connect()) as conn:
            db._ensure_columns(conn)
        columns = self._venue_columns()
        self.assertIn("lat", columns)
        self.assertIn("lng", columns)

    def test_is_idempotent_and_preserves_existing_rows(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Already Here")
        with closing(db.connect()) as conn:
            db._ensure_columns(conn)
            db._ensure_columns(conn)  # second run must be a no-op, not an error
        with closing(db.connect()) as conn:
            rows = conn.execute("SELECT name, lat FROM venues").fetchall()
        self.assertEqual([r["name"] for r in rows], ["Already Here"])
        self.assertIsNone(rows[0]["lat"])

    def test_backfill_copies_coordinates_from_the_seed_file(self):
        with closing(db.connect()) as conn:
            db._ensure_columns(conn)
        # A real seed-file venue that the geocoding pass resolved.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Science World")
        with closing(db.connect()) as conn:
            db._backfill_venue_coordinates(conn)
            row = conn.execute(
                "SELECT lat, lng FROM venues WHERE name = 'Science World'").fetchone()
        self.assertIsNotNone(row["lat"])
        self.assertIsNotNone(row["lng"])

    def test_backfill_leaves_unknown_venues_alone(self):
        with closing(db.connect()) as conn:
            db._ensure_columns(conn)
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Not In The Seed File")
        with closing(db.connect()) as conn:
            db._backfill_venue_coordinates(conn)
            row = conn.execute(
                "SELECT lat FROM venues WHERE name = 'Not In The Seed File'").fetchone()
        self.assertIsNone(row["lat"])


if __name__ == "__main__":
    unittest.main()
