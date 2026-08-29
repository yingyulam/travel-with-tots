"""The accommodation as the day's start and end anchor.

Until this, `accommodation` was free text and the planner could not measure
from it: the first stop was chosen from nowhere and the last was chosen without
knowing anyone had to get home. A pin makes both ends real.

The anchor **sorts**, it never filters, which is the same rule the rest of the
planner follows. A hotel on the far side of the city must not be able to empty
a day, so every test here that checks the anchor changed a plan is paired with
one checking the day still has its stops.
"""

import unittest
from unittest import mock

from werkzeug.datastructures import MultiDict

import app as app_module
from src.components import plan_trip as plan_module
from src.form_helpers import read_form
from src.geo import as_point, haversine_km
from src.itinerary import generate_plans

# Two real corners of the city, far enough apart that a day planned from one is
# nowhere near the other.
DOWNTOWN = (49.2870, -123.1440)      # the West End
SOUTH_EAST = (49.2200, -123.0400)    # Killarney

BASE = {"wake_up": "07:00", "bedtime": "20:00", "naps": [], "age_years": "3",
        "age_months": "0", "stop_count": "4", "dining": "on_the_go",
        "interest": [], "transit": "walk", "accommodation": "Where we stay"}


def _venue(name, lat, lng, rank):
    return {"id": rank, "name": name, "type": "park", "setting": "outdoor",
            "neighbourhood": "Somewhere", "lat": lat, "lng": lng,
            "open": "06:00", "close": "22:00", "can_eat": False,
            "nap_friendly": True, "seed_rank": rank}


# A pool split into two clusters, with the curator's ranking putting the
# *downtown* cluster first. So a south-east hotel can only reach its own
# cluster by being read: seed_rank alone would return the other one.
POOL = [
    _venue("Downtown A", 49.2880, -123.1430, 1),
    _venue("Downtown B", 49.2890, -123.1400, 2),
    _venue("Downtown C", 49.2860, -123.1470, 3),
    _venue("Downtown D", 49.2900, -123.1380, 4),
    _venue("South-east A", 49.2210, -123.0410, 5),
    _venue("South-east B", 49.2190, -123.0390, 6),
    _venue("South-east C", 49.2220, -123.0370, 7),
    _venue("South-east D", 49.2180, -123.0430, 8),
]


def _plan(pool=POOL, **inputs):
    return generate_plans(pool, {**BASE, **inputs})[0]


def _names(plan):
    return [s["venue"]["name"] for s in plan.stops if s["venue"]]


class ThePinChoosesWhereTheDayHappensTest(unittest.TestCase):
    def test_without_a_pin_the_curator_ranking_decides(self):
        # The behaviour every existing trip still gets.
        self.assertTrue(all(n.startswith("Downtown") for n in _names(_plan())))

    def test_a_far_pin_moves_the_whole_day_to_it(self):
        plan = _plan(accommodation_lat=SOUTH_EAST[0], accommodation_lng=SOUTH_EAST[1])
        self.assertTrue(all(n.startswith("South-east") for n in _names(plan)),
                        _names(plan))

    def test_the_first_stop_is_measured_from_the_accommodation(self):
        plan = _plan(accommodation_lat=SOUTH_EAST[0], accommodation_lng=SOUTH_EAST[1])
        first = next(s for s in plan.stops if s["venue"])
        self.assertIn("from your accommodation", first["reason"])
        self.assertNotIn("from your last stop", first["reason"])

    def test_the_last_stop_reports_the_journey_home(self):
        plan = _plan(accommodation_lat=SOUTH_EAST[0], accommodation_lng=SOUTH_EAST[1])
        last = [s for s in plan.stops if s["venue"]][-1]
        self.assertIn("back to your accommodation", last["reason"])

    def test_only_the_last_stop_reports_the_journey_home(self):
        plan = _plan(accommodation_lat=SOUTH_EAST[0], accommodation_lng=SOUTH_EAST[1])
        stops = [s for s in plan.stops if s["venue"]]
        homeward = [s for s in stops if "back to your accommodation" in s["reason"]]
        self.assertEqual(len(homeward), 1)
        self.assertIs(homeward[0], stops[-1])

    def test_the_day_ends_near_enough_to_get_home(self):
        # The point of the second tier: the last stop is not chosen as if the
        # family lived at the fourth stop.
        plan = _plan(accommodation_lat=SOUTH_EAST[0], accommodation_lng=SOUTH_EAST[1])
        last = [s for s in plan.stops if s["venue"]][-1]["venue"]
        home_km = haversine_km(*SOUTH_EAST, last["lat"], last["lng"])
        self.assertLess(home_km, 1.5)


class ThePinCannotEmptyADayTest(unittest.TestCase):
    """A sort, never a filter. This project has twice shipped a filter whose
    fallback only fired when nothing at all qualified."""

    def test_a_pin_nowhere_near_anything_still_plans_a_full_day(self):
        # Middle of the Strait of Georgia: every venue is out of reach.
        plan = _plan(accommodation_lat=49.30, accommodation_lng=-124.50)
        self.assertEqual(len(_names(plan)), 4)

    def test_a_far_pin_returns_the_same_number_of_stops(self):
        with_pin = _plan(accommodation_lat=SOUTH_EAST[0],
                         accommodation_lng=SOUTH_EAST[1])
        self.assertEqual(len(_names(with_pin)), len(_names(_plan())))

    def test_venues_without_coordinates_are_not_penalised(self):
        # Missing data must not push a venue to the back: the cost of being
        # wrong is a longer walk, not a wrong answer.
        pool = [_venue("No coordinates", None, None, 0)] + POOL
        plan = _plan(pool=pool, accommodation_lat=SOUTH_EAST[0],
                     accommodation_lng=SOUTH_EAST[1])
        self.assertIn("No coordinates", _names(plan))


