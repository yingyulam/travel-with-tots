"""`interest`: which kinds of place the parent asked for.

All that replaced the theme system, and deliberately the venue type list itself
rather than a grouping over it. Groups added nothing measurable -- because an
interest only ever *sorts*, asking for "museum" still reaches the aquarium a
few places down -- and a second vocabulary is exactly how themes rotted, with
10 of the 14 types ending up in no theme at all.

The faults this locks out, all measured on the old system:

1. A theme *filtered*, so one museum in the pool could throw five other open
   venues away and return a one-stop day.
2. Selecting no theme silently applied "Mixed", the union of the three type
   sets, which deprioritised 10 of 14 types. "No preference" was a preference.
3. A garden is outdoor *and* cultural and matched no theme at all, so an
   outdoor cultural day was inexpressible.

The form asks it as a fully ticked list now. "Any particular kind of place?
(optional)", answered by leaving it blank, hid the rule that blank meant a mix,
and made ticking two look like a filter when it was a sort. Ticking everything
and ticking nothing plan the same day, so the new question is the same
behaviour asked in a way a parent can see -- which is what the last class here
pins.
"""

import unittest
from datetime import date
from unittest import mock

from werkzeug.datastructures import MultiDict

import app as app_module
from src import data_loader, itinerary
from src.components import plan_trip as plan_module
from src.data_loader import VENUE_TYPES
from src.form_helpers import all_interests, default_form, read_form

ON = date(2026, 9, 15)
INPUTS = {"wake_up": "07:00", "bedtime": "19:30", "naps": [],
          "transit_nap": "no", "destination": "Vancouver", "transit": "car",
          "stop_count": "3", "dining": "on_the_go",
          "preferred_lunch_time": "11:30", "features": [],
          "age_months": 30, "age_years": 2, "interest": [],
          "trip_date": ON.isoformat()}


def _venue(name, venue_type, **over):
    return {"id": abs(hash(name)) % 9999, "name": name, "type": venue_type,
            "setting": "outdoor", "neighbourhood": "Downtown",
            "open": "08:00", "close": "20:00", "hours_source": "default",
            "can_eat": False, "nap_friendly": venue_type in ("park", "mall"),
            "lat": 49.28, "lng": -123.12, "maps_url": "", **over}


def _chosen(pool, interest=(), stop_count="3"):
    plans = itinerary.generate_plans(
        pool, {**INPUTS, "interest": list(interest), "stop_count": stop_count})
    return [s["venue"] for s in plans[0].stops if s.get("venue")]


class NothingIsExcludedTest(unittest.TestCase):
    def test_a_preference_never_empties_a_day(self):
        # The old filter's failure: six open venues, three stops asked for,
        # one returned, because only one matched the theme.
        pool = [_venue("Hillcrest Centre", "community centre"),
                _venue("Beaty Museum", "museum"),
                _venue("Kits Pool", "pool"),
                _venue("Maplewood Farm", "farm"),
                _venue("Kids Market", "market"),
                _venue("The Aquarium", "aquarium")]
        self.assertEqual(len(_chosen(pool, ["museum"])), 3)

    def test_every_type_can_reach_a_plan(self):
        # A type in the dropdown that no plan can contain is a trap for
        # whoever adds the next one, and nothing else would notice.
        for venue_type in VENUE_TYPES:
            with self.subTest(type=venue_type):
                pool = [_venue("Only Option", venue_type)]
                self.assertEqual(
                    [v["name"] for v in _chosen(pool, ["museum"], "1")],
                    ["Only Option"])

    def test_asking_for_one_kind_still_reaches_another(self):
        # Why no grouping layer is needed: a parent who ticks "museum" and
        # would also have enjoyed the aquarium still gets it.
        pool = [_venue("A Museum", "museum"), _venue("An Aquarium", "aquarium")]
        names = {v["name"] for v in _chosen(pool, ["museum"], "2")}
        self.assertEqual(names, {"A Museum", "An Aquarium"})


