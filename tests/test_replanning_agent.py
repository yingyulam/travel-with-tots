import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import sys
import unittest
from unittest import mock

sys.path.insert(0, ".")

from src.ai.agents import (
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
    base = {"name": name, "neighbourhood": "Downtown", "type": "park",
            "open": "06:00", "close": "23:00"}
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

        with mock.patch("src.ai.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.ai.agents.call_openrouter", side_effect=fake_call):
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
        with mock.patch("src.ai.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.ai.agents.call_openrouter", return_value=(reply, {}, 1.0)):
            with self.assertRaises(ReplanningAgentError):
                self.agent.adjust_replan(
                    self.draft, current_time="13:00", destination="Vancouver", age_months=30)

    def test_empty_edits_leaves_draft_unchanged(self):
        with mock.patch("src.ai.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.ai.agents.call_openrouter", return_value=('{"edits": []}', {}, 1.0)):
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

        with mock.patch("src.ai.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.ai.agents.call_openrouter", side_effect=fake_call):
            with self.assertRaises(ReplanningAgentError):
                self.agent.adjust_replan(
                    self.draft, current_time="13:00", destination="Vancouver", age_months=30)
        self.assertEqual(len(calls), 2)

    def test_near_neighbourhood_from_last_kept_stop(self):
        captured = {}

        def fake_get_candidates(*args, **kwargs):
            captured.update(kwargs)
            return self.candidates

        with mock.patch("src.ai.agents.db.get_candidate_venues", side_effect=fake_get_candidates), \
             mock.patch("src.ai.agents.call_openrouter", return_value=('{"edits": []}', {}, 1.0)):
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
        with mock.patch("src.ai.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.ai.agents.call_openrouter", return_value=('{"edits": []}', {}, 1.0)):
            result = self.agent.adjust_replan(
                draft, current_time="13:00", destination="Vancouver", age_months=30)
        self.assertNotIn("adjusted", result["stops"][0])


class ReplanAdjustRouteTest(unittest.TestCase):
    """/replan/adjust calls replan_trip, which looks `replan` up in its own
    module. Patching it on app hits the plain /replan route's import instead
    and does nothing, which is how these tests used to pass: the draft they
    injected had no stops, and the real replan also returns none for an empty
    plan, so the mock and reality gave the same answer. The draft below carries
    a recognisable stop, so a mock that is not wired up now fails."""

    DRAFT_STOP = {"time": "1:30 PM", "kind": "activity", "venue": None,
                  "reason": "from the rule-based draft"}

    def setUp(self):
        self.client = __import__("app").app.test_client()

    def _draft(self):
        return {"label": "P", "blurb": "b", "from_time": "1:00 PM",
                "stops": [dict(self.DRAFT_STOP)]}

    def _post(self):
        return self.client.post("/replan/adjust", json={
            "plan": {"stops": []}, "situation": "skip_next", "current_time": "13:00"})

    def test_missing_fields_returns_400(self):
        resp = self.client.post("/replan/adjust", json={"plan": {"stops": []}})
        self.assertEqual(resp.status_code, 400)

    def test_adjuster_error_falls_back_to_unadjusted_draft(self):
        with mock.patch("src.components.replan_trip.replan",
                        return_value=self._draft()), \
             mock.patch.object(ReplanningAgent, "adjust_replan",
                                side_effect=ReplanningAgentError("nope")):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertFalse(body["adjusted"])
        self.assertEqual(body["stops"], [self.DRAFT_STOP])

    def test_success_marks_adjusted_true(self):
        adjustment = {"stops": [{"time": "2:00 PM", "kind": "activity", "venue": None, "reason": "r"}],
                      "edits": [], "model": "m", "response_time": 1.0,
                      "input_tokens": 1, "output_tokens": 1}
        with mock.patch("src.components.replan_trip.replan",
                        return_value=self._draft()), \
             mock.patch.object(ReplanningAgent, "adjust_replan", return_value=adjustment):
            resp = self._post()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body["adjusted"])
        self.assertEqual(body["stops"], adjustment["stops"])

    def test_the_draft_reaches_the_adjuster(self):
        # The mock being wired to the right module is the whole point, so
        # assert the adjuster was handed the draft rather than something else.
        with mock.patch("src.components.replan_trip.replan",
                        return_value=self._draft()), \
             mock.patch.object(ReplanningAgent, "adjust_replan",
                                side_effect=ReplanningAgentError("nope")) as adjuster:
            self._post()
        self.assertEqual(adjuster.call_args.args[0]["stops"], [self.DRAFT_STOP])


