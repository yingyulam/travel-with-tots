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


# A pool wide enough that a six-stop day would be satisfiable if the age cap
# did not apply, which is what test_realistic_stop_count_applies_for_young_child
# needs to mean anything. Hours on every row, because a venue without them is
# not schedulable; coordinates because the travel limit filters on them; one
# can_eat for the lunch block; a mix of settings and nap-friendly types
# (data_loader.NAP_FRIENDLY_TYPES) so the draft has real choices to make.
_POOL = (
    ("Harbour Museum",   "museum",  "indoor",  0, 49.2860, -123.1120),
    ("Science Centre",   "museum",  "indoor",  1, 49.2735, -123.1035),
    ("Cedar Park",       "park",    "outdoor", 0, 49.2790, -123.1170),
    ("Fountain Garden",  "garden",  "outdoor", 0, 49.2700, -123.1250),
    ("English Beach",    "beach",   "outdoor", 0, 49.2865, -123.1430),
    ("Seaside Walk",     "seawall", "outdoor", 0, 49.2800, -123.1300),
    ("Central Mall",     "mall",    "indoor",  1, 49.2820, -123.1180),
    ("Riverside Market", "market",  "both",    1, 49.2715, -123.1085),
)


class _VenueDBTest(unittest.TestCase):
    """A temp database with a venue pool, built row by row.

    The planner reads venues from SQLite, so these tests need a real database
    rather than a patched get_venues -- exercising that read path is the point.
    They used to call schema._seed_venues to fill it, which stopped existing
    when startup seeding was retired; the pool below is explicit instead, and
    does not drift when data/venues.json is edited.

    addCleanup rather than tearDown: a setUp that raises never reaches tearDown,
    so a DB_PATH patch started here would leak into every later test in the
    process. That is exactly how retiring the seeder turned two broken fixtures
    into 45 unrelated failures.
    """

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(os.unlink, self.db_path)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)
            with conn:
                for rank, (name, kind, setting, can_eat, lat, lng) in enumerate(_POOL):
                    conn.execute(
                        "INSERT INTO venues (name, source, city, neighbourhood, "
                        "type, setting, can_eat, open_time, close_time, lat, lng, "
                        "seed_rank) VALUES (?, 'curated', 'Vancouver', 'Downtown', "
                        "?, ?, ?, '09:00', '18:00', ?, ?, ?)",
                        (name, kind, setting, can_eat, lat, lng, rank))


class ReplanTripTest(_VenueDBTest):
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
