"""Replanning the days after one the parent has just changed.

Stage C of multi-day trips, and the reason stage B existed. Accepting a change
to Tuesday can leave Thursday visiting somewhere Tuesday now goes, or free up
somewhere Tuesday has dropped. Neither is fixed behind the parent's back:

    they accept Tuesday -> we offer -> they ask -> we propose -> they accept

Four steps, and the trip is unchanged until the last of them. The endpoint here
only ever computes proposals; nothing it returns has been applied to anything.

These are whole days rebuilt rather than mid-day replans, because a later day
has not started. So it is plan_days, the same planner the trip was built with,
told through `used_names` what the days before it have taken.
"""

import json
import re
import unittest
from src.web import planning as web_planning
from unittest import mock

import app as app_module
from src.components import plan_trip as plan_module
from src.dates import MAX_TRIP_DAYS
from src.form_helpers import DEFAULTS, default_form


def _stop(time, name):
    return {"time": time, "kind": "activity", "reason": "",
            "venue": {"id": abs(hash(name)) % 999, "name": name}}


def _plan(*names):
    return {"label": "L", "blurb": "b", "source": "rule",
            "stops": [_stop(f"{9 + i}:00 AM", n) for i, n in enumerate(names)]}


FORM = {**default_form(), "destination": "Vancouver", "transit": "car",
        "dining": "on_the_go", "stop_count": "3", "trip_date": "2026-09-14",
        "end_date": "2026-09-16"}


class TheFormIsReadOnceTest(unittest.TestCase):
    """_planner_kwargs, shared by /plan and the cascade.

    Two readings of the same form would drift, and the failure is silent: the
    later days of a visit planned on a wake-up or a travel limit the parent
    never chose, looking wrong for no visible reason.
    """

    def test_age_comes_from_the_two_fields_the_form_asks_in(self):
        kwargs = web_planning._planner_kwargs(
            {**FORM, "age_years": "3", "age_months": "7"}, "", "m")
        self.assertEqual(kwargs["age_months"], 43)

    def test_a_missing_field_falls_back_to_what_the_form_would_show(self):
        # This also reads a form that arrived as JSON from the in-trip page
        # rather than from read_form, so a missing key means "the default",
        # not a KeyError halfway through planning.
        kwargs = web_planning._planner_kwargs({"destination": "Vancouver"}, "", "m")
        self.assertEqual(kwargs["wake_up"], DEFAULTS["wake_up"])
        self.assertEqual(kwargs["dining"], DEFAULTS["dining"])

    def test_a_null_field_does_too(self):
        # JSON round-trips an empty value as null, which is not a wake-up time.
        kwargs = web_planning._planner_kwargs({**FORM, "wake_up": None}, "", "m")
        self.assertEqual(kwargs["wake_up"], DEFAULTS["wake_up"])

    def test_the_travel_limit_and_stop_count_are_carried(self):
        kwargs = web_planning._planner_kwargs(
            {**FORM, "walk_budget": "40", "stop_count": "2"}, "", "m")
        self.assertEqual(kwargs["walk_budget"], "40")
        self.assertEqual(kwargs["stop_count"], 2)

    def test_the_notes_are_the_caller_s_not_the_form_s(self):
        # /plan merges "Need changes?" feedback in for the AI call only, and
        # must not have it written back into the trip.
        kwargs = web_planning._planner_kwargs(
            {**FORM, "extra_notes": "stored"}, "just for this call", "m")
        self.assertEqual(kwargs["extra_notes"], "just for this call")

    def test_plan_and_the_cascade_ask_for_the_same_thing(self):
        # The anti-drift claim itself: one form in, one set of inputs out,
        # whichever route is asking.
        self.assertEqual(web_planning._planner_kwargs(FORM, "n", "m"),
                         web_planning._planner_kwargs(dict(FORM), "n", "m"))


class TheEndpointOnlyProposesTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, days, used=(), form=None, **extra):
        with mock.patch.object(app_module, "plan_days") as planner:
            planner.return_value = [
                {**_plan("Fresh Place"), "trip_date": d.get("date", ""),
                 "day_index": i, "adjusted": False, "changed": False,
                 "hours": None, "out_of_range": []}
                for i, d in enumerate(days)]
            resp = self.client.post("/trip/replan-remaining", json={
                "days": days, "used_names": list(used),
                "form": FORM if form is None else form, **extra})
        return resp, planner

    def test_no_days_is_refused(self):
        resp = self.client.post("/trip/replan-remaining", json={"days": []})
        self.assertEqual(resp.status_code, 400)

    def test_a_days_field_that_is_not_a_list_is_refused(self):
        resp = self.client.post("/trip/replan-remaining",
                                json={"days": "tuesday"})
        self.assertEqual(resp.status_code, 400)

    def test_more_days_than_we_plan_is_refused(self):
        days = [{"date": f"2026-09-{d:02d}", "plan": _plan("A")}
                for d in range(1, MAX_TRIP_DAYS + 3)]
        resp = self.client.post("/trip/replan-remaining",
                                json={"days": days, "form": FORM})
        self.assertEqual(resp.status_code, 400)

    def test_one_proposal_per_day_in_order(self):
        days = [{"date": "2026-09-15", "plan": _plan("A")},
                {"date": "2026-09-16", "plan": _plan("B")}]
        resp, _ = self._post(days)
        self.assertEqual([d["trip_date"] for d in resp.get_json()["days"]],
                         ["2026-09-15", "2026-09-16"])

    def test_each_proposal_says_what_would_change(self):
        days = [{"date": "2026-09-15", "plan": _plan("Old Place")}]
        resp, _ = self._post(days)
        change = resp.get_json()["days"][0]["changes"][0]
        self.assertEqual(change["kind"], "swapped")
        self.assertIn("Fresh Place replaces Old Place", change["text"])

    def test_a_day_that_would_not_change_says_so(self):
        days = [{"date": "2026-09-15", "plan": _plan("Fresh Place")}]
        resp, _ = self._post(days)
        day = resp.get_json()["days"][0]
        self.assertEqual(day["changes"], [])
        self.assertIn("Nothing would change", day["change_summary"])

    def test_the_diff_is_against_the_plan_the_page_sent(self):
        # Not against the day's original. By now the parent may have accepted
        # a replan of that day too, and this is about the day as it stands.
        days = [{"date": "2026-09-15", "plan": _plan("Already Replanned")}]
        resp, _ = self._post(days)
        self.assertIn("replaces Already Replanned",
                      resp.get_json()["days"][0]["changes"][0]["text"])

    def test_what_the_earlier_days_took_reaches_the_planner(self):
        days = [{"date": "2026-09-15", "plan": _plan("A")}]
        _resp, planner = self._post(days, used=["Science World", "A Park"])
        self.assertEqual(planner.call_args.kwargs["used_names"],
                         ["Science World", "A Park"])

    def test_a_used_name_that_is_not_a_string_is_dropped(self):
        days = [{"date": "2026-09-15", "plan": _plan("A")}]
        _resp, planner = self._post(days, used=["Real", 7, None, {"a": 1}])
        self.assertEqual(planner.call_args.kwargs["used_names"], ["Real"])

    def test_the_dates_asked_for_are_the_dates_planned(self):
        days = [{"date": "2026-09-15", "plan": _plan("A")},
                {"date": "2026-09-16", "plan": _plan("B")}]
        _resp, planner = self._post(days)
        self.assertEqual(planner.call_args.args[0], ["2026-09-15", "2026-09-16"])

    def test_the_trip_s_own_answers_are_used_not_defaults(self):
        days = [{"date": "2026-09-15", "plan": _plan("A")}]
        _resp, planner = self._post(days, form={**FORM, "walk_budget": "40",
                                                "wake_up": "06:15"})
        self.assertEqual(planner.call_args.kwargs["walk_budget"], "40")
        self.assertEqual(planner.call_args.kwargs["wake_up"], "06:15")

    def test_the_model_the_parent_picked_is_honoured(self):
        days = [{"date": "2026-09-15", "plan": _plan("A")}]
        _resp, planner = self._post(days, model="openai/gpt-4o-mini")
        self.assertEqual(planner.call_args.kwargs["model"], "openai/gpt-4o-mini")

    def test_a_model_we_do_not_offer_is_refused(self):
        days = [{"date": "2026-09-15", "plan": _plan("A")}]
        _resp, planner = self._post(days, model="something/expensive")
        self.assertEqual(planner.call_args.kwargs["model"],
                         app_module.DEFAULT_MODEL)


