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

    def test_rejecting_sets_a_submission_aside_rather_than_deleting_it(self):
        # A reviewer can be wrong, and deleting the row would take the parent's
        # own words and every report about it (ON DELETE CASCADE) with it.
        venue_id = self._submit("Bad Entry", notes="what the parent said")
        db.add_report(venue_id, "has_family_room", 1, reported_by=self.parent_id)
        db.reject_submission(venue_id, self.admin_id)

        self.assertIsNotNone(self._venue("Bad Entry"))
        self.assertEqual(self._venue("Bad Entry")["notes"], "what the parent said")
        self.assertEqual(self._venue("Bad Entry")["rejected_by"], self.admin_id)
        self.assertNotIn("Bad Entry",
                         [v["name"] for v in db.get_pending_submissions()])
        self.assertIn("Bad Entry",
                      [v["name"] for v in db.get_rejected_submissions()])
        self.assertEqual(len(db.reported_flags([venue_id])), 1)

    def test_a_rejected_submission_cannot_be_verified(self):
        venue_id = self._submit("Set Aside")
        db.reject_submission(venue_id, self.admin_id)
        with self.assertRaises(db.PromotionError):
            db.promote_submission(venue_id, self.admin_id)

    def test_a_rejected_submission_can_be_restored(self):
        venue_id = self._submit("Second Thoughts")
        db.reject_submission(venue_id, self.admin_id)
        db.restore_submission(venue_id)
        self.assertIn("Second Thoughts",
                      [v["name"] for v in db.get_pending_submissions()])
        self.assertIsNone(self._venue("Second Thoughts")["rejected_at"])

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


