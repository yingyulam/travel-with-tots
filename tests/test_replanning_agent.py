import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")

from src.agents import (
    PLAN_EDITS_RESPONSE_FORMAT,
    ReplanningAgent,
    ReplanningAgentError,
)


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


def _draft_venue(name, **overrides):
    """A rule-based-origin venue in a replan draft -- no numeric id, matching
    what interactions.replan() actually produces."""
    base = {"name": name, "neighbourhood": "Downtown", "type": "park"}
    base.update(overrides)
    return base


def _draft_stop(time, kind, venue, reason="stop"):
    return {"time": time, "kind": kind, "venue": venue, "reason": reason}


class AdjustReplanTest(unittest.TestCase):
    def setUp(self):
        self.agent = ReplanningAgent()
        self.candidates = [_venue(7, "New Museum", type="museum")]
        self.draft = {
            "stops": [
                _draft_stop("10:00 AM", "activity", _draft_venue("Old Park"), reason="kept"),
                _draft_stop("2:00 PM", "activity", _draft_venue("Afternoon Stop"), reason="remaining"),
            ],
        }

    def _good_reply(self):
        return ('{"edits": [{"current_venue_name": "Afternoon Stop", "new_venue_id": 7, '
                '"new_time": null, "reason": "better fit"}]}')

    def test_calls_openrouter_with_the_edits_schema_and_applies_result(self):
        captured = {}

        def fake_call(messages, model, response_format=None):
            captured["response_format"] = response_format
            return self._good_reply(), {}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            result = self.agent.adjust_replan(
                self.draft, current_time="13:00", destination="Vancouver", age_months=30)

        self.assertEqual(captured["response_format"], PLAN_EDITS_RESPONSE_FORMAT)
        touched = next(s for s in result["stops"] if s["venue"]["name"] == "New Museum")
        self.assertTrue(touched["adjusted"])
        # The kept stop must survive untouched, in first position.
        self.assertEqual(result["stops"][0]["venue"]["name"], "Old Park")
        self.assertNotIn("adjusted", result["stops"][0])

    def test_kept_stop_cannot_be_targeted(self):
        # "Old Park" already happened (10:00 AM, before the 13:00 current
        # time) -- it's not even offered to the validator, so an edit
        # naming it fails exactly like naming a nonexistent venue.
        reply = ('{"edits": [{"current_venue_name": "Old Park", "new_venue_id": 7, '
                 '"new_time": null, "reason": "r"}]}')
        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", return_value=(reply, {}, 1.0)):
            with self.assertRaises(ReplanningAgentError):
                self.agent.adjust_replan(
                    self.draft, current_time="13:00", destination="Vancouver", age_months=30)

    def test_empty_edits_leaves_draft_unchanged(self):
        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", return_value=('{"edits": []}', {}, 1.0)):
            result = self.agent.adjust_replan(
                self.draft, current_time="13:00", destination="Vancouver", age_months=30)
        self.assertEqual(result["stops"][1]["venue"]["name"], "Afternoon Stop")
        self.assertEqual(result["edits"], [])

    def test_retry_then_raise_on_repeated_invalid_edits(self):
        calls = []

        def fake_call(messages, model, response_format=None):
            calls.append(1)
            return ('{"edits": [{"current_venue_name": "Nonexistent", "new_venue_id": 7, '
                    '"new_time": null, "reason": "r"}]}'), {}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            with self.assertRaises(ReplanningAgentError):
                self.agent.adjust_replan(
                    self.draft, current_time="13:00", destination="Vancouver", age_months=30)
        self.assertEqual(len(calls), 2)

    def test_near_neighbourhood_from_last_kept_stop(self):
        captured = {}

        def fake_get_candidates(*args, **kwargs):
            captured.update(kwargs)
            return self.candidates

        with mock.patch("src.agents.db.get_candidate_venues", side_effect=fake_get_candidates), \
             mock.patch("src.agents._call_openrouter", return_value=('{"edits": []}', {}, 1.0)):
            self.agent.adjust_replan(
                self.draft, current_time="13:00", destination="Vancouver", age_months=30)
        self.assertEqual(captured["near_neighbourhood"], "Downtown")

    def test_stale_adjusted_flag_on_kept_stop_is_stripped(self):
        draft = {
            "stops": [
                _draft_stop("10:00 AM", "activity", _draft_venue("Old Park"), reason="kept"),
            ],
        }
        draft["stops"][0]["adjusted"] = True
        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", return_value=('{"edits": []}', {}, 1.0)):
            result = self.agent.adjust_replan(
                draft, current_time="13:00", destination="Vancouver", age_months=30)
        self.assertNotIn("adjusted", result["stops"][0])


class ReplanAdjustRouteTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()

    def test_missing_fields_returns_400(self):
        resp = self.client.post("/replan/adjust", json={"plan": {"stops": []}})
        self.assertEqual(resp.status_code, 400)

    def test_adjuster_error_falls_back_to_unadjusted_draft(self):
        draft = {"label": "P", "blurb": "b", "from_time": "1:00 PM", "stops": []}
        with mock.patch.object(self.app_module, "replan", return_value=draft), \
             mock.patch.object(ReplanningAgent, "adjust_replan",
                                side_effect=ReplanningAgentError("nope")):
            resp = self.client.post("/replan/adjust", json={
                "plan": {"stops": []}, "situation": "skip_next", "current_time": "13:00"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["adjusted"])
        self.assertEqual(body["stops"], [])

    def test_success_marks_adjusted_true(self):
        draft = {"label": "P", "blurb": "b", "from_time": "1:00 PM", "stops": []}
        adjustment = {"stops": [{"time": "2:00 PM", "kind": "activity", "venue": None, "reason": "r"}],
                      "edits": [], "model": "m", "response_time": 1.0,
                      "input_tokens": 1, "output_tokens": 1}
        with mock.patch.object(self.app_module, "replan", return_value=draft), \
             mock.patch.object(ReplanningAgent, "adjust_replan", return_value=adjustment):
            resp = self.client.post("/replan/adjust", json={
                "plan": {"stops": []}, "situation": "skip_next", "current_time": "13:00"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["adjusted"])
        self.assertEqual(body["stops"], adjustment["stops"])


if __name__ == "__main__":
    unittest.main()
