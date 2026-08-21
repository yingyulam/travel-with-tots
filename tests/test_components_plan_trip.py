import unittest
from unittest import mock

from src.agents import PlanningAgent, PlanningAgentError
from src.components.plan_trip import plan_trip


class PlanTripTest(unittest.TestCase):
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
