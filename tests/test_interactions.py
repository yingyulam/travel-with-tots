import unittest

from src.interactions import replan


def _venue(name, category="activity", **overrides):
    base = {"name": name, "category": category, "kid_friendly": True,
            "can_eat": category == "food", "nap_friendly": False}
    base.update(overrides)
    return base


class ReplanSkipNextTest(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "label": "Outdoorsy",
            "blurb": "A day out.",
            "stops": [
                {"time": "2:00 PM", "kind": "activity",
                 "venue": _venue("Old Museum"), "reason": "kept"},
                {"time": "4:00 PM", "kind": "activity",
                 "venue": _venue("Later Park"), "reason": "kept"},
            ],
        }
        self.venues = [_venue("Old Museum"), _venue("Later Park"),
                       _venue("Spare Aquarium")]

    def test_skip_next_fills_the_gap_instead_of_dropping_it(self):
        result = replan(self.plan, "skip_next", "13:00", venues=self.venues)
        # Still two stops: the skipped slot got a different open venue
        # instead of just disappearing.
        self.assertEqual(len(result["stops"]), 2)
        self.assertEqual(result["stops"][0]["time"], "2:00 PM")
        self.assertEqual(result["stops"][0]["venue"]["name"], "Spare Aquarium")
        self.assertEqual(result["stops"][1]["venue"]["name"], "Later Park")

    def test_skip_next_with_no_open_alternative_falls_back_to_hint(self):
        result = replan(self.plan, "skip_next", "13:00", venues=[])
        self.assertEqual(len(result["stops"]), 2)
        self.assertIsNone(result["stops"][0]["venue"])

    def test_skip_next_on_the_last_remaining_stop(self):
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "2:00 PM", "kind": "activity",
             "venue": _venue("Old Museum"), "reason": "kept"},
        ]}
        result = replan(plan, "skip_next", "13:00", venues=self.venues)
        self.assertEqual(len(result["stops"]), 1)
        self.assertNotEqual(result["stops"][0]["venue"]["name"], "Old Museum")

    def test_skip_next_with_nothing_remaining_stays_empty(self):
        plan = {"label": "P", "blurb": "b", "stops": []}
        result = replan(plan, "skip_next", "13:00", venues=self.venues)
        self.assertEqual(result["stops"], [])


