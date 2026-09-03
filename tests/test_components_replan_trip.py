import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src.store import db, schema

from src.ai.agents import ReplanningAgent, ReplanningAgentError
from src.components.replan_trip import replan_trip


def _sample_plan():
    return {
        "label": "Outdoorsy", "blurb": "A day out.",
        "stops": [
            {"time": "9:00 AM", "kind": "activity",
             "venue": {"name": "Stanley Park Seawall", "neighbourhood": "West End",
                       "type": "park", "category": "activity",
                       "open": "06:00", "close": "22:00"},
             "reason": "kept"},
            {"time": "1:00 PM", "kind": "activity",
             "venue": {"name": "Science World", "neighbourhood": "Downtown",
                       "type": "museum", "category": "activity",
                       "open": "10:00", "close": "17:00"},
             "reason": "later stop"},
        ],
    }


class _SeededDBTest(unittest.TestCase):
    """A temp database seeded from data/venues.json.

    Needed because the planner now reads venues from SQLite. Without this these
    tests would read the developer's own data/app.db, which they never create,
    so they passed only where one already existed and failed on a fresh clone.
    Seeding from the real seed file keeps the venue set the same as production's.
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            schema.create_schema(conn)
            schema._seed_venues(conn)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)


class ReplanTripTest(_SeededDBTest):
    def test_adjusted_true_on_success(self):
        plan = _sample_plan()
        adjustment = {
            "stops": [plan["stops"][0],
                      {**plan["stops"][1], "reason": "adjusted", "adjusted": True}],
            "edits": [], "model": "m", "response_time": 1.0,
            "input_tokens": 1, "output_tokens": 1,
        }
        with mock.patch.object(ReplanningAgent, "adjust_replan", return_value=adjustment):
            result = replan_trip(plan=plan, situation="change_interest",
                                 current_time="11:00", interest=["museum"])
        self.assertTrue(result["adjusted"])
        self.assertEqual(result["stops"][1]["reason"], "adjusted")

    def test_adjusted_false_falls_back_to_unadjusted_draft(self):
        plan = _sample_plan()
        with mock.patch.object(ReplanningAgent, "adjust_replan",
                               side_effect=ReplanningAgentError("boom")):
            result = replan_trip(plan=plan, situation="weather_rain", current_time="11:00")
        self.assertFalse(result["adjusted"])
        self.assertTrue(result["stops"])

    def test_adjusted_false_on_missing_api_key(self):
        plan = _sample_plan()
        with mock.patch.object(ReplanningAgent, "adjust_replan",
                               side_effect=KeyError("OPENROUTER_API_KEY")):
            result = replan_trip(plan=plan, situation="skip_next", current_time="11:00")
        self.assertFalse(result["adjusted"])
        self.assertTrue(result["stops"])

    def test_kept_stops_untouched_real_rule_based_replan(self):
        # Real (unmocked) interactions.replan() underneath -- confirms
        # replan_trip actually calls the real rule-based logic, not a stub.
        plan = _sample_plan()
        with mock.patch.object(ReplanningAgent, "adjust_replan",
                               side_effect=ReplanningAgentError("boom")):
            result = replan_trip(plan=plan, situation="skip_next", current_time="11:00")
        # The 9:00 AM stop is before current_time -- kept exactly as it was.
        self.assertEqual(result["stops"][0]["venue"]["name"], "Stanley Park Seawall")
        self.assertEqual(result["stops"][0]["reason"], "kept")


if __name__ == "__main__":
    unittest.main()
