"""What the page says about the AI adjuster.

Three outcomes, not two: it can improve the day, read it and decide it is
already right, or fail outright. The last two used to look identical to a
parent, so an adjuster that agreed with the plan was reported as "couldn't
fine-tune it right now" and read as something being broken.
"""

import re
import unittest
from unittest import mock

import app as app_module
from src.components import plan_trip as plan_module
from src.components import replan_trip as replan_module

SETTLED = "This is already the best plan for your day. No changes needed."
FAILED = "finish this time"
BASE = {
    # The planning form asks for a day; /plan only builds one when asked.
    "generate": "1","destination": "Vancouver", "age_years": "2", "age_months": "0"}


def _stop(name="Science World", adjusted=False):
    stop = {"time": "9:00 AM", "kind": "stop", "reason": "why",
            "venue": {"name": name}}
    if adjusted:
        stop["adjusted"] = True
    return stop


class TheComponentReportsWhatHappenedTest(unittest.TestCase):
    """`adjusted` says whether the AI step ran; `changed` says whether it moved
    anything. Only both together can tell agreement from failure."""

    def _plan(self, returned_stops=None, fail=False):
        draft = mock.Mock(stops=[_stop()],
                          to_dict=lambda: {"label": "L", "blurb": "b",
                                           "stops": draft.stops})
        with mock.patch.object(plan_module, "generate_plans", return_value=[draft]), \
             mock.patch.object(plan_module, "PlanningAgent") as agent:
            if fail:
                agent.return_value.adjust_plan.side_effect = \
                    plan_module.PlanningAgentError("bad reply")
            else:
                agent.return_value.adjust_plan.return_value = {"stops": returned_stops}
            return plan_module.plan_trip(destination="Vancouver", age_months=24)

    def test_a_moved_stop_counts_as_changed(self):
        result = self._plan([_stop(adjusted=True)])
        self.assertTrue(result["adjusted"])
        self.assertTrue(result["changed"])

    def test_the_ai_leaving_the_day_alone_is_not_a_failure(self):
        # The state that had no representation before: the call succeeded and
        # marked nothing, because the draft was already right.
        result = self._plan([_stop()])
        self.assertTrue(result["adjusted"])
        self.assertFalse(result["changed"])

    def test_a_failed_call_is_still_a_failed_call(self):
        result = self._plan(fail=True)
        self.assertFalse(result["adjusted"])
        self.assertFalse(result["changed"])

    def test_replanning_reports_the_same_three_ways(self):
        draft = {"label": "L", "blurb": "b", "from_time": "12:00",
                 "stops": [_stop(adjusted=True)]}
        with mock.patch.object(replan_module, "replan",
                               side_effect=lambda *a, **k: dict(draft)), \
             mock.patch.object(replan_module, "ReplanningAgent") as agent:
            agent.return_value.adjust_replan.return_value = {
                "stops": [_stop(adjusted=True)]}
            changed = replan_module.replan_trip(plan={"stops": []},
                                                situation="nap_here",
                                                current_time="12:00")
            agent.return_value.adjust_replan.return_value = {"stops": [_stop()]}
            kept = replan_module.replan_trip(plan={"stops": []},
                                             situation="nap_here",
                                             current_time="12:00")
        self.assertTrue(changed["changed"])
        self.assertFalse(kept["changed"])
        self.assertTrue(kept["adjusted"])


class ThePlanningPageSaysSoTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, adjusted, changed, **extra):
        plan = {"label": "L", "blurb": "b", "stops": [], "source": "rule",
                "adjusted": adjusted, "changed": changed}
        with mock.patch.object(app_module, "plan_trip", return_value=plan):
            return self.client.post("/plan", data={**BASE, **extra},
                                    follow_redirects=True).get_data(as_text=True)

    def test_an_agreeing_adjuster_is_good_news(self):
        html = self._post(adjusted=True, changed=False)
        self.assertIn(SETTLED, html)
        self.assertNotIn(FAILED, html)

    def test_it_no_longer_says_anything_could_not_be_done(self):
        # The reported problem, stated directly.
        html = self._post(adjusted=True, changed=False)
        for alarming in ("couldn't fine-tune", "Couldn't fine-tune"):
            with self.subTest(alarming=alarming):
                self.assertNotIn(alarming, html)

    def test_a_real_failure_still_says_so(self):
        # Rewording this away would claim the AI approved a plan it never
        # successfully reviewed.
        html = self._post(adjusted=False, changed=False)
        self.assertIn(FAILED, html)
        self.assertNotIn(SETTLED, html)

    def test_an_improved_plan_says_nothing_on_a_first_generate(self):
        html = self._post(adjusted=True, changed=True)
        self.assertNotIn(SETTLED, html)
        self.assertNotIn(FAILED, html)

    def test_revising_reports_all_three_outcomes(self):
        revising = {"revise_count": "1"}
        self.assertIn("has been updated",
                      self._post(adjusted=True, changed=True, **revising))
        self.assertIn(SETTLED, self._post(adjusted=True, changed=False, **revising))
        self.assertIn(FAILED, self._post(adjusted=False, changed=False, **revising))


class TheTripPageSaysSoTest(unittest.TestCase):
    """The in-trip replan had the same two-way message."""

    def setUp(self):
        with open("templates/trip.html") as f:
            self.source = f.read()

    def test_it_distinguishes_the_three_outcomes(self):
        status = re.search(r"status\.textContent = !newPlan\.adjusted.*?;",
                           self.source, re.DOTALL).group(0)
        self.assertIn("newPlan.changed", status)
        self.assertIn("nothing to change", status)
        self.assertIn("didn't finish this time", status)

    def test_it_no_longer_says_it_could_not_fine_tune_a_good_plan(self):
        self.assertNotIn("couldn't fine-tune it right now", self.source)


if __name__ == "__main__":
    unittest.main()
