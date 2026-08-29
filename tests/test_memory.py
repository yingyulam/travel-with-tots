"""What the chat is allowed to remember about a parent.

A real SQLite database on a temp file, because the point of `recall` is the
shape of the rows that are actually stored, and the dirty ones are the reason it
exists: most saved trips have no naps at all, and the stop_count column still
holds legacy words.
"""

import json
import os
import tempfile
import unittest
from contextlib import closing
from datetime import date, timedelta
from unittest import mock

from src import db, memory
from src.form_helpers import DEFAULTS, MAX_AGE_YEARS

PLAN = json.dumps({"label": "Mixed", "blurb": "b", "stops": []})


def _dob(years=2, months=0):
    """A date of birth that is `years`/`months` old today, so the age a test
    asserts does not drift as the calendar moves."""
    today = date.today()
    month = today.month - months
    year = today.year - years
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, min(today.day, 28)).isoformat()


class _MemoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.parent_id = db.add_parent("p@example.com", "hash", name="P")
        self.other_id = db.add_parent("q@example.com", "hash", name="Q")

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _child(self, name="Maya", years=2, months=6, parent_id=None):
        return db.add_child(parent_id or self.parent_id, name,
                            _dob(years, months))

    def _trip(self, child_id=None, parent_id=None, **fields):
        fields.setdefault("plan_json", PLAN)
        fields.setdefault("trip_date", date.today().isoformat())
        return db.add_trip(parent_id or self.parent_id, child_id, **fields)


class NothingToRememberTest(_MemoryTest):
    def test_no_parent_recalls_nothing_and_asks_nothing(self):
        # The anonymous chat is the common case: the bubble is on every page.
        with mock.patch.object(memory, "get_children") as children:
            result = memory.recall(None)
        children.assert_not_called()
        self.assertEqual(result["remembered"], [])
        self.assertEqual(result["form"], {})
        self.assertIsNone(result["child"])

    def test_a_parent_with_nothing_stored_remembers_nothing(self):
        self.assertEqual(memory.recall(self.parent_id)["remembered"], [])

    def test_a_database_that_will_not_answer_costs_only_the_memory(self):
        # handle_message does not catch sqlite errors, so a raise here would
        # reach the route as a 500 and cost the parent their reply.
        with mock.patch.object(memory, "get_children",
                               side_effect=db.sqlite3.OperationalError("locked")):
            result = memory.recall(self.parent_id)
        self.assertEqual(result["remembered"], [])


class TheChildTest(_MemoryTest):
    def test_an_age_is_remembered_without_any_saved_trip(self):
        self._child(years=1, months=6)
        result = memory.recall(self.parent_id)
        self.assertEqual(result["form"]["age_years"], "1")
        self.assertEqual(result["form"]["age_months"], "6")
        self.assertIn("age_years", result["remembered"])
        self.assertEqual(result["child"]["name"], "Maya")

    def test_the_child_is_named_not_just_aged(self):
        # /plan recomputes the age from plan_child_id on every branch and
        # defaults to the youngest, so an age with no child attached is
        # silently replaced by a different child's.
        child_id = self._child()
        self.assertEqual(memory.recall(self.parent_id)["form"]["plan_child_id"],
                         str(child_id))

    def test_the_last_trips_child_wins_over_the_youngest(self):
        older = self._child("Sam", years=4)
        self._child("Baby", years=1)
        self._trip(child_id=older)
        self.assertEqual(memory.recall(self.parent_id)["child"]["name"], "Sam")

    def test_without_a_trip_the_youngest_child_is_chosen(self):
        # Matches resolve_plan_child, which is what /plan applies to whatever
        # this hands over, so the two cannot disagree.
        self._child("Sam", years=4)
        self._child("Baby", years=1)
        self.assertEqual(memory.recall(self.parent_id)["child"]["name"], "Baby")

    def test_a_trip_naming_no_child_falls_back_to_the_youngest(self):
        self._child("Sam", years=4)
        self._child("Baby", years=1)
        self._trip(child_id=None)
        self.assertEqual(memory.recall(self.parent_id)["child"]["name"], "Baby")