class PreferenceTest(unittest.TestCase):
    def test_what_was_asked_for_comes_first(self):
        pool = [_venue("A Pool", "pool"), _venue("A Museum", "museum")]
        self.assertEqual(_chosen(pool, ["museum"], "1")[0]["name"], "A Museum")

    def test_the_pool_order_does_not_override_the_preference(self):
        pool = [_venue("A Farm", "farm"), _venue("A Mall", "mall")]
        self.assertEqual([v["name"] for v in _chosen(pool, ["mall"], "2")],
                         ["A Mall", "A Farm"])

    def test_several_kinds_can_be_asked_for_at_once(self):
        pool = [_venue("A Pool", "pool"), _venue("A Museum", "museum"),
                _venue("A Garden", "garden")]
        first_two = {v["name"] for v in _chosen(pool, ["museum", "garden"], "2")}
        self.assertEqual(first_two, {"A Museum", "A Garden"})

    def test_an_outdoor_cultural_day_is_expressible(self):
        # The case the themes could not express: a garden is outdoor and
        # cultural, and matched none of Outdoorsy, Rainy-day or Culture.
        pool = [_venue("A Mall", "mall", setting="indoor"),
                _venue("VanDusen", "garden", setting="outdoor"),
                _venue("UBC Botanical", "garden", setting="outdoor")]
        picked = _chosen(pool, ["garden"], "2")
        self.assertTrue(all(v["type"] == "garden" for v in picked), picked)


class NoPreferenceIsNeutralTest(unittest.TestCase):
    def test_asking_for_nothing_sorts_nothing(self):
        # "Mixed" used to deprioritise 10 of 14 types. An empty interest must
        # leave the curator's order exactly as it is.
        pool = [_venue("First", "pool"), _venue("Second", "museum"),
                _venue("Third", "farm")]
        self.assertEqual([v["name"] for v in _chosen(pool, [], "3")],
                         ["First", "Second", "Third"])

    def test_an_unrecognised_kind_is_ignored_rather_than_obeyed(self):
        pool = [_venue("First", "pool"), _venue("Second", "museum")]
        self.assertEqual([v["name"] for v in _chosen(pool, ["Culture"], "2")],
                         ["First", "Second"])

    def test_a_real_day_is_naturally_mixed(self):
        venues = data_loader.get_venues("Vancouver", on_date=ON)
        settings = [v["setting"] for v in venues[:6]]
        self.assertIn("indoor", settings)
        self.assertIn("outdoor", settings)


class LabellingTest(unittest.TestCase):
    def test_a_day_with_no_preference_is_not_called_mixed(self):
        self.assertEqual(itinerary.interest_label([]), "A day out")

    def test_a_day_is_named_for_what_was_asked_for(self):
        self.assertEqual(itinerary.interest_label(["museum"]), "Museum")
        self.assertEqual(itinerary.interest_label(["museum", "garden"]),
                         "Museum and garden")

    def test_a_long_answer_gets_a_short_title(self):
        # The form starts fully ticked, so unticking two still asks for eight,
        # and eight names joined by "and" is not a title.
        kinds = ["park", "garden", "beach", "seawall"]
        self.assertEqual(itinerary.interest_label(kinds), "A day out")

    def test_the_blurb_says_a_lean_is_only_a_lean(self):
        # The parent has to be told, or an unticked kind turning up reads as
        # the form having been ignored.
        blurb = itinerary.interest_blurb(["museum"])
        self.assertIn("Other kinds of place can still appear", blurb)

    def test_a_mostly_ticked_answer_is_described_by_what_is_missing(self):
        blurb = itinerary.interest_blurb(["park", "garden", "beach"],
                                         skipped=["mall"])
        self.assertIn("with mall further down the list", blurb)
        self.assertNotIn("Leaning towards", blurb)

    def test_three_or_more_names_are_listed_not_chained(self):
        self.assertEqual(itinerary._and(["a", "b", "c"]), "a, b and c")
        self.assertEqual(itinerary._and(["a", "b"]), "a and b")


class TickingEverythingIsTickingNothingTest(unittest.TestCase):
    """The claim the whole redesign rests on.

    If these two ever plan different days, the form's default silently became a
    preference nobody chose -- which is the exact fault the themes had.
    """

    POOL = [_venue("A Pool", "pool"), _venue("A Museum", "museum"),
            _venue("A Garden", "garden")]

    def test_the_same_day_comes_back_either_way(self):
        every = sorted({v["type"] for v in self.POOL})
        self.assertEqual([v["name"] for v in _chosen(self.POOL, every, "3")],
                         [v["name"] for v in _chosen(self.POOL, [], "3")])

    def test_and_it_is_not_titled_after_all_of_them(self):
        every = sorted({v["type"] for v in self.POOL})
        plan = itinerary.generate_plans(
            self.POOL, {**INPUTS, "interest": every, "stop_count": "3"})[0]
        self.assertEqual(plan.label, "A day out")
        self.assertIn("A mix of places", plan.blurb)

    def test_effective_interest_drops_a_lean_that_leans_nowhere(self):
        self.assertEqual(
            itinerary.effective_interest(["park", "museum"], {"park", "museum"}),
            [])

    def test_and_keeps_a_real_one(self):
        self.assertEqual(
            itinerary.effective_interest(["museum"], {"park", "museum"}),
            ["museum"])


