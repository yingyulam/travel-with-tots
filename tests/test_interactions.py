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
             "venue": _venue("Nap Park", type="park"), "reason": "nap"},
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


class ReplanMinutesClampTest(unittest.TestCase):
    """A parent-typed duration reaches arithmetic with no guard of its own.
    Presets could only ever send a sane number; the custom textbox can send
    anything, so every one of these used to be reachable."""

    def setUp(self):
        self.plan = {
            "label": "Outdoorsy", "blurb": "A day out.",
            "stops": [
                {"time": "1:00 PM", "kind": "activity",
                 "venue": _venue("Here Now"), "reason": "current"},
                {"time": "3:00 PM", "kind": "activity",
                 "venue": _venue("Later Park"), "reason": "ahead"},
            ],
        }

    def _ahead(self, minutes, situation="running_behind"):
        result = replan(self.plan, situation, "13:30", minutes=minutes)
        return [s["time"] for s in result["stops"]]

    def test_text_does_not_raise(self):
        # int("abc") used to escape replan_trip's except clause as a 500.
        self.assertEqual(self._ahead("abc"), ["1:00 PM", "3:45 PM"])

    def test_a_negative_does_not_run_the_day_backwards(self):
        times = self._ahead(-90)
        self.assertEqual(times[0], "1:00 PM")
        self.assertEqual(times[1], "3:05 PM")

    def test_a_huge_value_does_not_wrap_past_midnight(self):
        # _minutes_to_display does minutes %= 24 * 60, so an unclamped value
        # reappeared in the small hours and re-sorted ahead of the kept stops.
        times = self._ahead(100000)
        self.assertEqual(times[0], "1:00 PM")
        self.assertTrue(times[1].endswith("PM"), times[1])

    def test_zero_clamps_up_rather_than_becoming_the_default(self):
        # `if minutes else DEFAULT` turned a typed 0 into a 45-minute shift.
        self.assertEqual(self._ahead(0), ["1:00 PM", "3:05 PM"])

    def test_none_still_means_the_situations_default(self):
        self.assertEqual(self._ahead(None), ["1:00 PM", "3:45 PM"])

    def test_a_string_number_is_accepted(self):
        # JSON from the browser can arrive as a string.
        self.assertEqual(self._ahead("30"), ["1:00 PM", "3:30 PM"])


class ReplanNapHappenedTest(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "label": "Outdoorsy", "blurb": "A day out.",
            "stops": [
                {"time": "1:00 PM", "kind": "activity",
                 "venue": _venue("Here Now"), "reason": "current"},
                {"time": "2:00 PM", "kind": "nap",
                 "venue": _venue("Quiet Corner"),
                 "reason": "scheduled nap"},
                {"time": "4:00 PM", "kind": "activity",
                 "venue": _venue("Later Park"), "reason": "ahead"},
            ],
        }

    def test_the_current_stop_records_the_nap(self):
        result = replan(self.plan, "nap_happened", "13:30", minutes=60)
        self.assertIn("Nap happened here", result["stops"][0]["reason"])
        self.assertIn("60 min", result["stops"][0]["reason"])

    def test_the_scheduled_nap_is_cancelled(self):
        result = replan(self.plan, "nap_happened", "13:30", minutes=60)
        self.assertNotIn("nap", [s["kind"] for s in result["stops"]])

    def test_stops_shift_only_as_far_as_the_nap_needs(self):
        # Nap from 1:00 runs to 2:30, so the 4:00 stop already clears it and
        # must not move. Contrast running_behind, which slides everything.
        result = replan(self.plan, "nap_happened", "13:30", minutes=90)
        self.assertEqual(result["stops"][-1]["time"], "4:00 PM")

    def test_a_long_nap_pushes_the_rest_later(self):
        result = replan(self.plan, "nap_happened", "13:30", minutes=240)
        self.assertEqual(result["stops"][-1]["time"], "5:00 PM")

    def test_stops_past_bedtime_are_dropped(self):
        result = replan(self.plan, "nap_happened", "13:30", minutes=240,
                        bedtime="17:00")
        self.assertEqual([s["time"] for s in result["stops"]], ["1:00 PM"])

    def test_kept_stops_come_back_untouched(self):
        # This branch used to run the kept stops through _enforce_hours too, so
        # a stop that had already happened could be venue-swapped or dropped,
        # contradicting the function's own "earlier stops kept as-is".
        closed = _venue("Here Now", open="09:00", close="10:00")
        plan = {"label": "P", "blurb": "b", "stops": [
            {"time": "1:00 PM", "kind": "activity", "venue": closed,
             "reason": "already happened"},
        ]}
        result = replan(plan, "nap_happened", "13:30", minutes=60,
                        venues=[_venue("Spare Aquarium")])
        self.assertEqual(len(result["stops"]), 1)
        self.assertEqual(result["stops"][0]["venue"]["name"], "Here Now")

    def test_before_the_day_starts_nothing_is_invented(self):
        result = replan(self.plan, "nap_happened", "08:00", minutes=60)
        self.assertEqual([s["time"] for s in result["stops"]],
                         ["1:00 PM", "4:00 PM"])


