import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import requests

from src import agent, intent
from src.intent import NO_WORKFLOW, classify_intent, log_decision
from src import workflows as workflows_module
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

    def test_a_workflow_with_nothing_behind_it_is_excluded(self):
        # Tested against a stub rather than a real module, because every
        # workflow is runnable now. The guard still matters: offering the
        # classifier a name with no run means it confidently picks something
        # that cannot then be executed, which is worse than the chatbot.
        stub = types.SimpleNamespace(
            WORKFLOW={"name": "Not built yet", "emoji": "🚧",
                      "trigger": "message", "description": "One sentence.",
                      "steps": [{"component": "Nothing", "built": False}]})
        with mock.patch.object(workflows_module, "_MODULES",
                               (*workflows_module._MODULES, stub)):
            offered = {w["name"] for w, _ in runnable_message_workflows()}
        self.assertNotIn("Not built yet", offered)

    def test_a_non_message_workflow_is_excluded_even_with_a_run(self):
        stub = types.SimpleNamespace(
            WORKFLOW={"name": "Started by something else", "emoji": "🔁",
                      "trigger": "event", "description": "One sentence.",
                      "steps": [{"component": "Thing", "built": True}]},
            run=lambda *a, **k: {"reply": "hi"})
        with mock.patch.object(workflows_module, "_MODULES",
                               (*workflows_module._MODULES, stub)):
            offered = {w["name"] for w, _ in runnable_message_workflows()}
        self.assertNotIn("Started by something else", offered)

    def test_the_registry_still_lists_every_workflow(self):
        # runnable_message_workflows filters; WORKFLOWS must not. Asserted
        # against the module list rather than a fixed count, so adding a
        # workflow does not fail a test that is about filtering.
        self.assertEqual(len(WORKFLOWS), len(workflows_module._MODULES))
        self.assertGreater(len(WORKFLOWS), len(list(runnable_message_workflows())))


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

    def test_the_agents_tool_lands_in_the_file(self):
        # The requirement is the file, not the call: routing accuracy is read
        # from data/intents.jsonl, and asserting the arguments alone let the
        # key be dropped from the record with every test still passing.
        log_decision("how do I save a plan?", None, ran=True,
                     tool="answer_faq_tool")
        entry = self._lines()[0]
        self.assertEqual(entry["tool"], "answer_faq_tool")
        self.assertIsNone(entry["workflow"])

    def test_a_workflow_and_a_tool_are_separate_keys(self):
        # /chatbot routes by tool and /workflows/<name>/run by workflow, so a
        # line says which router decided it by which key is filled. One field
        # holding either kind of name could not be counted.
        log_decision("a nursing room", "Find a nearby place", ran=True)
        entry = self._lines()[0]
        self.assertEqual(entry["workflow"], "Find a nearby place")
        self.assertIsNone(entry["tool"])

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

    def test_a_forced_decision_is_marked_as_forced(self):
        # This file is what classifier accuracy is measured from, and a turn an
        # admin test page directed never went near the classifier. Tested on
        # the real writer, not on a mock of it: asserting that handle_message
        # *passes* forced= says nothing about whether the logger records it.
        log_decision("anything", FILL_THE_FORM, ran=True, forced=True)
        self.assertTrue(self._lines()[0]["forced"])

    def test_an_ordinary_decision_is_not(self):
        log_decision("we're in Vancouver", FILL_THE_FORM, ran=True)
        self.assertFalse(self._lines()[0]["forced"])

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
    """What /chatbot does now: the agent, every time.

    A classifier used to run first and hand matching messages to a workflow,
    so two routers decided every turn and three of the four tools duplicated a
    workflow. The workflows still run, on /workflows/<name>/run, which is what
    the dispatch tests target now.
    """

    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.logged = self.log.start()

    def tearDown(self):
        self.log.stop()

    def test_every_message_reaches_the_agent(self):
        # Including one the classifier would once have caught. Nothing about
        # the message can route it anywhere else.
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "ok", "tool_calls": []}) as ran:
            agent.handle_message("we're in Vancouver on Saturday")
        ran.assert_called_once()

    def test_no_workflow_runs_from_here(self):
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "ok", "tool_calls": []}), \
             mock.patch("src.workflows.plan_from_chat.run") as workflow:
            result = agent.handle_message("we're in Vancouver on Saturday")
        workflow.assert_not_called()
        self.assertIsNone(result["workflow"])

    def test_the_classifier_is_not_consulted(self):
        # It is no longer imported here at all; this fails loudly if it returns.
        self.assertFalse(hasattr(agent, "classify_intent"))

    def test_the_reply_carries_the_keys_the_widget_needs(self):
        # The bubble reads these positionally; a missing key renders as
        # undefined rather than failing loudly. "workflow" and "conversation"
        # stay, always None, because the widget still reads both.
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "r", "sources": [],
                                             "model": "m", "response_time": None,
                                             "input_tokens": None,
                                             "output_tokens": None,
                                             "tool_calls": []}):
            result = agent.handle_message("m")
        for key in ("reply", "sources", "model", "response_time",
                    "input_tokens", "output_tokens", "tool_calls", "workflow",
                    "conversation"):
            self.assertIn(key, result)

    def test_the_tool_the_agent_picked_is_logged(self):
        # data/intents.jsonl is where routing accuracy is read from. The
        # classifier used to write that line; the agent's tool choice is the
        # same decision made by the thing that now makes it.
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "ok",
                                             "tool_calls": [{"name": "answer_faq_tool"}]}):
            agent.handle_message("how do I save a plan?")
        self.assertEqual(self.logged.call_args.kwargs["tool"], "answer_faq_tool")
        self.assertIs(self.logged.call_args.kwargs["ran"], True)

    def test_a_turn_that_used_no_tool_is_logged_as_such(self):
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "Hi!", "tool_calls": []}):
            agent.handle_message("hello")
        self.assertIsNone(self.logged.call_args.kwargs["tool"])
        self.assertIs(self.logged.call_args.kwargs["ran"], False)


if __name__ == "__main__":
    unittest.main()
