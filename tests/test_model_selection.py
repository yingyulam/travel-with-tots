"""The chat widget's dropdown is the one place a model is picked.

Planning and replanning are AI calls like the chat itself, so they run on the
model the parent chose there rather than on a default nobody can see. These
cover the server half: that the choice reaches the agent, and that a value the
app does not offer cannot.
"""

import re
import unittest
from unittest import mock

import app as app_module
from src.agents import ALLOWED_CHAT_MODELS, DEFAULT_MODEL

# Deliberately not DEFAULT_MODEL. A test that "picks" the default cannot tell a
# choice being honoured from a choice being dropped, and this file used to pick
# gpt-4o-mini, which became the default underneath it.
PICKED = "nvidia/nemotron-3-super-120b-a12b:free"
PINNED = "openai/gpt-4o-mini"
DRAFT = {"label": "L", "blurb": "b", "from_time": "12:00", "stops": []}


class PickedIsNotTheDefaultTest(unittest.TestCase):
    def test_the_fixture_can_tell_the_two_apart(self):
        # Guards every assertion below: if PICKED ever equals DEFAULT_MODEL,
        # they all pass whether or not the choice is honoured.
        self.assertNotEqual(PICKED, DEFAULT_MODEL)
        self.assertIn(PICKED, ALLOWED_CHAT_MODELS)


class PlanUsesTheChosenModelTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, **extra):
        plan = {"label": "L", "blurb": "b", "stops": [], "adjusted": True,
                "changed": True}
        with mock.patch.object(app_module, "plan_trip", return_value=plan) as planned:
            self.client.post("/plan",
                              data={"destination": "Vancouver",
                                    "generate": "1", **extra})
        return planned.call_args.kwargs["model"]

    def test_the_picked_model_reaches_the_planner(self):
        self.assertEqual(self._post(model=PICKED), PICKED)

    def test_no_choice_falls_back_to_the_default(self):
        self.assertEqual(self._post(), DEFAULT_MODEL)

    def test_a_model_the_app_does_not_offer_is_refused(self):
        # The field is client-supplied, so it names a model this app offers or
        # it names nothing. Otherwise the form is a way to bill any model.
        self.assertEqual(self._post(model="some/expensive-model"), DEFAULT_MODEL)
        self.assertEqual(self._post(model=""), DEFAULT_MODEL)


class ReplanUsesTheChosenModelTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, **extra):
        body = {"plan": {"stops": []}, "situation": "nap_here",
                "current_time": "12:00", **extra}
        with mock.patch.object(app_module, "replan_trip",
                               return_value={**DRAFT, "adjusted": True,
                                             "changed": True}) as replanned:
            self.client.post("/replan/adjust", json=body)
        return replanned.call_args.kwargs["model"]

    def test_the_picked_model_reaches_the_replanner(self):
        self.assertEqual(self._post(model=PICKED), PICKED)

    def test_no_choice_falls_back_to_the_default(self):
        self.assertEqual(self._post(), DEFAULT_MODEL)

    def test_a_model_the_app_does_not_offer_is_refused(self):
        self.assertEqual(self._post(model="some/expensive-model"), DEFAULT_MODEL)


class ComponentsPassItToTheAgentTest(unittest.TestCase):
    """The route choosing a model is worth nothing if the component drops it."""

    def test_plan_trip_hands_the_model_to_the_planning_agent(self):
        from src.components import plan_trip as module
        with mock.patch.object(module, "PlanningAgent") as agent:
            agent.return_value.adjust_plan.return_value = {"stops": []}
            module.plan_trip(destination="Vancouver", age_months=24, model=PICKED)
        agent.assert_called_once_with(PICKED)

    def test_replan_trip_hands_the_model_to_the_replanning_agent(self):
        from src.components import replan_trip as module
        with mock.patch.object(module, "replan", return_value=dict(DRAFT)), \
             mock.patch.object(module, "ReplanningAgent") as agent:
            agent.return_value.adjust_replan.return_value = {"stops": []}
            module.replan_trip(plan={"stops": []}, situation="nap_here",
                               current_time="12:00", model=PICKED)
        agent.assert_called_once_with(PICKED)

    def test_the_default_is_still_the_default(self):
        from src.components import plan_trip as module
        with mock.patch.object(module, "PlanningAgent") as agent:
            agent.return_value.adjust_plan.return_value = {"stops": []}
            module.plan_trip(destination="Vancouver", age_months=24)
        agent.assert_called_once_with(DEFAULT_MODEL)


