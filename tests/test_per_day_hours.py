"""Opening hours that vary by day of the week.

One pair per venue was measured against the curated attractions, which are
mostly open the same hours all week. It does not survive contact with the rest:
of 17 real OpenStreetMap strings, a weekday/weekend split holds 7 exactly and a
per-day table holds 12. The five neither holds are seasonal, which belongs in
`hours_note` in words a parent reads.

Two rules make the table unambiguous:

  * no rows  -> the venue's single pair applies every day
  * any rows -> those rows are the whole week, so a day with none is closed

The second is why this is a table and not columns: "closed on Mondays" is the
commonest real closure and a nullable column cannot tell it from "not filled in".
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import os
import tempfile
import unittest
from src.store import schema
from src.web import guards
from contextlib import closing
from datetime import date
from unittest import mock

import src.store.db as db
from src.data_loader import _hours_for
from src.clients.osm import compare, per_day_hours

# Real strings, from the review queue and from one batched Overpass query about
# the community centres that have no hours.
ART_GALLERY = "Sa-Th 10:00-17:00; Fr 10:00-20:00"
COAL_HARBOUR = "Mo-Th 09:00-21:00; Fr-Sa 09:00-17:00; Su 10:00-17:00"
HASTINGS = "Mo-Fr 09:00-21:45; Sa 09:00-16:45; Su 10:00-14:00"
KERRISDALE = "Mo-Fr 06:30-22:00; Sa,Su 09:00-18:00"
SCIENCE_WORLD = "Mo-Su 10:00-17:00"
CAPILANO = "Jan 09-Mar 10 09:00-17:00; Nov 24-Jan 08 11:00-21:00; Dec 25 off"
GROUSE = "Su-Sa 08:45-22:00; Dec 24 08:30-18:00"

MON, TUE, FRI, SAT, SUN = 0, 1, 4, 5, 6


def _row(open_time="10:00", close_time="17:00", venue_type="museum"):
    return {"open_time": open_time, "close_time": close_time, "type": venue_type}


class ReadingAWeekFromOsmTest(unittest.TestCase):
    """A one-click action only where OSM describes a plain week."""

    def test_a_friday_late_opening_is_read(self):
        week = per_day_hours(ART_GALLERY)
        self.assertEqual(week[TUE], ("10:00", "17:00"))
        self.assertEqual(week[FRI], ("10:00", "20:00"))

    def test_days_grouped_across_the_weekend_boundary_are_read(self):
        # "Fr-Sa" groups a weekday with a Saturday, which is exactly what a
        # weekday/weekend split cannot express.
        week = per_day_hours(COAL_HARBOUR)
        self.assertEqual(week[FRI], ("09:00", "17:00"))
        self.assertEqual(week[SAT], ("09:00", "17:00"))
        self.assertEqual(week[SUN], ("10:00", "17:00"))

    def test_a_saturday_and_sunday_that_differ_are_read(self):
        week = per_day_hours(HASTINGS)
        self.assertNotEqual(week[SAT], week[SUN])

    def test_the_comma_spelling_of_a_day_group_is_read(self):
        self.assertEqual(per_day_hours(KERRISDALE)[SUN], ("09:00", "18:00"))

    def test_a_week_with_one_pair_is_still_a_week(self):
        week = per_day_hours(SCIENCE_WORLD)
        self.assertEqual(len(set(week.values())), 1)

    def test_seasonal_strings_are_refused(self):
        # No timetable holds these, and collapsing one would drop real hours.
        for says in (CAPILANO, GROUSE):
            with self.subTest(says=says[:24]):
                self.assertIsNone(per_day_hours(says))

    def test_a_partial_week_is_refused_rather_than_read_as_closed(self):
        # OSM omitting Sunday usually means nobody tagged it. Reading the gap as
        # "shut" would drop the venue from every Sunday plan on no evidence.
        self.assertIsNone(per_day_hours("Mo-Fr 09:00-17:00"))

    def test_nothing_reads_as_nothing(self):
        self.assertIsNone(per_day_hours(""))
        self.assertIsNone(per_day_hours(None))


class ResolvingHoursForADayTest(unittest.TestCase):
    def test_a_day_in_the_week_uses_its_own_pair(self):
        week = per_day_hours(ART_GALLERY)
        friday = _hours_for(_row(), "weekday", FRI, week)
        self.assertEqual((friday["open"], friday["close"]), ("10:00", "20:00"))
        self.assertEqual(friday["hours_source"], "per_day")

    def test_a_day_missing_from_the_week_is_closed(self):
        # The pattern a weekday/weekend split cannot express at all.
        shut_mondays = {day: ("10:00", "17:00") for day in range(1, 7)}
        monday = _hours_for(_row(), "weekday", MON, shut_mondays)
        self.assertIsNone(monday["open"])
        self.assertEqual(monday["hours_source"], "closed_today")

    def test_no_week_at_all_keeps_the_single_pair(self):
        for day in range(7):
            with self.subTest(day=day):
                hours = _hours_for(_row(), "weekday", day, None)
                self.assertEqual((hours["open"], hours["close"]),
                                 ("10:00", "17:00"))

    def test_unknown_hours_stay_unknown(self):
        hours = _hours_for(_row(None, None), "weekday", MON, None)
        self.assertIsNone(hours["open"])
        self.assertEqual(hours["hours_source"], "missing")

    def test_a_holiday_still_wins_over_the_week(self):
        # Otherwise Christmas Day reads its hours off whichever weekday it is.
        week = per_day_hours(SCIENCE_WORLD)
        hours = _hours_for(_row(), "holiday", FRI, week)
        self.assertIsNone(hours["open"])

    def test_somewhere_with_no_door_keeps_its_pair_on_a_holiday(self):
        hours = _hours_for(_row(venue_type="park"), "holiday", FRI, None)
        self.assertEqual((hours["open"], hours["close"]), ("10:00", "17:00"))


class WhatToTellAReviewerTest(unittest.TestCase):
    def test_a_week_we_already_hold_agrees(self):
        self.assertEqual(
            compare("10:00", "17:00", ART_GALLERY,
                    our_per_day=per_day_hours(ART_GALLERY)),
            "agrees")

    def test_a_week_we_do_not_hold_is_a_difference_we_can_settle(self):
        self.assertEqual(compare("10:00", "17:00", ART_GALLERY), "differs")

    def test_one_pair_matching_all_week_agrees(self):
        self.assertEqual(compare("10:00", "17:00", SCIENCE_WORLD), "agrees")

    def test_seasonal_is_still_more_than_we_can_hold(self):
        self.assertEqual(compare("09:00", "17:00", CAPILANO), "more_detail")


class _HoursTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import app as app_module
        self.app_module = app_module
        patcher = mock.patch.object(db, "DB_PATH",
                                    os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)
        self.admin = db.add_parent("a@example.com", "h", name="A")
        self.venue = db.add_venue("A Gallery", source="curated", city="Vancouver",
                                  venue_type="museum", open_time="10:00",
                                  close_time="17:00")
        self.client = app_module.app.test_client()
        patcher = mock.patch.object(guards, "current_parent",
            return_value={"id": self.admin, "is_admin": True,
                          "name": "A", "email": "a@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _week(self):
        return db.get_venue_hours([self.venue]).get(self.venue)

    def _pair(self):
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT open_time, close_time, hours_note "
                               "FROM venues WHERE id = ?", (self.venue,)).fetchone()
        return row["open_time"], row["close_time"], row["hours_note"]


class TheStoreTest(_HoursTest):
    def test_a_week_is_replaced_not_merged(self):
        # Leaving an old row behind would say a venue is open on a day the new
        # answer omits, which is the mistake this table exists to prevent.
        db.set_venue_hours(self.venue, {day: ("09:00", "17:00") for day in range(7)})
        db.set_venue_hours(self.venue, {day: ("09:00", "17:00") for day in range(1, 7)})
        self.assertNotIn(MON, self._week())

    def test_an_empty_week_hands_the_venue_back_to_its_pair(self):
        db.set_venue_hours(self.venue, {day: ("09:00", "17:00") for day in range(7)})
        db.set_venue_hours(self.venue, {})
        self.assertIsNone(self._week())

    def test_venues_with_no_rows_are_absent_rather_than_empty(self):
        self.assertEqual(db.get_venue_hours([self.venue]), {})


class SettlingACheckTest(_HoursTest):
    def setUp(self):
        super().setUp()
        db.record_hours_check(self.venue, "osm", ART_GALLERY, "differs",
                              "10:00", "17:00")
        self.check = db.get_pending_hours_checks()[0]["id"]

    def _settle(self, **data):
        return self.client.post(f"/venues/hours/{self.check}",
                                data={"venue_id": self.venue, **data})

    def test_taking_osm_stores_the_whole_week(self):
        self._settle(action="take_osm", source_says=ART_GALLERY)
        self.assertEqual(self._week()[FRI], ("10:00", "20:00"))
        self.assertEqual(self._week()[TUE], ("10:00", "17:00"))

    def test_a_uniform_week_collapses_to_the_single_pair(self):
        # Seven identical rows would say nothing the pair does not, and would
        # make "has rows" stop meaning "is unusual".
        self._settle(action="take_osm", source_says=SCIENCE_WORLD)
        self.assertIsNone(self._week())
        self.assertEqual(self._pair()[:2], ("10:00", "17:00"))

    def test_taking_osm_refuses_a_shape_it_cannot_store(self):
        self._settle(action="take_osm", source_says=CAPILANO)
        self.assertIsNone(self._week())

    def test_a_typed_week_is_stored_with_its_note(self):
        data = {f"day{day}_open": "09:00" for day in range(1, 7)}
        data.update({f"day{day}_close": "17:00" for day in range(1, 7)})
        self._settle(action="update", hours_note="Shorter in January.", **data)
        self.assertNotIn(MON, self._week())      # blank Monday means closed
        self.assertEqual(self._pair()[2], "Shorter in January.")

    def test_a_plain_pair_still_works_and_clears_any_week(self):
        self._settle(action="take_osm", source_says=ART_GALLERY)
        self._settle(action="update", open_time="11:00", close_time="16:00")
        self.assertIsNone(self._week())
        self.assertEqual(self._pair()[:2], ("11:00", "16:00"))

    def test_keeping_ours_changes_nothing(self):
        self._settle(action="keep")
        self.assertIsNone(self._week())
        self.assertEqual(self._pair()[:2], ("10:00", "17:00"))

    def test_settling_closes_the_check(self):
        self._settle(action="take_osm", source_says=ART_GALLERY)
        self.assertEqual(db.get_pending_hours_checks(), [])


class ThePlannerSeesTheWeekTest(_HoursTest):
    def test_a_friday_evening_is_offered_only_on_friday(self):
        from src.data_loader import get_venues
        db.set_venue_hours(self.venue, per_day_hours(ART_GALLERY))
        # 2026-09-04 is a Friday, 2026-09-01 a Tuesday. Neither is a holiday.
        friday = next(v for v in get_venues(on_date=date(2026, 9, 4))
                      if v["name"] == "A Gallery")
        tuesday = next(v for v in get_venues(on_date=date(2026, 9, 1))
                       if v["name"] == "A Gallery")
        self.assertEqual(friday["close"], "20:00")
        self.assertEqual(tuesday["close"], "17:00")

    def test_a_closed_day_is_not_schedulable(self):
        from src.data_loader import get_venues
        from src.itinerary import venue_open_for
        db.set_venue_hours(self.venue,
                           {day: ("10:00", "17:00") for day in range(1, 7)})
        monday = next(v for v in get_venues(on_date=date(2026, 9, 7))
                      if v["name"] == "A Gallery")
        self.assertFalse(venue_open_for(monday, 12 * 60, 60))


class TheOldSlotTableIsGoneTest(unittest.TestCase):
    def test_a_database_holding_the_season_table_is_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "app.db")
            with mock.patch.object(db, "DB_PATH", path):
                with closing(db.connect()) as conn:
                    conn.execute("""CREATE TABLE venue_hours (
                        id INTEGER PRIMARY KEY, venue_id INTEGER,
                        season TEXT, day_type TEXT,
                        open_time TEXT, close_time TEXT)""")
                    conn.commit()
                    schema.create_schema(conn)
                    columns = {r["name"] for r in
                               conn.execute("PRAGMA table_info(venue_hours)")}
        self.assertIn("weekday", columns)
        self.assertNotIn("season", columns)


if __name__ == "__main__":
    unittest.main()
