"""How far apart a day's stops are allowed to be.

The bug: the plan form asked how the family gets around and the answer changed
nothing. Five mode combinations produced byte-identical plans, byte-identical
times and the same 8.8km, including a 4.1km leg between two stops. A family on
foot with a toddler got a driver's day.

Two causes. `transit_buffer_min` existed but was only ever used to *validate* an
AI edit, never to schedule. And `_pick` chose every stop independently, with no
notion of being near the last one.

The fix is selection, not timing. `_plan_times` spreads stops across the whole
day so gaps are hours wide and travel time vanishes into them.

Selection was a **sort** at first, which fixed the symptom and not the bug: a
sort ranks, it cannot refuse, so a family on foot at Richmond Centre got every
venue tied at "out of reach" and the curator's ranking back untouched, with
Stanley Park -- 20km, about four hours' walk -- as the first stop of the
morning. It is a filter now, against a limit the parent picks in minutes.

Still no routing: geo.route_km scales a straight line by DETOUR_FACTOR and
geo.estimate_minutes divides by a per-mode speed. Both are estimates, labelled
as estimates wherever a parent sees them, and both are what the Routes API
replaces when it is enabled.
"""

import unittest
from datetime import date

from src import itinerary
from src.geo import (DEFAULT_REACH_KM, DEFAULT_WALK_BUDGET_MIN, REACH_KM,
                     RIDING_BUDGET_OPTIONS, WALK_BUDGET_OPTIONS,
                     budget_options, haversine_km, reach_km, walk_budget_min,
                     within_budget, within_reach)

ON = date(2026, 9, 15)
BASE = {"wake_up": "07:00", "bedtime": "19:30", "naps": [],
        "transit_nap": "yes", "destination": "Vancouver",
        "dining": "on_the_go", "preferred_lunch_time": "11:30", "features": [],
        "age_months": 30, "age_years": 2, "interest": [],
        "trip_date": ON.isoformat()}

# A line of venues 1 km apart, so distance is the only thing that varies.
def _venue(name, km_east, venue_type="park"):
    return {"id": abs(hash(name)) % 9999, "name": name, "type": venue_type,
            "setting": "outdoor", "neighbourhood": "Downtown",
            "hours_note": None, "open": "06:00", "close": "22:00",
            "hours_source": "default", "can_eat": False,
            "nap_friendly": True, "maps_url": "",
            "lat": 49.2800, "lng": -123.1400 + km_east * 0.0138}


def _plan(pool, mode, stop_count="3", **extra):
    stops = itinerary.generate_plans(
        pool, {**BASE, "transit": mode, "stop_count": stop_count,
               **extra})[0].stops
    return [s["venue"] for s in stops if s.get("venue")]


def _minutes(a, b, mode):
    return itinerary.leg_minutes(a, b, mode)


def _legs(named):
    return [haversine_km(a["lat"], a["lng"], b["lat"], b["lng"])
            for a, b in zip(named, named[1:])]


class ReachTest(unittest.TestCase):
    def test_each_mode_has_its_own_reach(self):
        self.assertLess(reach_km("walk"), reach_km("transit"))
        self.assertLess(reach_km("transit"), reach_km("car"))

    def test_an_unknown_mode_takes_the_tightest(self):
        # A clustered day is fine for a family with a car; a spread-out one is
        # not fine for a family on foot, so unknown fails towards clustering.
        self.assertEqual(reach_km("helicopter"), DEFAULT_REACH_KM)
        self.assertEqual(reach_km(None), DEFAULT_REACH_KM)
        self.assertEqual(DEFAULT_REACH_KM, min(REACH_KM.values()))

    def test_the_old_list_shape_resolves_to_the_widest(self):
        # trips.transit was a JSON array, and ticking several always meant
        # "take the widest".
        self.assertEqual(reach_km(["walk", "car"]), reach_km("car"))
        self.assertEqual(reach_km([]), DEFAULT_REACH_KM)

    def test_a_missing_coordinate_counts_as_reachable(self):
        # Penalising a venue for incomplete data is the wrong direction: the
        # cost of being wrong is a longer walk, not a wrong answer.
        anchor = {"lat": 49.28, "lng": -123.14}
        self.assertTrue(within_reach({"lat": None, "lng": None}, anchor, 1.0))
        self.assertTrue(within_reach({"lat": 49.28, "lng": -123.14},
                                     {"lat": None, "lng": None}, 1.0))

    def test_no_anchor_means_anything_goes(self):
        self.assertTrue(within_reach({"lat": 50.0, "lng": -120.0}, None, 0.1))


