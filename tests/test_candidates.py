"""The candidate store is the agent's memory as well as the review artifact.

The load-bearing property is that a rejected name is never proposed again: it is
what separates a loop that converges from one that spends a reviewer's capacity
re-rejecting the same places.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src import candidates


class CandidateStoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(candidates, "CANDIDATES_PATH",
                                    Path(self._tmp.name) / "venue_candidates.csv")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _add_one(self, name="Bloedel Conservatory", **fields):
        candidates.add([{"name": name, "type": "garden",
                         "source_url": "https://example.org/b", **fields}])
        return candidates.load()[-1]

    def test_a_missing_file_reads_as_empty(self):
        # A first run should need no setup step.
        self.assertEqual(candidates.load(), [])
        self.assertEqual(candidates.known_names(), set())

    def test_a_corrupt_file_reads_as_empty_rather_than_raising(self):
        candidates.CANDIDATES_PATH.write_text("\x00 not really a csv")
        self.assertIsInstance(candidates.load(), list)

    def test_proposals_land_as_pending(self):
        row = self._add_one()
        self.assertEqual(row["status"], candidates.PENDING)
        self.assertTrue(row["id"])
        self.assertTrue(row["proposed_at"])

    def test_the_agent_writes_no_flag_and_no_hours(self):
        # A web snippet cannot establish these, so they stay blank until a
        # person fills them in at review.
        row = self._add_one()
        for column in candidates.REVIEWED_COLUMNS:
            self.assertEqual(row[column], "", column)

    def test_a_duplicate_name_in_the_same_batch_is_added_once(self):
        added = candidates.add([{"name": "Same Place"}, {"name": "same place"}])
        self.assertEqual(added, 1)

    def test_a_nameless_proposal_is_dropped(self):
        self.assertEqual(candidates.add([{"name": "  "}, {"type": "park"}]), 0)

    def test_a_name_already_on_file_is_not_added_again(self):
        self._add_one("Science World")
        self.assertEqual(candidates.add([{"name": "SCIENCE WORLD"}]), 0)

    def test_a_rejected_name_is_never_proposed_again(self):
        # The whole reason the store persists decisions.
        row = self._add_one("Rejected Place")
        candidates.set_status(row["id"], candidates.REJECTED, decided_by=7)
        self.assertIn(candidates.normalize_name("Rejected Place"),
                      candidates.known_names())
        self.assertEqual(candidates.add([{"name": "Rejected Place"}]), 0)

    def test_a_name_differing_only_in_spacing_is_the_same_place(self):
        # A live run proposed "Van Dusen Botanical Garden" when the database
        # already held "VanDusen Botanical Garden". A reviewer should not have to
        # catch that.
        self._add_one("VanDusen Botanical Garden")
        self.assertEqual(candidates.add([{"name": "Van Dusen Botanical Garden"}]), 0)
        self.assertEqual(candidates.add([{"name": "vandusen  botanical-garden"}]), 0)

    def test_review_can_correct_what_the_agent_proposed(self):
        row = self._add_one()
        candidates.update(row["id"], neighbourhood="Riley Park",
                          open_time="10:00", can_eat="1")
        edited = candidates.load()[0]
        self.assertEqual(edited["neighbourhood"], "Riley Park")
        self.assertEqual(edited["open_time"], "10:00")
        self.assertEqual(edited["can_eat"], "1")

    def test_coordinates_are_not_editable(self):
        # They come from the Places API. A hand-typed coordinate is worse than a
        # missing one: it silently mis-ranks distance instead of falling back.
        row = self._add_one()
        with self.assertRaises(ValueError):
            candidates.update(row["id"], lat="49.1")

    def test_the_citation_is_not_editable(self):
        row = self._add_one()
        with self.assertRaises(ValueError):
            candidates.update(row["id"], source_url="https://elsewhere.example")

    def test_an_unknown_field_fails_loudly(self):
        row = self._add_one()
        with self.assertRaises(ValueError):
            candidates.update(row["id"], sneaky="1")

    def test_an_unknown_status_is_refused(self):
        row = self._add_one()
        with self.assertRaises(ValueError):
            candidates.set_status(row["id"], "published")

    def test_a_decision_records_who_made_it_and_when(self):
        row = self._add_one()
        candidates.set_status(row["id"], candidates.APPROVED, decided_by=3)
        decided = candidates.load()[0]
        self.assertEqual(decided["status"], candidates.APPROVED)
        self.assertEqual(decided["decided_by"], "3")
        self.assertTrue(decided["decided_at"])

    def test_loading_by_status(self):
        keep = self._add_one("Keep This")
        drop = self._add_one("Drop This")
        candidates.set_status(drop["id"], candidates.REJECTED)
        self.assertEqual([r["name"] for r in candidates.load(candidates.PENDING)],
                         ["Keep This"])
        self.assertEqual(candidates.counts(),
                         {"pending": 1, "approved": 0, "rejected": 1})

    def test_a_blank_status_reads_as_pending(self):
        # So a truncated or hand-edited file degrades to "needs review" rather
        # than to a wrong decision.
        header = ",".join(candidates.COLUMNS)
        row = ["x" if c == "id" else "" for c in candidates.COLUMNS]
        row[candidates.COLUMNS.index("name")] = "No Status"
        candidates.CANDIDATES_PATH.write_text(f"{header}\n{','.join(row)}\n")
        self.assertEqual(candidates.load()[0]["status"], candidates.PENDING)

    def test_the_reviewed_columns_match_what_the_form_asks(self):
        # If these drift, the review form offers a flag the CSV cannot hold, or
        # the CSV holds one nobody is asked about. Five of the six become
        # venue_reports on approval and only can_eat stays a column, but the CSV
        # is the reviewer's working copy and needs all six.
        from src.db import CANDIDATE_FEATURE_COLUMNS, REPORTABLE_FIELDS
        self.assertEqual(set(candidates.REVIEWED_COLUMNS)
                         - set(candidates.PREFILLED_COLUMNS),
                         set(CANDIDATE_FEATURE_COLUMNS) | set(REPORTABLE_FIELDS))

    def test_hours_are_one_pair_and_nothing_else(self):
        # There were 12 more columns here, hours by season and day type, and
        # not one was ever filled. They went with the venue_hours table: the
        # model could not express a museum closed on Mondays anyway.
        hours = [c for c in candidates.COLUMNS
                 if c.startswith(("open_", "close_")) or c == "hours_note"]
        self.assertEqual(sorted(hours), ["close_time", "hours_note", "open_time"])

    def test_the_note_is_evidence_not_a_reviewer_judgment(self):
        self.assertIn("hours_note", candidates.PROPOSED_COLUMNS)
        self.assertNotIn("hours_note", candidates.EDITABLE)


if __name__ == "__main__":
    unittest.main()
