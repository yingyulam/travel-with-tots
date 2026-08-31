"""What a parent reports about a venue, and how conflicts resolve.

The behaviour that matters most: a parent can contradict the app. Before this,
11 venues asserted a nursing room on nobody's authority and no parent could
correct one.
"""

import json
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src import db


class ReportedFlagsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.parent = db.add_parent("p@example.com", "h", name="P")
        self.other = db.add_parent("q@example.com", "h", name="Q")
        self.venue = db.add_venue("A Museum", source="curated", city="Vancouver",
                                  venue_type="museum")

    def _flags(self):
        return db.reported_flags([self.venue]).get(self.venue, {})

    def _report(self, field, value, by=None, at=None):
        report_id = db.add_report(self.venue, field, value, reported_by=by)
        if at:
            with closing(db.connect()) as conn, conn:
                conn.execute("UPDATE venue_reports SET reported_at = ? WHERE id = ?",
                             (at, report_id))
        return report_id

    def test_a_field_nobody_reported_on_is_absent_not_false(self):
        # The whole point: "nobody has said" has to differ from "somebody looked
        # and there was none", because the columns cannot tell them apart.
        self.assertNotIn("has_nursing_room", self._flags())

    def test_absence_is_a_real_report(self):
        self._report("has_nursing_room", 0, by=self.parent)
        self.assertIs(self._flags()["has_nursing_room"], False)

    def test_a_parent_can_contradict_a_seed_claim(self):
        # reported_by None is how the hand-typed flags are represented.
        self._report("has_nursing_room", 1, by=None)
        self.assertIs(self._flags()["has_nursing_room"], True)
        self._report("has_nursing_room", 0, by=self.parent)
        self.assertIs(self._flags()["has_nursing_room"], False)

    def test_a_real_report_beats_a_seed_claim_whatever_the_dates(self):
        self._report("has_family_room", 0, by=self.parent, at="2020-01-01 00:00:00")
        self._report("has_family_room", 1, by=None, at="2026-01-01 00:00:00")
        self.assertIs(self._flags()["has_family_room"], False)

    def test_between_real_reports_the_newest_wins(self):
        self._report("has_washroom", 1, by=self.other, at="2026-01-01 00:00:00")
        self._report("has_washroom", 0, by=self.parent, at="2026-08-01 00:00:00")
        self.assertIs(self._flags()["has_washroom"], False)

    def test_an_unreportable_field_raises(self):
        # can_eat follows the kind of place and is set when a venue is added.
        with self.assertRaises(ValueError):
            db.add_report(self.venue, "can_eat", 1, reported_by=self.parent)

    def test_reports_are_scoped_to_the_venues_asked_about(self):
        other_venue = db.add_venue("A Park", source="curated", city="Vancouver")
        db.add_report(other_venue, "has_washroom", 1, reported_by=self.parent)
        self.assertEqual(db.reported_flags([self.venue]), {})

    def test_no_venue_ids_means_no_query_and_no_results(self):
        self.assertEqual(db.reported_flags([]), {})


class ReportRouteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import app as app_module
        self.app_module = app_module
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.parent = db.add_parent("p@example.com", "h", name="P")
        self.child = db.add_child(self.parent, "Sam", "2024-01-01")
        self.venue = db.add_venue("A Museum", source="curated", city="Vancouver",
                                  venue_type="museum")
        self.trip = db.add_trip(self.parent, self.child, destination="Vancouver",
                                plan_label="Mixed",
                                plan_json=json.dumps({"label": "Mixed", "stops": []}))
        self.client = app_module.app.test_client()
        self._as(self.parent, "p@example.com")

    def _as(self, parent_id, email):
        patcher = mock.patch.object(
            self.app_module, "_current_parent",
            return_value={"id": parent_id, "is_admin": False,
                          "name": "P", "email": email})
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def _post(self, found=(), shown=None, **extra):
        """Tick `found`; `shown` defaults to everything the panel offered."""
        body = {"trip_id": self.trip, "found": list(found),
                "shown": list(db.REPORTABLE_FIELDS if shown is None else shown),
                **extra}
        return self.client.post(f"/venues/{self.venue}/report", json=body)

    def _flags(self):
        return db.reported_flags([self.venue]).get(self.venue, {})

    def _count(self):
        with closing(db.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM venue_reports").fetchone()[0]

    def test_a_tick_is_recorded_against_the_parent(self):
        self._post(found=["has_washroom"])
        self.assertIs(self._flags()["has_washroom"], True)
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT reported_by FROM venue_reports").fetchone()
        self.assertEqual(row["reported_by"], self.parent)

    def test_ticking_nothing_writes_nothing(self):
        # An untouched panel is the common case: a parent ticks the one thing
        # they noticed and sends. The rest must not become claims.
        self._post()
        self.assertEqual(self._count(), 0)
        self.assertNotIn("has_washroom", self._flags())

    def test_an_unticked_unknown_is_not_a_claim_that_it_is_missing(self):
        # "I did not tick it" is not "I looked and there was none". Only a field
        # somebody had already claimed can be corrected by unticking.
        self._post(found=["has_washroom"])
        self.assertNotIn("has_family_room", self._flags())

    def test_an_unchanged_answer_is_not_written_again(self):
        # Recency decides a conflict, so sending the same panel twice must not
        # manufacture a report and push the timestamp forward.
        self._post(found=["has_washroom"])
        before = self._count()
        self._post(found=["has_washroom"])
        self.assertEqual(self._count(), before)

    def test_unticking_something_we_hold_reports_it_gone(self):
        # The "something is different" case, with no extra control for it.
        self._post(found=["has_washroom"])
        self._post(found=[])
        self.assertIs(self._flags()["has_washroom"], False)

    def test_a_field_the_panel_never_offered_is_left_alone(self):
        # A highchair is not offered at a park, so it must not be answered for.
        self._post(found=["has_washroom"],
                   shown=["has_washroom", "has_family_room"])
        self.assertNotIn("has_highchair", self._flags())

    def test_the_reply_confirms_what_was_saved(self):
        body = self._post(found=["has_washroom"]).get_json()
        self.assertEqual(body["saved"], 1)
        self.assertIn("noted 1 thing", body["message"])

    def test_a_closed_venue_files_a_check_for_an_admin(self):
        self._post(hours_wrong=True)
        checks = db.get_pending_hours_checks()
        self.assertEqual([c["source"] for c in checks],
                         [self.app_module.PARENT_HOURS_SOURCE])

    def test_the_time_they_were_sent_is_recorded(self):
        # The point of the report. "Closed at 17:00" is something a reviewer can
        # check against the hours we hold; "reported on the 31st" was not, and
        # that is all this used to say.
        self._post(hours_wrong=True, closed_at="17:00")
        says = db.get_pending_hours_checks()[0]["source_says"]
        self.assertIn("17:00", says)

    def test_a_missing_time_still_files_the_report(self):
        # The widget sends the stop's time, but a report is worth keeping even
        # from a caller that did not.
        self._post(hours_wrong=True)
        self.assertEqual(len(db.get_pending_hours_checks()), 1)

    def test_a_client_supplied_time_cannot_run_long(self):
        # It is rendered on the review page, so it is trimmed rather than
        # trusted.
        self._post(hours_wrong=True, closed_at="x" * 200)
        says = db.get_pending_hours_checks()[0]["source_says"]
        self.assertNotIn("x" * 10, says)

    def test_reporting_works_without_a_saved_trip(self):
        # The day being run has not necessarily been saved, and that is exactly
        # when a parent is standing at the stop.
        self.client.post(f"/venues/{self.venue}/report",
                         json={"found": ["has_washroom"],
                               "shown": ["has_washroom"]})
        self.assertIs(self._flags()["has_washroom"], True)

    def test_the_review_page_does_not_label_a_parent_report_as_osm(self):
        # Two sources reach that queue now and they say different kinds of
        # thing, so the label cannot be hardcoded.
        self._post(hours_wrong=True)
        admin = db.add_parent("a@example.com", "h", name="A")
        with mock.patch.object(
                self.app_module, "_current_parent",
                return_value={"id": admin, "is_admin": True,
                              "name": "A", "email": "a@example.com"}):
            html = self.client.get("/venues/review").get_data(as_text=True)
        self.assertIn("A parent found this closed", html)
        self.assertNotIn("OSM:", html.split("A parent found this closed")[0][-200:])

    def test_a_parent_cannot_report_against_another_parents_trip(self):
        other = db.add_parent("z@example.com", "h", name="Z")
        with mock.patch.object(
                self.app_module, "_current_parent",
                return_value={"id": other, "is_admin": False,
                              "name": "Z", "email": "z@example.com"}):
            response = self._post(found=["has_washroom"])
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self._count(), 0)


if __name__ == "__main__":
    unittest.main()
