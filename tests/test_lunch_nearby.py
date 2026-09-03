"""Lunch is answered from what we know, and the rest is handed to Google Maps.

The venue table holds attractions, so it can say "the aquarium has a cafe" but
never "here are the restaurants on this street". It used to try the second
through a web search, which returned pages rather than places: no distance, no
hours, nothing a parent on a street corner can act on.

So lunch is three tiers now -- somewhere they already are, then what is within
reach, then a Maps link -- and it is the one need that never touches the web.
Every other need is untouched, which several of these assert directly, because
the easiest way to get this wrong is to break them on the way past.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src import db, interactions
from src.components import find_nearby as module
from src.components.find_nearby import find_nearby
from src.data_loader import maps_search_url

# Downtown Vancouver, and a point about 6km away: outside a walk and a transit
# hop, inside a car's reach. Real coordinates so the distances are real.
HERE = (49.2827, -123.1207)
FAR = (49.3350, -123.1400)


def _venue(conn, name, *, can_eat=0, lat=None, lng=None, city="Vancouver"):
    conn.execute(
        "INSERT INTO venues (name, city, neighbourhood, type, source, "
        "can_eat, lat, lng) VALUES (?, ?, 'Downtown', 'mall', 'curated', ?, ?, ?)",
        (name, city, int(can_eat), lat, lng))


class TheRestaurantNeedMatchesCuratedVenuesTest(unittest.TestCase):
    """`can_eat` is not "this is a restaurant" -- it is "you can get food here",
    which is the question a hungry toddler actually poses."""

    def test_a_venue_with_food_matches(self):
        self.assertTrue(interactions.NEED_FILTERS["restaurant"]({"can_eat": True}))

    def test_a_venue_without_food_does_not(self):
        self.assertFalse(interactions.NEED_FILTERS["restaurant"]({"can_eat": False}))

    def test_an_unknown_venue_does_not(self):
        # Read with .get() like every other filter: absent is not False, but it
        # is certainly not a promise of lunch either.
        self.assertFalse(interactions.NEED_FILTERS["restaurant"]({}))


class _WithVenues(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            db.create_schema(conn)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _lunch(self, transit="", **kwargs):
        return find_nearby(need="restaurant", city="Vancouver",
                           lat=HERE[0], lng=HERE[1], transit=transit, **kwargs)


class LunchIsCappedByHowTheyTravelTest(_WithVenues):
    def setUp(self):
        super().setUp()
        with closing(db.connect()) as conn, conn:
            _venue(conn, "Close Cafe", can_eat=1, lat=HERE[0], lng=HERE[1])
            _venue(conn, "Far Food Court", can_eat=1, lat=FAR[0], lng=FAR[1])

    def test_walking_does_not_reach_six_kilometres(self):
        names = [p["name"] for p in self._lunch(transit="walk")["places"]]
        self.assertEqual(names, ["Close Cafe"])

    def test_driving_does(self):
        names = [p["name"] for p in self._lunch(transit="car")["places"]]
        self.assertIn("Far Food Court", names)

    def test_an_unknown_mode_takes_the_tightest_reach(self):
        # geo.reach_km's rule: a spread-out day is fine with a car and not fine
        # on foot, so anything unrecognised gets the walking cap.
        names = [p["name"] for p in self._lunch(transit="")["places"]]
        self.assertEqual(names, ["Close Cafe"])

    def test_a_venue_with_no_coordinates_is_kept(self):
        # Deliberate, and load-bearing: four curated venues have no coordinates,
        # including both Granville Island markets. Dropping them for incomplete
        # data would hide exactly the places this should surface.
        with closing(db.connect()) as conn, conn:
            _venue(conn, "Granville Island Market", can_eat=1)
        names = [p["name"] for p in self._lunch(transit="walk", limit=5)["places"]]
        self.assertIn("Granville Island Market", names)

    def test_a_venue_without_food_is_never_offered(self):
        with closing(db.connect()) as conn, conn:
            _venue(conn, "Playground", can_eat=0, lat=HERE[0], lng=HERE[1])
        names = [p["name"] for p in self._lunch(transit="walk", limit=5)["places"]]
        self.assertNotIn("Playground", names)


class LunchNeverSearchesTheWebTest(_WithVenues):
    """The change this file exists for. Other needs keep the fallback."""

    def test_no_web_search_even_when_nothing_is_found(self):
        with mock.patch.object(module, "search_web") as searched:
            result = self._lunch(transit="walk")
        searched.assert_not_called()
        self.assertEqual(result["places"], [])
        self.assertEqual(result["source"], "none")

    def test_no_web_search_when_something_is_found(self):
        with closing(db.connect()) as conn, conn:
            _venue(conn, "Close Cafe", can_eat=1, lat=HERE[0], lng=HERE[1])
        with mock.patch.object(module, "search_web") as searched:
            self._lunch(transit="walk")
        searched.assert_not_called()

    def test_other_needs_still_search_the_web(self):
        # Google Maps is a poor answer for "nursing room", and for these needs a
        # web result is the only answer there is. Breaking them on the way past
        # is the likeliest way to get this change wrong.
        with mock.patch.object(module, "search_web", return_value=[]) as searched:
            find_nearby(need="nursing_room", city="Vancouver",
                        lat=HERE[0], lng=HERE[1])
        searched.assert_called_once()


class TheMapsHandoffTest(_WithVenues):
    def test_lunch_always_carries_a_maps_link(self):
        # Offered alongside results, not only instead of them: "here is what we
        # know, and here is where to look for more".
        with closing(db.connect()) as conn, conn:
            _venue(conn, "Close Cafe", can_eat=1, lat=HERE[0], lng=HERE[1])
        result = self._lunch(transit="walk")
        self.assertTrue(result["places"])
        self.assertIsNotNone(result["maps_search_url"])

    def test_lunch_with_no_results_still_carries_one(self):
        self.assertIsNotNone(self._lunch(transit="walk")["maps_search_url"])

    def test_the_link_is_anchored_on_the_parent_not_the_city(self):
        # A search for restaurants near a city is the whole map: it would send
        # somebody standing in Stanley Park a list starting downtown.
        url = self._lunch(transit="walk")["maps_search_url"]
        self.assertIn(f"@{HERE[0]},{HERE[1]}", url)
        self.assertNotIn("Vancouver", url)

    def test_without_coordinates_it_anchors_on_the_current_stop(self):
        result = find_nearby(need="restaurant", city="Vancouver",
                             near_place="Science World")
        self.assertIn("Science+World", result["maps_search_url"])

    def test_the_resolved_address_is_preferred_to_the_stop_name(self):
        # A street address is where they are; the stop is where the plan says
        # they are. When both are known, the first one wins.
        result = find_nearby(need="restaurant", city="Vancouver",
                             place_name="1455 Quebec St",
                             near_place="Science World")
        self.assertIn("1455+Quebec+St", result["maps_search_url"])

    def test_no_anchor_means_no_link_rather_than_a_city_wide_one(self):
        # The honest outcome. Offering "restaurants near Vancouver" would look
        # like an answer while being the entire map.
        result = find_nearby(need="restaurant", city="Vancouver")
        self.assertIsNone(result["maps_search_url"])

    def test_other_needs_carry_none(self):
        with mock.patch.object(module, "search_web", return_value=[]):
            result = find_nearby(need="nursing_room", city="Vancouver",
                                 lat=HERE[0], lng=HERE[1])
        self.assertIsNone(result["maps_search_url"])


class TheMapsSearchUrlTest(unittest.TestCase):
    def test_coordinates_centre_the_map(self):
        url = maps_search_url("kid friendly restaurants", 49.2827, -123.1207)
        self.assertIn("kid+friendly+restaurants", url)
        self.assertIn("@49.2827,-123.1207", url)

    def test_without_coordinates_it_uses_the_documented_form(self):
        # The fallback is deliberately the shape Google documents, so if the
        # centred form ever stops working the loss is centring, not the link.
        url = maps_search_url("kid friendly restaurants", near="Stanley Park")
        self.assertIn("api=1", url)
        self.assertIn("Stanley+Park", url)

    def test_it_is_a_google_maps_link(self):
        for url in (maps_search_url("lunch", 1.0, 2.0), maps_search_url("lunch")):
            with self.subTest(url=url):
                self.assertTrue(url.startswith("https://www.google.com/maps/"))


if __name__ == "__main__":
    unittest.main()
