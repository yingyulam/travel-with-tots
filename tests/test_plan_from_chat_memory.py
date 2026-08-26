"""What the form-filling chat does with what the app already remembers.

`recall` is mocked in every test here, so nothing touches SQLite: what is under
test is the conversation's use of a recall, not the recall itself, which
tests/test_memory.py covers against a real database.
"""

import unittest
from unittest import mock

from src.form_helpers import DEFAULTS
from src.workflows import plan_from_chat
from src.workflows.plan_from_chat import (
    CHANGED_CHOICE,
    CHAT_CHOICE,
    CONFIRM_CHOICE,
    FORM_CHOICE,
    RECALLED_PREFACE,
    SAME_AS_LAST_TIME,
    STAGE_CONFIRMING,
    STAGE_OFFERED,
    run,
)

CONTEXT = {"parent_id": 7}

# Every question answered, which is the returning-parent case.
FULL = {
    "child": {"name": "Maya", "age_years": 2, "age_months": 6},
    "form": {"destination": "Vancouver", "age_years": "2", "age_months": "6",
             "wake_up": "06:45", "bedtime": "19:00",
             "naps": [{"start": "12:30", "duration_min": 45}],
             "plan_child_id": "3"},
    "remembered": ["age_months", "age_years", "bedtime", "destination", "naps",
                   "wake_up"],
    "trip_saved_at": "2026-08-15",
}

# Only the child is on file, which is the commoner case: most saved trips have
# no naps and dirty stop counts, so much of a routine never survives validation.
CHILD_ONLY = {
    "child": {"name": "Maya", "age_years": 1, "age_months": 6},
    "form": {"age_years": "1", "age_months": "6", "plan_child_id": "3"},
    "remembered": ["age_months", "age_years"],
    "trip_saved_at": None,
}

NOTHING = {"child": None, "form": {}, "remembered": [], "trip_saved_at": None}


def _extraction(**supplied):
    form = dict(DEFAULTS)
    form.update(supplied)
    return {"form": form, "found": sorted(supplied), "model": "m",
            "response_time": 1.0}


def _turn(message, state=None, known=NOTHING, context=CONTEXT, **supplied):
    with mock.patch.object(plan_from_chat, "recall", return_value=known), \
         mock.patch.object(plan_from_chat, "extract_form",
                           return_value=_extraction(**supplied)):
        return run(message, state, context)


class AnonymousIsUnchangedTest(unittest.TestCase):
    def test_no_parent_never_asks_what_it_cannot_know(self):
        with mock.patch.object(plan_from_chat, "recall") as recall:
            with mock.patch.object(plan_from_chat, "extract_form",
                                   return_value=_extraction()):
                result = run("plan a trip", None, {"parent_id": None})
        recall.assert_not_called()
        self.assertEqual(result["state"]["stage"], STAGE_OFFERED)

    def test_no_context_at_all_is_fine(self):
        with mock.patch.object(plan_from_chat, "extract_form",
                               return_value=_extraction()):
            self.assertEqual(run("plan a trip", None, None)["state"]["stage"],
                             STAGE_OFFERED)

    def test_a_recall_that_raises_costs_the_memory_not_the_turn(self):
        with mock.patch.object(plan_from_chat, "recall",
                               side_effect=RuntimeError("boom")), \
             mock.patch.object(plan_from_chat, "extract_form",
                               return_value=_extraction()):
            result = run("plan a trip", None, CONTEXT)
        self.assertEqual(result["state"]["stage"], STAGE_OFFERED)


class MemoryAnswersTheQuestionsTest(unittest.TestCase):
    def test_a_bare_intent_still_offers_the_two_ways(self):
        # Memory does not railroad a returning parent into the chat: the offer
        # is shown whatever is on file, and only the questions change.
        self.assertEqual(_turn("plan a trip", known=CHILD_ONLY)["state"]["stage"],
                         STAGE_OFFERED)

    def test_a_remembered_field_is_not_asked_about(self):
        offered = _turn("plan a trip", known=CHILD_ONLY)
        chosen = _turn(CHAT_CHOICE, offered["state"], known=CHILD_ONLY)
        # Age is on file, so the first thing asked is the next one along, and
        # the everything-at-once question is skipped because it would list an
        # age already known.
        self.assertNotIn("How old", chosen["reply"])
        self.assertIn("Which city", chosen["reply"])

    def test_the_parent_is_told_memory_contributed(self):
        # A bare question gives no clue whether the rest was remembered or lost.
        offered = _turn("plan a trip", known=CHILD_ONLY)
        chosen = _turn(CHAT_CHOICE, offered["state"], known=CHILD_ONLY)
        self.assertIn(RECALLED_PREFACE, chosen["reply"])

    def test_a_full_memory_goes_straight_to_the_summary(self):
        result = _turn("plan a trip", known=FULL)
        self.assertEqual(result["state"]["stage"], STAGE_CONFIRMING)
        self.assertIn(SAME_AS_LAST_TIME, result["reply"])

    def test_the_summary_says_where_each_value_came_from(self):
        reply = _turn("plan a trip", known=FULL)["reply"]
        self.assertIn("From Maya's details:", reply)
        self.assertIn("From your last trip, saved 2026-08-15:", reply)
        self.assertIn("Using defaults for the rest:", reply)

    def test_a_full_memory_offers_confirm_change_and_the_form(self):
        # Every offered label has to parse, since the widget sends a button's
        # own text back as the message.
        self.assertEqual(_turn("plan a trip", known=FULL)["choices"],
                         [CONFIRM_CHOICE, CHANGED_CHOICE, FORM_CHOICE])

    def test_confirming_hands_over_the_remembered_form(self):
        offered = _turn("plan a trip", known=FULL)
        done = _turn(CONFIRM_CHOICE, offered["state"], known=FULL)
        self.assertEqual(done["form"]["wake_up"], "06:45")
        self.assertEqual(done["form"]["destination"], "Vancouver")
        self.assertIsNone(done["state"])

    def test_the_child_is_named_in_the_handed_over_form(self):
        # /plan recomputes the age from plan_child_id and defaults to the
        # youngest child, so an age with no child attached is silently replaced.
        offered = _turn("plan a trip", known=FULL)
        done = _turn(CONFIRM_CHOICE, offered["state"], known=FULL)
        self.assertEqual(done["form"]["plan_child_id"], "3")


