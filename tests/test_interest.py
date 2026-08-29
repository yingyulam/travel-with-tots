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
"""

import unittest
from datetime import date

from src import data_loader, itinerary
from src.data_loader import VENUE_TYPES

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
