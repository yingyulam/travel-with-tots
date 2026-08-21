import unittest
from unittest import mock

from src.agents import ReplanningAgent, ReplanningAgentError
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


class ReplanTripTest(unittest.TestCase):
    def test_adjusted_true_on_success(self):
        plan = _sample_plan()
        adjustment = {
            "stops": [plan["stops"][0],
                      {**plan["stops"][1], "reason": "adjusted", "adjusted": True}],
            "edits": [], "model": "m", "response_time": 1.0,
            "input_tokens": 1, "output_tokens": 1,
        }
        with mock.patch.object(ReplanningAgent, "adjust_replan", return_value=adjustment):
            result = replan_trip(plan=plan, situation="change_theme",
                                 current_time="11:00", theme="Rainy-day")
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
