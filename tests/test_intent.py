import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import requests

from src import agent, intent
from src.intent import NO_WORKFLOW, classify_intent, log_decision
from src.workflows import WORKFLOWS, runnable_message_workflows

FILL_THE_FORM = "Fill the form from a chat message"


def _reply(name):
    return json.dumps({"workflow": name})


def _classify(reply, workflows=None, **kwargs):
    """Fake only the OpenRouter boundary: the real prompt, the real schema and
    the real post-check all run."""
    offered = workflows if workflows is not None else [
        {"name": FILL_THE_FORM, "description": "Turns a described day into the form."}]
    with mock.patch.object(intent, "call_openrouter",
                           return_value=(reply, {}, 1.0), **kwargs) as call:
        return classify_intent("some message", offered), call


class ClassifyTest(unittest.TestCase):
    def test_a_matching_message_returns_the_name(self):
        chosen, _ = _classify(_reply(FILL_THE_FORM))
        self.assertEqual(chosen, FILL_THE_FORM)

    def test_no_match_returns_none(self):
        chosen, _ = _classify(_reply(NO_WORKFLOW))
        self.assertEqual(chosen, NO_WORKFLOW)

    def test_a_name_that_was_never_offered_is_refused(self):
        # Not a routing decision, a hallucination. Dispatching on it would run
        # something the parent never asked for.
        chosen, _ = _classify(_reply("Summarize Interac Spending"))
        self.assertEqual(chosen, NO_WORKFLOW)

    def test_the_offered_names_constrain_the_schema(self):
        _, call = _classify(_reply(FILL_THE_FORM))
        schema = call.call_args[0][2]["json_schema"]["schema"]
        self.assertEqual(schema["properties"]["workflow"]["enum"],
                         [FILL_THE_FORM, NO_WORKFLOW])
        self.assertTrue(call.call_args[0][2]["json_schema"]["strict"])

    def test_the_workflow_menu_reaches_the_prompt(self):
        _, call = _classify(_reply(NO_WORKFLOW))
        prompt = call.call_args[0][0][0]["content"]
        self.assertIn(FILL_THE_FORM, prompt)
        self.assertIn("Turns a described day into the form.", prompt)

    def test_nothing_offered_means_no_model_call_at_all(self):
        with mock.patch.object(intent, "call_openrouter") as call:
            self.assertEqual(classify_intent("hello", []), NO_WORKFLOW)
        call.assert_not_called()

    def test_an_unreachable_model_falls_through_rather_than_raising(self):
        # A routing hint is not worth failing the parent's message for.
        with mock.patch.object(intent, "call_openrouter",
                               side_effect=requests.exceptions.RequestException("down")):
            self.assertEqual(classify_intent("hi", [{"name": FILL_THE_FORM,
                                                     "description": "d"}]),
                             NO_WORKFLOW)

    def test_an_unparseable_answer_falls_through(self):
        chosen, _ = _classify("not json at all")
        self.assertEqual(chosen, NO_WORKFLOW)


class OfferedWorkflowsTest(unittest.TestCase):
    """What the classifier is allowed to pick from. Offering a workflow with
    nothing behind it means it gets picked and then cannot run."""

    def test_only_message_triggered_workflows_are_offered(self):
        for workflow, _ in runnable_message_workflows():
            with self.subTest(workflow=workflow["name"]):
                self.assertEqual(workflow["trigger"], "message")

    def test_every_offered_workflow_can_actually_run(self):
        for workflow, run in runnable_message_workflows():
            with self.subTest(workflow=workflow["name"]):
                self.assertTrue(callable(run))

    def test_declaration_only_workflows_are_excluded(self):
        offered = {w["name"] for w, _ in runnable_message_workflows()}
        self.assertNotIn("Nap-time rescue", offered)

    def test_the_registry_still_lists_every_workflow(self):
        # runnable_message_workflows filters; WORKFLOWS must not.
        self.assertEqual(len(WORKFLOWS), 4)


class LogTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False)
        tmp.close()
        os.unlink(tmp.name)
        self.path = Path(tmp.name)
        self.patcher = mock.patch.object(intent, "INTENT_LOG_PATH", self.path)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        if self.path.exists():
            os.unlink(self.path)

    def _lines(self):
        return [json.loads(line) for line in
                self.path.read_text().splitlines() if line.strip()]

    def test_a_decision_is_recorded(self):
        log_decision("we're in Vancouver", FILL_THE_FORM, ran=True)
        entry = self._lines()[0]
        self.assertEqual(entry["message"], "we're in Vancouver")
        self.assertEqual(entry["workflow"], FILL_THE_FORM)
        self.assertTrue(entry["ran"])
        self.assertIn("timestamp", entry)

    def test_appending_keeps_the_earlier_lines(self):
        # Append-only on purpose: results.json rewrites the whole file, so one
        # bad write there loses everything.
        log_decision("first", None, ran=False)
        log_decision("second", FILL_THE_FORM, ran=True)
        self.assertEqual([e["message"] for e in self._lines()], ["first", "second"])

    def test_a_no_match_is_recorded_too(self):
        # "Nothing matched" is the answer worth auditing most: it is how you
        # find messages the router should have caught.
        log_decision("hello", None, ran=False)
        self.assertIsNone(self._lines()[0]["workflow"])

    def test_an_unwritable_log_does_not_raise(self):
        with mock.patch.object(intent, "INTENT_LOG_PATH",
                              Path("/nonexistent-root/x/intents.jsonl")):
            log_decision("m", None, ran=False)  # must not raise


class HandleMessageTest(unittest.TestCase):
    """The dispatch. A match runs the workflow and skips the agent; anything
    else reaches the agent untouched."""

    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.log.start()

    def tearDown(self):
        self.log.stop()

    def test_a_match_runs_the_workflow_and_not_the_agent(self):
        with mock.patch.object(agent, "classify_intent", return_value=FILL_THE_FORM), \
             mock.patch.object(agent, "run_agent") as ran_agent, \
             mock.patch("src.workflows.plan_from_chat.run",
                        return_value={"reply": "Filled it in.", "form": {},
                                      "found": ["destination"]}) as ran_workflow:
            result = agent.handle_message("we're in Vancouver on Saturday")
        ran_workflow.assert_called_once()
        ran_agent.assert_not_called()
        self.assertEqual(result["reply"], "Filled it in.")
        self.assertEqual(result["workflow"], FILL_THE_FORM)

    def test_no_match_falls_through_to_the_agent(self):
        with mock.patch.object(agent, "classify_intent", return_value=NO_WORKFLOW), \
             mock.patch.object(agent, "run_agent",
                               return_value={"reply": "Tap Save.", "sources": []}) as ran:
            result = agent.handle_message("how do I save a plan?")
        ran.assert_called_once()
        self.assertIsNone(result["workflow"])
        self.assertEqual(result["reply"], "Tap Save.")

    def test_a_failing_workflow_falls_through_rather_than_erroring(self):
        from src.components.extract_form import FormExtractionError
        with mock.patch.object(agent, "classify_intent", return_value=FILL_THE_FORM), \
             mock.patch("src.workflows.plan_from_chat.run",
                        side_effect=FormExtractionError("bad json")), \
             mock.patch.object(agent, "run_agent",
                               return_value={"reply": "fallback", "sources": []}) as ran:
            result = agent.handle_message("we're in Vancouver")
        ran.assert_called_once()
        self.assertEqual(result["reply"], "fallback")
        self.assertIsNone(result["workflow"])

    def test_the_workflow_key_is_present_on_both_branches(self):
        # None rather than absent, so a caller can tell "nothing matched" from
        # "this response predates routing".
        with mock.patch.object(agent, "classify_intent", return_value=NO_WORKFLOW), \
             mock.patch.object(agent, "run_agent", return_value={"reply": "x"}):
            self.assertIn("workflow", agent.handle_message("hi"))

    def test_a_workflow_reply_carries_the_keys_the_widget_needs(self):
        # The bubble reads these positionally; a missing key renders as
        # undefined rather than failing loudly.
        with mock.patch.object(agent, "classify_intent", return_value=FILL_THE_FORM), \
             mock.patch("src.workflows.plan_from_chat.run",
                        return_value={"reply": "r", "form": {}, "found": []}):
            result = agent.handle_message("m")
        for key in ("reply", "sources", "model", "response_time",
                    "input_tokens", "output_tokens", "tool_calls", "workflow"):
            self.assertIn(key, result)


if __name__ == "__main__":
    unittest.main()