if __name__ == "__main__":
    unittest.main()


class ReplanContextTest(unittest.TestCase):
    """What the adjuster is told. It used to be given the situation's label but
    neither the duration nor what was asked for, so it could quietly undo the parent's
    explicit request while staying inside its own nudge allowance."""

    def setUp(self):
        self.agent = ReplanningAgent()
        self.candidates = [_venue(7, "New Museum", type="museum")]
        self.draft = {
            "stops": [
                _draft_stop("10:00 AM", "activity", _draft_venue("Old Park"), reason="kept"),
                _draft_stop("2:00 PM", "activity", _draft_venue("Afternoon Stop"), reason="ahead"),
            ],
        }

    def _prompt(self, **kwargs):
        captured = {}

        def fake_call(messages, model, response_format=None):
            captured["prompt"] = messages[0]["content"]
            return '{"edits": []}', {}, 1.0

        with mock.patch("src.ai.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.ai.agents.call_openrouter", side_effect=fake_call):
            self.agent.adjust_replan(
                self.draft, current_time="13:00", destination="Vancouver",
                age_months=30, **kwargs)
        return captured["prompt"]

    def test_a_nap_length_reaches_the_prompt(self):
        prompt = self._prompt(situation="nap_happened", minutes=180)
        self.assertIn("180 minutes", prompt)

    def test_a_stay_longer_tells_it_not_to_pull_stops_back(self):
        prompt = self._prompt(situation="running_behind", minutes=90)
        self.assertIn("90 minutes", prompt)
        self.assertIn("Do not pull stops back", prompt)

    def test_what_the_parent_now_wants_reaches_the_prompt(self):
        prompt = self._prompt(situation="change_interest", interest=["museum"])
        self.assertIn("museum", prompt)

    def test_rain_says_the_venue_must_work_indoors(self):
        prompt = self._prompt(situation="weather_rain")
        self.assertIn("rain", prompt.lower())

    def test_no_duration_is_stated_plainly(self):
        prompt = self._prompt(situation="skip_next")
        self.assertIn("did not give a duration", prompt)

    def test_the_note_only_situation_carries_no_change_or_duration(self):
        prompt = self._prompt(situation="something_else")
        self.assertIn("did not give a duration", prompt)
        self.assertIn("No change of plan", prompt)


class ReplanPastTimeGuardTest(unittest.TestCase):
    """An edit may not move a stop to a time already gone. _apply_plan_edits
    re-sorts by time, so a stop nudged before `now` would be filed among the
    stops already done, where the page renders it as finished."""

    def setUp(self):
        self.agent = ReplanningAgent()
        self.candidates = [_venue(7, "New Museum", type="museum")]
        self.draft = {
            "stops": [
                _draft_stop("10:00 AM", "activity", _draft_venue("Old Park"), reason="kept"),
                _draft_stop("1:30 PM", "activity", _draft_venue("Afternoon Stop"), reason="ahead"),
            ],
        }

    def _run(self, new_time):
        reply = ('{"edits": [{"current_venue_name": "Afternoon Stop", '
                 f'"new_venue_id": 7, "new_time": "{new_time}", "reason": "r"}}]}}')
        with mock.patch("src.ai.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.ai.agents.call_openrouter",
                        return_value=(reply, {}, 1.0)):
            return self.agent.adjust_replan(
                self.draft, current_time="13:00", destination="Vancouver",
                age_months=30, situation="skip_next")

    def test_a_time_before_now_is_rejected(self):
        # 12:45 is inside the 60-minute nudge allowance from 1:30 PM, so only
        # the current-time check can catch it.
        with self.assertRaises(ReplanningAgentError):
            self._run("12:45 PM")

    def test_a_time_at_now_is_rejected(self):
        with self.assertRaises(ReplanningAgentError):
            self._run("1:00 PM")

    def test_a_time_after_now_is_allowed(self):
        result = self._run("1:45 PM")
        touched = next(s for s in result["stops"] if s["venue"]["name"] == "New Museum")
        self.assertEqual(touched["time"], "1:45 PM")
