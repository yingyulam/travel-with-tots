import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src.store import db, schema


def _insert_venue(conn, name, *, city="Vancouver", neighbourhood="Downtown",
                   can_eat=False, venue_type="park", source="curated",
                   open_time="06:00", close_time="22:00", **flags):
    """A venue with hours, because a venue without them is not schedulable and
    get_candidate_venues therefore will not offer it. Pass open_time=None to
    build one deliberately.

    Amenity kwargs become venue_reports rows, not columns: kid_friendly,
    nap_friendly and the age range are long gone, and the five amenities left
    that layer too, so a claim carries an author and a date.
    """
    cur = conn.execute(
        "INSERT INTO venues (name, city, neighbourhood, type, can_eat, source, "
        "open_time, close_time) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, city, neighbourhood, venue_type, int(can_eat), source,
         open_time, close_time))
    from src.store.db import REPORTABLE_FIELDS
    for field, value in flags.items():
        if field in REPORTABLE_FIELDS:
            conn.execute(
                "INSERT INTO venue_reports (venue_id, field, value, reported_by) "
                "VALUES (?, ?, ?, NULL)",
                (cur.lastrowid, field, int(bool(value))))


class GetCandidateVenuesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            schema.create_schema(conn)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def test_filters_by_city(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Local")
            _insert_venue(conn, "Wrong City", city="Toronto")
        names = {v["name"] for v in db.get_candidate_venues("Vancouver")}
        self.assertEqual(names, {"Local"})

    def test_an_age_is_accepted_and_ignored(self):
        # Every row ever written had a 0-60 month range, so the clause never
        # excluded anything. Age paces the day (realistic_stop_count), it does
        # not filter venues. The argument stays so agents.py needs no change.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "For Babies")
            _insert_venue(conn, "For Big Kids")
        names = {v["name"] for v in db.get_candidate_venues("Vancouver", age_months=12)}
        self.assertEqual(names, {"For Babies", "For Big Kids"})

    def test_a_requested_feature_is_accepted_and_ignored(self):
        # Amenity filtering moved to find_nearby, where a parent asks in the
        # moment. Narrowing a whole day to venues someone happened to have
        # reported on would return almost nothing.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Has Nursing Room", has_nursing_room=1)
            _insert_venue(conn, "No Nursing Room", has_nursing_room=0)
        names = {v["name"] for v in
                 db.get_candidate_venues("Vancouver", features=["has_nursing_room"])}
        self.assertEqual(names, {"Has Nursing Room", "No Nursing Room"})

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
        rows = db.get_candidate_venues("Vancouver", age_months=12, transit="walk")
        self.assertTrue(all(v["neighbourhood"] == "Big" for v in rows))

    def test_keeps_all_neighbourhoods_with_a_car(self):
        with closing(db.connect()) as conn, conn:
            for i in range(db.MIN_CLUSTER_SIZE):
                _insert_venue(conn, f"Big {i}", neighbourhood="Big")
            _insert_venue(conn, "Small", neighbourhood="Small")
        rows = db.get_candidate_venues("Vancouver", age_months=12, transit="car")
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

    def test_a_venue_without_hours_is_never_offered_as_a_candidate(self):
        # It could only ever be replaced: unknown hours mean not schedulable
        # (data_loader._hours_for_slot), so offering it spends one of a small
        # candidate budget on a stop the validator will refuse. 27 hourless
        # community centres would crowd out most of an 18-venue budget.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Knows Its Hours")
            _insert_venue(conn, "No Hours", open_time=None, close_time=None)
            _insert_venue(conn, "Blank Hours", open_time="", close_time="")
        names = {v["name"] for v in db.get_candidate_venues("Vancouver")}
        self.assertEqual(names, {"Knows Its Hours"})

    def test_user_submitted_venues_are_never_planned_around(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Reviewed")
            conn.execute(
                "INSERT INTO venues (name, city, neighbourhood, source) "
                "VALUES ('Unreviewed', 'Vancouver', 'Downtown', 'user_submitted')")
        rows = db.get_candidate_venues("Vancouver", age_months=12)
        self.assertEqual([v["name"] for v in rows], ["Reviewed"])


class EnsureColumnsMigrationTest(unittest.TestCase):
    """The migration path had no coverage: every other test builds its schema
    with create_schema, which already has every column, so
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
            schema._ensure_columns(conn)
        columns = self._venue_columns()
        self.assertIn("lat", columns)
        self.assertIn("lng", columns)

    def test_is_idempotent_and_preserves_existing_rows(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Already Here")
        with closing(db.connect()) as conn:
            schema._ensure_columns(conn)
            schema._ensure_columns(conn)  # second run must be a no-op, not an error
        with closing(db.connect()) as conn:
            rows = conn.execute("SELECT name, lat FROM venues").fetchall()
        self.assertEqual([r["name"] for r in rows], ["Already Here"])
        self.assertIsNone(rows[0]["lat"])

    def test_adds_the_provenance_columns_to_an_older_database(self):
        with closing(db.connect()) as conn:
            schema._ensure_columns(conn)
        columns = self._venue_columns()
        for column in ("source_url", "external_id", "verified_at",
                       "verified_by", "seed_rank"):
            self.assertIn(column, columns)


class VenueIdentityTest(unittest.TestCase):
    """The venue uniqueness rules, against a current schema where the indexes
    exist.

    Was SeedVenuesTest. Startup seeding is retired, and what these guard is the
    identity constraints rather than the seeder -- see tests/test_seed_venues.py
    for the bootstrap script itself.
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        # addCleanup rather than tearDown: a setUp that raises never reaches
        # tearDown, so a DB_PATH patch started here would leak into every later
        # test in the process.
        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.unlink, self.db_path)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)

    def test_a_duplicate_external_id_is_refused(self):
        with closing(db.connect()) as conn, conn:
            conn.execute("INSERT INTO venues (name, source, external_id) "
                         "VALUES ('A', 'curated', 'osm:node/1')")
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(db.connect()) as conn, conn:
                conn.execute("INSERT INTO venues (name, source, external_id) "
                             "VALUES ('B', 'curated', 'osm:node/1')")

    def test_rows_without_an_external_id_are_not_treated_as_duplicates(self):
        with closing(db.connect()) as conn, conn:
            conn.execute("INSERT INTO venues (name, source) VALUES ('A', 'curated')")
            conn.execute("INSERT INTO venues (name, source) VALUES ('B', 'curated')")
        with closing(db.connect()) as conn:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0], 2)

    def test_two_curated_copies_of_one_place_are_refused(self):
        with closing(db.connect()) as conn, conn:
            conn.execute("INSERT INTO venues (name, city, source) "
                         "VALUES ('Dup Park', 'Vancouver', 'curated')")
        with self.assertRaises(sqlite3.IntegrityError):
            with closing(db.connect()) as conn, conn:
                conn.execute("INSERT INTO venues (name, city, source) "
                             "VALUES ('Dup Park', 'Vancouver', 'curated')")

    def test_the_provenance_columns_round_trip(self):
        with closing(db.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO venues (name, city, source, source_url, external_id, "
                "verified_at, verified_by) VALUES ('Cited', 'Vancouver', "
                "'municipal_open_data', 'https://example.org/r/1', "
                "'vanopendata:parks/1', '2026-08-27', NULL)")
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT source_url, external_id, verified_at, "
                               "verified_by FROM venues WHERE name = 'Cited'").fetchone()
        self.assertEqual(row["source_url"], "https://example.org/r/1")
        self.assertEqual(row["external_id"], "vanopendata:parks/1")
        self.assertEqual(row["verified_at"], "2026-08-27")
        self.assertIsNone(row["verified_by"])


if __name__ == "__main__":
    unittest.main()
