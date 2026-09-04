"""The one-time bootstrap script, scripts/seed_venues.py.

Startup used to seed data/venues.json into the venues table on every boot and
upsert, so an edit to the file reached a populated database. That also meant a
restart silently reverted an admin's correction, which is the behaviour these
tests are here to make sure never comes back: the script inserts and never
updates, so it cannot revert a decision even run by accident.

Loaded by path rather than imported, because it lives in scripts/ and scripts
are not a package. Four of these moved here from tests/test_db.py, where they
covered the seeder as a startup step.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import importlib.util
import json
import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src.store import db, schema

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "seed_venues.py"
_spec = importlib.util.spec_from_file_location("seed_venues_script", _SCRIPT)
seed_venues = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_venues)


class _FreshDBTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.unlink, self.db_path)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)

    def _seed(self):
        with closing(db.connect()) as conn:
            return seed_venues.seed(conn)

    def _rows(self, sql, params=()):
        with closing(db.connect()) as conn:
            return conn.execute(sql, params).fetchall()


class ItInsertsAndNeverUpdatesTest(_FreshDBTest):
    """The property that makes retiring startup seeding safe."""

    def test_an_existing_row_is_left_exactly_as_it_was(self):
        # The whole reason this script is insert-only. An admin confirms a
        # venue on /venues/review and corrects its neighbourhood; running this
        # afterwards must not put the file's value back. Startup seeding did
        # precisely that, and nobody was told -- it happened to the Vancouver
        # Aquarium's opening hours before hours were made fill-only.
        name = json.loads(seed_venues.VENUES_SEED.read_text())[0]["name"]
        with closing(db.connect()) as conn, conn:
            conn.execute(
                "INSERT INTO venues (name, source, city, neighbourhood, type, "
                "setting, open_time, close_time, verified_at, seed_rank) VALUES "
                "(?, 'curated', 'Vancouver', 'Corrected By Hand', 'museum', "
                "'indoor', '11:00', '16:00', '2026-09-01 10:00:00', 99)",
                (name,))
        inserted, skipped = self._seed()
        row = self._rows("SELECT * FROM venues WHERE name = ?", (name,))[0]
        self.assertEqual(row["neighbourhood"], "Corrected By Hand")
        self.assertEqual(row["open_time"], "11:00")
        self.assertEqual(row["verified_at"], "2026-09-01 10:00:00")
        self.assertEqual(row["seed_rank"], 99)
        self.assertEqual(skipped, 1)
        self.assertGreater(inserted, 0)

    def test_is_idempotent(self):
        self._seed()
        first = self._rows("SELECT COUNT(*) c FROM venues")[0]["c"]
        self._seed()
        second = self._rows("SELECT COUNT(*) c FROM venues")[0]["c"]
        self.assertEqual(first, second)
        self.assertGreater(first, 0)

    def test_venues_not_in_the_seed_file_are_left_alone(self):
        with closing(db.connect()) as conn, conn:
            conn.execute("INSERT INTO venues (name, city, source) "
                         "VALUES ('Not In The Seed File', 'Vancouver', 'curated')")
        self._seed()
        row = self._rows("SELECT lat, seed_rank FROM venues "
                         "WHERE name = 'Not In The Seed File'")[0]
        self.assertIsNone(row["lat"])
        self.assertIsNone(row["seed_rank"])

    def test_a_user_submission_does_not_block_a_curated_entry(self):
        # The live database has user-submitted "Science World" rows. Matching
        # against every row instead of only curated ones let them suppress the
        # curated entry entirely.
        name = json.loads(seed_venues.VENUES_SEED.read_text())[0]["name"]
        with closing(db.connect()) as conn, conn:
            conn.execute("INSERT INTO venues (name, city, source) "
                         "VALUES (?, 'Vancouver', 'user_submitted')", (name,))
        self._seed()
        sources = [r["source"] for r in
                   self._rows("SELECT source FROM venues WHERE name = ?", (name,))]
        self.assertIn("curated", sources)
        self.assertIn("user_submitted", sources)


class RankingTest(_FreshDBTest):
    def test_seed_rank_follows_the_order_of_the_seed_file(self):
        # The planner takes the first venue that fits a slot, so this order is
        # a ranking. Without it the fallback ORDER BY name would quietly demote
        # whatever the curator put first.
        self._seed()
        names = [v["name"] for v in
                 json.loads(seed_venues.VENUES_SEED.read_text(encoding="utf-8"))]
        seeded = [r["name"] for r in self._rows(
            "SELECT name FROM venues WHERE source = 'curated' ORDER BY seed_rank")]
        self.assertEqual(seeded, names)


class StartupDoesNotSeedTest(unittest.TestCase):
    """The contract this whole change exists to establish."""

    def test_init_db_creates_no_venues(self):
        # Venues arrive through review, and the table is the source of truth,
        # so a boot that wrote to it could only overwrite somebody's decision.
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(db, "DB_PATH", os.path.join(tmp, "fresh.db")):
                schema.init_db()
                with closing(db.connect()) as conn:
                    count = conn.execute(
                        "SELECT COUNT(*) c FROM venues").fetchone()["c"]
        self.assertEqual(count, 0)

    def test_the_seeder_is_not_importable_from_the_runtime_package(self):
        # It lives in scripts/ now. A future startup step reaching for it again
        # is the regression worth catching.
        self.assertFalse(hasattr(schema, "_seed_venues"))
        self.assertFalse(hasattr(schema, "VENUES_SEED"))


if __name__ == "__main__":
    unittest.main()
