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


if __name__ == "__main__":
    unittest.main()
