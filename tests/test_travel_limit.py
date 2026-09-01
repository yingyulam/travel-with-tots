"""How far the family is willing to travel, as a constraint rather than a hint.

The bug this closes, reported from the deployed app: accommodation Richmond
Centre, getting around **on foot**. The plan came back with Stanley Park
Seawall and Second Beach, 20km away, about four hours' walk each way, presented
as an ordinary morning.

Both inputs were being read. Neither could refuse anything: proximity sorted
the pool, and when every candidate is equally out of reach a sort returns the
curator's ranking untouched. So the day was built as though the parent had a
car and lived downtown.

What the parent controls now, and what these assert:

- a limit in minutes (20/30/40, default 20), per leg, not per day
- applied to every leg of the chain, the journey back to the accommodation
  included
- never widened for them: an out-of-range slot is left empty and explained,
  and only they can ask for places beyond it
"""

import unittest
from unittest import mock

import app as app_module
from src.components import plan_trip as plan_module
from src.geo import DEFAULT_WALK_BUDGET_MIN, WALK_BUDGET_OPTIONS, leg_minutes
from src.itinerary import generate_plans, over_budget, travel_rules

# The real pin from the report, and two venues from the real data set.
RICHMOND_CENTRE = {"lat": 49.1666, "lng": -123.1367}
STANLEY_PARK = {"lat": 49.3017, "lng": -123.1417}

BASE = {"wake_up": "07:00", "bedtime": "20:00", "naps": [], "age_years": "3",
        "age_months": "0", "stop_count": "3", "dining": "on_the_go",
        "interest": [], "transit": "walk", "accommodation": "Richmond Centre",
        "accommodation_lat": RICHMOND_CENTRE["lat"],
        "accommodation_lng": RICHMOND_CENTRE["lng"]}

# A degree of longitude is about 72km at this latitude, so 0.0138 is a
# kilometre east. Everything sits on one line, which makes distance the only
# variable in the pool.
KM_EAST = 0.0138


def _venue(name, km_east, lat=None, rank=1):
    return {"id": abs(hash(name)) % 9999, "name": name, "type": "park",
            "setting": "outdoor", "neighbourhood": "Somewhere",
            "lat": RICHMOND_CENTRE["lat"] if lat is None else lat,
            "lng": RICHMOND_CENTRE["lng"] + km_east * KM_EAST,
            "open": "06:00", "close": "22:00", "can_eat": False,
            "nap_friendly": True, "seed_rank": rank}


def _plan(pool, **inputs):
    return generate_plans(pool, {**BASE, **inputs})[0]


def _names(plan):
    return [s["venue"]["name"] for s in plan.stops if s["venue"]]


def _stop_names(result):
    # A finished plan carries notes as well as stops -- the "leave by" line
    # holds no venue.
    return [s["venue"]["name"] for s in result["stops"] if s["venue"]]


class TheReportedBugTest(unittest.TestCase):
    """Richmond Centre, on foot. The exact scenario from the report."""

    def test_a_four_hour_walk_is_not_a_morning_activity(self):
        pool = [_venue("Stanley Park Seawall", 0, lat=STANLEY_PARK["lat"], rank=1),
                _venue("Near The Hotel", 0.5, rank=2)]
        self.assertNotIn("Stanley Park Seawall", _names(_plan(pool)))

    def test_the_near_venue_is_what_comes_back(self):
        # Paired with the test above, because "returns nothing" would also
        # satisfy it, and an empty day is not the fix.
        pool = [_venue("Stanley Park Seawall", 0, lat=STANLEY_PARK["lat"], rank=1),
                _venue("Near The Hotel", 0.5, rank=2)]
        self.assertEqual(_names(_plan(pool)), ["Near The Hotel"])

    def test_the_curator_ranking_no_longer_decides_it(self):
        # Why a sort could not have fixed this: Stanley Park is first in the
        # pool, so ranking alone hands it back however far away it is.
        pool = [_venue("Stanley Park Seawall", 0, lat=STANLEY_PARK["lat"], rank=1)]
        self.assertEqual(_names(_plan(pool)), [])

    def test_and_says_so_instead_of_returning_a_quietly_shorter_day(self):
        pool = [_venue("Stanley Park Seawall", 0, lat=STANLEY_PARK["lat"], rank=1)]
        blurb = _plan(pool).blurb
        self.assertIn("could not build a day", blurb)
        self.assertIn("20 minutes on foot", blurb)

    def test_a_car_reaches_what_walking_cannot(self):
        # The limit is travel time, not distance, so the mode is what decides.
        # 5km is an hour and a half on foot and seventeen minutes in a car,
        # against the same twenty minute limit.
        pool = [_venue("Across Town", 5, rank=1)]
        self.assertEqual(_names(_plan(pool)), [])
        self.assertEqual(_names(_plan(pool, transit="car")), ["Across Town"])


