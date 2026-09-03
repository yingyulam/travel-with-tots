"""Replanning the rest of the day by talking to the chat.

Was "Nap-time rescue", a declaration with no run() that named one situation out
of seven. A long nap is the commonest reason a day stops fitting, but a closed
stop, rain, or wanting to stay put are the same request and the replan
component already handles all of them.

What this collects is what the trip page's situation buttons collect. What it
does not do is replan: that page holds the plan, its versions and the current
time, and its runReplan is the one implementation.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import unittest
from unittest import mock

from src import interactions
from src.ai import tool_agent as agent
from src.workflows import replan_on_the_go
from src.workflows.replan_on_the_go import (
    FREE_TEXT_SITUATION,
    STAGE_SITUATION,
    WORKFLOW,
    read_minutes,
    read_situation,
    run,
)

ON_TRIP = {"on_trip": True}
LABELS = [label for _, label in interactions.SITUATION_OPTIONS]


def _asking(**values):
    return {"stage": STAGE_SITUATION, "values": values}


class ReadingTheSituationTest(unittest.TestCase):
    def test_every_chip_label_is_understood(self):
        # The widget sends a chip's own label, so a label this module cannot
        # read is a button that does nothing.
        for key, label in interactions.SITUATION_OPTIONS:
            with self.subTest(label=label):
                self.assertEqual(read_situation(label), key)

    def test_the_ways_a_parent_would_say_it(self):
        for message, expected in (
            ("she napped for ages", "nap_happened"),
            ("he fell asleep in the stroller", "nap_happened"),
            ("it's pouring out here", "weather_rain"),
            ("can we skip the next stop", "skip_next"),
            ("we finished early", "finished_early"),
            ("we want to stay here longer", "running_behind"),
            ("we're running behind", "running_behind"),
            ("let's do something different", "change_interest"),
            ("we'd rather do something else", "change_interest"),
        ):
            with self.subTest(message=message):
                self.assertEqual(read_situation(message), expected)

    def test_a_nap_wins_over_running_behind(self):
        # Both sets of words are in this sentence, and only the order decides.
        # The nap is the thing that happened; being behind is its consequence.
        self.assertEqual(read_situation("she slept two hours so we're behind"),
                         "nap_happened")

    def test_anything_unrecognised_is_still_a_replan(self):
        # The trip page's own free-text box does the same: re-time what is
        # left and read the note. A dead end would be worse.
        self.assertEqual(read_situation("the museum has a burst pipe"),
                         FREE_TEXT_SITUATION)


class ReadingHowLongTest(unittest.TestCase):
    def test_minutes_and_hours(self):
        for message, expected in (("she napped 90 minutes", 90),
                                  ("about 45 min", 45),
                                  ("a 2 hour nap", 120),
                                  ("1 hr behind", 60)):
            with self.subTest(message=message):
                self.assertEqual(read_minutes(message), expected)

    def test_no_number_leaves_the_default_alone(self):
        self.assertIsNone(read_minutes("she napped for ages"))

    def test_a_bare_number_is_not_a_duration(self):
        # "3 stops" is not three minutes. A unit is required.
        self.assertIsNone(read_minutes("we have 3 stops left"))


class ItNeedsAStartedDayTest(unittest.TestCase):
    """There is nothing to reshape without one, and collecting a situation it
    cannot act on would waste the parent's turn."""

    def test_off_the_trip_page_it_says_where_to_go(self):
        answer = run("we need to replan", None, {"on_trip": False})
        self.assertIsNone(answer["state"])
        self.assertIn("Open your trip", answer["reply"])
        self.assertIsNone(answer.get("choices"))

    def test_a_missing_context_counts_as_no_trip(self):
        self.assertIsNone(run("we need to replan")["state"])

    def test_a_specific_opening_situation_is_read_not_re_asked(self):
        # A parent who says what happened should not be asked what happened.
        answer = run("she napped 90 minutes", None, ON_TRIP)
        self.assertEqual(answer["replan_request"],
                         {"situation": "nap_happened", "minutes": 90,
                          "note": "she napped 90 minutes"})

    def test_a_vague_opening_still_gets_the_chips(self):
        # read_situation falls back to free text for anything it does not
        # recognise, so reading it blindly would skip past the six chips, which
        # are the useful thing to offer someone who has not said yet.
        answer = run("we need to replan", None, ON_TRIP)
        self.assertEqual(answer["choices"], LABELS)
        self.assertIsNone(answer.get("replan_request"))

    def test_on_the_trip_page_it_asks_what_happened(self):
        answer = run("we need to replan", None, ON_TRIP)
        self.assertEqual(answer["state"]["stage"], STAGE_SITUATION)
        self.assertEqual(answer["choices"], LABELS)


