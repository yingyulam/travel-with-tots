"""An armed workflow test page directs its own messages.

The chat bubble is both a workflow's input and the app's general front door, so
a test page could not reach its own workflow when the classifier preferred
another: you pressed Run, typed your best phrasing, and were told the message
went elsewhere. Arming decides where the message goes.

It is a URL now rather than a flag. The page posts to /workflows/<name>/run,
behind an admin login, and /chatbot is the agent's alone.
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


class NamingAWorkflowRunsItTest(unittest.TestCase):
    """A test page names the workflow it is testing, and that one runs.

    It used to do this by beating the classifier to the message. There is no
    classifier on this path any more: the page posts to /workflows/<name>/run
    and the name in the URL is the whole routing decision.
    """

    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.logged = self.log.start()
        self.addCleanup(self.log.stop)

    def _send(self, message, name=None, conversation=None):
        with mock.patch.object(plan_from_chat, "extract_form",
                               return_value=_extraction()), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError("reached the agent")):
            return agent.run_workflow_turn(name, message,
                                           conversation=conversation,
                                           forced=True)

    def test_the_named_workflow_runs(self):
        # The reported case: armed on the fill-form page, a message that reads
        # like Log a place runs fill-form instead.
        answer = self._send("I want to log a place", name=FILLING)
        self.assertEqual(answer["workflow"], FILLING)

    def test_an_in_flight_conversation_wins_over_the_name(self):
        # Mid-conversation "Vancouver" is an answer to the question just asked.
        # Honouring the name here would restart the flow on every turn.
        conversation = {"workflow": LOGGING, "state": {"stage": "name"}}
        answer = self._send("Vancouver", name=FILLING, conversation=conversation)
        self.assertEqual(answer["workflow"], LOGGING)

    def test_cancelling_wins_over_the_name(self):
        conversation = {"workflow": LOGGING, "state": {"stage": "name"}}
        answer = self._send("never mind", name=FILLING, conversation=conversation)
        self.assertIsNone(answer["conversation"])
        self.assertEqual(answer["cancelled"], LOGGING)

    def test_a_name_nobody_offers_runs_nothing(self):
        # Client-supplied, so it is checked against the registry. None means
        # the caller gets an error rather than a workflow it did not ask for.
        for bogus in ("Nap-time rescue", "Delete everything", "", None):
            with self.subTest(name=bogus):
                self.assertIsNone(self._send("I want to log a place", name=bogus))

    def test_every_offered_name_works(self):
        # The pages name a workflow, so a name the registry offers must run or
        # that page's Run button does nothing.
        for name in NAMES:
            with self.subTest(name=name):
                answer = self._send("anything at all", name=name)
                self.assertEqual(answer["workflow"], name)

    def test_a_directed_turn_is_marked_in_the_log(self):
        # data/intents.jsonl is what routing accuracy is measured from, and
        # test traffic must not be counted as a real routing decision.
        self._send("I want to log a place", name=FILLING)
        self.assertTrue(self.logged.call_args.kwargs["forced"])


class TheRouteIgnoresItTest(unittest.TestCase):
    """/chatbot used to take the workflow name from the request body.

    It is a public route, so that let anyone decide which workflow their
    message ran. The only caller that needed it was a workflow test page, and
    those post to /workflows/<name>/run now, behind an admin login, which is
    what the tests above drive.
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
