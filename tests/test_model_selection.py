"""The chat widget's dropdown is the one place a model is picked.

Planning and replanning are AI calls like the chat itself, so they run on the
model the parent chose there rather than on a default nobody can see. These
cover the server half: that the choice reaches the agent, and that a value the
app does not offer cannot.
"""

import unittest
from unittest import mock

import app as app_module
from src.agents import ALLOWED_CHAT_MODELS, DEFAULT_MODEL

PICKED = "openai/gpt-4o-mini"
DRAFT = {"label": "L", "blurb": "b", "from_time": "12:00", "stops": []}


class PlanUsesTheChosenModelTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, **extra):
        plan = {"label": "L", "blurb": "b", "stops": [], "adjusted": True,
                "changed": True}
        with mock.patch.object(app_module, "plan_trip", return_value=plan) as planned:
            self.client.post("/plan",
                              data={"destination": "Burnaby",
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
            module.plan_trip(destination="Burnaby", age_months=24, model=PICKED)
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
            module.plan_trip(destination="Burnaby", age_months=24)
        agent.assert_called_once_with(DEFAULT_MODEL)


class TheDropdownIsTheSourceTest(unittest.TestCase):
    def test_every_offered_model_is_accepted(self):
        # The guard is the widget's own list, so adding an option to the
        # dropdown cannot leave planning silently refusing it.
        for model in ALLOWED_CHAT_MODELS:
            with self.subTest(model=model):
                self.assertEqual(app_module._chosen_model(model), model)

    def test_the_widget_offers_exactly_those_models(self):
        with open("templates/_chatbot_widget.html") as f:
            markup = f.read()
        for model in ALLOWED_CHAT_MODELS:
            with self.subTest(model=model):
                self.assertIn(f'value="{model}"', markup)


if __name__ == "__main__":
    unittest.main()
