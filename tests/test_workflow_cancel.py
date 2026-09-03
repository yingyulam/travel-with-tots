"""Leaving a workflow.

Skipping the classifier while a workflow is open is what makes "yes" an answer
rather than an intent. It also made a workflow a room with no door: every
message went to the flow, so the only ways out were finishing it or ending the
chat and losing the transcript. Cancelling is checked before dispatch, so it
works for every workflow rather than each one having to remember.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import unittest
from unittest import mock

from src import agent
from src.form_helpers import DEFAULTS
from src.intent import CANCEL_CHOICE, CANCEL_WORDS, is_cancel
from src.workflows import find_nearby_place, plan_from_chat
from src.workflows.plan_from_chat import STAGE_COLLECTING, STAGE_CONFIRMING

FILLING = "Fill the form from a chat message"
NEARBY = "Find a nearby place"


def _mid_form(stage=STAGE_COLLECTING):
    return {"workflow": FILLING,
            "state": {"stage": stage, "form": dict(DEFAULTS), "found": [],
                      "skipped": [], "asked_extras": False}}


class ReadingACancelTest(unittest.TestCase):
    def test_the_ways_a_parent_backs_out(self):
        for message in ("cancel", "Stop", "never mind", "nevermind!",
                        "forget it", "not now", "quit", "exit",
                        "start over", "go back"):
            with self.subTest(message=message):
                self.assertTrue(is_cancel(message))

    def test_the_offered_button_parses_as_one(self):
        # The widget sends the button's own label. A label the server cannot
        # read is a button that does nothing, which this project has shipped
        # once already.
        self.assertTrue(is_cancel(CANCEL_CHOICE))

    def test_a_softener_in_front_does_not_hide_it(self):
        # Found by walking the flow: "actually never mind" is the obvious way
        # to say this, and a whole-message match had no room for the
        # "actually". Every one of these failed before CANCEL_FILLER existed.
        for message in ("actually never mind", "ok never mind", "sorry, forget it",
                        "can we stop", "i want to stop", "let's do something else"):
            with self.subTest(message=message):
                self.assertTrue(is_cancel(message))

    def test_a_softener_on_its_own_is_not_a_cancel(self):
        # "ok" is dropped as filler here, which must leave nothing rather than
        # leaving a cancel. It is a yes to a different question.
        for message in ("ok", "okay", "actually", "well"):
            with self.subTest(message=message):
                self.assertFalse(is_cancel(message))

    def test_a_cancel_word_inside_a_sentence_is_not_a_cancel(self):
        # Whole message only. These are answers to the questions being asked.
        for message in ("stop by the park at 3", "we have one stop planned",
                        "she never minds the bus", "quiet spot", "no naps"):
            with self.subTest(message=message):
                self.assertFalse(is_cancel(message))


class LeavingTheFormFlowTest(unittest.TestCase):
    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.logged = self.log.start()
        self.addCleanup(self.log.stop)

    def _cancel(self, conversation, message="never mind"):
        # Through run_workflow_turn, which is where a workflow runs now.
        # /chatbot has no flow to leave: the agent holds no conversation, so
        # cancelling belongs to the surface that does.
        with mock.patch.object(agent, "run_agent") as ran, \
             mock.patch.object(plan_from_chat, "extract_form") as extract:
            answer = agent.run_workflow_turn(FILLING, message,
                                             conversation=conversation)
        return answer, ran, extract

    def test_it_ends_the_flow(self):
        answer, _, _ = self._cancel(_mid_form())
        self.assertIsNone(answer["conversation"])
        self.assertEqual(answer["cancelled"], FILLING)

    def test_it_reaches_neither_the_workflow_nor_the_agent(self):
        # Not the extractor either: a model call to be told "never mind" is a
        # call worth not making.
        _, ran, extract = self._cancel(_mid_form())
        ran.assert_not_called()
        extract.assert_not_called()

    def test_it_works_at_every_stage(self):
        for stage in (STAGE_COLLECTING, STAGE_CONFIRMING):
            with self.subTest(stage=stage):
                answer, _, _ = self._cancel(_mid_form(stage))
                self.assertIsNone(answer["conversation"])

    def test_no_form_is_handed_over(self):
        # Cancelling at the confirmation must not be read as confirming.
        answer, _, _ = self._cancel(_mid_form(STAGE_CONFIRMING))
        self.assertIsNone(answer["form"])
        self.assertFalse(answer["open_form"])

    def test_the_abandoned_state_is_not_resumed(self):
        # Cancelling clears the conversation, so the next message begins the
        # flow rather than continuing the one that was walked out of.
        answer, _, _ = self._cancel(_mid_form())
        with mock.patch.object(plan_from_chat, "run",
                               return_value={"reply": "r"}) as ran:
            agent.run_workflow_turn(FILLING, "we're in Vancouver",
                                    conversation=answer["conversation"])
        self.assertIsNone(ran.call_args.args[1])

    def test_it_is_logged_against_the_workflow_that_was_open(self):
        self._cancel(_mid_form())
        self.logged.assert_called_once_with("never mind", FILLING, ran=False)


class LeavingAnyWorkflowTest(unittest.TestCase):
    """Checked before dispatch, so it is not a feature of one workflow."""

    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.log.start()
        self.addCleanup(self.log.stop)

    def test_the_nearby_flow_can_be_left_too(self):
        conversation = {"workflow": NEARBY, "state": {"stage": "need"}}
        with mock.patch.object(find_nearby_place, "find_nearby") as component, \
             mock.patch.object(agent, "run_agent") as ran:
            answer = agent.run_workflow_turn(conversation["workflow"], "cancel",
                                             conversation=conversation)
        component.assert_not_called()
        ran.assert_not_called()
        self.assertIsNone(answer["conversation"])


class TheWayOutIsOfferedTest(unittest.TestCase):
    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.log.start()
        self.addCleanup(self.log.stop)

    def _turn(self, message, conversation=None, classified=None):
        extraction = {"form": dict(DEFAULTS), "found": [], "model": "m",
                      "response_time": 1.0}
        with \
             mock.patch.object(plan_from_chat, "extract_form",
                               return_value=extraction):
            return agent.run_workflow_turn(classified, message,
                                           conversation=conversation)

    def test_every_turn_that_keeps_the_flow_open_offers_it(self):
        answer = self._turn("plan a trip", classified=FILLING)
        self.assertIsNotNone(answer["conversation"])
        self.assertEqual(answer["cancel_choice"], CANCEL_CHOICE)

        answer = self._turn("plan through chat", answer["conversation"])
        self.assertEqual(answer["cancel_choice"], CANCEL_CHOICE)

    def test_a_finished_flow_does_not_offer_it(self):
        # Nothing to leave, so offering a way out would be noise.
        conversation = {"workflow": FILLING,
                        "state": {"stage": STAGE_CONFIRMING,
                                  "form": dict(DEFAULTS), "found": ["destination"]}}
        answer = self._turn("yes", conversation)
        self.assertIsNone(answer["conversation"])
        self.assertIsNone(answer["cancel_choice"])

    def test_the_cancel_words_are_not_a_secret(self):
        # The button exists because guessing the magic word is not a way out.
        self.assertIn("never mind", CANCEL_WORDS)


if __name__ == "__main__":
    unittest.main()
