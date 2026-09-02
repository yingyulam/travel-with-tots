"""Checking our stored hours against an outside source.

The gap this closes: hours are typed in once when a venue is approved and
nothing ever writes them again, so a venue's hours were frozen at whatever was
entered that day while the planner trusted them completely.

The tool never changes anything itself. Half of what it finds needs judgment --
a mall tagged as closing at half four is more likely a mis-tagged building than
a mall that closes at half four -- so a finding goes to a person.
"""

import os
import tempfile
import unittest
from src.web import guards
from contextlib import closing
from unittest import mock

from src import db, osm


class CompareTest(unittest.TestCase):
    def test_an_exact_match_agrees(self):
        self.assertEqual(osm.compare("10:00", "17:00", "10:00-17:00"), "agrees")

    def test_a_leading_zero_does_not_defeat_the_comparison(self):
        self.assertEqual(osm.compare("09:00", "17:00", "9:00-17:00"), "agrees")

    def test_different_times_differ(self):
        self.assertEqual(osm.compare("09:30", "17:00", "10:00-17:00"), "differs")

    def test_day_specific_hours_are_more_detail_than_one_pair_holds(self):
        # A real finding: the Maritime Museum closes on Mondays from September,
        # which a single pair cannot express.
        self.assertEqual(
            osm.compare("10:00", "17:00", "Su-Sa 10:00-17:00; Sep-May: Mo off"),
            "more_detail")

    def test_nothing_from_osm_is_unverifiable_not_agreement(self):
        # The honesty rule: absence of contradiction is not confirmation.
        self.assertEqual(osm.compare("10:00", "17:00", ""), "unverifiable")

    def test_hours_with_no_readable_time_are_unverifiable(self):
        self.assertEqual(osm.compare("10:00", "17:00", "sunrise-sunset"),
                         "unverifiable")

    def test_always_open_is_only_agreement_if_we_say_so(self):
        self.assertEqual(osm.compare("00:00", "23:59", "24/7"), "agrees")
        self.assertEqual(osm.compare("10:00", "17:00", "24/7"), "differs")

    def test_a_name_key_is_safe_to_put_in_a_regex(self):
        self.assertEqual(osm._name_key("Trout Lake (John Hendry Park)"), "Trout Lake")
        self.assertNotIn('"', osm._name_key('A "Quoted" Place'))
        self.assertNotIn("[", osm._name_key("A [Bracketed] Place"))


class HoursCheckTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.admin = db.add_parent("a@example.com", "h", name="A")
        self.venue = db.add_venue("An Aquarium", source="curated", city="Vancouver",
                                  venue_type="aquarium",
                                  open_time="09:30", close_time="17:00")

    def test_a_finding_reaches_the_queue_with_both_values(self):
        db.record_hours_check(self.venue, "osm", "10:00-17:00", "differs",
                              "09:30", "17:00")
        check = db.get_pending_hours_checks()[0]
        self.assertEqual(check["name"], "An Aquarium")
        self.assertEqual(check["source_says"], "10:00-17:00")
        self.assertEqual(check["current_open"], "09:30")

    def test_re_running_refreshes_a_finding_rather_than_stacking_one(self):
        db.record_hours_check(self.venue, "osm", "10:00-17:00", "differs")
        db.record_hours_check(self.venue, "osm", "10:00-16:00", "differs")
        checks = db.get_pending_hours_checks()
        self.assertEqual(len(checks), 1)
        self.assertEqual(checks[0]["source_says"], "10:00-16:00")

    def test_correcting_the_hours_is_possible_at_all(self):
        # There was no path by which an approved venue's hours could change:
        # EDITABLE_VENUE_FIELDS excludes them and nothing else wrote them.
        db.set_venue_default_hours(self.venue, "10:00", "17:00")
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT open_time, close_time FROM venues WHERE id = ?",
                               (self.venue,)).fetchone()
        self.assertEqual((row["open_time"], row["close_time"]), ("10:00", "17:00"))

    def test_a_settled_finding_leaves_the_queue(self):
        db.record_hours_check(self.venue, "osm", "10:00-17:00", "differs")
        check_id = db.get_pending_hours_checks()[0]["id"]
        db.resolve_hours_check(check_id, self.admin)
        self.assertEqual(db.get_pending_hours_checks(), [])

    def test_deleting_a_venue_takes_its_findings(self):
        db.record_hours_check(self.venue, "osm", "10:00-17:00", "differs")
        with closing(db.connect()) as conn, conn:
            conn.execute("DELETE FROM venues WHERE id = ?", (self.venue,))
        self.assertEqual(db.get_pending_hours_checks(), [])


class HoursDecisionRouteTest(unittest.TestCase):
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
        self.admin = db.add_parent("a@example.com", "h", name="A")
        self.venue = db.add_venue("An Aquarium", source="curated", city="Vancouver",
                                  venue_type="aquarium",
                                  open_time="09:30", close_time="17:00")
        db.record_hours_check(self.venue, "osm", "10:00-17:00", "differs",
                              "09:30", "17:00")
        self.check_id = db.get_pending_hours_checks()[0]["id"]
        self.client = app_module.app.test_client()
        patcher = mock.patch.object(guards, "current_parent",
            return_value={"id": self.admin, "is_admin": True,
                          "name": "A", "email": "a@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _hours(self):
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT open_time, close_time FROM venues WHERE id = ?",
                               (self.venue,)).fetchone()
        return row["open_time"], row["close_time"]

    def test_updating_writes_the_new_hours_and_closes_the_finding(self):
        self.client.post(f"/venues/hours/{self.check_id}", data={
            "action": "update", "venue_id": self.venue,
            "open_time": "10:00", "close_time": "17:00"})
        self.assertEqual(self._hours(), ("10:00", "17:00"))
        self.assertEqual(db.get_pending_hours_checks(), [])

    def test_keeping_ours_changes_nothing_but_closes_the_finding(self):
        self.client.post(f"/venues/hours/{self.check_id}",
                         data={"action": "keep", "venue_id": self.venue})
        self.assertEqual(self._hours(), ("09:30", "17:00"))
        self.assertEqual(db.get_pending_hours_checks(), [])

    def test_a_half_filled_update_is_refused_and_stays_open(self):
        self.client.post(f"/venues/hours/{self.check_id}", data={
            "action": "update", "venue_id": self.venue, "open_time": "10:00"})
        self.assertEqual(self._hours(), ("09:30", "17:00"))
        self.assertEqual(len(db.get_pending_hours_checks()), 1)

    def test_a_non_admin_cannot_settle_a_finding(self):
        other = db.add_parent("p@example.com", "h", name="P")
        with mock.patch.object(guards, "current_parent",
                return_value={"id": other, "is_admin": False,
                              "name": "P", "email": "p@example.com"}):
            response = self.client.post(f"/venues/hours/{self.check_id}", data={
                "action": "update", "venue_id": self.venue,
                "open_time": "10:00", "close_time": "17:00"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self._hours(), ("09:30", "17:00"))

    def test_a_corrected_venue_is_planned_with_the_new_hours(self):
        # The whole point: the correction has to reach the planner.
        from datetime import date
        from src.data_loader import get_venues
        self.client.post(f"/venues/hours/{self.check_id}", data={
            "action": "update", "venue_id": self.venue,
            "open_time": "10:00", "close_time": "17:00"})
        venue = get_venues(on_date=date(2026, 8, 28))[0]
        self.assertEqual(venue["open"], "10:00")


if __name__ == "__main__":
    unittest.main()
