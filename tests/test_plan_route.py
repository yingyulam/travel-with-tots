import re
import unittest
from unittest import mock

from src.agents import PlanningAgent, PlanningAgentError

BASE_FORM = {
    # The planning form asks for a day; /plan only builds one when asked.
    "generate": "1",
    "destination": "Vancouver", "age_years": "1", "age_months": "6",
    "wake_up": "07:00", "bedtime": "19:30", "stop_count": "3",
    "dining": "dine_out", "transit_nap": "sometimes",
}


class PlanRouteFormWiringTest(unittest.TestCase):
    """The /plan route hands the form to plan_trip field by field, so a field
    can be collected, validated, rendered, and still never reach the planner.
    That happened to `themes`: selecting one had no effect on the plan for as
    long as the argument was missing from the call, with nothing failing.
    """

    def setUp(self):
        import app as app_module
        self.client = app_module.app.test_client()

    def _plan_label(self, **extra):
        # Skip the AI adjuster so the assertion is about the rule-based draft
        # the form produced, not about what a model decided to change.
        with mock.patch.object(PlanningAgent, "adjust_plan",
                               side_effect=PlanningAgentError("rule-based only")):
            html = self.client.post("/plan", data={**BASE_FORM, **extra}) \
                              .get_data(as_text=True)
        match = re.search(r'class="plan-option-title">([^<]+)<', html)
        return match.group(1).strip() if match else None

    def test_no_theme_gives_a_mixed_day(self):
        self.assertEqual(self._plan_label(), "Mixed")

    def test_a_chosen_theme_reaches_the_planner(self):
        self.assertEqual(self._plan_label(themes=["Outdoorsy"]), "Outdoorsy")
        self.assertEqual(self._plan_label(themes=["Culture"]), "Culture")

    def test_transit_nap_reaches_the_adjuster_prompt(self):
        # Being passed to plan_trip is not enough: this field only matters if
        # it lands in the prompt, where it decides how strict a nap stop's
        # venue has to be. It was orphaned once already, when the legacy
        # planner that consumed it was deleted.
        for choice in ("yes", "no", "sometimes"):
            captured = {}

            def fake_call(messages, model, response_format=None):
                captured["prompt"] = messages[0]["content"]
                return '{"edits": []}', {}, 1.0

            with mock.patch("src.agents.call_openrouter", side_effect=fake_call):
                self.client.post("/plan", data={**BASE_FORM, "transit_nap": choice})
            with self.subTest(transit_nap=choice):
                self.assertIn(f"Can nap during transit: {choice}", captured["prompt"])
                self.assertIn("How strict a nap stop's venue needs to be",
                              captured["prompt"])

    def test_every_form_field_the_planner_accepts_is_actually_passed(self):
        # Guards the whole class of bug rather than just themes: if plan_trip
        # grows a parameter that read_form already collects, wire it up or add
        # it to the exemptions below with a reason. Asserts on the real call
        # rather than on the route's source text, so how the value is spelled
        # at the call site does not matter.
        import inspect

        import app as app_module
        from src.components.plan_trip import plan_trip
        from src.form_helpers import DEFAULTS

        with mock.patch.object(app_module, "plan_trip") as planner:
            planner.return_value = {"label": "Mixed", "blurb": "b", "stops": [],
                                    "source": "rule", "adjusted": False,
                                    "changed": False}
            self.client.post("/plan", data=BASE_FORM)
        passed = set(planner.call_args.kwargs)

        accepted = set(inspect.signature(plan_trip).parameters)
        collected = set(DEFAULTS)
        # Deliberately not passed: age is split into years/months and
        # recombined into age_months, and child ids and revise state are
        # UI-only.
        exempt = {"age_years", "age_months", "child_ids", "plan_child_id",
                  "revise_feedback"}
        for field in sorted((accepted & collected) - exempt):
            with self.subTest(field=field):
                self.assertIn(field, passed,
                              f"plan_trip accepts {field!r} and the form collects "
                              f"it, but /plan never passes it")


if __name__ == "__main__":
    unittest.main()