class ReplanThemeTest(unittest.TestCase):
    def test_weather_rain_swaps_outdoor_stop_for_indoor_one(self):
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "2:00 PM", "kind": "activity",
             "venue": _venue("Stanley Park", type="park"), "reason": "kept"},
        ]}
        venues = [_venue("Stanley Park", type="park"),
                  _venue("Science World", type="museum")]
        result = replan(plan, "weather_rain", "13:00", venues=venues)
        self.assertEqual(result["stops"][0]["venue"]["name"], "Science World")
        self.assertIn("Swapped for the new theme", result["stops"][0]["reason"])

    def test_change_theme_targets_the_chosen_theme(self):
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "2:00 PM", "kind": "activity",
             "venue": _venue("Science World", type="museum"), "reason": "kept"},
        ]}
        venues = [_venue("Science World", type="museum"),
                  _venue("Stanley Park", type="park")]
        result = replan(plan, "change_theme", "13:00", venues=venues, theme="Outdoorsy")
        self.assertEqual(result["stops"][0]["venue"]["name"], "Stanley Park")

    def test_already_on_theme_venue_is_kept_but_may_move_earlier(self):
        # 2:00 PM is further off than the 30-min wrap-up buffer from 1:00 PM,
        # so the stop gets pulled forward -- the venue itself, already on
        # theme, is left as-is.
        stop = {"time": "2:00 PM", "kind": "activity",
                "venue": _venue("Science World", type="museum"), "reason": "kept"}
        plan = {"label": "P", "blurb": "b", "stops": [stop]}
        venues = [_venue("Science World", type="museum"),
                  _venue("Another Museum", type="museum")]
        result = replan(plan, "weather_rain", "13:00", venues=venues)
        self.assertEqual(result["stops"][0]["venue"]["name"], "Science World")
        self.assertEqual(result["stops"][0]["time"], "1:30 PM")
        self.assertIn("Moved earlier for the theme change.", result["stops"][0]["reason"])

    def test_stop_already_within_the_wrap_up_buffer_is_untouched(self):
        stop = {"time": "1:15 PM", "kind": "activity",
                "venue": _venue("Science World", type="museum"), "reason": "kept"}
        plan = {"label": "P", "blurb": "b", "stops": [stop]}
        venues = [_venue("Science World", type="museum")]
        result = replan(plan, "weather_rain", "13:00", venues=venues)
        self.assertEqual(result["stops"][0]["time"], "1:15 PM")
        self.assertEqual(result["stops"][0]["reason"], "kept")

    def test_meal_and_nap_stops_are_never_rethemed(self):
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "12:00 PM", "kind": "meal",
             "venue": _venue("Picnic Spot", type="park", category="food"), "reason": "lunch"},
            {"time": "2:00 PM", "kind": "nap",
             "venue": _venue("Nap Park", type="park", nap_friendly=True), "reason": "nap"},
        ]}
        venues = [_venue("Science World", type="museum")]
        result = replan(plan, "weather_rain", "11:00", venues=venues)
        self.assertEqual(result["stops"][0]["venue"]["name"], "Picnic Spot")
        self.assertEqual(result["stops"][1]["venue"]["name"], "Nap Park")

    def test_meal_stop_time_is_never_pulled_earlier(self):
        # Regression: the earlier-shift for weather_rain/change_theme once
        # dragged a meal stop along with everything else, landing "lunch"
        # absurdly early (e.g. 9:40 AM). The meal must keep its own time;
        # only the activity stop is eligible to be pulled into the gap.
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "12:00 PM", "kind": "meal",
             "venue": _venue("Lunch Spot", type="cafe", category="food"), "reason": "lunch"},
            {"time": "2:00 PM", "kind": "activity",
             "venue": _venue("Afternoon Park", type="park"), "reason": "afternoon"},
        ]}
        venues = [_venue("Lunch Spot", type="cafe", category="food"),
                  _venue("Afternoon Park", type="park"),
                  _venue("Rainy Museum", type="museum")]
        result = replan(plan, "change_theme", "09:10", venues=venues,
                        bedtime="20:00", theme="Rainy-day")
        meal = next(s for s in result["stops"] if s["kind"] == "meal")
        activity = next(s for s in result["stops"] if s["kind"] == "activity")
        self.assertEqual(meal["time"], "12:00 PM")
        self.assertEqual(meal["reason"], "lunch")
        self.assertEqual(activity["time"], "9:40 AM")

    def test_no_matching_theme_venue_leaves_venue_unchanged(self):
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "2:00 PM", "kind": "activity",
             "venue": _venue("Stanley Park", type="park"), "reason": "kept"},
        ]}
        result = replan(plan, "weather_rain", "13:00", venues=[_venue("Stanley Park", type="park")])
        self.assertEqual(result["stops"][0]["venue"]["name"], "Stanley Park")
        self.assertEqual(result["stops"][0]["time"], "1:30 PM")

    def test_no_remaining_stops_inserts_a_themed_bonus_stop(self):
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "1:00 PM", "kind": "activity",
             "venue": _venue("Stanley Park", type="park"), "reason": "kept"},
        ]}
        venues = [_venue("Stanley Park", type="park"), _venue("Science World", type="museum")]
        result = replan(plan, "weather_rain", "13:00", venues=venues, bedtime="20:00")
        self.assertEqual(len(result["stops"]), 2)
        bonus = result["stops"][1]
        self.assertEqual(bonus["kind"], "bonus")
        self.assertEqual(bonus["time"], "1:30 PM")
        self.assertEqual(bonus["venue"]["name"], "Science World")

    def test_stale_adjusted_flag_is_stripped(self):
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "1:00 PM", "kind": "activity",
             "venue": _venue("Stanley Park", type="park"), "reason": "kept", "adjusted": True},
        ]}
        result = replan(plan, "change_theme", "13:30", venues=[_venue("Stanley Park", type="park")],
                        theme="Outdoorsy")
        self.assertNotIn("adjusted", result["stops"][0])


if __name__ == "__main__":
    unittest.main()