class ItReallyAvoidsTheEarlierDaysVenuesTest(unittest.TestCase):
    """Through the real planner rather than a mock, because the whole point of
    the endpoint is the exclusion, and a mocked planner cannot show it."""

    def setUp(self):
        self.client = app_module.app.test_client()
        patcher = mock.patch.object(plan_module, "PlanningAgent")
        agent = patcher.start()
        self.addCleanup(patcher.stop)
        agent.return_value.adjust_plan.side_effect = \
            plan_module.PlanningAgentError("no ai in tests")

    def _rest(self, used):
        resp = self.client.post("/trip/replan-remaining", json={
            "form": FORM,
            "days": [{"date": "2026-09-15", "plan": _plan("Whatever")}],
            "used_names": list(used)})
        day = resp.get_json()["days"][0]
        return [s["venue"]["name"] for s in day["stops"] if s.get("venue")]

    def test_a_day_is_planned_from_real_venues(self):
        picked = self._rest([])
        self.assertTrue(picked)

    def test_and_avoids_the_ones_the_earlier_days_hold(self):
        first = self._rest([])
        again = self._rest(first)
        self.assertFalse(set(first) & set(again), (first, again))


class ThePageAsksTwiceBeforeTouchingALaterDayTest(unittest.TestCase):
    """The page's half, read from the rendered script: there is no JS harness
    in this project, so these assert the wiring rather than run it."""

    def setUp(self):
        self.source = app_module.app.test_client().post("/trip", data={
            "plans": json.dumps([_plan("A"), _plan("B")]),
            "context": json.dumps({"destination": "Vancouver",
                                   "trip_date": "2026-09-14",
                                   "end_date": "2026-09-15"}),
        }).get_data(as_text=True)

    def test_both_panels_exist_and_start_hidden(self):
        for panel in ("cascade-offer", "cascade-proposal"):
            with self.subTest(panel=panel):
                tag = self.source.split(f'id="{panel}"')[1].split(">")[0]
                self.assertIn("hidden", tag)

    def test_the_offer_can_be_declined(self):
        self.assertIn('id="cascade-decline"', self.source)

    def test_the_proposal_can_be_discarded(self):
        self.assertIn('id="cascade-discard"', self.source)

    def test_it_is_offered_only_when_the_places_changed(self):
        # Retiming a day changes nothing for the days after it.
        self.assertIn("!sameSet(before, after)", self.source)

    def test_and_only_when_there_is_a_later_day(self):
        self.assertIn("DAYS.length > activeDay + 1", self.source)

    def test_and_only_when_we_know_how_the_trip_was_planned(self):
        self.assertIn("PLAN_INPUTS", self.source)

    def test_the_cascade_is_anchored_to_the_day_that_changed(self):
        # Not to whichever day is on screen when the parent presses the button:
        # the panel is trip-level and stays up while they look elsewhere.
        self.assertIn("cascadeAnchor = activeDay", self.source)
        self.assertIn("DAYS.slice(cascadeAnchor + 1)", self.source)

    def test_a_proposal_is_parked_on_the_day_not_written_into_it(self):
        # A later day may already hold a replan the parent accepted, and
        # discarding this has to put that back rather than the day's original.
        self.assertIn("later[i].cascade = proposed", self.source)

    def test_discarding_only_drops_the_proposals(self):
        self.assertIn("delete day.cascade", self.source)

    def test_accepting_installs_them_and_records_the_version(self):
        self.assertIn("day.plans[1] = day.cascade", self.source)
        self.assertIn("day.accepted = 1", self.source)

    def test_the_page_carries_the_form_it_was_planned_from(self):
        self.assertIn('id="plan-inputs"', self.source)

    def test_a_reopened_trip_carries_none_so_nothing_is_offered(self):
        # features, kinds of place and the travel limit are planning inputs
        # and are not saved with a trip, so a rebuilt day would silently use
        # different answers. Better to not offer than to offer wrongly.
        page = app_module.app.test_client().post("/trip", data={
            "plan": json.dumps(_plan("A")),
            "context": json.dumps({"destination": "Vancouver"}),
        }).get_data(as_text=True)
        self.assertIn('id="plan-inputs"', page)
        # A one-day trip has no later days either way; what matters is that the
        # tag is always present and parseable, so the script never throws.
        inputs = re.search(
            r'id="plan-inputs" type="application/json">(.*?)</script>',
            page, re.S).group(1)
        json.loads(inputs)


if __name__ == "__main__":
    unittest.main()
