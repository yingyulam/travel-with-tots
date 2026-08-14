import unittest
from unittest import mock

from src.agents import (
    PLAN_EDITS_RESPONSE_FORMAT,
    STOPS_RESPONSE_FORMAT,
    PlanningAgent,
    PlanningAgentError,
    _apply_plan_edits,
    _validate_plan_edits,
)


def _venue(id, name, **overrides):
    base = {
        "id": id, "name": name, "category": "activity", "type": "park",
        "neighbourhood": "Downtown", "city": "Vancouver",
        "open_time": "08:00", "close_time": "22:00",
        "min_age_months": 0, "max_age_months": 60,
        "nap_friendly": False, "can_eat": False, "kid_friendly": True,
        "has_family_room": False, "has_nursing_room": False,
        "stroller_accessible": True,
    }
    base.update(overrides)
    return base


def _draft_venue(name, **overrides):
    """A rule-based-origin venue in a draft plan -- no numeric id, matching
    what itinerary.py's generate_plans actually produces."""
    base = {"name": name, "neighbourhood": "Downtown", "type": "park"}
    base.update(overrides)
    return base


def _draft_stop(time, kind, venue, reason="kept"):
    return {"time": time, "kind": kind, "venue": venue, "reason": reason}


class PlanningAgentStructuredOutputTest(unittest.TestCase):
    def setUp(self):
        self.agent = PlanningAgent()
        self.candidates = [_venue(1, "Aquarium"), _venue(2, "Museum")]

    def _good_reply(self):
        return ('{"stops": [{"venue_id": 1, "time": "9:00 AM", "reason": "fits", '
                '"is_nap": false, "is_meal": false}]}')

    def test_calls_openrouter_with_the_shared_stops_schema(self):
        captured = {}

        def fake_call(messages, model, response_format=None):
            captured["response_format"] = response_format
            return self._good_reply(), {"prompt_tokens": 10, "completion_tokens": 5}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            self.agent.generate_plan_for_themes(
                [], destination="Vancouver", age_months=30, pace="balanced",
                wake_up="07:00", bedtime="19:30", features=[])

        self.assertEqual(captured["response_format"], STOPS_RESPONSE_FORMAT)

    def test_retry_call_also_uses_the_schema(self):
        calls = []

        def fake_call(messages, model, response_format=None):
            calls.append(response_format)
            if len(calls) == 1:
                return ('{"stops": [{"venue_id": 999, "time": "9:00 AM", "reason": "r", '
                        '"is_nap": false, "is_meal": false}]}', {}, 1.0)
            return self._good_reply(), {}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            self.agent.generate_plan_for_themes(
                [], destination="Vancouver", age_months=30, pace="balanced",
                wake_up="07:00", bedtime="19:30", features=[])

        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c == STOPS_RESPONSE_FORMAT for c in calls))


class ApplyPlanEditsTest(unittest.TestCase):
    def setUp(self):
        self.draft = {
            "stops": [
                _draft_stop("9:00 AM", "activity", _draft_venue("Old Park")),
                _draft_stop("12:00 PM", "meal", _draft_venue("Old Cafe", category="food")),
            ],
        }
        self.by_id = {7: _venue(7, "New Museum", type="museum")}

    def test_swaps_venue_and_marks_adjusted(self):
        edits = [{"current_venue_name": "Old Park", "new_venue_id": 7,
                  "new_time": None, "reason": "better fit"}]
        result = _apply_plan_edits(self.draft, edits, self.by_id)
        touched = next(s for s in result if s["venue"]["name"] == "New Museum")
        self.assertTrue(touched["adjusted"])
        self.assertEqual(touched["time"], "9:00 AM")
        untouched = next(s for s in result if s["kind"] == "meal")
        self.assertNotIn("adjusted", untouched)

    def test_changes_time_only(self):
        edits = [{"current_venue_name": "Old Cafe", "new_venue_id": None,
                  "new_time": "12:15 PM", "reason": "flow"}]
        result = _apply_plan_edits(self.draft, edits, self.by_id)
        touched = next(s for s in result if s["venue"]["name"] == "Old Cafe")
        self.assertEqual(touched["time"], "12:15 PM")
        self.assertTrue(touched["adjusted"])

    def test_never_mutates_the_draft(self):
        original = {**self.draft, "stops": [dict(s) for s in self.draft["stops"]]}
        _apply_plan_edits(self.draft, [{"current_venue_name": "Old Park",
                                         "new_venue_id": 7, "new_time": None,
                                         "reason": "r"}], self.by_id)
        self.assertEqual(self.draft, original)


