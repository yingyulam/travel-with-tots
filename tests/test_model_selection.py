"""The chat widget's dropdown is the one place a model is picked.

Planning and replanning are AI calls like the chat itself, so they run on the
model the parent chose there rather than on a default nobody can see. These
cover the server half: that the choice reaches the agent, and that a value the
app does not offer cannot.
"""

import re
import unittest
from src.web import planning as web_planning
from unittest import mock

import app as app_module
from src.agents import ALLOWED_CHAT_MODELS, DEFAULT_MODEL

# Deliberately not DEFAULT_MODEL. A test that "picks" the default cannot tell a
# choice being honoured from a choice being dropped. This has now swapped twice
# underneath itself -- it was gpt-4o-mini until that became the default, then
# the free model until *that* did -- which is what PickedIsNotTheDefaultTest
# below is for.
PICKED = "openai/gpt-4o-mini"

# The two operations that keep gpt-4o-mini whatever the parent picks. The same
# string as PICKED today, and kept a separate name because it is a separate
# claim: one is "a choice travels", the other is "this one never changes".
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
        # plan_days, not plan_trip: the route asks for a list of days now, and
        # forwards the model to it once for the whole trip.
        plan = {"label": "L", "blurb": "b", "stops": [], "adjusted": True,
                "changed": True}
        with mock.patch.object(web_planning, "plan_days",
                               return_value=[plan]) as planned:
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
        mocks."""
        from langchain_core.messages import ToolMessage
        from src import agent as module

        called = ToolMessage(content="ok", name=tool, tool_call_id="1",
                             artifact={"reply": "ok"})
        fake = mock.Mock()
        fake.invoke.return_value = {
            "messages": [called, mock.Mock(content="done", spec=["content"])]}
        with mock.patch.object(module, "_build_agent", return_value=fake) as built, \
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
        # Every build, not one. A turn builds the agent twice when the model
        # still has to word an answer from a tool's result, and both must run
        # on the parent's choice.
        built, _faq, _planned = self._turn("answer_faq_tool")
        self.assertTrue(built.call_args_list)
        for call in built.call_args_list:
            self.assertEqual(call.args[0], PICKED)

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

    def test_routing_no_longer_costs_a_pinned_model_call(self):
        # There was a classifier here, pinned so that picking the free
        # reasoning model did not put 25-75s on the critical path of every
        # message. Routing is the agent's tool selection now, on the parent's
        # own choice like everything else, so there is no second call to pin.
        from src import intent
        self.assertFalse(hasattr(intent, "INTENT_MODEL"))

    def test_form_extraction_stays_pinned(self):
        from src.components import extract_form
        self.assertEqual(extract_form.EXTRACTOR_MODEL, PINNED)

    def test_venue_proposing_stays_pinned(self):
        from src.workflows import propose_venues
        self.assertEqual(propose_venues.CURATOR_MODEL, PINNED)

    def test_no_routing_call_is_made_at_all_now(self):
        # There used to be a classifier here, pinned to a fast model so that
        # picking the free reasoning one did not put 25-75s on the critical
        # path of every message. The agent's tool selection is the routing
        # decision now, and it runs on the parent's choice like everything
        # else, so there is no second call left to pin.
        from src import agent as module
        self.assertFalse(hasattr(module, "classify_intent"))

    def test_the_extractor_tool_does_not_take_the_turns_model(self):
        # Deliberate: the extractor keeps its own known-good model because it
        # needs structured outputs and its failure mode is "no form at all".
        # It is the workflow's call now rather than a tool's, and the rule is
        # the same.
        from src.workflows import plan_from_chat
        with mock.patch.object(plan_from_chat, "extract_form",
                               return_value={"found": [], "form": {},
                                             "model": "m",
                                             "response_time": 1.0}) as extracted:
            plan_from_chat.run("a day out with a toddler",
                               {"stage": plan_from_chat.STAGE_COLLECTING,
                                "form": {}, "found": []}, None)
        self.assertNotIn("model", extracted.call_args.kwargs)


class TheDropdownIsTheSourceTest(unittest.TestCase):
    def test_every_offered_model_is_accepted(self):
        # The guard is the widget's own list, so adding an option to the
        # dropdown cannot leave planning silently refusing it.
        for model in ALLOWED_CHAT_MODELS:
            with self.subTest(model=model):
                self.assertEqual(web_planning._chosen_model(model), model)

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
                self.assertEqual(web_planning._chosen_model(value), value)

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


class AStoredChoiceMeansTheParentMadeOneTest(unittest.TestCase):
    """Why the deployed dropdown kept showing the paid model after the default
    moved to the free one.

    The widget wrote the dropdown's value to localStorage on every page load,
    not only when it changed. So whatever the default happened to be on a
    visitor's first visit was frozen into their browser, and then overrode
    every later change to that default -- indistinguishable, from the outside,
    from the change never having shipped.

    Asserted against the script's source, because this is browser storage and
    there is no JS harness here. Thin, but it pins the two lines that matter.
    """

    def setUp(self):
        with open("static/chatbot.js") as f:
            self.source = f.read()

    def test_the_model_is_stored_in_exactly_one_place(self):
        # Counted rather than located: the first version of this test checked
        # the region after `const savedModel` and passed when a setItem was
        # added just above it, which is the same bug in a different line.
        self.assertEqual(self.source.count("setItem(TWT_MODEL_STORAGE_KEY"), 1)

    def test_and_that_place_is_the_change_handler(self):
        # So storing means "the parent picked this", and a page load does not.
        handler = self.source.index('modelSelect.addEventListener("change"')
        self.assertGreater(self.source.index("setItem(TWT_MODEL_STORAGE_KEY"),
                           handler)

    def test_a_stored_model_we_no_longer_offer_is_dropped(self):
        # Otherwise a page without the widget sends a model nothing can check.
        self.assertIn("removeItem(TWT_MODEL_STORAGE_KEY)", self.source)

    def test_the_key_was_renamed_to_orphan_the_frozen_values(self):
        # There is no way to tell retroactively which stored values were real
        # choices, so the old ones are abandoned rather than guessed at.
        self.assertIn('TWT_MODEL_STORAGE_KEY = "twt_chatbot_model_v2"', self.source)
        self.assertIn("removeItem(TWT_MODEL_STORAGE_KEY_LEGACY)", self.source)


if __name__ == "__main__":
    unittest.main()