class TheChatCarriesTheChoiceTest(unittest.TestCase):
    """A chat turn, and everything the agent may reach from inside it.

    A LangGraph tool takes its arguments from the model, so the model choice
    cannot be one of them -- it travels on `agent._TURN_MODEL`, set once per
    turn. These assert the seams, so a tool added later without reading it is
    visible here rather than at the point somebody notices the wrong model
    answered.
    """

    def _turn(self, tool, model=PICKED):
        """Run one turn in which the agent calls `tool`, and hand back the
        mocks. classify_intent is stubbed to "none" so the turn reaches the
        agent rather than a workflow, and so no real routing call is made."""
        from langchain_core.messages import ToolMessage
        from src import agent as module

        called = ToolMessage(content="ok", name=tool, tool_call_id="1",
                             artifact={"reply": "ok"})
        fake = mock.Mock()
        fake.invoke.return_value = {
            "messages": [called, mock.Mock(content="done", spec=["content"])]}
        with mock.patch.object(module, "_build_agent", return_value=fake) as built, \
             mock.patch.object(module, "classify_intent", return_value="none"), \
             mock.patch.object(module, "ask_website_chatbot",
                               return_value={"reply": "ok"}) as faq, \
             mock.patch.object(module, "plan_trip",
                               return_value=dict(DRAFT)) as planned:
            module.handle_message("hello", model=model)
            # Invoked here, inside the patches: LangGraph would normally call
            # these, and this test drives the agent rather than a real model.
            if tool == "answer_faq_tool":
                module.answer_faq_tool.func("q")
            elif tool == "plan_trip_tool":
                module.plan_trip_tool.func("Vancouver", 24)
        return built, faq, planned

    def test_the_agent_itself_runs_on_the_chosen_model(self):
        built, _faq, _planned = self._turn("answer_faq_tool")
        built.assert_called_once_with(PICKED)

    def test_the_knowledge_base_answer_runs_on_the_chosen_model(self):
        _built, faq, _planned = self._turn("answer_faq_tool")
        self.assertEqual(faq.call_args.kwargs["model"], PICKED)

    def test_planning_through_chat_runs_on_the_chosen_model(self):
        # The regression this closes: planning from the form honoured the
        # dropdown while planning from the chat fell back to the default, so
        # one feature answered two ways with nothing saying which had run.
        _built, _faq, planned = self._turn("plan_trip_tool")
        self.assertEqual(planned.call_args.kwargs["model"], PICKED)

    def test_no_choice_still_means_the_default(self):
        _built, faq, _planned = self._turn("answer_faq_tool", model=DEFAULT_MODEL)
        self.assertEqual(faq.call_args.kwargs["model"], DEFAULT_MODEL)


class ThePinnedThreeDoNotFollowTheDropdownTest(unittest.TestCase):
    """Three operations keep gpt-4o-mini whatever the parent picks.

    Each for a recorded reason, not by accident: routing must not change with a
    model choice, and extraction and proposing both need structured output from
    a non-reasoning model -- measured at ~2s against 25-75s in commit a853b6c.
    """

    def test_intent_routing_stays_pinned(self):
        from src import intent
        self.assertEqual(intent.INTENT_MODEL, PINNED)

    def test_form_extraction_stays_pinned(self):
        from src.components import extract_form
        self.assertEqual(extract_form.EXTRACTOR_MODEL, PINNED)

    def test_venue_proposing_stays_pinned(self):
        from src.workflows import propose_venues
        self.assertEqual(propose_venues.CURATOR_MODEL, PINNED)

    def test_choosing_a_free_model_does_not_move_the_routing_call(self):
        # The one that would actually break: handle_message must not pass the
        # turn's model to the classifier, or picking the free reasoning model
        # would put 25-75s on the critical path of every message.
        from src import agent as module
        with mock.patch.object(module, "classify_intent",
                               return_value="none") as classified, \
             mock.patch.object(module, "_build_agent") as built:
            built.return_value.invoke.return_value = {
                "messages": [mock.Mock(content="hi", spec=["content"])]}
            module.handle_message("hello", model=PICKED)
        self.assertNotIn("model", classified.call_args.kwargs)

    def test_the_extractor_tool_does_not_take_the_turns_model(self):
        # Deliberate, and documented on the tool: the extractor keeps its own
        # known-good model because its failure mode is "no form at all".
        from src import agent as module
        with mock.patch.object(module, "extract_form",
                               return_value={"found": [], "form": {}}) as extracted:
            module._TURN_MODEL.set(PICKED)
            module.extract_form_tool.func("a day out with a toddler")
        self.assertNotIn("model", extracted.call_args.kwargs)


class TheDropdownIsTheSourceTest(unittest.TestCase):
    def test_every_offered_model_is_accepted(self):
        # The guard is the widget's own list, so adding an option to the
        # dropdown cannot leave planning silently refusing it.
        for model in ALLOWED_CHAT_MODELS:
            with self.subTest(model=model):
                self.assertEqual(app_module._chosen_model(model), model)

    def _rendered_options(self):
        """(values, selected) from the widget's dropdown as a browser sees it.

        Rendered rather than read from the template source, because the options
        come from the server now. The old version grepped the file for each
        allowed model and passed while the template *also* offered three it had
        never heard of, one of them selected -- which is exactly the drift this
        is supposed to catch.
        """
        app_module.app.config["TESTING"] = True
        html = app_module.app.test_client().get("/login").get_data(as_text=True)
        block = html.split("twt-chatbot-model-row")[1].split("</select>")[0]
        values = re.findall(r'<option value="([^"]+)"', block)
        selected = re.findall(r'<option value="([^"]+)" selected', block)
        return values, selected

    def test_the_widget_offers_exactly_those_models(self):
        values, _ = self._rendered_options()
        self.assertEqual(set(values), set(ALLOWED_CHAT_MODELS))

    def test_the_widget_offers_nothing_the_server_would_refuse(self):
        # The half the old test was missing. An option the server does not allow
        # is silently swapped for the default, so a parent picks a model and
        # gets a different one with nothing said.
        values, _ = self._rendered_options()
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(app_module._chosen_model(value), value)

    def test_the_default_is_the_one_pre_selected(self):
        # This drifted in production: the dropdown still defaulted to a free
        # router after DEFAULT_MODEL had moved, so every page load chose a model
        # nobody had picked, and free models queue.
        _values, selected = self._rendered_options()
        self.assertEqual(selected, [DEFAULT_MODEL])

    def test_no_expensive_model_is_offered_to_anonymous_callers(self):
        # /chatbot does not ask who is calling before honouring a model, so this
        # set is the ceiling on what an anonymous caller can spend. Sonnet was
        # about $5.40 an hour to abuse at the rate limit against gpt-4o-mini's
        # $0.37, so it is not offered rather than merely not default.
        self.assertNotIn("anthropic/claude-sonnet-5", ALLOWED_CHAT_MODELS)


if __name__ == "__main__":
    unittest.main()