class ApprovalClashTest(_ReviewTest):
    """A clash must cost one row, never the batch.

    idx_venues_curated_identity is unique on (name, city) where source is
    'curated'. The IntegrityError it raises was uncaught: it unwound the
    `for row in candidates.load(PENDING)` loop, so every row *after* the
    failing one lost its edits and its decision, the reviewer got an opaque
    500 with no flash, and re-submitting failed on the same row and lost the
    tail again. The batch was unrecoverable through the UI.

    Reachable even with the warning badge in place, because `name` and `city`
    are both editable: a reviewer can create the clash themselves after the
    page was drawn.
    """

    def _batch(self, *names):
        candidates.add([{"name": n, "type": "museum", "setting": "indoor",
                         "neighbourhood": "Downtown", "city": "Vancouver",
                         "source_url": "https://example.org/x",
                         "evidence": "e"} for n in names])
        return candidates.load(candidates.PENDING)

    def _approve_all(self, rows):
        ids = [r["id"] for r in rows]
        data = {"action": "approve", "picked": ids, "on_page": ids}
        for i in ids:
            data.update({f"{i}-open_time": "10:00", f"{i}-close_time": "17:00"})
        return self.client.post("/venues/review/candidates", data=data,
                                follow_redirects=True)

    def test_the_rows_after_a_clash_are_still_approved(self):
        db.add_venue("Clashing Venue", source="curated", city="Vancouver")
        rows = self._batch("First Ok", "Clashing Venue", "Third Ok")
        response = self._approve_all(rows)

        self.assertEqual(response.status_code, 200)  # was a 500
        by_name = {r["name"]: r["status"] for r in candidates.load()}
        self.assertEqual(by_name["First Ok"], candidates.APPROVED)
        self.assertEqual(by_name["Third Ok"], candidates.APPROVED)
        # The clashing row is left for the reviewer rather than silently lost.
        self.assertEqual(by_name["Clashing Venue"], candidates.PENDING)

    def test_the_reviewer_is_told_which_row_was_refused(self):
        db.add_venue("Clashing Venue", source="curated", city="Vancouver")
        rows = self._batch("Clashing Venue", "Other Ok")
        body = self._approve_all(rows).get_data(as_text=True)
        self.assertIn("Clashing Venue", body)
        self.assertIn("already a curated venue", body)

    def test_no_second_venue_is_inserted(self):
        db.add_venue("Clashing Venue", source="curated", city="Vancouver")
        rows = self._batch("Clashing Venue")
        self._approve_all(rows)
        with closing(db.connect()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM venues WHERE name = 'Clashing Venue'"
            ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_the_page_warns_and_disables_the_checkbox(self):
        # As far as a page-render-time check can go. It does not close the
        # hole: a disabled checkbox is simply not submitted, so a stale page
        # gets through, and a reviewer can create the clash by editing the
        # name after the page was drawn. Hence the caught IntegrityError.
        db.add_venue("Clashing Venue", source="curated", city="Vancouver")
        self._batch("Clashing Venue")
        body = self.client.get("/venues/review").get_data(as_text=True)
        self.assertIn("Already in the database", body)
        self.assertIn("disabled", body)


class CandidateBatchTest(_ReviewTest):
    def _propose(self, name="Bloedel Conservatory", **fields):
        candidates.add([{"name": name, "type": "garden",
                         "city": "Vancouver", "lat": 49.24, "lng": -123.11,
                         "source_url": "https://example.org/b",
                         "evidence": "domed garden", **fields}])
        return candidates.load()[-1]

    def _required(self, row, **extra):
        """The fields approval will not proceed without, keyed for one row."""
        fields = {f"{row['id']}-name": row["name"],
                  f"{row['id']}-type": row["type"] or "park",
                  f"{row['id']}-setting": row["setting"] or "outdoor",
                  f"{row['id']}-city": row["city"] or "Vancouver",
                  f"{row['id']}-open_time": "10:00",
                  f"{row['id']}-close_time": "17:00"}
        fields.update({f"{row['id']}-{k}": v for k, v in extra.items()})
        return fields

    def _post(self, action, picked=(), on_page=None, **fields):
        # on_page defaults to every pending id: the route only touches rows the
        # page actually rendered, so a test that omits it would change nothing.
        if on_page is None:
            on_page = [r["id"] for r in candidates.load(candidates.PENDING)]
        data = {"action": action, "picked": list(picked), "on_page": list(on_page)}
        data.update(fields)
        return self.client.post("/venues/review/candidates", data=data)

    def test_approving_inserts_the_venue_with_its_citation_and_stamp(self):
        row = self._propose()
        self._post("approve", [row["id"]], **self._required(row, has_family_room="on"))
        venue = self._venue("Bloedel Conservatory")
        self.assertIsNotNone(venue)
        self.assertEqual(venue["source"], "curated")
        self.assertEqual(venue["source_url"], "https://example.org/b")
        self.assertEqual(venue["verified_by"], self.admin_id)
        self.assertTrue(venue["verified_at"])
        self.assertEqual(venue["open_time"], "10:00")
        self.assertEqual(venue["has_family_room"], 1)
        self.assertEqual(candidates.load()[0]["status"], candidates.APPROVED)

    def test_an_unticked_candidate_stays_pending(self):
        keep = self._propose("Keep Pending")
        other = self._propose("Approve Me")
        self._post("approve", [other["id"]], **self._required(other))
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

    def test_approving_without_hours_is_refused(self):
        # A venue with no hours is treated as open all day by the planner, which
        # is how a museum gets scheduled at eight in the evening. Deciding
        # whether a place can be visited at a time is most of what the planner
        # does, so hours are not optional.
        row = self._propose()
        self._post("approve", [row["id"]], **{
            f"{row['id']}-name": row["name"],
            f"{row['id']}-type": "garden",
            f"{row['id']}-city": "Vancouver",
        })
        self.assertIsNone(self._venue(row["name"]))
        self.assertEqual(candidates.load()[0]["status"], candidates.PENDING)

    def test_approving_without_a_type_is_refused(self):
        # type is not a label: is_nap_friendly reads it, so a blank one silently
        # changes which venues can hold a nap.
        row = self._propose()
        fields = self._required(row)
        fields[f"{row['id']}-type"] = ""
        self._post("approve", [row["id"]], **fields)
        self.assertIsNone(self._venue(row["name"]))

    def test_the_hours_reach_the_venue(self):
        row = self._propose()
        self._post("approve", [row["id"]],
                   **self._required(row, open_time="08:30", close_time="16:45"))
        venue = self._venue(row["name"])
        self.assertEqual(venue["open_time"], "08:30")
        self.assertEqual(venue["close_time"], "16:45")

    def test_seasonal_hours_reach_the_venue(self):
        row = self._propose()
        fields = self._required(row)
        fields.update({
            f"{row['id']}-open_winter_weekday": "10:00",
            f"{row['id']}-close_winter_weekday": "16:00",
            f"{row['id']}-open_summer_weekend": "09:00",
            f"{row['id']}-close_summer_weekend": "20:00",
        })
        self._post("approve", [row["id"]], **fields)
        venue_id = self._venue(row["name"])["id"]
        slots = db.venue_hours_by_slot([venue_id])[venue_id]
        self.assertEqual(slots[("winter", "weekday")], ("10:00", "16:00"))
        self.assertEqual(slots[("summer", "weekend")], ("09:00", "20:00"))
        self.assertNotIn(("summer", "weekday"), slots)

    def test_a_half_filled_hour_slot_is_skipped_not_guessed(self):
        row = self._propose()
        fields = self._required(row)
        fields[f"{row['id']}-open_winter_weekday"] = "10:00"   # no closing time
        self._post("approve", [row["id"]], **fields)
        venue_id = self._venue(row["name"])["id"]
        self.assertEqual(db.venue_hours_by_slot([venue_id]), {})

    def test_no_seasonal_hours_means_the_default_pair_applies(self):
        row = self._propose()
        self._post("approve", [row["id"]], **self._required(row))
        venue_id = self._venue(row["name"])["id"]
        self.assertEqual(db.venue_hours_by_slot([venue_id]), {})

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

    def test_only_a_page_of_proposals_is_shown_at_a_time(self):
        for i in range(23):
            self._propose(f"Venue {i:02}")
        body = self.client.get("/venues/review").get_data(as_text=True)
        shown = [f"Venue {i:02}" for i in range(23) if f"Venue {i:02}" in body]
        self.assertEqual(len(shown), self.app_module.PROPOSAL_PAGE_SIZE)
        self.assertIn(f"of {23}", body)

    def test_the_queue_advances_as_batches_are_decided(self):
        for i in range(15):
            self._propose(f"Venue {i:02}")
        page = candidates.load(candidates.PENDING)[:10]
        self._post("reject", [c["id"] for c in page], on_page=[c["id"] for c in page])
        body = self.client.get("/venues/review").get_data(as_text=True)
        # The five that were never on the first page are now the batch.
        self.assertIn("of 5", body)

    def test_an_off_page_candidate_keeps_its_flags(self):
        # The trap pagination introduces: a checkbox that was never rendered
        # comes back absent, which reads identically to unticked. Iterating the
        # whole queue would wipe the flags of everything the reviewer never saw.
        for i in range(15):
            self._propose(f"Venue {i:02}")
        off_page = candidates.load(candidates.PENDING)[14]
        candidates.update(off_page["id"], has_washroom="1")

        page = candidates.load(candidates.PENDING)[:10]
        self._post("save", [], on_page=[c["id"] for c in page])

        kept = next(r for r in candidates.load() if r["id"] == off_page["id"])
        self.assertEqual(kept["has_washroom"], "1")

    def test_a_rejected_proposal_is_kept_and_restorable(self):
        row = self._propose()
        self._post("reject", [row["id"]])
        self.assertEqual(candidates.counts()["rejected"], 1)
        self.client.post("/venues/restore", data={"candidate_id": row["id"]})
        self.assertEqual(candidates.counts()["pending"], 1)
        self.assertEqual(candidates.counts()["rejected"], 0)

    def test_the_highchair_question_is_only_asked_where_a_meal_can_happen(self):
        self._propose("A Park")
        body = self.client.get("/venues/review").get_data(as_text=True)
        self.assertNotIn("has_highchair", body)

        candidates.update(candidates.load()[0]["id"], can_eat="1")
        body = self.client.get("/venues/review").get_data(as_text=True)
        self.assertIn("has_highchair", body)

    def test_approval_leaves_the_reviewed_values_on_the_record(self):
        # scripts/replay_candidates.py rebuilds from the CSV, so anything the
        # reviewer set has to be saved there, not only pushed into the venue.
        # Otherwise a rebuild silently restores venues without their hours or
        # flags, which looks like the data was never entered.
        row = self._propose()
        self._post("approve", [row["id"]], **{
            f"{row['id']}-city": "Vancouver",
            f"{row['id']}-setting": "indoor",
            f"{row['id']}-open_time": "10:00",
            f"{row['id']}-close_time": "17:00",
            f"{row['id']}-has_family_room": "on",
        })
        record = candidates.load()[0]
        self.assertEqual(record["status"], candidates.APPROVED)
        self.assertEqual(record["open_time"], "10:00")
        self.assertEqual(record["close_time"], "17:00")
        self.assertEqual(record["has_family_room"], "1")
        self.assertEqual(record["setting"], "indoor")

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
