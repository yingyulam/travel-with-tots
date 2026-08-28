"""The review queue: the human-in-the-loop gate, and the only path that turns a
proposal or a submission into a venue the app will plan around.

Uses a real SQLite database on a temp file and a real CSV, so the CHECK on
source, the unique indexes and the VERIFIED_SOURCES filter all run for real.
"""

import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src import candidates, db


class _ReviewTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = os.path.join(self._tmp.name, "app.db")
        self.csv_path = Path(self._tmp.name) / "venue_candidates.csv"

        import app as app_module
        self.app_module = app_module
        for target, attr, value in (
                (db, "DB_PATH", self.db_path),
                (candidates, "CANDIDATES_PATH", self.csv_path)):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.admin_id = db.add_parent("admin@example.com", "h", name="Admin")
        self.parent_id = db.add_parent("p@example.com", "h", name="P")

        self.client = app_module.app.test_client()
        patcher = mock.patch.object(
            app_module, "_current_parent",
            return_value={"id": self.admin_id, "is_admin": True,
                          "name": "Admin", "email": "admin@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _venue(self, name):
        with closing(db.connect()) as conn:
            return conn.execute("SELECT * FROM venues WHERE name = ?", (name,)).fetchone()

    def _submit(self, name, **fields):
        fields.setdefault("city", "Vancouver")
        return db.add_venue(name, source="user_submitted",
                            parent_id=self.parent_id, **fields)


class PromoteSubmissionTest(_ReviewTest):
    def test_verifying_stamps_who_and_when_and_makes_it_searchable(self):
        venue_id = self._submit("Nourish Kitchen")
        self.assertNotIn("Nourish Kitchen",
                         [v["name"] for v in db.get_venues_in_city("Vancouver")])
        db.promote_submission(venue_id, self.admin_id)
        row = self._venue("Nourish Kitchen")
        self.assertEqual(row["source"], "curated")
        self.assertTrue(row["verified_at"])
        self.assertEqual(row["verified_by"], self.admin_id)
        self.assertIn("Nourish Kitchen",
                      [v["name"] for v in db.get_venues_in_city("Vancouver")])

    def test_a_submission_with_no_city_is_refused_not_half_published(self):
        # city matching is a LIKE, so a curated row without one would be
        # published and unsearchable at the same time.
        venue_id = db.add_venue("Nowhere", source="user_submitted",
                                parent_id=self.parent_id)
        with self.assertRaises(db.PromotionError):
            db.promote_submission(venue_id, self.admin_id)
        self.assertEqual(self._venue("Nowhere")["source"], "user_submitted")

    def test_a_clash_with_a_curated_venue_is_refused(self):
        db.add_venue("Science World", source="curated", city="Vancouver")
        venue_id = self._submit("Science World")
        with self.assertRaises(db.PromotionError):
            db.promote_submission(venue_id, self.admin_id)
        self.assertEqual(self._venue_count("Science World"), 2)

    def _venue_count(self, name):
        with closing(db.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM venues WHERE name = ?",
                                (name,)).fetchone()[0]

    def test_an_already_decided_submission_is_refused(self):
        with self.assertRaises(db.PromotionError):
            db.promote_submission(9999, self.admin_id)

    def test_rejecting_removes_a_submission(self):
        venue_id = self._submit("Bad Entry")
        db.reject_submission(venue_id)
        self.assertIsNone(self._venue("Bad Entry"))

    def test_rejecting_cannot_remove_a_curated_venue(self):
        # An admin acting on a stale page must not delete a verified venue.
        venue_id = db.add_venue("Real Venue", source="curated", city="Vancouver")
        db.reject_submission(venue_id)
        self.assertIsNotNone(self._venue("Real Venue"))


class UnverifiedBacklogTest(_ReviewTest):
    def test_curated_but_unchecked_venues_are_listed(self):
        db.add_venue("Never Checked", source="curated", city="Vancouver")
        self.assertIn("Never Checked",
                      [v["name"] for v in db.get_unverified_venues()])

    def test_a_confirmed_venue_drops_off_the_backlog(self):
        venue_id = db.add_venue("Checked", source="curated", city="Vancouver")
        db.mark_verified(venue_id, self.admin_id)
        self.assertNotIn("Checked",
                         [v["name"] for v in db.get_unverified_venues()])
        row = self._venue("Checked")
        self.assertTrue(row["verified_at"])
        self.assertEqual(row["verified_by"], self.admin_id)

    def test_confirming_does_not_publish_a_pending_submission(self):
        # mark_verified only ever stamps; it must not become a second, unguarded
        # way to promote something.
        venue_id = self._submit("Still Pending")
        db.mark_verified(venue_id, self.admin_id)
        self.assertEqual(self._venue("Still Pending")["source"], "user_submitted")
        self.assertIsNone(self._venue("Still Pending")["verified_at"])

    def test_submissions_are_not_mixed_into_the_backlog(self):
        self._submit("A Submission")
        self.assertNotIn("A Submission",
                         [v["name"] for v in db.get_unverified_venues()])


class ReviewPageTest(_ReviewTest):
    def test_the_page_renders_for_an_admin(self):
        self._submit("Pending Place")
        body = self.client.get("/venues/review").get_data(as_text=True)
        self.assertIn("Pending Place", body)
        self.assertIn("Proposed venues", body)
        self.assertIn("Unverified venues", body)

    def test_a_non_admin_is_redirected(self):
        with mock.patch.object(
                self.app_module, "_current_parent",
                return_value={"id": self.parent_id, "is_admin": False,
                              "name": "P", "email": "p@example.com"}):
            self.assertEqual(self.client.get("/venues/review").status_code, 302)

    def test_an_unknown_action_changes_nothing(self):
        venue_id = self._submit("Untouched")
        self.client.post(f"/venues/review/{venue_id}", data={"action": "explode"})
        self.assertEqual(self._venue("Untouched")["source"], "user_submitted")


class CandidateBatchTest(_ReviewTest):
    def _propose(self, name="Bloedel Conservatory", **fields):
        candidates.add([{"name": name, "type": "garden", "category": "activity",
                         "city": "Vancouver", "lat": 49.24, "lng": -123.11,
                         "source_url": "https://example.org/b",
                         "evidence": "domed garden", **fields}])
        return candidates.load()[-1]

    def _post(self, action, picked=(), **fields):
        data = {"action": action, "picked": list(picked)}
        data.update(fields)
        return self.client.post("/venues/review/candidates", data=data)

    def test_approving_inserts_the_venue_with_its_citation_and_stamp(self):
        row = self._propose()
        self._post("approve", [row["id"]], **{
            f"{row['id']}-name": row["name"],
            f"{row['id']}-category": "activity",
            f"{row['id']}-city": "Vancouver",
            f"{row['id']}-open_time": "10:00",
            f"{row['id']}-close_time": "17:00",
            f"{row['id']}-kid_friendly": "on",
        })
        venue = self._venue("Bloedel Conservatory")
        self.assertIsNotNone(venue)
        self.assertEqual(venue["source"], "curated")
        self.assertEqual(venue["source_url"], "https://example.org/b")
        self.assertEqual(venue["verified_by"], self.admin_id)
        self.assertTrue(venue["verified_at"])
        self.assertEqual(venue["open_time"], "10:00")
        self.assertEqual(venue["kid_friendly"], 1)
        self.assertEqual(candidates.load()[0]["status"], candidates.APPROVED)

    def test_approving_without_a_category_is_refused(self):
        # A venue with no category fills neither an activity nor a food slot,
        # but would still be eligible as a nap stop.
        row = self._propose(category="")
        self._post("approve", [row["id"]], **{f"{row['id']}-category": ""})
        self.assertIsNone(self._venue("Bloedel Conservatory"))
        self.assertEqual(candidates.load()[0]["status"], candidates.PENDING)

    def test_an_unticked_candidate_stays_pending(self):
        keep = self._propose("Keep Pending")
        other = self._propose("Approve Me")
        self._post("approve", [other["id"]], **{
            f"{other['id']}-category": "activity",
            f"{other['id']}-city": "Vancouver",
        })
        by_name = {r["name"]: r["status"] for r in candidates.load()}
        self.assertEqual(by_name["Keep Pending"], candidates.PENDING)
        self.assertEqual(by_name["Approve Me"], candidates.APPROVED)

    def test_saving_edits_approves_nothing(self):
        row = self._propose()
        self._post("save", [], **{f"{row['id']}-neighbourhood": "Riley Park"})
        self.assertEqual(candidates.load()[0]["neighbourhood"], "Riley Park")
        self.assertEqual(candidates.load()[0]["status"], candidates.PENDING)
        self.assertIsNone(self._venue("Bloedel Conservatory"))

    def test_edits_are_saved_for_rows_that_were_not_ticked(self):
        # So a half-finished review is not lost.
        row = self._propose()
        self._post("approve", [], **{f"{row['id']}-open_time": "09:30"})
        self.assertEqual(candidates.load()[0]["open_time"], "09:30")

    def test_unticking_a_flag_clears_it(self):
        row = self._propose()
        self._post("save", [], **{f"{row['id']}-can_eat": "on"})
        self.assertEqual(candidates.load()[0]["can_eat"], "1")
        self._post("save", [])
        self.assertEqual(candidates.load()[0]["can_eat"], "")

    def test_rejecting_records_the_decision_and_writes_no_venue(self):
        row = self._propose()
        self._post("reject", [row["id"]])
        self.assertEqual(candidates.load()[0]["status"], candidates.REJECTED)
        self.assertIsNone(self._venue("Bloedel Conservatory"))

    def test_a_rejected_candidate_is_never_proposed_again(self):
        row = self._propose()
        self._post("reject", [row["id"]])
        self.assertEqual(candidates.add([{"name": "Bloedel Conservatory"}]), 0)

    def test_a_candidate_already_in_the_database_is_flagged(self):
        db.add_venue("Bloedel Conservatory", source="curated", city="Vancouver")
        self._propose()
        body = self.client.get("/venues/review").get_data(as_text=True)
        self.assertIn("Already in the database", body)

    def test_a_javascript_url_is_not_rendered_as_a_link(self):
        # The URL came from a model reading the open web. Jinja escapes the text
        # but will not stop a javascript: href.
        self._propose(source_url="javascript:alert(1)")
        body = self.client.get("/venues/review").get_data(as_text=True)
        self.assertNotIn('href="javascript:', body)

    def test_a_non_admin_cannot_approve(self):
        row = self._propose()
        with mock.patch.object(
                self.app_module, "_current_parent",
                return_value={"id": self.parent_id, "is_admin": False,
                              "name": "P", "email": "p@example.com"}):
            self.assertEqual(self._post("approve", [row["id"]]).status_code, 302)
        self.assertIsNone(self._venue("Bloedel Conservatory"))

    def test_approval_leaves_the_reviewed_values_on_the_record(self):
        # scripts/replay_candidates.py rebuilds from the CSV, so anything the
        # reviewer set has to be saved there, not only pushed into the venue.
        # Otherwise a rebuild silently restores venues without their hours or
        # flags, which looks like the data was never entered.
        row = self._propose()
        self._post("approve", [row["id"]], **{
            f"{row['id']}-category": "activity",
            f"{row['id']}-city": "Vancouver",
            f"{row['id']}-open_time": "10:00",
            f"{row['id']}-close_time": "17:00",
            f"{row['id']}-kid_friendly": "on",
            f"{row['id']}-nap_friendly": "on",
        })
        record = candidates.load()[0]
        self.assertEqual(record["status"], candidates.APPROVED)
        self.assertEqual(record["open_time"], "10:00")
        self.assertEqual(record["close_time"], "17:00")
        self.assertEqual(record["kid_friendly"], "1")
        self.assertEqual(record["nap_friendly"], "1")
        self.assertEqual(record["can_eat"], "")

    def test_confirming_the_backlog_in_a_batch(self):
        first = db.add_venue("One", source="curated", city="Vancouver")
        second = db.add_venue("Two", source="curated", city="Vancouver")
        self.client.post("/venues/confirm",
                         data={"picked": [str(first), str(second)]})
        self.assertTrue(self._venue("One")["verified_at"])
        self.assertTrue(self._venue("Two")["verified_at"])


if __name__ == "__main__":
    unittest.main()
