"""Who owns a turn: a running workflow, or the agent.

The bug this shape fixes: a tool needs arguments and a conversation starts with
none, so log_place_tool(name) could not be called before a name existed.
Measured, three of four bare intents -- "I want to add a place", "I want to log
a place we're missing", "I want to plan a day" -- called no tool at all and were
answered as conversation instead of starting anything.

So a workflow tool takes no arguments. Selecting it is the only decision the
model makes; the questions, the chips, the state, the cancelling and the
completion all belong to the workflow, which is what those four are built for.

And once one is running it owns every message until it finishes or the parent
cancels, decided without a model call: "yes" and "Vancouver" are answers to the
question just asked, and re-classifying them is how a flow gets derailed.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import unittest
from unittest import mock

from src.ai import tool_agent as agent
from src.workflows import runnable_message_workflows


class ToolsAreTheRegistryTest(unittest.TestCase):
    def test_there_is_one_tool_per_message_workflow(self):
        # Generated rather than listed, so a new workflow becomes a capability
        # and the two lists cannot disagree about what exists.
        names = {t.name for t in agent.TOOLS}
        for workflow, _run in runnable_message_workflows():
            with self.subTest(workflow=workflow["name"]):
                self.assertIn(agent._slug(workflow["name"]), names)

    def test_the_only_other_tool_is_the_knowledge_base(self):
        expected = {"answer_faq_tool"} | {
            agent._slug(w["name"]) for w, _ in runnable_message_workflows()}
        self.assertEqual({t.name for t in agent.TOOLS}, expected)

    def test_no_workflow_tool_takes_arguments(self):
        # The fix, stated. An argument is something the model has to invent
        # before the parent has said it.
        for workflow, _run in runnable_message_workflows():
            tool = next(t for t in agent.TOOLS
                        if t.name == agent._slug(workflow["name"]))
            with self.subTest(workflow=workflow["name"]):
                self.assertEqual(tool.args, {})

    def test_each_tool_describes_its_own_workflow(self):
        # The description is what the model chooses on, and it comes from the
        # workflow's own declaration rather than a second copy.
        for workflow, _run in runnable_message_workflows():
            tool = next(t for t in agent.TOOLS
                        if t.name == agent._slug(workflow["name"]))
            with self.subTest(workflow=workflow["name"]):
                self.assertEqual(tool.description, workflow["description"])

    def test_a_workflow_writes_its_own_reply(self):
        # Its chips answer the exact question it asked, so the model does not
        # get a turn to reword it.
        for workflow, _run in runnable_message_workflows():
            with self.subTest(workflow=workflow["name"]):
                self.assertIn(agent._slug(workflow["name"]),
                              agent.FINAL_ANSWER_TOOLS)


class StartingAWorkflowTest(unittest.TestCase):
    NAME = "Log a place we don't have"

    def _tool(self):
        return next(t for t in agent.TOOLS
                    if t.name == agent._slug(self.NAME))

    def test_it_hands_over_the_parents_own_words(self):
        # Not a paraphrase. split_name and read_situation both read the raw
        # sentence, so a model-written version would change what they see.
        with mock.patch.object(agent, "run_workflow_turn",
                               return_value={"reply": "ok"}) as ran:
            token = agent._TURN_MESSAGE.set("I want to add a place")
            try:
                self._tool().func()
            finally:
                agent._TURN_MESSAGE.reset(token)
        self.assertEqual(ran.call_args.args, (self.NAME, "I want to add a place"))

    def test_it_hands_over_the_request_context(self):
        # Coordinates, on_trip and parent_id all live here, and every workflow
        # reads some of it.
        context = {"lat": 49.2, "lng": -123.1, "parent_id": 7}
        with mock.patch.object(agent, "run_workflow_turn",
                               return_value={"reply": "ok"}) as ran:
            token = agent._TURN_CONTEXT.set(context)
            try:
                self._tool().func()
            finally:
                agent._TURN_CONTEXT.reset(token)
        self.assertEqual(ran.call_args.kwargs["context"], context)

    def test_a_workflow_that_cannot_run_says_so_rather_than_raising(self):
        with mock.patch.object(agent, "run_workflow_turn", return_value=None):
            content, artifact = self._tool().func()
        self.assertEqual(artifact, {})
        self.assertIn("could not run", content)


class ARunningWorkflowOwnsTheTurnTest(unittest.TestCase):
    CONVERSATION = {"workflow": "Log a place we don't have",
                    "state": {"stage": "name", "values": {}}}

    def test_the_agent_is_never_built_mid_flow(self):
        # Not "the agent decides not to": it is never asked. A model call to
        # re-classify "Bean There" is both wasted and a way to lose the flow.
        with mock.patch.object(agent, "_build_agent",
                               side_effect=AssertionError("agent was built")), \
             mock.patch.object(agent, "run_workflow_turn",
                               return_value={"reply": "ok"}) as ran:
            agent.handle_message("Bean There", conversation=self.CONVERSATION)
        self.assertEqual(ran.call_args.args[0], self.CONVERSATION["workflow"])

    def test_the_conversation_is_passed_through(self):
        with mock.patch.object(agent, "run_workflow_turn",
                               return_value={"reply": "ok"}) as ran:
            agent.handle_message("Bean There", conversation=self.CONVERSATION)
        self.assertEqual(ran.call_args.kwargs["conversation"], self.CONVERSATION)

    def test_a_workflow_that_vanished_falls_back_to_the_agent(self):
        # Only reachable if it was unregistered mid-conversation or raised. The
        # flow is over either way, and the parent should not lose their turn.
        with mock.patch.object(agent, "run_workflow_turn", return_value=None), \
             mock.patch.object(agent, "run_agent",
                               return_value={"reply": "hi", "tool_calls": []}) as ran, \
             mock.patch.object(agent, "log_decision"):
            agent.handle_message("Bean There", conversation=self.CONVERSATION)
        ran.assert_called_once()

    def test_no_conversation_means_the_agent_decides(self):
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "hi", "tool_calls": []}) as ran, \
             mock.patch.object(agent, "log_decision"):
            agent.handle_message("hello")
        ran.assert_called_once()


class RoutingStaysObservableTest(unittest.TestCase):
    def test_a_workflow_turn_is_logged_once(self):
        # run_workflow_turn writes its own line. Logging again in
        # handle_message would count one message twice in the file routing
        # accuracy is read from.
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "ok", "tool_calls": [],
                                             "workflow": "Find a nearby place"}), \
             mock.patch.object(agent, "log_decision") as logged:
            agent.handle_message("I need a nursing room")
        logged.assert_not_called()

    def test_a_tool_less_turn_is_logged_here(self):
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "hi", "tool_calls": []}), \
             mock.patch.object(agent, "log_decision") as logged:
            agent.handle_message("hello")
        logged.assert_called_once()


if __name__ == "__main__":
    unittest.main()
