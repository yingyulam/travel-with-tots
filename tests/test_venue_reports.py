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

    def _post(self, **fields):
        return self.client.post(
            f"/trip/{self.trip}/report/{self.venue}", data=fields)

    def _flags(self):
        return db.reported_flags([self.venue]).get(self.venue, {})

    def _count(self):
        with closing(db.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM venue_reports").fetchone()[0]

    def test_a_yes_is_recorded_against_the_parent(self):
        self._post(has_washroom="yes")
        self.assertIs(self._flags()["has_washroom"], True)
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT reported_by FROM venue_reports").fetchone()
        self.assertEqual(row["reported_by"], self.parent)

    def test_not_sure_writes_nothing(self):
        self._post(has_washroom="", has_family_room="")
        self.assertEqual(self._count(), 0)
        self.assertNotIn("has_washroom", self._flags())

    def test_an_unchanged_answer_is_not_written_again(self):
        # Recency decides a conflict, so re-saving an unchanged form must not
        # manufacture a report and push the timestamp forward.
        self._post(has_washroom="yes")
        before = self._count()
        self._post(has_washroom="yes")
        self.assertEqual(self._count(), before)

    def test_a_changed_answer_is_written(self):
        self._post(has_washroom="yes")
        self._post(has_washroom="no")
        self.assertIs(self._flags()["has_washroom"], False)

    def test_a_note_is_kept_with_the_report(self):
        self._post(has_nursing_room="no", note="Closed for refurbishment.")
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT note FROM venue_reports").fetchone()
        self.assertEqual(row["note"], "Closed for refurbishment.")

    def test_a_parent_cannot_report_against_another_parents_trip(self):
        other = db.add_parent("z@example.com", "h", name="Z")
        with mock.patch.object(
                self.app_module, "_current_parent",
                return_value={"id": other, "is_admin": False,
                              "name": "Z", "email": "z@example.com"}):
            response = self._post(has_washroom="yes")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._count(), 0)


if __name__ == "__main__":
    unittest.main()