class TheReturnLegTest(unittest.TestCase):
    """The last stop has to be somewhere the family can get back from.

    A day that is walkable outward and not homeward has stranded them, which is
    worse than a shorter day and reads the same on the page.
    """

    # Home, then 1km, then another 1km. Every hop is a comfortable walk; the
    # far end is 2km from the accommodation, which is not.
    POOL = [_venue("One Km", 1, rank=1), _venue("Two Km", 2, rank=2)]

    def test_the_last_stop_is_within_the_limit_of_the_accommodation(self):
        plan = _plan(self.POOL, stop_count="2")
        last = [s["venue"] for s in plan.stops if s["venue"]][-1]
        self.assertLessEqual(leg_minutes(last, RICHMOND_CENTRE, "walk"),
                             DEFAULT_WALK_BUDGET_MIN)

    def test_so_the_far_end_is_left_out_even_though_the_hop_to_it_is_short(self):
        # One Km -> Two Km is a ten minute walk. It is the journey home from
        # Two Km, 34 minutes, that rules it out.
        self.assertEqual(_names(_plan(self.POOL, stop_count="2")), ["One Km"])
        self.assertLess(leg_minutes(self.POOL[0], self.POOL[1], "walk"),
                        DEFAULT_WALK_BUDGET_MIN)

    def test_without_a_pin_there_is_no_return_leg_to_check(self):
        # Nothing is invented in place of an accommodation nobody gave: the
        # day is judged on the legs that can honestly be measured.
        plan = _plan(self.POOL, stop_count="2", accommodation_lat=None,
                     accommodation_lng=None)
        self.assertEqual(_names(plan), ["One Km", "Two Km"])

    def test_and_the_plan_says_that_leg_was_not_checked(self):
        plan = _plan(self.POOL, stop_count="2", accommodation_lat=None,
                     accommodation_lng=None)
        self.assertIn("couldn't check the journey there and back", plan.blurb)

    def test_the_last_stop_reports_the_journey_home_in_minutes(self):
        plan = _plan(self.POOL, stop_count="2")
        last = [s for s in plan.stops if s["venue"]][-1]
        self.assertRegex(last["reason"],
                         r"About \d+ min on foot \(\d+\.\d km\) "
                         r"back to your accommodation\.")


class ThePerLegRuleTest(unittest.TestCase):
    """The limit is per hop. A day of four short walks is a walkable day."""

    POOL = [_venue(f"Hop {i}", i * 0.8, rank=i) for i in range(1, 5)]

    def test_four_short_hops_are_allowed(self):
        plan = _plan(self.POOL, stop_count="4", accommodation_lat=None,
                     accommodation_lng=None)
        self.assertEqual(len(_names(plan)), 4)

    def test_even_though_they_add_up_to_more_than_the_limit(self):
        plan = _plan(self.POOL, stop_count="4", accommodation_lat=None,
                     accommodation_lng=None)
        venues = [s["venue"] for s in plan.stops if s["venue"]]
        total = sum(leg_minutes(a, b, "walk") for a, b in zip(venues, venues[1:]))
        self.assertGreater(total, DEFAULT_WALK_BUDGET_MIN)


class OnlyTheParentWidensItTest(unittest.TestCase):
    POOL = [_venue("Stanley Park Seawall", 0, lat=STANLEY_PARK["lat"], rank=1)]

    def test_a_day_the_limit_emptied_stays_empty(self):
        # No automatic second pass at a wider limit. The parent picked 20
        # minutes; a planner that quietly tries 40 has answered a different
        # question and said nothing about it.
        self.assertEqual(_names(_plan(self.POOL)), [])

    def test_until_they_ask(self):
        self.assertEqual(_names(_plan(self.POOL, beyond_budget=True)),
                         ["Stanley Park Seawall"])

    def test_and_then_the_explanation_goes_away(self):
        plan = _plan(self.POOL, beyond_budget=True)
        self.assertNotIn("could not build a day", plan.blurb)

    def test_a_slot_nothing_can_fill_is_still_explained(self):
        # Beyond the limit is not the only reason a slot can come up empty, and
        # the note must not claim the limit when the limit was lifted.
        shut = dict(_venue("Shut All Day", 0.2, rank=1), open="06:00",
                    close="06:30")
        plan = _plan([shut], beyond_budget=True)
        self.assertIn("even with no distance limit", plan.blurb)


