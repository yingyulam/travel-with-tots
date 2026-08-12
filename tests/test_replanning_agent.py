import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")

from src.agents import ReplanningAgent, ReplanningAgentError


def _venue(id, name, neighbourhood="Downtown", **overrides):
    base = {
        "id": id, "name": name, "category": "activity", "type": "park",
        "neighbourhood": neighbourhood, "city": "Vancouver",
        "open_time": "08:00", "close_time": "18:00",
        "min_age_months": 0, "max_age_months": 60,
        "nap_friendly": False, "can_eat": False, "kid_friendly": True,
        "has_family_room": False, "has_nursing_room": False,
        "stroller_accessible": True,
    }
    base.update(overrides)
    return base


def _kept_stop(time, venue):
    return {"time": time, "kind": "activity", "venue": venue, "reason": "kept"}


class ReplanningAgentTest(unittest.TestCase):
    def setUp(self):
        self.agent = ReplanningAgent()
        self.candidates = [_venue(1, "Aquarium"), _venue(2, "Museum")]

    def _current_plan(self, kept_time="10:00 AM", kept_venue_name="Old Stop"):
        return {
            "label": "Outdoorsy",
            "blurb": "A day out.",
            "stops": [
                _kept_stop(kept_time, _venue(99, kept_venue_name, neighbourhood="Downtown")),
            ],
        }

    def _good_reply(self, venue_id=1, time="2:00 PM"):
        return (f'{{"stops": [{{"venue_id": {venue_id}, "time": "{time}", '
                f'"reason": "fits", "is_nap": false, "is_meal": false}}]}}')

    def test_happy_path_never_mutates_current_plan(self):
        current_plan = self._current_plan()
        original_copy = {**current_plan, "stops": [dict(s) for s in current_plan["stops"]]}

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter",
                        return_value=(self._good_reply(), {"prompt_tokens": 10, "completion_tokens": 5}, 1.0)):
            result = self.agent.replan_day(
                "running_behind", current_plan, current_time="13:00",
                destination="Vancouver", age_months=30, minutes=45)

        self.assertEqual(result["source"], "ai")
        self.assertEqual(len(result["stops"]), 2)  # kept + 1 new
        self.assertEqual(result["stops"][0]["reason"], "kept")
        self.assertEqual(result["stops"][1]["venue"]["name"], "Aquarium")
        # current_plan itself must be untouched.
        self.assertEqual(current_plan, original_copy)

    def test_near_neighbourhood_from_last_kept_stop(self):
        current_plan = self._current_plan()
        captured = {}

        def fake_get_candidates(*args, **kwargs):
            captured.update(kwargs)
            return self.candidates

        with mock.patch("src.agents.db.get_candidate_venues", side_effect=fake_get_candidates), \
             mock.patch("src.agents._call_openrouter",
                        return_value=(self._good_reply(), {}, 1.0)):
            self.agent.replan_day("skip_next", current_plan, current_time="13:00",
                                   destination="Vancouver", age_months=30)
        self.assertEqual(captured["near_neighbourhood"], "Downtown")

        # No kept stops (situation fires before the day starts) -> no narrowing.
        empty_plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "3:00 PM", "kind": "activity", "venue": None, "reason": "r"}]}
        with mock.patch("src.agents.db.get_candidate_venues", side_effect=fake_get_candidates), \
             mock.patch("src.agents._call_openrouter",
                        return_value=(self._good_reply(), {}, 1.0)):
            self.agent.replan_day("skip_next", empty_plan, current_time="13:00",
                                   destination="Vancouver", age_months=30)
        self.assertIsNone(captured["near_neighbourhood"])

    def test_dedup_by_name_not_id_for_rule_based_venue(self):
        # A rule-based-origin venue has no "id" key at all.
        rule_based_venue = {"name": "Aquarium", "neighbourhood": "Downtown",
                             "category": "activity", "type": "park"}
        current_plan = {
            "label": "P", "blurb": "b",
            "stops": [_kept_stop("10:00 AM", rule_based_venue)],
        }
        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter",
                        return_value=(self._good_reply(venue_id=2), {}, 1.0)):
            result = self.agent.replan_day(
                "skip_next", current_plan, current_time="13:00",
                destination="Vancouver", age_months=30)
        # venue_id 1 ("Aquarium") should have been excluded by name; only
        # venue_id 2 ("Museum") was offered, and that's what got cited.
        self.assertEqual(result["stops"][1]["venue"]["name"], "Museum")

    def test_retry_then_raise(self):
        current_plan = self._current_plan()
        calls = []

        def fake_call(messages, model):
            calls.append(messages)
            return '{"stops": [{"venue_id": 999, "time": "2:00 PM", "reason": "r", "is_nap": false, "is_meal": false}]}', {}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            with self.assertRaises(ReplanningAgentError):
                self.agent.replan_day("skip_next", current_plan, current_time="13:00",
                                       destination="Vancouver", age_months=30)
        self.assertEqual(len(calls), 2)

    def test_retry_then_succeed_sums_tokens(self):
        current_plan = self._current_plan()
        calls = []

        def fake_call(messages, model):
            calls.append(messages)
            if len(calls) == 1:
                return ('{"stops": [{"venue_id": 999, "time": "2:00 PM", "reason": "r", '
                        '"is_nap": false, "is_meal": false}]}',
                        {"prompt_tokens": 100, "completion_tokens": 10}, 1.0)
            return (self._good_reply(), {"prompt_tokens": 50, "completion_tokens": 5}, 1.0)

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            result = self.agent.replan_day("skip_next", current_plan, current_time="13:00",
                                            destination="Vancouver", age_months=30)
        self.assertEqual(result["input_tokens"], 150)
        self.assertEqual(result["output_tokens"], 15)

    def test_meal_cap_rejects_second_meal(self):
        plan_with_meal = {
            "label": "P", "blurb": "b",
            "stops": [{"time": "10:00 AM", "kind": "meal",
                       "venue": _venue(99, "Old Lunch"), "reason": "kept meal"}],
        }
        reply = ('{"stops": [{"venue_id": 1, "time": "2:00 PM", "reason": "r", '
                 '"is_nap": false, "is_meal": true}]}')
        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", return_value=(reply, {}, 1.0)):
            with self.assertRaises(ReplanningAgentError):
                self.agent.replan_day("skip_next", plan_with_meal, current_time="13:00",
                                       destination="Vancouver", age_months=30, dining="dine_out")

    def test_anchor_floor_rejects_early_start(self):
        current_plan = self._current_plan()
        # nap_happened with a 60-min nap starting at 13:00 -> anchor is 14:00;
        # a reply starting at 13:30 must be rejected.
        reply = ('{"stops": [{"venue_id": 1, "time": "1:30 PM", "reason": "r", '
                 '"is_nap": false, "is_meal": false}]}')
        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", return_value=(reply, {}, 1.0)):
            with self.assertRaises(ReplanningAgentError):
                self.agent.replan_day("nap_happened", current_plan, current_time="13:00",
                                       destination="Vancouver", age_months=30, minutes=60)

    def test_no_candidates_raises_without_calling_model(self):
        current_plan = self._current_plan()
        with mock.patch("src.agents.db.get_candidate_venues", return_value=[]), \
             mock.patch("src.agents._call_openrouter") as mock_call:
            with self.assertRaises(ReplanningAgentError):
                self.agent.replan_day("skip_next", current_plan, current_time="13:00",
                                       destination="Vancouver", age_months=30)
            mock_call.assert_not_called()


class ReplanAiRouteTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()

    def test_missing_fields_returns_400(self):
        resp = self.client.post("/replan/ai", json={"plan": {"stops": []}})
        self.assertEqual(resp.status_code, 400)

    def test_replanning_agent_error_returns_502(self):
        with mock.patch.object(self.app_module.ReplanningAgent, "replan_day",
                                side_effect=self.app_module.ReplanningAgentError("nope")):
            resp = self.client.post("/replan/ai", json={
                "plan": {"stops": []}, "situation": "skip_next", "current_time": "13:00"})
        self.assertEqual(resp.status_code, 502)

    def test_request_exception_returns_502(self):
        import requests
        with mock.patch.object(self.app_module.ReplanningAgent, "replan_day",
                                side_effect=requests.exceptions.RequestException("down")):
            resp = self.client.post("/replan/ai", json={
                "plan": {"stops": []}, "situation": "skip_next", "current_time": "13:00"})
        self.assertEqual(resp.status_code, 502)

    def test_key_error_returns_500(self):
        with mock.patch.object(self.app_module.ReplanningAgent, "replan_day",
                                side_effect=KeyError("OPENROUTER_API_KEY")):
            resp = self.client.post("/replan/ai", json={
                "plan": {"stops": []}, "situation": "skip_next", "current_time": "13:00"})
        self.assertEqual(resp.status_code, 500)

    def test_success_echoes_result(self):
        canned = {"label": "P", "blurb": "b", "stops": [], "source": "ai"}
        with mock.patch.object(self.app_module.ReplanningAgent, "replan_day", return_value=canned):
            resp = self.client.post("/replan/ai", json={
                "plan": {"stops": []}, "situation": "skip_next", "current_time": "13:00"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), canned)


if __name__ == "__main__":
    unittest.main()
