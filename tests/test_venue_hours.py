"""Which of a venue's hours apply on the day being planned.

Deciding whether a place can be visited at a given time is most of what the
planner does, so a museum that shuts at four must not be offered a five
o'clock slot, and a venue whose hours nobody knows must not be offered at all.

**One pair, plus a rule about holidays.** There used to be a `venue_hours`
table keyed on (season, day_type). It never held a single row, and the real
data showed why it could not: of seven venues OSM disagreed with us about, it
could express one. A museum closed on Mondays from September and a mountain
with its own Christmas Eve hours both need something a 6-slot grid has no shape
for. What a single pair cannot hold now goes in `hours_note`, in words a parent
reads, and the planner is honest that it does not model the rest.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import os
import tempfile
import unittest
from contextlib import closing
from datetime import date
from unittest import mock

from src import data_loader
from src.store import db, schema
from src.data_loader import HOURS_ARE_A_CONVENTION
from src.dates import bc_holidays, day_type_for, parse_date


class DateTest(unittest.TestCase):
    def test_weekend_is_saturday_and_sunday(self):
        self.assertEqual(day_type_for(date(2026, 8, 28)), "weekday")   # Friday
        self.assertEqual(day_type_for(date(2026, 8, 29)), "weekend")   # Saturday
        self.assertEqual(day_type_for(date(2026, 8, 30)), "weekend")   # Sunday
        self.assertEqual(day_type_for(date(2026, 8, 31)), "weekday")   # Monday

    def test_a_holiday_outranks_the_day_of_the_week(self):
        # Christmas Day 2026 is a Friday.
        self.assertEqual(day_type_for(date(2026, 12, 25)), "holiday")

    def test_an_unusable_date_degrades_to_today(self):
        # A bad date must not cost a parent their plan.
        self.assertEqual(parse_date("not a date"), date.today())
        self.assertEqual(parse_date(""), date.today())
        self.assertEqual(parse_date(None), date.today())

    def test_a_good_date_is_kept(self):
        self.assertEqual(parse_date("2026-12-24"), date(2026, 12, 24))


class _WithVenues(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)

    def _add(self, name, venue_type, opens="10:00", closes="17:00"):
        return db.add_venue(name, source="curated", city="Vancouver",
                            venue_type=venue_type,
                            open_time=opens, close_time=closes)

    def _venue_on(self, iso, name=None):
        venues = data_loader.get_venues(on_date=date.fromisoformat(iso))
        if name is None:
            return venues[0]
        return next(v for v in venues if v["name"] == name)

    def _hours_on(self, iso, name=None):
        venue = self._venue_on(iso, name)
        return venue["open"], venue["close"]


class DefaultPairTest(_WithVenues):
    def test_the_default_pair_applies_on_an_ordinary_day(self):
        self._add("A Museum", "museum")
        self.assertEqual(self._hours_on("2026-07-15"), ("10:00", "17:00"))
        self.assertEqual(self._hours_on("2026-01-14"), ("10:00", "17:00"))

    def test_a_weekend_is_the_same_as_a_weekday(self):
        # The distinction existed only for the slot table, and no venue ever
        # used it. A venue that really differs says so in hours_note.
        self._add("A Museum", "museum")
        self.assertEqual(self._hours_on("2026-08-28"),      # Friday
                         self._hours_on("2026-08-29"))      # Saturday

    def test_no_date_means_today(self):
        self._add("A Museum", "museum")
        self.assertEqual(data_loader.get_venues()[0]["open"], "10:00")

    def test_a_venue_with_no_pair_has_unknown_hours(self):
        self._add("Hours Unknown", "museum", opens=None, closes=None)
        self.assertEqual(self._hours_on("2026-07-15"), (None, None))
        self.assertEqual(self._venue_on("2026-07-15")["hours_source"], "missing")

    def test_a_venue_with_unknown_hours_cannot_be_scheduled(self):
        from src.itinerary import venue_open_for
        self._add("Hours Unknown", "museum", opens=None, closes=None)
        venue = self._venue_on("2026-07-15")
        self.assertFalse(venue_open_for(venue, 11 * 60, 60))


class HolidayTest(_WithVenues):
    """A holiday depends on whether there is a door.

    Refusing every venue on a holiday made the app useless on 11 days a year:
    Canada Day produced a plan with zero stops, while every park in the city
    sat there open.
    """

    def test_a_venue_with_a_door_does_not_inherit_its_default_pair(self):
        # A default pair is a statement about ordinary days. Carrying it over to
        # Christmas Day would be inventing an answer, and most paid attractions
        # keep different hours or shut altogether.
        self._add("A Museum", "museum")
        self.assertEqual(self._hours_on("2026-12-24"), ("10:00", "17:00"))
        self.assertEqual(self._hours_on("2026-12-25"), (None, None))
        self.assertEqual(self._venue_on("2026-12-25")["hours_source"],
                         "holiday_unknown")

    def test_a_venue_with_no_door_keeps_its_hours(self):
        for venue_type in HOURS_ARE_A_CONVENTION:
            with self.subTest(type=venue_type):
                self._add(f"A {venue_type}", venue_type,
                          opens="06:00", closes="22:00")
                self.assertEqual(self._hours_on("2026-12-25", f"A {venue_type}"),
                                 ("06:00", "22:00"))

    def test_a_ticketed_garden_is_not_treated_as_a_park(self):
        # All four of ours are gated and ticketed, and shut on Christmas like
        # any other paid attraction.
        self.assertNotIn("garden", HOURS_ARE_A_CONVENTION)
        self._add("VanDusen", "garden")
        self.assertEqual(self._hours_on("2026-12-25"), (None, None))

    def test_a_door_less_venue_still_needs_a_pair_to_be_scheduled(self):
        # The convention says "nothing is locked", not "open at all hours".
        self._add("A Park", "park", opens=None, closes=None)
        self.assertEqual(self._venue_on("2026-12-25")["hours_source"], "missing")

    def test_every_statutory_holiday_behaves_the_same(self):
        self._add("A Museum", "museum")
        self._add("A Park", "park", opens="06:00", closes="22:00")
        for holiday in sorted(bc_holidays(2026)):
            with self.subTest(holiday=holiday.isoformat()):
                iso = holiday.isoformat()
                self.assertEqual(self._hours_on(iso, "A Museum"), (None, None))
                self.assertEqual(self._hours_on(iso, "A Park"),
                                 ("06:00", "22:00"))


class PlanningWithHoursTest(_WithVenues):
    """The point of all of it: the day being planned changes what fits."""

    def _stops(self, iso):
        from src.itinerary import generate_plans
        inputs = {"wake_up": "07:00", "bedtime": "20:00", "naps": [],
                  "age_years": "3", "age_months": "0", "destination": "Vancouver",
                  "stop_count": 3, "features": [], "interest": [],
                  "dining": "on_the_go", "accommodation": "",
                  "preferred_lunch_time": "", "transit_nap": ""}
        venues = data_loader.get_venues(on_date=date.fromisoformat(iso))
        plan = generate_plans(venues, inputs)[0].to_dict()
        return [s["venue"]["name"] for s in plan["stops"] if s.get("venue")]

    def test_a_venue_that_shuts_early_loses_a_late_slot(self):
        self._add("Shuts At Noon", "museum", opens="10:00", closes="12:00")
        self._add("A Park", "park", opens="06:00", closes="22:00")
        stops = self._stops("2026-07-15")
        self.assertIn("A Park", stops)

    def test_canada_day_is_plannable(self):
        # The bug this replaced: 0 of 28 venues schedulable, so a plan with no
        # stops at all.
        self._add("A Museum", "museum")
        for i in range(3):
            self._add(f"Park {i}", "park", opens="06:00", closes="22:00")
        stops = self._stops("2026-07-01")
        self.assertTrue(stops, "Canada Day produced an empty plan")
        self.assertNotIn("A Museum", stops)

    def test_a_holiday_with_only_ticketed_venues_is_still_empty(self):
        # Honest, not defeatist: we genuinely do not know, and a wrong stop is
        # worse than an empty slot.
        self._add("A Museum", "museum")
        self._add("An Aquarium", "aquarium")
        self.assertEqual(self._stops("2026-07-01"), [])


class HoursNoteTest(_WithVenues):
    """What a single pair cannot hold, in words a parent reads."""

    def test_a_venue_can_carry_a_note(self):
        venue_id = db.add_venue(
            "Maritime Museum", source="curated", city="Vancouver",
            venue_type="museum", open_time="10:00", close_time="17:00",
            hours_note="Closed Mondays September to May.")
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT hours_note FROM venues WHERE id = ?",
                               (venue_id,)).fetchone()
        self.assertEqual(row["hours_note"], "Closed Mondays September to May.")

    def test_the_note_is_not_parsed_or_acted_on(self):
        # Deliberately: the planner does not pretend to understand it. It is
        # for the parent, alongside the Google Maps link on every stop.
        self._add("A Museum", "museum")
        venue = self._venue_on("2026-08-31")            # a Monday
        self.assertEqual((venue["open"], venue["close"]), ("10:00", "17:00"))


if __name__ == "__main__":
    unittest.main()
