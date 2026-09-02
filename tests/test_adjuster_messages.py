"""What the page says about the AI adjuster, which is: nothing, mostly.

The component still reports three outcomes -- the AI can improve the day, read
it and decide it is already right, or fail outright -- because the difference
is real and worth logging. A parent is not shown it: they asked for a day out,
and all three outcomes hand them a real plan. Development keeps the detail in
the browser console.

The exception is revising, where the parent asked for one specific change.
Silence there reads as the button doing nothing, so all three outcomes still
get a message -- worded by what happened to their plan, not by which step of
ours produced it.
"""

import re
import unittest
from unittest import mock

import app as app_module
from src.components import plan_trip as plan_module
from src.components import replan_trip as replan_module

SETTLED = "This is already the best plan for your day. No changes needed."
FAILED = "update your plan this time"   # apostrophe arrives escaped
UPDATED = "Your plan has been updated."
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


class ThePlanningPageStaysQuietTest(unittest.TestCase):
    """A first generate says nothing about the adjuster, whatever it did."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, adjusted, changed, **extra):
        plan = {"label": "L", "blurb": "b", "stops": [], "source": "rule",
                "adjusted": adjusted, "changed": changed}
        with mock.patch.object(app_module, "plan_days", return_value=[plan]):
            return self.client.post("/plan", data={**BASE, **extra},
                                    follow_redirects=True).get_data(as_text=True)

    def test_no_outcome_is_announced_on_a_first_generate(self):
        for adjusted, changed in ((True, True), (True, False), (False, False)):
            with self.subTest(adjusted=adjusted, changed=changed):
                html = self._post(adjusted=adjusted, changed=changed)
                for said in (SETTLED, FAILED, UPDATED):
                    self.assertNotIn(said, html)

    def test_a_failed_adjuster_is_not_named_to_the_parent(self):
        # The rule-based plan is a real plan. Naming the step that did not run
        # tells a parent about our pipeline, which is not theirs to act on.
        html = self._post(adjusted=False, changed=False)
        for leaking in ("AI fine-tuning", "fine-tuning step", "didn&#39;t finish",
                        "couldn&#39;t fine-tune", "Showing the rule-based plan"):
            with self.subTest(leaking=leaking):
                self.assertNotIn(leaking, html)

    def test_the_outcome_still_reaches_the_console(self):
        # Removed from the UI, not from the page: this is what keeps it
        # visible in development.
        html = self._post(adjusted=False, changed=False)
        self.assertIn("console.debug", html)
        self.assertIn('"adjusted": false', html)
        self.assertIn('"changed": false', html)

    def test_revising_still_reports_all_three_outcomes(self):
        # The parent asked for a change here, so each outcome owes them an
        # answer about their own request.
        revising = {"revise_count": "1"}
        self.assertIn(UPDATED, self._post(adjusted=True, changed=True, **revising))
        self.assertIn(SETTLED, self._post(adjusted=True, changed=False, **revising))
        self.assertIn(FAILED, self._post(adjusted=False, changed=False, **revising))


class TheTripPageStaysQuietTest(unittest.TestCase):
    """The in-trip replan had a per-stop badge and a three-way status."""

    def setUp(self):
        with open("templates/trip.html") as f:
            self.source = f.read()

    def test_the_status_no_longer_branches_on_the_adjuster(self):
        # "Proposed from ..." since a replan became something the parent
        # accepts rather than something that has already happened. The rule is
        # unchanged: this line reports what the parent can act on, and says
        # nothing about whether the AI step ran.
        status = re.search(r"say\(status, `Proposed from.*?\);",
                           self.source, re.DOTALL).group(0)
        for leaking in ("newPlan.adjusted", "nothing to change",
                        "didn't finish this time", "rule-based"):
            with self.subTest(leaking=leaking):
                self.assertNotIn(leaking, status)

    def test_no_stop_wears_an_adjusted_badge(self):
        self.assertNotIn("adjusted</span>", self.source)
        self.assertNotIn('"✨ adjusted"', self.source)

    def test_it_logs_the_outcome_instead(self):
        self.assertIn("logAdjustment(newPlan", self.source)
        # plans()[0] since a trip became a list of days: the original of
        # whichever day is open, rather than of the only one there was.
        self.assertIn("logAdjustment(plans()[0]", self.source)

    def test_it_no_longer_says_it_could_not_fine_tune_a_good_plan(self):
        self.assertNotIn("couldn't fine-tune it right now", self.source)


class ThePlanPreviewCarriesNoBadgeTest(unittest.TestCase):
    """The macro behind every stop line on the planning page."""

    def setUp(self):
        with open("templates/_stop_preview.html") as f:
            self.source = f.read()

    def test_the_badge_is_gone(self):
        self.assertNotIn("adjusted", self.source)

    def test_the_reason_tooltip_is_no_longer_a_tell(self):
        # It used to appear only on adjusted stops, which made its presence
        # the signal. Every stop has a reason, so every stop shows one.
        self.assertIn('{% if stop.reason %} title="{{ stop.reason }}"', self.source)


if __name__ == "__main__":
    unittest.main()