class TheParentStillWinsTest(unittest.TestCase):
    def test_their_own_words_override_a_remembered_value(self):
        result = _turn("we're in Vancouver, she's 3 now", known=FULL,
                       age_years="3", destination="Vancouver")
        self.assertEqual(result["state"]["form"]["age_years"], "3")

    def test_a_corrected_field_is_credited_to_them_not_to_memory(self):
        # _merge adds to `found` without removing from `remembered`, so the
        # bucket order in _summarise is what moves it.
        offered = _turn("plan a trip", known=FULL)
        corrected = _turn("actually she's 3", offered["state"], known=FULL,
                          age_years="3")
        reply = corrected["reply"]
        theirs = reply.index("From what you told me:")
        self.assertIn("age years: 3", reply[theirs:])

    def test_a_described_day_skips_the_offer_and_still_uses_memory(self):
        result = _turn("Vancouver, up at 7", known=CHILD_ONLY,
                       destination="Vancouver", wake_up="07:00")
        self.assertNotEqual(result["state"]["stage"], STAGE_OFFERED)
        self.assertIn("age_years", result["state"]["remembered"])


class ChangingYourMindTest(unittest.TestCase):
    def test_saying_something_changed_clears_what_was_recalled(self):
        offered = _turn("plan a trip", known=FULL)
        changed = _turn(CHANGED_CHOICE, offered["state"], known=FULL)
        self.assertEqual(changed["state"]["remembered"], [])
        self.assertEqual(changed["state"]["form"]["wake_up"], DEFAULTS["wake_up"])
        self.assertIn("Which city", changed["reply"])

    def test_the_form_door_stays_open_from_the_summary(self):
        offered = _turn("plan a trip", known=FULL)
        left = _turn(FORM_CHOICE, offered["state"], known=FULL)
        self.assertTrue(left["open_form"])
        self.assertIsNone(left["state"])

    def test_a_denied_nap_leaves_the_form_even_when_remembered(self):
        # Memory answered the nap question, so the parent was never asked and
        # the ordinary decline cannot fire. Without this the summary shows a nap
        # they just said does not happen, and posts it to /plan.
        offered = _turn("plan a trip", known=FULL)
        denied = _turn("she doesn't nap anymore", offered["state"], known=FULL)
        self.assertEqual(denied["state"]["form"]["naps"], [])
        self.assertNotIn("naps", denied["state"]["remembered"])


class TheSeedSurvivesTest(unittest.TestCase):
    def test_choosing_chat_from_the_offer_does_not_lose_memory(self):
        # _start's state used to be rebuilt blank, so picking chat threw away
        # everything memory had supplied.
        offered = _turn("plan a trip", known=CHILD_ONLY)
        chosen = _turn(CHAT_CHOICE, offered["state"], known=CHILD_ONLY)
        self.assertIn("age_years", chosen["state"]["remembered"])
        self.assertEqual(chosen["state"]["form"]["age_years"], "1")

    def test_memory_survives_several_collecting_turns(self):
        # The regression that matters: _collect rebuilds its carried state from
        # a fresh dict, so a missing key there loses the seed after one turn.
        offered = _turn("plan a trip", known=CHILD_ONLY)
        state = _turn(CHAT_CHOICE, offered["state"], known=CHILD_ONLY)["state"]
        for message, supplied in (("Vancouver", {"destination": "Vancouver"}),
                                  ("up at 7", {"wake_up": "07:00"}),
                                  ("bed at 7", {"bedtime": "19:00"})):
            state = _turn(message, state, known=CHILD_ONLY, **supplied)["state"]
        self.assertIn("age_years", state["remembered"])
        self.assertEqual(state["form"]["age_years"], "1")


if __name__ == "__main__":
    unittest.main()
