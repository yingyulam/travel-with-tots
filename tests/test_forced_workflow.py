"""An armed workflow test page directs its own messages.

The chat is both a workflow's input and the app's general front door, so a test
page could not reach its own workflow when the classifier preferred another:
you pressed Run, typed your best phrasing, and got told the message went
elsewhere. Arming now decides where the message goes.

This grants nothing new. A parent can already trigger any workflow by typing
the right words; forcing only decides which workflow those words reach, and
only from an admin page.
"""

import unittest
from unittest import mock

import app as app_module
from src import agent
from src.form_helpers import DEFAULTS
from src.workflows import plan_from_chat, runnable_message_workflows

FILLING = "Fill the form from a chat message"
LOGGING = "Log a place we don't have"
NAMES = [w["name"] for w, _ in runnable_message_workflows()]


def _extraction(**supplied):
    form = dict(DEFAULTS)
    form.update(supplied)
    return {"form": form, "found": sorted(supplied), "model": "m",
            "response_time": 1.0}


class ForcingBeatsTheClassifierTest(unittest.TestCase):
    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.logged = self.log.start()
        self.addCleanup(self.log.stop)

    def _send(self, message, forced=None, conversation=None):
        with mock.patch.object(agent, "classify_intent",
                              return_value=LOGGING) as classify, \
             mock.patch.object(plan_from_chat, "extract_form",
                               return_value=_extraction()), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError("fell through")):
            answer = agent.handle_message(message, conversation=conversation,
                                          force_workflow=forced)
        return answer, classify

    def test_the_forced_workflow_runs_and_the_classifier_is_not_asked(self):
        # The reported case: armed on the fill-form page, a message the
        # classifier would send to Log a place runs fill-form instead.
        answer, classify = self._send("I want to log a place", forced=FILLING)
        self.assertEqual(answer["workflow"], FILLING)
        classify.assert_not_called()

    def test_without_forcing_the_classifier_still_decides(self):
        answer, classify = self._send("I want to log a place")
        self.assertEqual(answer["workflow"], LOGGING)
        classify.assert_called_once()

    def test_an_in_flight_conversation_wins_over_forcing(self):
        # Mid-conversation "Vancouver" is an answer to the question just asked.
        # Forcing here would restart the flow on every turn.
        conversation = {"workflow": LOGGING, "state": {"stage": "name"}}
        answer, classify = self._send("Vancouver", forced=FILLING,
                                      conversation=conversation)
        self.assertEqual(answer["workflow"], LOGGING)
        classify.assert_not_called()

    def test_cancelling_wins_over_forcing(self):
        conversation = {"workflow": LOGGING, "state": {"stage": "name"}}
        answer, _ = self._send("never mind", forced=FILLING,
                               conversation=conversation)
        self.assertIsNone(answer["conversation"])
        self.assertEqual(answer["cancelled"], LOGGING)

    def test_only_a_registered_workflow_can_be_forced(self):
        # Client-supplied, so it is re-checked against the registry, the same
        # way the classifier's own answer is.
        for bogus in ("Nap-time rescue", "Delete everything", "", None):
            with self.subTest(forced=bogus):
                answer, classify = self._send("I want to log a place",
                                              forced=bogus)
                self.assertEqual(answer["workflow"], LOGGING)
                classify.assert_called_once()

    def test_every_offered_name_is_forceable(self):
        # The pages force by name, so a name the registry offers must work or
        # that page's Run silently falls back to the classifier.
        for name in NAMES:
            with self.subTest(name=name):
                answer, _ = self._send("anything at all", forced=name)
                self.assertEqual(answer["workflow"], name)

    def test_a_forced_turn_is_marked_in_the_log(self):
        # data/intents.jsonl is what classifier accuracy is measured from, and
        # these turns never went near the classifier.
        self._send("I want to log a place", forced=FILLING)
        self.assertTrue(self.logged.call_args.kwargs["forced"])

    def test_a_classified_turn_is_not(self):
        self._send("I want to log a place")
        self.assertFalse(self.logged.call_args.kwargs["forced"])


class TheRouteIgnoresItTest(unittest.TestCase):
    """/chatbot used to take the workflow name from the request body.

    It is a public route, so that let anyone decide which workflow their
    message ran. The only caller that needed it was a workflow test page, and
    those post to /workflows/<name>/run now, behind an admin login. Forcing is
    still reachable in handle_message, which is where the tests above use it.
    """

    def _post(self, **body):
        with mock.patch.object(app_module, "handle_message",
                               return_value={"reply": "ok"}) as handled, \
             mock.patch.object(app_module.rag, "get_status",
                               return_value={"state": "ready"}):
            app_module.app.test_client().post("/chatbot",
                                              json={"message": "hi", **body})
        return handled

    def test_a_caller_cannot_choose_a_workflow_from_the_body(self):
        handled = self._post(force_workflow=FILLING)
        self.assertNotIn("force_workflow", handled.call_args.kwargs)

    def test_a_normal_turn_is_unaffected(self):
        handled = self._post()
        self.assertNotIn("force_workflow", handled.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