class DirtyAgesTest(_MemoryTest):
    def test_a_date_of_birth_in_the_future_is_clamped_not_negative(self):
        # compute_age floor-divides, so a future date gives (-1, 7); read_form
        # would clamp that to 0 and the chat would show an age the planner is
        # not using.
        future = (date.today() + timedelta(days=200)).isoformat()
        db.add_child(self.parent_id, "Bump", future)
        result = memory.recall(self.parent_id)
        self.assertEqual(result["form"]["age_years"], "0")
        self.assertEqual(result["form"]["age_months"], "0")

    def test_a_child_over_the_cap_is_clamped_once_here(self):
        self._child(years=6, months=7)
        result = memory.recall(self.parent_id)
        self.assertEqual(result["form"]["age_years"], str(MAX_AGE_YEARS))
        self.assertEqual(result["form"]["age_months"], "0")

    def test_one_unreadable_date_of_birth_does_not_cost_the_recall(self):
        with closing(db.connect()) as conn:
            conn.execute("INSERT INTO children (parent_id, name, date_of_birth) "
                         "VALUES (?, ?, ?)", (self.parent_id, "Broken", "10/05/2023"))
            conn.commit()
        self._child("Fine", years=2)
        self.assertEqual(memory.recall(self.parent_id)["child"]["name"], "Fine")