class ReplanStayLongerTest(unittest.TestCase):
    """"Need to stay here longer" (key: running_behind)."""

    def setUp(self):
        self.plan = {
            "label": "Outdoorsy", "blurb": "A day out.",
            "stops": [
                {"time": "1:00 PM", "kind": "activity",
                 "venue": _venue("Here Now"), "reason": "current"},
                {"time": "3:00 PM", "kind": "activity",
                 "venue": _venue("Later Park"), "reason": "ahead"},
                {"time": "4:30 PM", "kind": "activity",
                 "venue": _venue("Last Stop"), "reason": "ahead"},
            ],
        }

    def test_every_remaining_stop_moves_by_the_delay(self):
        result = replan(self.plan, "running_behind", "13:30", minutes=30)
        self.assertEqual([s["time"] for s in result["stops"]],
                         ["1:00 PM", "3:30 PM", "5:00 PM"])

    def test_gaps_between_remaining_stops_are_preserved(self):
        result = replan(self.plan, "running_behind", "13:30", minutes=45)
        times = result["stops"]
        self.assertEqual(times[1]["time"], "3:45 PM")
        self.assertEqual(times[2]["time"], "5:15 PM")

    def test_stops_past_bedtime_are_dropped(self):
        result = replan(self.plan, "running_behind", "13:30", minutes=120,
                        bedtime="18:00")
        self.assertEqual([s["time"] for s in result["stops"]],
                         ["1:00 PM", "5:00 PM"])

    def test_the_reason_says_why_it_moved(self):
        result = replan(self.plan, "running_behind", "13:30", minutes=30)
        self.assertIn("Pushed later", result["stops"][1]["reason"])


class ReplanFinishedEarlyTest(unittest.TestCase):
    def setUp(self):
        self.plan = {
            "label": "Outdoorsy", "blurb": "A day out.",
            "stops": [
                {"time": "1:00 PM", "kind": "activity",
                 "venue": _venue("Here Now"), "reason": "current"},
                {"time": "4:00 PM", "kind": "activity",
                 "venue": _venue("Later Park"), "reason": "ahead"},
            ],
        }
        self.venues = [_venue("Here Now"), _venue("Later Park"),
                       _venue("Spare Aquarium")]

    def test_remaining_stops_move_earlier(self):
        result = replan(self.plan, "finished_early", "13:30", venues=self.venues)
        self.assertEqual(result["stops"][1]["time"], "1:45 PM")
        self.assertIn("Moved up", result["stops"][1]["reason"])

    def test_the_freed_slot_gets_a_real_stop(self):
        result = replan(self.plan, "finished_early", "13:30", venues=self.venues)
        self.assertEqual(result["stops"][-1]["time"], "4:00 PM")
        self.assertEqual(result["stops"][-1]["venue"]["name"], "Spare Aquarium")

    def test_the_extra_stop_is_not_placed_past_bedtime(self):
        # This branch never checked bedtime, so it could invent a stop after
        # the child was meant to be asleep.
        result = replan(self.plan, "finished_early", "13:30",
                        venues=self.venues, bedtime="16:00")
        self.assertEqual([s["time"] for s in result["stops"]],
                         ["1:00 PM", "1:45 PM"])


class ReplanSomethingElseTest(unittest.TestCase):
    """The note-only option. Its rule-based pass must be a no-op, or a parent
    asking for something indoors would find the day reshuffled underneath the
    request as well."""

    def setUp(self):
        self.plan = {
            "label": "Outdoorsy", "blurb": "A day out.",
            "stops": [
                {"time": "1:00 PM", "kind": "activity",
                 "venue": _venue("Here Now"), "reason": "current"},
                {"time": "4:00 PM", "kind": "activity",
                 "venue": _venue("Later Park"), "reason": "ahead"},
            ],
        }

    def test_remaining_stops_keep_their_times(self):
        result = replan(self.plan, "something_else", "13:30",
                        venues=[_venue("Here Now"), _venue("Later Park")])
        self.assertEqual([s["time"] for s in result["stops"]],
                         ["1:00 PM", "4:00 PM"])
        self.assertEqual([s["venue"]["name"] for s in result["stops"]],
                         ["Here Now", "Later Park"])

    def test_it_has_a_label_but_is_not_a_button(self):
        # Submitted from the free-text box, not tapped, so it must stay out of
        # the chip row while keeping a label: the AI prompt and the replan
        # blurb both look it up, and a missing entry would echo the raw key.
        from src.interactions import (
            NOTE_ONLY_SITUATION, SITUATION_LABELS, SITUATION_OPTIONS)
        self.assertEqual(SITUATION_LABELS["something_else"], "Anything else")
        self.assertNotIn("something_else", dict(SITUATION_OPTIONS))
        self.assertEqual(NOTE_ONLY_SITUATION[0], "something_else")
