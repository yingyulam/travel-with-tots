"""The last check before a plan reaches a parent.

The draft only picks venues that are open, and the AI adjuster refuses an edit
that moves a stop outside a venue's hours. This holds the finished plan
accountable for the specific day, and repairs what it finds.

The rule it exists to enforce: a venue whose hours we do not know is not
schedulable. Not knowing is a reason to leave a place out, never to include it.
"""

import unittest
from datetime import date

from src.components.validate_hours import CLOSED, UNVERIFIED, check_plan, enforce
from src.data_loader import maps_url

ORDINARY = date(2026, 8, 28)      # a Friday
HOLIDAY = date(2026, 12, 25)      # Christmas Day, a Friday


def _venue(name, opens="09:00", closes="17:00", source="default", vtype="park"):
    return {"id": abs(hash(name)) % 100000, "name": name, "type": vtype,
            "neighbourhood": "Downtown", "has_washroom": False,
            "has_family_room": False, "has_nursing_room": False,
            "stroller_accessible": True, "has_highchair": False,
            "can_eat": vtype in ("mall", "market"),
            "nap_friendly": vtype in ("park", "mall"),
            "open": opens, "close": closes, "hours_source": source,
            "lat": 49.28, "lng": -123.12, "maps_url": maps_url(name)}


def _plan(*stops):
    return {"label": "Mixed", "blurb": "", "source": "rule", "stops": list(stops)}


def _stop(time, venue, kind="activity"):
    return {"time": time, "kind": kind, "venue": venue, "reason": "because"}


class CheckPlanTest(unittest.TestCase):
    def test_a_plan_whose_venues_are_open_passes(self):
        plan = _plan(_stop("10:00 AM", _venue("Open Park", "06:00", "22:00")))
        self.assertTrue(check_plan(plan, ORDINARY)["ok"])

    def test_a_stop_after_closing_is_reported_as_closed(self):
        plan = _plan(_stop("3:00 PM", _venue("Morning Museum", "09:00", "12:00",
                                             vtype="museum")))
        report = check_plan(plan, ORDINARY)
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"][0]["kind"], CLOSED)
        self.assertEqual(report["problems"][0]["venue"], "Morning Museum")

    def test_unknown_hours_are_reported_as_unverified_not_assumed_open(self):
        plan = _plan(_stop("10:00 AM", _venue("Mystery Hall", None, None, "missing")))
        report = check_plan(plan, ORDINARY)
        self.assertFalse(report["ok"])
        self.assertEqual(report["problems"][0]["kind"], UNVERIFIED)

    def test_unknown_holiday_hours_are_unverified(self):
        plan = _plan(_stop("10:00 AM",
                           _venue("Aquarium", None, None, "holiday_unknown",
                                  vtype="aquarium")))
        report = check_plan(plan, HOLIDAY)
        self.assertEqual(report["problems"][0]["kind"], UNVERIFIED)
        self.assertIn("holiday", report["problems"][0]["why"])

    def test_a_stop_with_no_venue_is_not_a_problem(self):
        # A lunch handoff block names nowhere on purpose.
        plan = _plan({"time": "12:00 PM", "kind": "meal", "venue": None,
                      "reason": "Find lunch nearby."})
        self.assertTrue(check_plan(plan, ORDINARY)["ok"])

    def test_the_report_names_the_day_it_checked(self):
        report = check_plan(_plan(), HOLIDAY)
        self.assertEqual(report["day_type"], "holiday")
        self.assertEqual(report["season"], "winter")


class VenueHoursShapeTest(unittest.TestCase):
    """Two shapes reach venue_hours, and only one used to be read."""

    def test_a_candidate_row_from_the_database_is_understood(self):
        # get_candidate_venues returns column names. The AI adjuster swaps those
        # straight into a stop, so reading only open/close meant its own
        # "isn't open at" check passed every swapped venue: the hours looked
        # unknown when they were right there.
        from src.itinerary import venue_hours, venue_open_for
        row = {"name": "From the DB", "open_time": "09:00", "close_time": "12:00"}
        self.assertEqual(venue_hours(row), (9 * 60, 12 * 60))
        self.assertFalse(venue_open_for(row, 15 * 60, 60))
        self.assertTrue(venue_open_for(row, 10 * 60, 60))

    def test_a_venue_dict_from_data_loader_is_understood(self):
        from src.itinerary import venue_hours
        self.assertEqual(venue_hours(_venue("X", "09:00", "12:00")),
                         (9 * 60, 12 * 60))

    def test_a_venue_with_neither_is_unknown(self):
        from src.itinerary import venue_hours, venue_open_for
        self.assertIsNone(venue_hours({"name": "X"}))
        self.assertFalse(venue_open_for({"name": "X"}, 10 * 60, 60))