class SelectionTest(unittest.TestCase):
    # Pool order is preference order (what the parent asked for, then the
    # curator's ranking). The far venues come *first*, which is the only case
    # where the limit can matter: with the near ones already preferred, every
    # mode agrees and nothing needs deciding.
    POOL = [_venue("Start", 0), _venue("Six Km", 6), _venue("Twelve Km", 12),
            _venue("One Km", 1), _venue("Two Km", 2)]

    def _fits(self, named, mode, budget=DEFAULT_WALK_BUDGET_MIN):
        return all(_minutes(a, b, mode) <= budget
                   for a, b in zip(named, named[1:]))

    def test_walking_keeps_the_day_close(self):
        named = _plan(self.POOL, "walk", "3")
        self.assertTrue(self._fits(named, "walk"), [v["name"] for v in named])

    def test_driving_reaches_the_preferred_venue_walking_cannot(self):
        # Same limit, same pool: 6km is 20 minutes at driving speed and four
        # times that on foot, so the mode is what decides.
        named = _plan(self.POOL, "car", "2", walk_budget="30")
        self.assertIn("Six Km", [v["name"] for v in named])

    def test_walking_passes_over_it_for_something_near(self):
        named = _plan(self.POOL, "walk", "2")
        self.assertNotIn("Six Km", [v["name"] for v in named])

    def test_the_mode_actually_changes_the_plan(self):
        # The whole point. Every mode used to produce identical output.
        walking = [v["name"] for v in _plan(self.POOL, "walk", "3")]
        driving = [v["name"] for v in _plan(self.POOL, "car", "3")]
        self.assertNotEqual(walking, driving)

    def test_preference_still_decides_among_venues_in_range(self):
        # The limit filters, it does not rank: inside it the pool's own order
        # stands, so the curator's ranking is not replaced by a tape measure.
        pool = [_venue("Preferred", 1), _venue("Nearer", 0.2)]
        self.assertEqual(_plan(pool, "walk", "1")[0]["name"], "Preferred")

    def test_a_far_venue_is_excluded_not_deprioritised(self):
        # The reversal. As a sort this returned all three, because a sort has
        # nothing to say when every candidate is equally out of reach -- which
        # is how a four-hour walk became the first stop of a morning.
        pool = [_venue("Start", 0), _venue("Far", 20), _venue("Further", 40)]
        named = _plan(pool, "walk", "3")
        self.assertEqual([v["name"] for v in named], ["Start"])

    def test_the_parent_can_still_ask_for_them(self):
        # Paired with the test above: refusing is only acceptable because the
        # parent can overrule it. Nothing sets this for them.
        pool = [_venue("Start", 0), _venue("Far", 20), _venue("Further", 40)]
        named = _plan(pool, "walk", "3", beyond_budget=True)
        self.assertEqual(len(named), 3, [v["name"] for v in named])

    def test_a_walking_day_is_as_long_as_the_venues_allow(self):
        # Three near venues, four stops asked for. The fourth is not filled
        # with something 6km away.
        named = _plan(self.POOL, "walk", "4")
        self.assertEqual(len(named), 3, [v["name"] for v in named])

    def test_and_a_car_fills_it(self):
        # Guards the test above: the fourth stop is missing for being too far
        # on foot, not because the pool ran out or the count was clamped. The
        # same five venues, forty minutes by car.
        named = _plan(self.POOL, "car", "4", walk_budget="40")
        self.assertEqual(len(named), 4, [v["name"] for v in named])

    def test_the_first_stop_is_unconstrained_without_a_pin(self):
        # Nothing to measure from: `accommodation` is free text, and no pin
        # means no anchor, so the day can still begin anywhere.
        pool = [_venue("Only Far Away", 30)]
        self.assertEqual([v["name"] for v in _plan(pool, "walk", "1")],
                         ["Only Far Away"])

    def test_the_limit_is_measured_from_the_previous_stop_not_the_first(self):
        # A chain of 1km hops is a walkable day even though the last stop is
        # 3km from the first.
        pool = [_venue(f"Hop {i}", i) for i in range(5)]
        named = _plan(pool, "walk", "4")
        self.assertEqual(len(named), 4)
        self.assertTrue(self._fits(named, "walk"))

    def test_it_applies_to_each_leg_not_to_the_day_as_a_whole(self):
        # Four 1km hops is over an hour's walking in total and every leg is
        # inside a 20 minute limit, which is exactly what the parent asked for.
        pool = [_venue(f"Hop {i}", i) for i in range(5)]
        named = _plan(pool, "walk", "4")
        total = sum(_minutes(a, b, "walk") for a, b in zip(named, named[1:]))
        self.assertGreater(total, DEFAULT_WALK_BUDGET_MIN)


