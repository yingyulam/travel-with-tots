"""get_venues() is the boundary between the venues table and the planners.

Its job is to hand back the exact dict shape the planners have always been
given, so most of these are shape guards: if one fails, something downstream
(itinerary.py, filters.py, interactions.py, trip.html) breaks quietly rather
than loudly.
"""

import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src import data_loader, db


class GetVenuesTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            db.create_schema(conn)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _add(self, name, **fields):
        """A venue. Amenity kwargs become venue_reports rows, since they are no
        longer columns -- a claim needs an author and a date, and an unexamined
        amenity has to read as absent rather than as "no"."""
        reports = {f: fields.pop(f) for f in list(fields)
                   if f in db.REPORTABLE_FIELDS}
        fields.setdefault("city", "Vancouver")
        fields.setdefault("source", "curated")
        columns = ", ".join(fields)
        placeholders = ", ".join("?" for _ in fields)
        with closing(db.connect()) as conn, conn:
            cur = conn.execute(f"INSERT INTO venues (name, {columns}) "
                               f"VALUES (?, {placeholders})",
                               (name, *fields.values()))
            for field, value in reports.items():
                conn.execute(
                    "INSERT INTO venue_reports (venue_id, field, value, "
                    "reported_by) VALUES (?, ?, ?, NULL)",
                    (cur.lastrowid, field, int(bool(value))))

    def test_returns_plain_dicts_not_database_rows(self):
        # filters.py, itinerary.py and interactions.py all call .get() on these,
        # which sqlite3.Row does not have, and Plan.to_dict() puts them through
        # jsonify, which cannot serialise a Row.
        self._add("A Park")
        venue = data_loader.get_venues()[0]
        self.assertIsInstance(venue, dict)
        self.assertNotIsInstance(venue, sqlite3.Row)
        self.assertIsNone(venue.get("definitely_not_a_key"))

    def test_carries_every_key_the_planners_read(self):
        self._add("A Park", open_time="09:00", close_time="17:00")
        venue = data_loader.get_venues()[0]
        for key in data_loader.VENUE_KEYS:
            self.assertIn(key, venue)
        self.assertEqual(venue["open"], "09:00")
        self.assertEqual(venue["close"], "17:00")
        self.assertIn("maps_url", venue)

    def test_hours_use_the_planner_names_not_the_column_names(self):
        # itinerary.venue_hours reads open/close. Venue dicts already saved into
        # trips.plan_json carry those names too.
        self._add("A Park", open_time="09:00", close_time="17:00")
        venue = data_loader.get_venues()[0]
        self.assertNotIn("open_time", venue)
        self.assertNotIn("close_time", venue)

    def test_flags_are_booleans_not_sqlite_integers(self):
        # SQLite has no boolean type. Left as 0/1 these would reach the browser
        # and trips.plan_json, where every previously saved venue has true/false.
        self._add("A Park", has_family_room=1, can_eat=0)
        venue = data_loader.get_venues()[0]
        self.assertIs(venue["has_family_room"], True)
        self.assertIs(venue["can_eat"], False)

    def test_excludes_unverified_submissions(self):
        self._add("Reviewed")
        self._add("Unreviewed", source="user_submitted")
        self.assertEqual([v["name"] for v in data_loader.get_venues()], ["Reviewed"])

    def test_internal_columns_never_reach_a_plan_or_the_browser(self):
        self._add("A Park", source_url="https://example.org", notes="a note")
        venue = data_loader.get_venues()[0]
        for key in ("source", "parent_id", "created_at", "notes",
                    "address", "source_url", "external_id", "verified_at",
                    "verified_by", "rejected_at", "rejected_by", "seed_rank"):
            self.assertNotIn(key, venue)

    def test_the_id_is_the_one_internal_column_that_travels(self):
        # A parent reporting a change table has to say which venue, and a name
        # is not a stable identity. Everything else internal stays out.
        self._add("A Park")
        self.assertIn("id", data_loader.get_venues()[0])

    def test_seeded_venues_keep_the_curators_order(self):
        # The planner takes the first venue that fits a slot, so this order is a
        # ranking. Alphabetical order would demote whatever the curator put first.
        self._add("Zebra Park", seed_rank=0)
        self._add("Apple Park", seed_rank=1)
        self.assertEqual([v["name"] for v in data_loader.get_venues()],
                         ["Zebra Park", "Apple Park"])

    def test_unranked_venues_follow_the_seeded_ones_alphabetically(self):
        self._add("Zebra Park", seed_rank=0)
        self._add("Beta Park")
        self._add("Alpha Park")
        self.assertEqual([v["name"] for v in data_loader.get_venues()],
                         ["Zebra Park", "Alpha Park", "Beta Park"])

    def test_a_city_filter_narrows_the_result(self):
        self._add("Local Park")
        self._add("Far Park", city="Toronto")
        self.assertEqual([v["name"] for v in data_loader.get_venues("Vancouver")],
                         ["Local Park"])

    def test_is_not_cached_between_calls(self):
        # The whole point of the change: a venue added to the table has to be
        # visible to the next plan without restarting the app.
        self._add("First Park")
        self.assertEqual(len(data_loader.get_venues()), 1)
        self._add("Second Park")
        self.assertEqual(len(data_loader.get_venues()), 2)


if __name__ == "__main__":
    unittest.main()
