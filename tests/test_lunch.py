"""Where lunch goes, now that the table holds attractions and not restaurants.

Two rules, and the second one is the interesting one: the planner will not
insert a venue just to have somewhere to eat. Before this, deleting the
restaurants made it send a parent standing in Stanley Park to a mall seven
kilometres south, because the old logic picked the nearest unused venue that
served food.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import unittest

from src.data_loader import maps_url
from src.itinerary import generate_plans


def _venue(name, vtype="park", can_eat=False, open_time="09:00", close="18:00"):
    return {"name": name, "type": vtype, "neighbourhood": "Downtown",
            "has_family_room": False, "has_nursing_room": False,
            "stroller_accessible": True, "nap_friendly": vtype in ("park", "mall"),
            "can_eat": can_eat, "open": open_time, "close": close,
            "lat": 49.28, "lng": -123.12, "maps_url": maps_url(name)}


def _day(venues, **overrides):
    inputs = {"wake_up": "07:00", "bedtime": "20:00", "naps": [],
              "age_years": "3", "age_months": "0", "destination": "Vancouver",
              "stop_count": 3, "features": [], "themes": [], "dining": "dine_out",
              "accommodation": "", "preferred_lunch_time": "12:00",
              "transit_nap": ""}
    inputs.update(overrides)
    return generate_plans(venues, inputs)[0].to_dict()["stops"]


def _meal(stops):
    return next(s for s in stops if s["kind"] == "meal")


class LunchTest(unittest.TestCase):
    def test_lunch_is_taken_at_the_stop_the_parent_is_already_at(self):
        stops = _day([_venue("Public Market", "mall", can_eat=True),
                      _venue("Kids Market", "attraction"),
                      _venue("Sutcliffe Park")])
        meal = _meal(stops)
        self.assertEqual(meal["venue"]["name"], "Public Market")
        self.assertIn("already there", meal["reason"])

    def test_lunch_names_nowhere_when_no_stop_serves_food(self):
        stops = _day([_venue("Seawall"), _venue("A Museum", "museum"),
                      _venue("A Garden", "garden")])
        meal = _meal(stops)
        self.assertIsNone(meal["venue"])
        self.assertIn("Find lunch", meal["reason"])

    def test_the_handoff_names_the_stop_to_look_near(self):
        stops = _day([_venue("Seawall"), _venue("A Museum", "museum")])
        self.assertIn("near Seawall", _meal(stops)["reason"])

    def test_no_venue_is_inserted_just_to_have_somewhere_to_eat(self):
        # The regression this rule exists for. A mall that the day does not
        # visit must not become the lunch stop.
        stops = _day([_venue("Seawall"), _venue("A Museum", "museum")]
                     + [_venue("Faraway Mall", "mall", can_eat=True)],
                     stop_count=2)
        named = {s["venue"]["name"] for s in stops if s.get("venue")}
        meal = _meal(stops)
        if meal["venue"] is not None:
            self.assertIn(meal["venue"]["name"], named,
                          "lunch named a venue that is not a stop on the day")

    def test_a_food_stop_later_in_the_day_is_not_where_lunch_happens(self):
        # "You are already there" has to be true. A mall visited at four in the
        # afternoon was being named as the midday meal, and appeared twice in
        # one plan.
        stops = _day([_venue("Morning Park"), _venue("A Museum", "museum"),
                      _venue("Evening Mall", "mall", can_eat=True)],
                     stop_count=3, bedtime="21:00")
        meal = _meal(stops)
        names = [s["venue"]["name"] for s in stops if s.get("venue")]
        self.assertEqual(len(names), len(set(names)), f"a venue is named twice: {names}")
        if meal["venue"] is not None:
            before = [s for s in stops
                      if s["kind"] != "meal" and s.get("venue")
                      and s["time"] <= meal["time"]]
            self.assertTrue(before, "lunch claimed a stop the parent had not reached")

    def test_on_the_go_dining_has_no_lunch_block(self):
        stops = _day([_venue("Seawall"), _venue("A Museum", "museum")],
                     dining="on_the_go")
        self.assertFalse([s for s in stops if s["kind"] == "meal"])


if __name__ == "__main__":
    unittest.main()
