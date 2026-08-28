"""Which venues a theme may choose from.

The bug this file locks out: theme matching was a filter, so a venue whose type
no theme names was discarded rather than deprioritised. Ten of the fourteen
allowed types name no theme, and `activities or list(matches)` only rescued a
day where *nothing* matched -- so one museum in the pool was enough to throw
five other open venues away, and a request for three stops returned one.

It landed hardest on the City import: all 27 community centres were
unplannable, and retyping the Aquarium from "attraction" to "aquarium", which
the review dropdown invites, would have dropped it from every plan in silence.
"""

import unittest
from datetime import date

from src import itinerary
from src.data_loader import NAP_FRIENDLY_TYPES, VENUE_TYPES

ON = date(2026, 9, 15)
INPUTS = {"wake_up": "07:00", "bedtime": "19:30", "naps": [],
          "transit_nap": "no", "destination": "Vancouver", "transit": ["car"],
          "stop_count": "3", "dining": "on_the_go",
          "preferred_lunch_time": "11:30", "features": [],
          "age_months": 30, "age_years": 2, "themes": [],
          "trip_date": ON.isoformat()}

# Every type any theme names. Four of fourteen, which is the whole problem.
THEMED_TYPES = set().union(*(theme["types"] for theme in itinerary.THEMES))


def _venue(name, venue_type, **over):
    return {"id": abs(hash(name)) % 9999, "name": name, "type": venue_type,
            "neighbourhood": "Downtown", "open": "08:00", "close": "20:00",
            "hours_source": "default", "can_eat": False,
            "nap_friendly": venue_type in NAP_FRIENDLY_TYPES,
            "lat": 49.28, "lng": -123.12, "maps_url": "", **over}


def _chosen(pool, themes=(), stop_count="3"):
    plans = itinerary.generate_plans(
        pool, {**INPUTS, "themes": list(themes), "stop_count": stop_count})
    return [stop["venue"] for stop in plans[0].stops if stop.get("venue")]


class UnmappedTypesTest(unittest.TestCase):
    def test_a_day_is_no_longer_collapsed_by_one_themed_venue(self):
        # The measured failure: six open venues, three stops asked for, one
        # returned, because only the museum matched and the filter kept only it.
        pool = [_venue("Hillcrest Centre", "community centre"),
                _venue("Beaty Museum", "museum"),
                _venue("Kits Pool", "pool"),
                _venue("Maplewood Farm", "farm"),
                _venue("Kids Market", "market"),
                _venue("The Aquarium", "aquarium")]
        self.assertEqual(len(_chosen(pool, ["Culture"])), 3)

    def test_every_allowed_type_can_reach_a_plan(self):
        # The property that stops this recurring. A type in the review
        # dropdown that no plan can ever contain is a trap for whoever adds
        # the next one, and nothing else in the app would notice.
        for venue_type in VENUE_TYPES:
            with self.subTest(type=venue_type):
                pool = [_venue("Only Option", venue_type)]
                self.assertEqual(
                    [v["name"] for v in _chosen(pool, ["Culture"], "1")],
                    ["Only Option"])

    def test_an_unmapped_type_is_reachable_alongside_a_themed_one(self):
        pool = [_venue("A Museum", "museum"), _venue("A Pool", "pool")]
        names = {v["name"] for v in _chosen(pool, ["Culture"], "2")}
        self.assertEqual(names, {"A Museum", "A Pool"})


class ThemePreferenceTest(unittest.TestCase):
    """Reachable is not the same as equal. A themed day must still look themed."""

    def test_a_themed_venue_is_chosen_before_an_unmapped_one(self):
        pool = [_venue("A Pool", "pool"), _venue("A Museum", "museum")]
        self.assertEqual(_chosen(pool, ["Culture"], "1")[0]["name"], "A Museum")

    def test_the_pool_order_is_the_theme_order_not_the_input_order(self):
        # The unmapped venue is first in the pool and must still come second.
        pool = [_venue("A Farm", "farm"), _venue("A Mall", "mall")]
        self.assertEqual([v["name"] for v in _chosen(pool, ["Rainy-day"], "2")],
                         ["A Mall", "A Farm"])

    def test_plenty_of_themed_venues_means_only_themed_venues(self):
        # The regression that matters: nothing changes for a database with
        # enough of the four mapped types, which is every plan today.
        pool = [_venue(f"Museum {i}", "museum") for i in range(4)]
        pool += [_venue("A Pool", "pool"), _venue("A Farm", "farm")]
        picked = {v["type"] for v in _chosen(pool, ["Culture"], "3")}
        self.assertEqual(picked, {"museum"})

    def test_the_curators_order_survives_inside_a_group(self):
        # Python's sort is stable, and get_venues hands rows over in seed_rank
        # order, which is the curator's ranking of what to offer first.
        pool = [_venue("First Choice", "museum"), _venue("Second Choice", "museum")]
        self.assertEqual(_chosen(pool, ["Culture"], "1")[0]["name"], "First Choice")


class ThemeMappingTest(unittest.TestCase):
    """What the taxonomy currently says, recorded so a change is deliberate."""

    def test_most_allowed_types_still_name_no_theme(self):
        # Not asserted as correct, asserted as known. The fix makes this
        # survivable rather than fixing the taxonomy, which is a separate
        # decision: these types are reachable but never preferred.
        unmapped = [t for t in VENUE_TYPES if t not in THEMED_TYPES]
        self.assertEqual(sorted(unmapped), sorted([
            "aquarium", "beach", "community centre", "farm", "garden",
            "library", "market", "playground", "pool", "seawall"]))

    def test_a_theme_naming_a_type_that_cannot_exist_matches_nothing(self):
        # Rainy-day still names "cafe", which left VENUE_TYPES when restaurants
        # left the table. Harmless now that matching only sorts, and left in
        # place because THEMES contents are part of the taxonomy discussion.
        self.assertIn("cafe", THEMED_TYPES)
        self.assertNotIn("cafe", VENUE_TYPES)


if __name__ == "__main__":
    unittest.main()