class ValidatePlanEditsTest(unittest.TestCase):
    def setUp(self):
        self.draft_stops = [
            _draft_stop("9:00 AM", "activity", _draft_venue("Old Park")),
            _draft_stop("12:00 PM", "meal", _draft_venue("Old Cafe", category="food")),
            _draft_stop("2:00 PM", "nap", _draft_venue("Old Nap Spot")),
        ]
        self.by_id = {
            7: _venue(7, "New Museum", type="museum"),
            8: _venue(8, "New Cafe", can_eat=True, category="food"),
            9: _venue(9, "New Nap Spot", nap_friendly=True),
        }
        self.ctx = dict(bedtime="19:30", strict_schedule=False, naps=None,
                         preferred_lunch_time="", activity_duration_min=60,
                         meal_duration_min=90, transit_buffer_min=20)

    def test_empty_edits_is_valid(self):
        self.assertEqual(_validate_plan_edits([], self.draft_stops, self.by_id, self.ctx), ([], None))

    def test_valid_venue_swap_passes(self):
        edits = [{"current_venue_name": "Old Park", "new_venue_id": 7,
                  "new_time": None, "reason": "better fit"}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(error)
        self.assertEqual(cleaned[0]["new_venue_id"], 7)

    def test_unknown_target_rejected(self):
        edits = [{"current_venue_name": "Nonexistent", "new_venue_id": 7,
                  "new_time": None, "reason": "r"}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)
        self.assertIn("Nonexistent", error)

    def test_noop_edit_rejected(self):
        edits = [{"current_venue_name": "Old Park", "new_venue_id": None,
                  "new_time": None, "reason": "r"}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)

    def test_missing_reason_rejected(self):
        edits = [{"current_venue_name": "Old Park", "new_venue_id": 7,
                  "new_time": None, "reason": ""}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)

    def test_meal_swap_requires_can_eat_venue(self):
        edits = [{"current_venue_name": "Old Cafe", "new_venue_id": 7,
                  "new_time": None, "reason": "r"}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)
        self.assertIn("meal is possible", error)

        edits_ok = [{"current_venue_name": "Old Cafe", "new_venue_id": 8,
                     "new_time": None, "reason": "r"}]
        cleaned_ok, error_ok = _validate_plan_edits(edits_ok, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(error_ok)

    def test_nap_swap_requires_nap_friendly_venue(self):
        edits = [{"current_venue_name": "Old Nap Spot", "new_venue_id": 7,
                  "new_time": None, "reason": "r"}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)
        self.assertIn("nap-friendly", error)

        edits_ok = [{"current_venue_name": "Old Nap Spot", "new_venue_id": 9,
                     "new_time": None, "reason": "r"}]
        cleaned_ok, error_ok = _validate_plan_edits(edits_ok, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(error_ok)

    def test_large_time_nudge_rejected(self):
        edits = [{"current_venue_name": "Old Park", "new_venue_id": None,
                  "new_time": "11:00 AM", "reason": "r"}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)
        self.assertIn("nudge", error)

    def test_small_time_nudge_passes(self):
        edits = [{"current_venue_name": "Old Park", "new_venue_id": None,
                  "new_time": "9:30 AM", "reason": "r"}]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(error)

    def test_meal_time_outside_lunch_window_rejected(self):
        # Isolated single-stop draft so only the lunch-window check is in
        # play, not spacing against a neighbour. 170 min from noon is still
        # in-window; nudging 15 min further (well within the 45-min nudge
        # bound) pushes it to 185 min from noon, outside the 180-min radius.
        stops = [_draft_stop("2:50 PM", "meal", _draft_venue("Old Cafe", category="food"))]
        edits = [{"current_venue_name": "Old Cafe", "new_venue_id": None,
                  "new_time": "3:05 PM", "reason": "r"}]
        ctx = dict(self.ctx, preferred_lunch_time="12:00")
        cleaned, error = _validate_plan_edits(edits, stops, self.by_id, ctx)
        self.assertIsNone(cleaned)
        self.assertIn("lunch window", error)

    def test_bedtime_overrun_rejected_when_strict(self):
        # Original stop ends exactly at bedtime (6:30 PM + 60 min = 7:30 PM);
        # a 20-minute nudge pushes the end 20 minutes past it.
        stops = [_draft_stop("6:30 PM", "activity", _draft_venue("Late Park"))]
        edits = [{"current_venue_name": "Late Park", "new_venue_id": None,
                  "new_time": "6:50 PM", "reason": "r"}]
        strict_ctx = dict(self.ctx, bedtime="19:30", strict_schedule=True)
        cleaned, error = _validate_plan_edits(edits, stops, self.by_id, strict_ctx)
        self.assertIsNone(cleaned)
        self.assertIn("bedtime", error)

    def test_bedtime_overrun_allowed_when_flexible(self):
        # Same 20-minute overrun, within the 30-minute flexible allowance.
        stops = [_draft_stop("6:30 PM", "activity", _draft_venue("Late Park"))]
        edits = [{"current_venue_name": "Late Park", "new_venue_id": None,
                  "new_time": "6:50 PM", "reason": "r"}]
        flexible_ctx = dict(self.ctx, bedtime="19:30", strict_schedule=False)
        cleaned, error = _validate_plan_edits(edits, stops, self.by_id, flexible_ctx)
        self.assertIsNone(error)

    def test_transit_buffer_violation_rejected(self):
        stops = [
            _draft_stop("9:00 AM", "activity", _draft_venue("Old Park")),
            _draft_stop("9:45 AM", "activity", _draft_venue("Next Stop")),
        ]
        # Moving "Old Park" later so it now runs into "Next Stop" with no buffer.
        edits = [{"current_venue_name": "Old Park", "new_venue_id": None,
                  "new_time": "9:30 AM", "reason": "r"}]
        cleaned, error = _validate_plan_edits(edits, stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)
        self.assertIn("travel buffer", error)

    def test_duplicate_new_venue_id_rejected(self):
        edits = [
            {"current_venue_name": "Old Park", "new_venue_id": 7, "new_time": None, "reason": "r"},
            {"current_venue_name": "Old Nap Spot", "new_venue_id": 7, "new_time": None, "reason": "r"},
        ]
        cleaned, error = _validate_plan_edits(edits, self.draft_stops, self.by_id, self.ctx)
        self.assertIsNone(cleaned)
        self.assertIn("more than one edit", error)


class AdjustPlanTest(unittest.TestCase):
    def setUp(self):
        self.agent = PlanningAgent()
        self.draft = {
            "label": "Mixed", "blurb": "b",
            "stops": [_draft_stop("9:00 AM", "activity", _draft_venue("Old Park"))],
        }
        self.candidates = [_venue(7, "New Museum", type="museum")]

    def test_calls_openrouter_with_the_edits_schema_and_applies_result(self):
        captured = {}

        def fake_call(messages, model, response_format=None):
            captured["response_format"] = response_format
            return ('{"edits": [{"current_venue_name": "Old Park", "new_venue_id": 7, '
                    '"new_time": null, "reason": "better fit"}]}'), {}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            result = self.agent.adjust_plan(
                self.draft, destination="Vancouver", age_months=30,
                wake_up="07:00", bedtime="19:30", pace="balanced", dining="dine_out")

        self.assertEqual(captured["response_format"], PLAN_EDITS_RESPONSE_FORMAT)
        self.assertEqual(result["stops"][0]["venue"]["name"], "New Museum")
        self.assertEqual(len(result["edits"]), 1)

    def test_empty_edits_leaves_draft_unchanged(self):
        def fake_call(messages, model, response_format=None):
            return '{"edits": []}', {}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            result = self.agent.adjust_plan(
                self.draft, destination="Vancouver", age_months=30,
                wake_up="07:00", bedtime="19:30", pace="balanced", dining="dine_out")

        self.assertEqual(result["stops"][0]["venue"]["name"], "Old Park")
        self.assertEqual(result["edits"], [])

    def test_retry_then_raise_on_repeated_invalid_edits(self):
        calls = []

        def fake_call(messages, model, response_format=None):
            calls.append(1)
            return ('{"edits": [{"current_venue_name": "Nonexistent", "new_venue_id": 7, '
                    '"new_time": null, "reason": "r"}]}'), {}, 1.0

        with mock.patch("src.agents.db.get_candidate_venues", return_value=self.candidates), \
             mock.patch("src.agents._call_openrouter", side_effect=fake_call):
            with self.assertRaises(PlanningAgentError):
                self.agent.adjust_plan(
                    self.draft, destination="Vancouver", age_months=30,
                    wake_up="07:00", bedtime="19:30", pace="balanced", dining="dine_out")
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