class TheFormStartsFullyTickedTest(unittest.TestCase):
    def test_a_blank_form_has_every_kind_ticked(self):
        self.assertEqual(default_form()["interest"], all_interests())

    def test_all_interests_is_what_the_form_offers(self):
        self.assertEqual(all_interests(), data_loader.interest_options())

    def test_a_post_that_never_showed_the_boxes_gets_them_all(self):
        # The chat hand-off posts the fields it collected and nothing else, so
        # an absent `interest` means "not asked", not "cleared".
        self.assertEqual(read_form(MultiDict())["interest"], all_interests())

    def test_a_post_from_the_form_can_be_empty(self):
        # Unticked checkboxes are not submitted, so only the marker the form
        # carries can tell "cleared every box" from "never offered".
        self.assertEqual(
            read_form(MultiDict([("interest_offered", "1")]))["interest"], [])

    def test_what_was_ticked_is_what_arrives(self):
        posted = MultiDict([("interest_offered", "1"), ("interest", "museum"),
                            ("interest", "garden")])
        self.assertEqual(read_form(posted)["interest"], ["museum", "garden"])


class TheFormRefusesAnEmptyPickTest(unittest.TestCase):
    """Nothing ticked is not a question we can answer, so it is not planned."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, **extra):
        with mock.patch.object(plan_module, "PlanningAgent") as agent:
            agent.return_value.adjust_plan.side_effect = \
                plan_module.PlanningAgentError("skipped")
            page = self.client.post("/plan", data={
                "generate": "1", "destination": "Vancouver", "age_years": "3",
                "age_months": "0", **extra})
        return page.get_data(as_text=True)

    def test_clearing_every_box_plans_nothing(self):
        html = self._post(interest_offered="1")
        self.assertNotIn('id="plan-results"', html)

    def _error_tag(self, html):
        # The message is always in the markup and hidden until it applies, so
        # asserting the text alone would pass whatever the server decided.
        return html.split('id="interest-error"')[1].split(">")[0]

    def test_and_says_why(self):
        self.assertNotIn("hidden", self._error_tag(self._post(interest_offered="1")))

    def test_and_keeps_quiet_when_it_does_not_apply(self):
        self.assertIn("hidden", self._error_tag(self._post()))

    def test_one_kind_is_enough(self):
        html = self._post(interest_offered="1", interest="park")
        self.assertIn('id="plan-results"', html)

    def test_a_post_without_the_marker_still_plans(self):
        # Guards the test above from passing for the wrong reason: /plan is
        # posted to by more than the rendered form.
        self.assertIn('id="plan-results"', self._post())

    def test_the_page_offers_a_select_all_and_the_marker(self):
        html = self.client.get("/plan").get_data(as_text=True)
        self.assertIn('id="interest-toggle"', html)
        self.assertIn('name="interest_offered"', html)

    def test_every_offered_kind_starts_ticked(self):
        html = self.client.get("/plan").get_data(as_text=True)
        chips = html.split('id="interest-chips"')[1].split("</div>")[0]
        for kind in all_interests():
            with self.subTest(kind=kind):
                after = chips.split(f'value="{kind}"')[1][:40]
                self.assertIn("checked", after)


class OptionsComeFromTheDataTest(unittest.TestCase):
    def test_only_kinds_with_venues_are_offered(self):
        options = data_loader.interest_options()
        self.assertTrue(options)
        self.assertTrue(set(options) <= set(VENUE_TYPES))

    def test_a_kind_with_no_venues_is_not_offered(self):
        # Offering a choice that returns nothing is worse than not offering it.
        in_use = {v["type"] for v in data_loader.get_venues("")}
        for option in data_loader.interest_options():
            self.assertIn(option, in_use)

    def test_the_order_is_stable_not_arbitrary(self):
        options = data_loader.interest_options()
        self.assertEqual(options, [t for t in VENUE_TYPES if t in options])


if __name__ == "__main__":
    unittest.main()
