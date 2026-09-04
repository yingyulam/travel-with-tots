import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src.store import db, schema

from src.ai.agents import PlanningAgent, PlanningAgentError
from src.components.plan_trip import plan_trip


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


class PlanTripTest(_VenueDBTest):
    def test_adjusted_true_on_success(self):
        with mock.patch.object(PlanningAgent, "adjust_plan", return_value={
                "stops": [{"time": "9:00 AM", "kind": "activity", "venue": None,
                           "reason": "adjusted", "adjusted": True}],
                "edits": [], "model": "m", "response_time": 1.0,
                "input_tokens": 1, "output_tokens": 1}):
            result = plan_trip(destination="Vancouver", age_months=30, stop_count=3)
        self.assertTrue(result["adjusted"])
        self.assertEqual(result["stops"][0]["reason"], "adjusted")
        self.assertEqual(result["source"], "rule")

    def test_adjusted_false_falls_back_to_unadjusted_draft(self):
        with mock.patch.object(PlanningAgent, "adjust_plan",
                               side_effect=PlanningAgentError("boom")):
            result = plan_trip(destination="Vancouver", age_months=30, stop_count=3)
        self.assertFalse(result["adjusted"])
        self.assertTrue(result["stops"])  # still a usable, non-empty plan

    def test_adjusted_false_on_missing_api_key(self):
        with mock.patch.object(PlanningAgent, "adjust_plan", side_effect=KeyError("OPENROUTER_API_KEY")):
            result = plan_trip(destination="Vancouver", age_months=30, stop_count=3)
        self.assertFalse(result["adjusted"])
        self.assertTrue(result["stops"])

    def test_realistic_stop_count_applies_for_young_child(self):
        # Real (unmocked) rule-based draft underneath -- confirms plan_trip
        # actually calls the real generate_plans, not a stub.
        with mock.patch.object(PlanningAgent, "adjust_plan",
                               side_effect=PlanningAgentError("boom")):
            result = plan_trip(destination="Vancouver", age_months=12, stop_count=6)
        non_meal = [s for s in result["stops"] if s["kind"] != "meal"]
        self.assertLess(len(non_meal), 6)  # capped down for a 1-year-old
        self.assertIn("more realistic", result["blurb"])


if __name__ == "__main__":
    unittest.main()