class TheLeaveNoteTest(unittest.TestCase):
    def test_it_reports_the_distance_once_there_is_a_pin(self):
        plan = _plan(accommodation_lat=SOUTH_EAST[0], accommodation_lng=SOUTH_EAST[1])
        leave = next(s for s in plan.stops if s["kind"] == "leave")
        self.assertIn("Leave Where we stay by", leave["reason"])
        self.assertRegex(leave["reason"], r"\d+\.\d km away\.")

    def test_it_no_longer_calls_itself_a_placeholder(self):
        plan = _plan(accommodation_lat=SOUTH_EAST[0], accommodation_lng=SOUTH_EAST[1])
        leave = next(s for s in plan.stops if s["kind"] == "leave")
        self.assertNotIn("Placeholder", leave["reason"])

    def test_text_alone_still_produces_a_note_without_a_distance(self):
        leave = next(s for s in _plan().stops if s["kind"] == "leave")
        self.assertIn("Leave Where we stay by", leave["reason"])
        self.assertNotIn("km away", leave["reason"])

    def test_a_pin_with_no_text_is_still_somewhere_to_leave_from(self):
        # Clicking the map names nothing, so the note has to stand on the pin.
        plan = _plan(accommodation="", accommodation_lat=SOUTH_EAST[0],
                     accommodation_lng=SOUTH_EAST[1])
        leave = next(s for s in plan.stops if s["kind"] == "leave")
        self.assertIn("Leave your accommodation by", leave["reason"])

    def test_no_accommodation_at_all_means_no_note(self):
        self.assertEqual([s for s in _plan(accommodation="").stops
                          if s["kind"] == "leave"], [])


class TheFormKeepsBothHalvesOrNeitherTest(unittest.TestCase):
    """Half a coordinate cannot be measured from, and storing one would leave a
    trip that looks pinned and is not."""

    def _read(self, **data):
        values = read_form(MultiDict(data))
        return values["accommodation_lat"], values["accommodation_lng"]

    def test_a_real_pin_survives(self):
        self.assertEqual(
            self._read(accommodation_lat="49.287", accommodation_lng="-123.144"),
            ("49.287", "-123.144"))

    def test_half_a_pin_is_dropped(self):
        self.assertEqual(self._read(accommodation_lat="49.287"), ("", ""))

    def test_nonsense_is_dropped_rather_than_carried(self):
        self.assertEqual(self._read(accommodation_lat="tell me",
                                    accommodation_lng="no"), ("", ""))

    def test_an_impossible_coordinate_is_dropped(self):
        self.assertEqual(self._read(accommodation_lat="91", accommodation_lng="0"),
                         ("", ""))

    def test_no_pin_is_the_ordinary_case(self):
        self.assertEqual(self._read(), ("", ""))


class AsPointTest(unittest.TestCase):
    def test_it_reads_strings_and_numbers_alike(self):
        # Strings arrive from a form, floats from a REAL column.
        self.assertEqual(as_point("49.28", "-123.12"), as_point(49.28, -123.12))

    def test_a_missing_coordinate_is_no_point_rather_than_an_error(self):
        # An accommodation is optional, so this must not raise.
        self.assertIsNone(as_point(None, None))


class ThePlanRouteCarriesThePinTest(unittest.TestCase):
    """The bug this stage also fixed: plan_trip built the planner's inputs
    without `transit`, so the mode a parent picked never reached the draft."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def _inputs(self, **extra):
        seen = {}
        real = plan_module.generate_plans

        def spy(venues, inputs):
            seen.update(inputs)
            return real(venues, inputs)

        with mock.patch.object(plan_module, "generate_plans", spy), \
             mock.patch.object(plan_module, "PlanningAgent") as agent:
            agent.return_value.adjust_plan.side_effect = \
                plan_module.PlanningAgentError("skipped")
            self.client.post("/plan", data={
                "generate": "1", "destination": "Vancouver", "age_years": "3",
                "age_months": "0", **extra}, follow_redirects=True)
        return seen

    def test_the_pin_reaches_the_planner(self):
        seen = self._inputs(accommodation="Sylvia Hotel",
                            accommodation_lat="49.287",
                            accommodation_lng="-123.144")
        self.assertEqual(as_point(seen["accommodation_lat"],
                                  seen["accommodation_lng"]),
                         {"lat": 49.287, "lng": -123.144})

    def test_the_transport_mode_reaches_the_planner(self):
        self.assertEqual(self._inputs(transit="car")["transit"], "car")

    def test_the_form_renders_the_map_and_its_hidden_fields(self):
        html = self.client.get("/plan").get_data(as_text=True)
        for needed in ("accommodation-map", 'name="accommodation_lat"',
                       'name="accommodation_lng"', "plan-accommodation.js"):
            with self.subTest(needed=needed):
                self.assertIn(needed, html)


if __name__ == "__main__":
    unittest.main()