class TheBudgetTest(unittest.TestCase):
    """The limit itself: what the form may offer, and what it refuses."""

    def test_the_form_offers_three_lengths(self):
        self.assertEqual(WALK_BUDGET_OPTIONS, (20, 30, 40))

    def test_the_shortest_is_the_default(self):
        # A day that is too tightly packed can be widened by the parent; a day
        # that is too spread out cannot be walked.
        self.assertEqual(DEFAULT_WALK_BUDGET_MIN, min(WALK_BUDGET_OPTIONS))

    def test_anything_else_falls_back_to_the_default(self):
        # Client-supplied. A hand-made post asking for 500 minutes is not a
        # preference, and a trip saved before this control existed has none.
        for value in (500, "500", 25, None, "", "twenty", [20]):
            with self.subTest(value=value):
                self.assertEqual(walk_budget_min(value), DEFAULT_WALK_BUDGET_MIN)

    def test_every_offered_length_is_accepted(self):
        for minutes in WALK_BUDGET_OPTIONS:
            with self.subTest(minutes=minutes):
                self.assertEqual(walk_budget_min(str(minutes)), minutes)

    def test_riding_is_offered_two_longer_ones(self):
        for mode in ("car", "transit"):
            with self.subTest(mode=mode):
                self.assertEqual(budget_options(mode), RIDING_BUDGET_OPTIONS)
                for minutes in (60, 90):
                    self.assertEqual(walk_budget_min(str(minutes), mode),
                                     minutes)

    def test_and_walking_is_not(self):
        # An hour riding is an hour sitting down; on foot it is five kilometres
        # pushing a stroller.
        for mode in ("walk", "helicopter", None):
            with self.subTest(mode=mode):
                self.assertEqual(budget_options(mode), WALK_BUDGET_OPTIONS)
                self.assertEqual(walk_budget_min("90", mode),
                                 DEFAULT_WALK_BUDGET_MIN)

    def test_an_unmeasurable_leg_does_not_fit(self):
        # The opposite of within_reach, and the reason the two coexist: as a
        # ranking hint "no coordinates" is no opinion, as a filter it would be
        # a free pass for exactly the venues nobody can place.
        here = {"lat": 49.28, "lng": -123.14}
        self.assertFalse(within_budget(here, {"lat": None, "lng": None}, 40))
        self.assertFalse(within_budget({"lat": None, "lng": None}, here, 40))
        self.assertFalse(within_budget(here, None, 40))


class ClosedVenuesStillWinTest(unittest.TestCase):
    def test_a_near_venue_that_is_shut_is_not_chosen(self):
        # Proximity sorts the pool; the hours check still decides.
        pool = [_venue("Start", 0),
                dict(_venue("Near But Shut", 1), open="06:00", close="07:00"),
                _venue("Far But Open", 6)]
        names = [v["name"] for v in _plan(pool, "car", "2")]
        self.assertNotIn("Near But Shut", names)


class ReasonTest(unittest.TestCase):
    def test_a_stop_says_how_far_it_is(self):
        # The only thing a parent can see that proves the mode was read, and
        # how they judge whether a day is walkable.
        pool = [_venue("Start", 0), _venue("One Km", 1)]
        stops = itinerary.generate_plans(
            pool, {**BASE, "transit": "walk", "stop_count": "2"})[0].stops
        named = [s for s in stops if s.get("venue")]
        self.assertNotIn("from your last stop", named[0]["reason"])
        self.assertIn("from your last stop", named[1]["reason"])

    def test_the_distance_is_measured_from_the_previous_stop(self):
        # Not from itself. Setting the anchor before building the reason made
        # every stop report 0 km.
        pool = [_venue("Start", 0), _venue("One Km", 1)]
        stops = [s for s in itinerary.generate_plans(
            pool, {**BASE, "transit": "walk", "stop_count": "2"})[0].stops
            if s.get("venue")]
        # 1.35 km, not 1.0: a street is longer than the line between two
        # points, and the note reports the walk rather than the crow's flight.
        self.assertIn("1.4 km", stops[1]["reason"])
        self.assertIn("17 min on foot", stops[1]["reason"])


class BufferTest(unittest.TestCase):
    """Still only a guard on AI edits, never a scheduling input."""

    def test_it_is_keyed_on_the_modes_the_form_offers(self):
        for mode in ("car", "transit", "walk"):
            self.assertIn(mode, itinerary.TRANSIT_BUFFER_MIN)

    def test_walking_gets_the_longest_allowance(self):
        self.assertGreater(itinerary.transit_buffer_min("walk"),
                           itinerary.transit_buffer_min("car"))

    def test_it_tolerates_the_old_list_shape(self):
        self.assertEqual(itinerary.transit_buffer_min(["car", "walk"]),
                         itinerary.transit_buffer_min("walk"))


if __name__ == "__main__":
    unittest.main()
