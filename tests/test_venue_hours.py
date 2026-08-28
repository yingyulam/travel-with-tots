"""Which of a venue's hours apply on the day being planned.

Deciding whether a place can be visited at a given time is most of what the
planner does, so a museum that shuts at four in January must not be offered a
five o'clock slot. Hours vary two ways here: by season and by weekday/weekend.
"""

import os
import tempfile
import unittest
from contextlib import closing
from datetime import date
from unittest import mock

from src import data_loader, db
from src.dates import day_type_for, parse_date, season_for


class DateSlotTest(unittest.TestCase):
    def test_summer_runs_may_to_september(self):
        self.assertEqual(season_for(date(2026, 4, 30)), "winter")
        self.assertEqual(season_for(date(2026, 5, 1)), "summer")
        self.assertEqual(season_for(date(2026, 9, 30)), "summer")
        self.assertEqual(season_for(date(2026, 10, 1)), "winter")

    def test_weekend_is_saturday_and_sunday(self):
        self.assertEqual(day_type_for(date(2026, 8, 28)), "weekday")   # Friday
        self.assertEqual(day_type_for(date(2026, 8, 29)), "weekend")   # Saturday
        self.assertEqual(day_type_for(date(2026, 8, 30)), "weekend")   # Sunday
        self.assertEqual(day_type_for(date(2026, 8, 31)), "weekday")   # Monday

    def test_an_unusable_date_degrades_to_today(self):
        # A bad date must not cost a parent their plan.
        self.assertEqual(parse_date("not a date"), date.today())
        self.assertEqual(parse_date(""), date.today())
        self.assertEqual(parse_date(None), date.today())

    def test_a_good_date_is_kept(self):
        self.assertEqual(parse_date("2026-12-24"), date(2026, 12, 24))


class VenueHoursTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.venue = db.add_venue("A Museum", source="curated", city="Vancouver",
                                  venue_type="museum",
                                  open_time="10:00", close_time="17:00")

    def _hours_on(self, iso):
        venue = data_loader.get_venues(on_date=date.fromisoformat(iso))[0]
        return venue["open"], venue["close"]

    def test_the_default_pair_applies_when_no_slot_is_set(self):
        # Most venues are the same all year, so no slot rows at all is the
        # common case rather than missing data.
        self.assertEqual(self._hours_on("2026-01-14"), ("10:00", "17:00"))
        self.assertEqual(self._hours_on("2026-07-11"), ("10:00", "17:00"))

    def test_a_slot_overrides_the_default_on_matching_days_only(self):
        db.set_venue_hours(self.venue, "winter", "weekday", "10:00", "16:00")
        self.assertEqual(self._hours_on("2026-01-14"), ("10:00", "16:00"))
        self.assertEqual(self._hours_on("2026-01-17"), ("10:00", "17:00"))
        self.assertEqual(self._hours_on("2026-07-15"), ("10:00", "17:00"))

    def test_all_four_slots_resolve_independently(self):
        for season, day_type, opens, closes in (
                ("summer", "weekday", "09:00", "18:00"),
                ("summer", "weekend", "08:00", "20:00"),
                ("winter", "weekday", "10:00", "16:00"),
                ("winter", "weekend", "11:00", "15:00")):
            db.set_venue_hours(self.venue, season, day_type, opens, closes)
        self.assertEqual(self._hours_on("2026-07-15"), ("09:00", "18:00"))
        self.assertEqual(self._hours_on("2026-07-11"), ("08:00", "20:00"))
        self.assertEqual(self._hours_on("2026-01-14"), ("10:00", "16:00"))
        self.assertEqual(self._hours_on("2026-01-17"), ("11:00", "15:00"))

    def test_setting_a_slot_twice_replaces_it(self):
        db.set_venue_hours(self.venue, "winter", "weekday", "10:00", "16:00")
        db.set_venue_hours(self.venue, "winter", "weekday", "11:00", "15:00")
        self.assertEqual(self._hours_on("2026-01-14"), ("11:00", "15:00"))
        with closing(db.connect()) as conn:
            count = conn.execute("SELECT COUNT(*) FROM venue_hours").fetchone()[0]
        self.assertEqual(count, 1)

    def test_clearing_a_slot_returns_to_the_default(self):
        db.set_venue_hours(self.venue, "winter", "weekday", "10:00", "16:00")
        db.clear_venue_hours(self.venue, "winter", "weekday")
        self.assertEqual(self._hours_on("2026-01-14"), ("10:00", "17:00"))

    def test_an_unknown_slot_raises(self):
        with self.assertRaises(ValueError):
            db.set_venue_hours(self.venue, "autumn", "weekday", "10:00", "16:00")
        with self.assertRaises(ValueError):
            db.set_venue_hours(self.venue, "winter", "tuesday", "10:00", "16:00")

    def test_hours_are_scoped_to_the_venues_asked_about(self):
        other = db.add_venue("A Park", source="curated", city="Vancouver")
        db.set_venue_hours(other, "winter", "weekday", "08:00", "20:00")
        self.assertEqual(db.venue_hours_by_slot([self.venue]), {})

    def test_no_date_means_today(self):
        # get_venues() with no date must still resolve, not raise.
        self.assertEqual(len(data_loader.get_venues()), 1)

    def test_deleting_a_venue_takes_its_hours(self):
        db.set_venue_hours(self.venue, "winter", "weekday", "10:00", "16:00")
        with closing(db.connect()) as conn, conn:
            conn.execute("DELETE FROM venues WHERE id = ?", (self.venue,))
            left = conn.execute("SELECT COUNT(*) FROM venue_hours").fetchone()[0]
        self.assertEqual(left, 0)


class PlanningWithHoursTest(unittest.TestCase):
    """The point of all of it: the day being planned changes what fits."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.museum = db.add_venue("Late Museum", source="curated",
                                   city="Vancouver", venue_type="museum",
                                   open_time="10:00", close_time="18:00")
        db.add_venue("A Park", source="curated", city="Vancouver",
                     venue_type="park", open_time="06:00", close_time="22:00")

    def _stops(self, iso):
        from src.itinerary import generate_plans
        inputs = {"wake_up": "07:00", "bedtime": "20:00", "naps": [],
                  "age_years": "3", "age_months": "0", "destination": "Vancouver",
                  "stop_count": 3, "features": [], "themes": [],
                  "dining": "on_the_go", "accommodation": "",
                  "preferred_lunch_time": "", "transit_nap": ""}
        venues = data_loader.get_venues(on_date=date.fromisoformat(iso))
        plan = generate_plans(venues, inputs)[0].to_dict()
        return [(s["time"], s["venue"]["name"]) for s in plan["stops"] if s.get("venue")]

    def test_a_venue_closed_early_in_winter_loses_its_late_slot(self):
        db.set_venue_hours(self.museum, "winter", "weekday", "10:00", "12:00")
        summer = [name for _, name in self._stops("2026-07-15")]
        winter = [name for _, name in self._stops("2026-01-14")]
        self.assertIn("Late Museum", summer)
        # In winter it can only fill a morning slot, so a late stop cannot be it.
        late_winter = [name for time, name in self._stops("2026-01-14")
                       if time.endswith("PM") and not time.startswith("12")]
        self.assertNotIn("Late Museum", late_winter)


if __name__ == "__main__":
    unittest.main()