class TheLimitItselfTest(unittest.TestCase):
    def test_the_three_lengths_the_form_offers(self):
        self.assertEqual(WALK_BUDGET_OPTIONS, (20, 30, 40))

    def test_a_wider_limit_reaches_further(self):
        pool = [_venue("Two Km", 2, rank=1)]
        self.assertEqual(_names(_plan(pool, stop_count="1",
                                      accommodation_lat=None,
                                      accommodation_lng=None,
                                      walk_budget="40")),
                         ["Two Km"])

    def test_and_the_default_does_not(self):
        pool = [_venue("Two Km", 2, rank=1), _venue("Near", 0.3, rank=2)]
        self.assertEqual(_names(_plan(pool, stop_count="1")), ["Near"])


class TheAiPassCannotBreakItTest(unittest.TestCase):
    """The adjuster reorders and swaps stops, and cannot measure a leg.

    So the day it returns is checked against the same rule the draft was built
    under, and an adjustment that breaks it is discarded whole. Reverting to a
    known-good draft is the only safe direction: a partial revert would leave a
    day nobody planned.
    """

    def _stops(self, *venues):
        return [{"time": "9:00 AM", "kind": "activity", "venue": v,
                 "reason": ""} for v in venues]

    def test_a_day_inside_the_limit_passes(self):
        near = _venue("Near", 0.5)
        self.assertEqual(over_budget(self._stops(near), BASE), 0)

    def test_a_leg_over_the_limit_is_counted(self):
        far = _venue("Far", 0, lat=STANLEY_PARK["lat"])
        # Out and back, so both ends of the chain are checked.
        self.assertEqual(over_budget(self._stops(far), BASE), 2)

    def test_the_journey_home_is_part_of_the_chain(self):
        # Reachable outward, not homeward: one leg over, not two.
        near, edge = _venue("One Km", 1), _venue("Two Km", 2)
        self.assertEqual(over_budget(self._stops(near, edge), BASE), 1)

    def test_a_venue_nobody_can_place_is_not_assumed_close(self):
        nowhere = dict(_venue("Nowhere", 0), lat=None, lng=None)
        self.assertEqual(over_budget(self._stops(nowhere), BASE), 2)

    def test_nothing_is_over_a_limit_the_parent_lifted(self):
        far = _venue("Far", 0, lat=STANLEY_PARK["lat"])
        self.assertEqual(over_budget(self._stops(far),
                                     {**BASE, "beyond_budget": True}), 0)

    def test_the_component_throws_out_an_adjustment_that_breaks_it(self):
        pool = [_venue("Near The Hotel", 0.5, rank=1)]
        far = _venue("Far Away", 0, lat=STANLEY_PARK["lat"])
        with mock.patch.object(plan_module, "get_venues", return_value=pool), \
             mock.patch.object(plan_module, "PlanningAgent") as agent:
            agent.return_value.adjust_plan.return_value = {
                "stops": self._stops(far)}
            result = plan_module.plan_trip(
                destination="Vancouver", age_months=36, transit="walk",
                accommodation_lat=RICHMOND_CENTRE["lat"],
                accommodation_lng=RICHMOND_CENTRE["lng"], stop_count=1,
                dining="on_the_go")
        self.assertEqual(_stop_names(result), ["Near The Hotel"])
        self.assertFalse(result["adjusted"])

    def test_and_keeps_one_that_does_not(self):
        # Guards the test above: the adjustment is discarded for breaking the
        # limit, not because adjustments are ignored.
        pool = [_venue("Near The Hotel", 0.5, rank=1)]
        swapped = _venue("Also Near", 0.6, rank=2)
        with mock.patch.object(plan_module, "get_venues", return_value=pool), \
             mock.patch.object(plan_module, "PlanningAgent") as agent:
            agent.return_value.adjust_plan.return_value = {
                "stops": self._stops(swapped)}
            result = plan_module.plan_trip(
                destination="Vancouver", age_months=36, transit="walk",
                accommodation_lat=RICHMOND_CENTRE["lat"],
                accommodation_lng=RICHMOND_CENTRE["lng"], stop_count=1,
                dining="on_the_go")
        self.assertEqual(_stop_names(result), ["Also Near"])
        self.assertTrue(result["adjusted"])


class TheFormCarriesItTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _inputs(self, **extra):
        seen = {}
        real = plan_module.generate_plans

        def spy(venues, inputs, **kwargs):
            seen.update(inputs)
            return real(venues, inputs, **kwargs)

        with mock.patch.object(plan_module, "generate_plans", spy), \
             mock.patch.object(plan_module, "PlanningAgent") as agent:
            agent.return_value.adjust_plan.side_effect = \
                plan_module.PlanningAgentError("skipped")
            self.client.post("/plan", data={
                "generate": "1", "destination": "Vancouver", "age_years": "3",
                "age_months": "0", **extra}, follow_redirects=True)
        return seen

    def test_the_chosen_length_reaches_the_planner(self):
        self.assertEqual(self._inputs(walk_budget="40")["walk_budget"], "40")

    def test_no_choice_means_the_default(self):
        self.assertEqual(self._inputs()["walk_budget"],
                         str(DEFAULT_WALK_BUDGET_MIN))

    def test_a_length_the_form_never_offered_is_refused(self):
        # The field is client-supplied, and this one decides which venues are
        # eligible at all, so it names a length we offered or it names nothing.
        self.assertEqual(self._inputs(walk_budget="500")["walk_budget"],
                         str(DEFAULT_WALK_BUDGET_MIN))

    def test_the_opt_in_only_counts_when_the_parent_sends_it(self):
        self.assertFalse(self._inputs()["beyond_budget"])
        self.assertTrue(self._inputs(beyond_budget="1")["beyond_budget"])

    def test_the_form_offers_exactly_those_lengths(self):
        html = self.client.get("/plan").get_data(as_text=True)
        block = html.split('name="walk_budget"')
        self.assertEqual(len(block) - 1, len(WALK_BUDGET_OPTIONS))
        for minutes in WALK_BUDGET_OPTIONS:
            with self.subTest(minutes=minutes):
                self.assertIn(f'value="{minutes}"', html)

    def test_the_default_is_the_one_pre_selected(self):
        html = self.client.get("/plan").get_data(as_text=True)
        chosen = html.split(f'value="{DEFAULT_WALK_BUDGET_MIN}"')[1][:40]
        self.assertIn("checked", chosen)


class TheChoiceIsOfferedNotTakenTest(unittest.TestCase):
    """The page offers the wider search only once a day has come up short."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _page(self, out_of_range, **extra):
        plan = {"label": "L", "blurb": "b", "stops": [], "adjusted": True,
                "changed": False, "hours": None, "out_of_range": out_of_range}
        with mock.patch.object(app_module, "plan_days", return_value=[plan]):
            page = self.client.post("/plan", data={
                "generate": "1", "destination": "Vancouver", "age_years": "3",
                "age_months": "0", **extra})
        return page.get_data(as_text=True)

    def test_a_full_day_is_not_asked_to_look_further(self):
        self.assertNotIn("Include places further away", self._page([]))

    def test_a_short_day_is(self):
        self.assertIn("Include places further away", self._page(["activity"]))

    def test_the_offer_submits_the_opt_in(self):
        html = self._page(["activity"])
        offer = html.split("Include places further away")[0]
        self.assertIn('name="beyond_budget" value="1"', offer)

    def test_it_is_not_offered_twice(self):
        # Already looking further: there is nothing left to opt into, and
        # showing the button again reads as the first press having failed.
        self.assertNotIn("Include places further away",
                         self._page(["activity"], beyond_budget="1"))


class OneReadingOfTheRulesTest(unittest.TestCase):
    """The draft and the check that guards the AI pass share travel_rules.

    Two readings would drift, and the failure would be silent: a guard applying
    a different limit passes days the planner would have refused.
    """

    def test_it_reads_the_pin_the_mode_and_the_limit(self):
        home, mode, budget, beyond = travel_rules(
            {**BASE, "transit": "car", "walk_budget": "40"})
        self.assertEqual(home, RICHMOND_CENTRE)
        self.assertEqual((mode, budget, beyond), ("car", 40, False))

    def test_no_pin_reads_as_no_anchor(self):
        home, _mode, _budget, _beyond = travel_rules(
            {**BASE, "accommodation_lat": None, "accommodation_lng": None})
        self.assertIsNone(home)

    def test_a_legacy_transit_list_resolves_to_one_mode(self):
        # trips.transit was a JSON array before the form became one question.
        _home, mode, _budget, _beyond = travel_rules(
            {**BASE, "transit": ["walk", "car"]})
        self.assertEqual(mode, "car")


if __name__ == "__main__":
    unittest.main()