class TheRoutineTest(_MemoryTest):
    def test_a_saved_days_shape_comes_back(self):
        self._trip(destination="Vancouver", wake_up="06:45", bedtime="19:00",
                   stop_count="4", dining="on_the_go",
                   naps=json.dumps([{"start": "12:30", "duration_min": 45}]))
        result = memory.recall(self.parent_id)
        form = result["form"]
        self.assertEqual(form["wake_up"], "06:45")
        self.assertEqual(form["bedtime"], "19:00")
        self.assertEqual(form["stop_count"], "4")
        self.assertEqual(form["naps"], [{"start": "12:30", "duration_min": 45}])
        self.assertIn("naps", result["remembered"])

    def test_a_trip_saved_with_the_old_list_shape_still_recalls(self):
        # transit was a JSON array until the form became one question about
        # getting between stops. An old trip must still prefill something
        # usable, and the widest of several is what ticking several meant.
        self._trip(transit=json.dumps(["bus", "stroller"]))
        form = memory.recall(self.parent_id)["form"]
        self.assertEqual(form["transit"], "transit")   # bus, the wider of the two

    def test_a_trip_saved_with_the_new_shape_recalls_as_is(self):
        self._trip(transit="car")
        form = memory.recall(self.parent_id)["form"]
        self.assertEqual(form["transit"], "car")

    def test_an_unrecognised_stored_mode_falls_back(self):
        self._trip(transit=json.dumps(["stroller"]))
        form = memory.recall(self.parent_id)["form"]
        self.assertEqual(form["transit"], "walk")

    def test_the_notes_are_never_recalled(self):
        # They reach the AI adjuster's prompt, so recalling them would ship a
        # months-old note into a new model request.
        self._trip(nap_notes="she wakes when moved",
                   extra_notes="she's on antibiotics")
        result = memory.recall(self.parent_id)
        self.assertNotIn("nap_notes", result["remembered"])
        self.assertNotIn("extra_notes", result["remembered"])
        self.assertNotIn("extra_notes", result["form"])

    def test_a_timestamp_tie_is_broken_by_the_later_row(self):
        # Saving one day for several children writes a row per child with an
        # identical created_at, which is the real shape in the dev database.
        # Ordering on created_at alone leaves the winner up to row order, so the
        # id breaks the tie and the last row written wins.
        a = self._child("Sam", years=4)
        b = self._child("Baby", years=1)
        stamp = "2026-08-11 20:06:19"
        with closing(db.connect()) as conn:
            for child_id, dest in ((a, "Vancouver"), (b, "Burnaby")):
                conn.execute(
                    "INSERT INTO trips (parent_id, child_id, destination, "
                    "plan_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (self.parent_id, child_id, dest, PLAN, stamp))
            conn.commit()
        result = memory.recall(self.parent_id)
        self.assertEqual(result["child"]["name"], "Baby")
        self.assertEqual(result["form"]["destination"], "Burnaby")


class DirtyRoutineTest(_MemoryTest):
    def test_naps_null_means_unknown_not_none(self):
        # The common case: most saved rows have no naps at all.
        self._trip(destination="Vancouver")
        result = memory.recall(self.parent_id)
        self.assertNotIn("naps", result["remembered"])
        self.assertNotIn("naps", result["form"])

    def test_malformed_naps_are_dropped_without_raising(self):
        for stored in ("", "{}", "[1,2]", "not json",
                       json.dumps([{"duration_min": 30}])):
            with self.subTest(stored=stored):
                with closing(db.connect()) as conn:
                    conn.execute("DELETE FROM trips")
                    conn.commit()
                self._trip(destination="Vancouver", naps=stored)
                result = memory.recall(self.parent_id)
                self.assertNotIn("naps", result["remembered"])

    def test_a_nap_with_no_duration_still_counts(self):
        self._trip(naps=json.dumps([{"start": "13:00", "duration_min": None}]))
        form = memory.recall(self.parent_id)["form"]
        self.assertEqual(form["naps"][0]["start"], "13:00")
        self.assertGreater(form["naps"][0]["duration_min"], 0)

    def test_a_legacy_stop_count_is_dropped_not_clamped(self):
        # "balanced" clamps to 3, and offering that back as the parent's own
        # answer is exactly what the survived-unchanged rule prevents.
        self._trip(stop_count="balanced")
        result = memory.recall(self.parent_id)
        self.assertNotIn("stop_count", result["remembered"])
        self.assertNotIn("stop_count", result["form"])

    def test_a_null_column_is_not_remembered_as_a_default(self):
        self._trip(destination="Vancouver")  # transit_nap left NULL
        result = memory.recall(self.parent_id)
        self.assertNotIn("transit_nap", result["remembered"])
        self.assertEqual(result["form"]["destination"], "Vancouver")


class FreshnessTest(_MemoryTest):
    def _old_trip(self, days, **fields):
        stamp = (date.today() - timedelta(days=days)).isoformat()
        self._trip(trip_date=stamp, **fields)

    def test_an_old_days_clock_is_not_offered_back(self):
        # Sleep moves every few months at these ages, and it is what the plan
        # is shaped around, so a stale wake-up is wrong rather than merely old.
        self._old_trip(memory.STALE_AFTER_DAYS + 10, destination="Vancouver",
                       wake_up="05:30", bedtime="18:00",
                       naps=json.dumps([{"start": "12:00", "duration_min": 30}]))
        result = memory.recall(self.parent_id)
        for field in ("wake_up", "bedtime", "naps"):
            self.assertNotIn(field, result["remembered"])
        self.assertIn("destination", result["remembered"])

    def test_a_recent_days_clock_is_offered(self):
        self._old_trip(1, wake_up="05:30")
        self.assertIn("wake_up", memory.recall(self.parent_id)["remembered"])


class OwnershipTest(_MemoryTest):
    def test_one_parent_never_sees_anothers(self):
        self._child("Theirs", parent_id=self.other_id)
        self._trip(parent_id=self.other_id, destination="Richmond")
        self.assertEqual(memory.recall(self.parent_id)["remembered"], [])
        self.assertIsNone(memory.recall(self.parent_id)["child"])

    def test_the_result_survives_being_serialised(self):
        # The whole reply goes through jsonify, and get_children returns
        # sqlite3.Row objects, so one leaked row is a 500 at the route.
        self._child()
        self._trip(destination="Vancouver")
        json.dumps(memory.recall(self.parent_id))


class ShapeTest(_MemoryTest):
    def test_remembered_names_only_real_form_fields(self):
        self._child()
        self._trip(destination="Vancouver", wake_up="07:15")
        result = memory.recall(self.parent_id)
        for field in result["remembered"]:
            self.assertIn(field, DEFAULTS, f"{field} is not a form field")
        self.assertNotIn("nap_start", result["remembered"])
        self.assertNotIn("plan_child_id", result["remembered"])

    def test_the_form_holds_only_what_was_remembered(self):
        self._trip(destination="Vancouver")
        result = memory.recall(self.parent_id)
        self.assertEqual(set(result["form"]), set(result["remembered"]))


if __name__ == "__main__":
    unittest.main()