class TheConversationTest(unittest.TestCase):
    def test_a_tapped_chip_is_read_back_with_the_request_ready(self):
        # One button, not two. An earlier draft confirmed with a chip and then
        # offered a Replan button, which is two controls for one decision.
        answer = run("Nap happened here", _asking(), ON_TRIP)
        self.assertIsNone(answer["state"])
        self.assertEqual(answer["replan_request"]["situation"], "nap_happened")
        self.assertIsNone(answer.get("choices"))

    def test_a_tapped_chip_adds_no_note(self):
        # The label says nothing the situation does not already say.
        answer = run("It's raining", _asking(), ON_TRIP)
        self.assertNotIn("note", answer["replan_request"])

    def test_typed_words_ride_along_as_the_note(self):
        answer = run("the aquarium is shut", _asking(), ON_TRIP)
        values = answer["replan_request"]
        self.assertEqual(values["situation"], FREE_TEXT_SITUATION)
        self.assertEqual(values["note"], "the aquarium is shut")

    def test_a_duration_is_read_for_the_situations_that_need_one(self):
        answer = run("she napped 90 minutes", _asking(), ON_TRIP)
        self.assertEqual(answer["replan_request"]["minutes"], 90)

    def test_a_duration_is_ignored_where_it_means_nothing(self):
        # "Skip next stop" has no duration; carrying a number would imply the
        # replan used one.
        answer = run("skip the next stop, we have 20 minutes", _asking(), ON_TRIP)
        self.assertEqual(answer["replan_request"]["situation"], "skip_next")
        self.assertNotIn("minutes", answer["replan_request"])

    def test_the_confirmation_reads_the_situation_back(self):
        answer = run("Nap happened here", _asking(), ON_TRIP)
        self.assertIn("Nap happened here", answer["reply"])

    def test_the_flow_ends_once_the_request_is_ready(self):
        # Pressing the button is an action on the trip page, not a message, so
        # the next thing typed goes back through the classifier.
        answer = run("she napped ages", _asking(), ON_TRIP)
        self.assertIsNone(answer["state"])


class HandingTheReplanOverTest(unittest.TestCase):
    """The chat collects; the in-trip page re-times. It holds the plan, the
    versions and the clock, and doing it here would be a second
    implementation whose result never reached that page's version switcher."""

    def test_the_situation_turn_hands_the_request_over(self):
        answer = run("she napped 90 minutes", _asking(), ON_TRIP)
        self.assertIsNone(answer["state"])
        self.assertEqual(answer["replan_request"]["situation"], "nap_happened")
        self.assertEqual(answer["replan_request"]["minutes"], 90)

    def test_nothing_is_handed_over_before_a_situation_is_given(self):
        # The opening turn only asks; there is nothing to replan with yet.
        self.assertIsNone(run("we need to replan", None, ON_TRIP)
                          .get("replan_request"))
        self.assertIsNone(run("we need to replan", None, {"on_trip": False})
                          .get("replan_request"))

    def test_it_never_replans_itself(self):
        with mock.patch.object(interactions, "replan") as replanned:
            state = run("we need to replan", None, ON_TRIP)["state"]
            run("Nap happened here", state, ON_TRIP)
        replanned.assert_not_called()

    def test_the_keys_are_what_the_trip_page_takes(self):
        # runReplan(situation, minutes, theme, note) is what receives this.
        request = run("she went down late, 90 minutes",
                      _asking(), ON_TRIP)["replan_request"]
        self.assertTrue(set(request) <= {"situation", "minutes", "note"},
                        f"unknown keys: {set(request) - {'situation', 'minutes', 'note'}}")


class ItIsRoutableTest(unittest.TestCase):
    def test_the_old_declaration_only_workflow_is_gone(self):
        from src.workflows import WORKFLOWS
        names = [w["name"] for w in WORKFLOWS]
        self.assertIn("Replan on the go", names)
        self.assertNotIn("Nap-time rescue", names)

    def test_the_classifier_is_offered_it(self):
        from src.workflows import runnable_message_workflows
        offered = [w["name"] for w, _ in runnable_message_workflows()]
        self.assertIn(WORKFLOW["name"], offered)

    def test_a_replan_message_names_the_workflow(self):
        with \
             mock.patch.object(agent, "log_decision"), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError("fell through")):
            answer = agent.run_workflow_turn(WORKFLOW["name"], "she napped way too long",
                                          context=ON_TRIP)
        self.assertEqual(answer["workflow"], WORKFLOW["name"])

    def test_the_request_reaches_the_widget(self):
        conversation = {"workflow": WORKFLOW["name"], "state": _asking()}
        with mock.patch.object(agent, "log_decision"), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError("fell through")):
            answer = agent.run_workflow_turn(WORKFLOW["name"], "It's raining",
                                          conversation=conversation,
                                          context=ON_TRIP)
        self.assertEqual(answer["replan_request"], {"situation": "weather_rain"})

    def test_it_can_be_left_like_any_workflow(self):
        conversation = {"workflow": WORKFLOW["name"], "state": _asking()}
        with mock.patch.object(agent, "log_decision"), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError("fell through")):
            answer = agent.run_workflow_turn(WORKFLOW["name"], "never mind", conversation=conversation)
        self.assertIsNone(answer["conversation"])


if __name__ == "__main__":
    unittest.main()