class EnforceTest(unittest.TestCase):
    def test_a_closed_venue_is_swapped_for_one_that_is_open(self):
        closed = _venue("Morning Museum", "09:00", "12:00", vtype="museum")
        pool = [_venue("Late Beach", "06:00", "21:00", vtype="beach")]
        fixed, report = enforce(_plan(_stop("3:00 PM", closed)), pool, ORDINARY)
        self.assertEqual(fixed["stops"][0]["venue"]["name"], "Late Beach")
        self.assertTrue(fixed["stops"][0]["hours_swapped"])
        self.assertEqual(len(report["replaced"]), 1)
        self.assertTrue(report["ok"])

    def test_the_swap_says_why_in_words_a_parent_can_read(self):
        closed = _venue("Morning Museum", "09:00", "12:00", vtype="museum")
        pool = [_venue("Late Beach", "06:00", "21:00", vtype="beach")]
        fixed, _ = enforce(_plan(_stop("3:00 PM", closed)), pool, ORDINARY)
        reason = fixed["stops"][0]["reason"]
        self.assertIn("Late Beach instead of Morning Museum", reason)
        self.assertIn("because", reason)
        self.assertNotIn("which we", reason)   # the phrasing bug this had

    def test_with_nothing_open_the_slot_goes_free_rather_than_wrong(self):
        closed = _venue("Morning Museum", "09:00", "12:00", vtype="museum")
        fixed, report = enforce(_plan(_stop("3:00 PM", closed)), [], ORDINARY)
        self.assertIsNone(fixed["stops"][0]["venue"])
        self.assertEqual(len(report["dropped"]), 1)
        self.assertIn("yours to fill", fixed["stops"][0]["reason"])

    def test_no_closed_venue_ever_survives(self):
        # The invariant. Whatever happens, nothing shut is left in the plan.
        stops = [_stop("3:00 PM", _venue("Shut", "09:00", "12:00", vtype="museum")),
                 _stop("4:00 PM", _venue("Unknown", None, None, "missing")),
                 _stop("10:00 AM", _venue("Fine", "06:00", "22:00"))]
        fixed, _ = enforce(_plan(*stops), [], ORDINARY)
        self.assertTrue(check_plan(fixed, ORDINARY)["ok"])

    def test_a_substitute_is_not_reused_from_elsewhere_in_the_day(self):
        already = _venue("Late Beach", "06:00", "21:00", vtype="beach")
        closed = _venue("Morning Museum", "09:00", "12:00", vtype="museum")
        fixed, report = enforce(
            _plan(_stop("10:00 AM", already), _stop("3:00 PM", closed)),
            [already], ORDINARY)
        names = [s["venue"]["name"] for s in fixed["stops"] if s.get("venue")]
        self.assertEqual(len(names), len(set(names)))

    def test_a_nap_slot_is_replaced_with_somewhere_a_nap_works(self):
        closed = _venue("Shut Park", "09:00", "10:00")
        pool = [_venue("A Museum", "06:00", "22:00", vtype="museum"),
                _venue("Open Park", "06:00", "22:00")]
        fixed, _ = enforce(_plan(_stop("2:00 PM", closed, kind="nap")), pool, ORDINARY)
        self.assertEqual(fixed["stops"][0]["venue"]["name"], "Open Park")

    def test_the_original_plan_is_never_mutated(self):
        closed = _venue("Morning Museum", "09:00", "12:00", vtype="museum")
        plan = _plan(_stop("3:00 PM", closed))
        enforce(plan, [_venue("Late Beach", "06:00", "21:00", vtype="beach")], ORDINARY)
        self.assertEqual(plan["stops"][0]["venue"]["name"], "Morning Museum")

    def test_it_says_when_no_venue_has_hours_for_the_day(self):
        # Otherwise a holiday reads as a mysteriously empty day.
        pool = [_venue("A", None, None, "holiday_unknown"),
                _venue("B", None, None, "holiday_unknown")]
        _, report = enforce(_plan(), pool, HOLIDAY)
        self.assertEqual(report["venues_without_hours"], 2)
        self.assertIn("holiday hours", report["note"])

    def test_it_says_when_the_day_is_only_thin(self):
        pool = [_venue("A", None, None, "missing"),
                _venue("B", None, None, "missing"),
                _venue("C", "09:00", "17:00")]
        _, report = enforce(_plan(), pool, ORDINARY)
        self.assertIn("thinner than usual", report["note"])

    def test_it_stays_quiet_when_the_data_is_good(self):
        pool = [_venue("A"), _venue("B")]
        _, report = enforce(_plan(), pool, ORDINARY)
        self.assertEqual(report["note"], "")


if __name__ == "__main__":
    unittest.main()
