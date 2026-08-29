import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from src import results


class ResultsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(results, "RESULTS_PATH", Path(self._tmp.name) / "results.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_result_defaults_to_chatbot_kind(self):
        entry = results.save_result(
            question="q", response="r", rating="up", model="m",
            response_time=1.0, input_tokens=10, output_tokens=5)
        self.assertEqual(entry["kind"], "chatbot")

    def test_get_results_and_stats_filter_by_kind(self):
        results.save_result(question="q1", response="r1", rating="up", model="m",
                             response_time=1.0, input_tokens=1, output_tokens=1, kind="chatbot")
        results.save_result(question="q2", response="r2", rating="down", model="m",
                             response_time=1.0, input_tokens=1, output_tokens=1, kind="plan")
        results.save_result(question="q3", response="r3", rating="up", model="m",
                             response_time=1.0, input_tokens=1, output_tokens=1, kind="plan")

        chatbot_results = results.get_results("chatbot")
        plan_results = results.get_results("plan")
        self.assertEqual(len(chatbot_results), 1)
        self.assertEqual(chatbot_results[0]["question"], "q1")
        self.assertEqual(len(plan_results), 2)
        self.assertEqual({r["question"] for r in plan_results}, {"q2", "q3"})

        chatbot_stats = results.get_stats("chatbot")
        plan_stats = results.get_stats("plan")
        self.assertEqual(chatbot_stats, {"up": 1, "down": 0, "total": 1, "percent_positive": 100.0})
        self.assertEqual(plan_stats, {"up": 1, "down": 1, "total": 2, "percent_positive": 50.0})

    def test_legacy_record_without_kind_counts_as_chatbot(self):
        # Simulate a pre-existing record written before "kind" existed.
        legacy_entry = {
            "id": "legacy", "question": "old question", "response": "old response",
            "rating": "up", "model": "m", "timestamp": "2020-01-01T00:00:00+00:00",
            "response_time": 1.0, "input_tokens": 1, "output_tokens": 1,
        }
        results.RESULTS_PATH.write_text(json.dumps([legacy_entry]))

        self.assertEqual(len(results.get_results("chatbot")), 1)
        self.assertEqual(results.get_results("chatbot")[0]["id"], "legacy")
        self.assertEqual(len(results.get_results("plan")), 0)
        self.assertEqual(results.get_stats("chatbot")["total"], 1)
        self.assertEqual(results.get_stats("plan")["total"], 0)

    def test_plan_question_response_are_humanized(self):
        question = json.dumps({
            "destination": "Vancouver", "age_years": "2", "age_months": "0",
            "stop_count": "3", "dining": "dine_out", "transit": "walk",
            "features": ["kid_friendly"], "bedtime": "19:30",
        })
        response = json.dumps({
            "label": "Outdoorsy", "blurb": "A day out.",
            "stops": [{"time": "9:00 AM", "kind": "activity", "reason": "fits",
                       "venue": {"name": "Stanley Park", "neighbourhood": "West End"}}],
        })
        results.save_result(question=question, response=response, rating="up",
                             model="m", response_time=1.0, input_tokens=1,
                             output_tokens=1, kind="plan")
        entry = results.get_results("plan")[0]
        self.assertIn("Destination: Vancouver", entry["question_display"])
        self.assertIn("Outdoorsy", entry["response_display"])
        self.assertIn("Stanley Park (West End)", entry["response_display"])
        # Raw fields stay untouched, still the source of truth on disk.
        self.assertEqual(entry["question"], question)
        self.assertEqual(entry["response"], response)

    def test_replan_question_response_are_humanized(self):
        question = json.dumps({
            "situation": "finished_early", "current_time": "13:00",
            "destination": "Vancouver", "age_months": 24,
            "plan": {"label": "Outdoorsy"},
        })
        response = json.dumps({
            "label": "Outdoorsy - AI replan from 1:00 PM",
            "blurb": "AI-replanned after finishing early.",
            "stops": [{"time": "1:00 PM", "kind": "activity", "reason": "nearby",
                       "venue": {"name": "Science World", "neighbourhood": "False Creek"}}],
        })
        results.save_result(question=question, response=response, rating="down",
                             model="m", response_time=1.0, input_tokens=1,
                             output_tokens=1, kind="replan")
        entry = results.get_results("replan")[0]
        self.assertIn("finished_early", entry["question_display"])
        self.assertIn("Replanning from: Outdoorsy", entry["question_display"])
        self.assertIn("Science World (False Creek)", entry["response_display"])

    def test_chatbot_display_fields_equal_raw_text(self):
        results.save_result(question="How do I save a plan?", response="Tap Save.",
                             rating="up", model="m", response_time=1.0,
                             input_tokens=1, output_tokens=1, kind="chatbot")
        entry = results.get_results("chatbot")[0]
        self.assertEqual(entry["question_display"], "How do I save a plan?")
        self.assertEqual(entry["response_display"], "Tap Save.")

    def test_malformed_plan_json_falls_back_to_raw_text(self):
        results.save_result(question="not json", response="also not json",
                             rating="up", model="m", response_time=1.0,
                             input_tokens=1, output_tokens=1, kind="plan")
        entry = results.get_results("plan")[0]
        self.assertEqual(entry["question_display"], "not json")
        self.assertEqual(entry["response_display"], "also not json")

    def test_percent_positive_zero_when_no_ratings_for_kind(self):
        self.assertEqual(results.get_stats("chatbot"), {"up": 0, "down": 0, "total": 0, "percent_positive": 0})
        self.assertEqual(results.get_stats("plan"), {"up": 0, "down": 0, "total": 0, "percent_positive": 0})

        # Only rate "chatbot" -- "plan" should still be all-zero, independent.
        results.save_result(question="q", response="r", rating="up", model="m",
                             response_time=1.0, input_tokens=1, output_tokens=1, kind="chatbot")
        self.assertEqual(results.get_stats("plan"), {"up": 0, "down": 0, "total": 0, "percent_positive": 0})


if __name__ == "__main__":
    unittest.main()
