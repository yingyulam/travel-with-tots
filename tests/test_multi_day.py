"""A trip is a list of days, and a day out is a list of one.

The app planned exactly one day. Multi-day could have been a parallel path --
a second planner, a second template, a second table -- and that is two of
everything to keep in step. Instead a day became the unit: one `trips` row is
one day, a visit is rows sharing a `trip_group_id`, and `plan_days` is a loop
over the planner that already existed.

What that buys, and what these pin:

- **The single-day path is not a special case.** It is a list of one, through
  the same code. Half the tests here assert the one-day answer is unchanged,
  because "we did not break the thing that worked" is the requirement that
  actually gets broken.
- **Days do not repeat each other's venues**, via the `used` set `_build_plan`
  already kept to stop a day repeating itself.
- **Each day resolves its own opening hours**, because `plan_trip` parses the
  date it is handed and it is handed a different one each time.
- **Accommodation is per day** in the model, though the form asks once. That is
  the seam a different hotel on Thursday goes through.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import json
import os
import re
import tempfile
import unittest
from src import schema
from src.web import planning as web_planning
from contextlib import closing
from unittest import mock

from werkzeug.datastructures import MultiDict

import app as app_module
import src.db as db
from src.components import plan_trip as plan_module
from src.dates import MAX_TRIP_DAYS, date_range, days_between
from src.form_helpers import read_form, trip_dates, trip_too_long
from src.itinerary import generate_plans
from src.models import Day, Plan, Trip

BASE = {"wake_up": "07:00", "bedtime": "20:00", "naps": [], "age_years": "3",
        "age_months": "0", "destination": "Vancouver", "stop_count": "3",
        "features": [], "interest": [], "dining": "on_the_go",
        "transit": "car", "accommodation": "", "accommodation_lat": None,
        "accommodation_lng": None}


def _venue(name, km_east=0.0, rank=1):
    return {"id": abs(hash(name)) % 9999, "name": name, "type": "park",
            "setting": "outdoor", "neighbourhood": "Somewhere",
            "lat": 49.2827, "lng": -123.1207 + km_east * 0.0138,
            "open": "06:00", "close": "22:00", "can_eat": False,
            "nap_friendly": True, "seed_rank": rank}


POOL = [_venue(f"Venue {i}", km_east=i * 0.3, rank=i) for i in range(1, 10)]


def _names(plan):
    return [s["venue"]["name"] for s in plan.stops if s.get("venue")]


class ARangeIsAListOfDaysTest(unittest.TestCase):
    def test_one_day_when_there_is_no_end(self):
        self.assertEqual(date_range("2026-09-14", ""), ["2026-09-14"])

    def test_one_day_when_both_ends_match(self):
        self.assertEqual(date_range("2026-09-14", "2026-09-14"), ["2026-09-14"])

    def test_every_day_in_between_inclusive(self):
        self.assertEqual(date_range("2026-09-14", "2026-09-16"),
                         ["2026-09-14", "2026-09-15", "2026-09-16"])

    def test_a_backwards_range_is_one_day(self):
        # Degrades rather than raising, the way parse_date does: a muddled date
        # must not cost the parent a plan.
        self.assertEqual(date_range("2026-09-18", "2026-09-14"), ["2026-09-18"])

    def test_it_never_returns_more_than_the_cap(self):
        self.assertEqual(len(date_range("2026-09-01", "2026-12-31")),
                         MAX_TRIP_DAYS)

    def test_it_is_never_empty(self):
        # Every caller indexes it. An empty list would be a day with no plan.
        for start, end in [("", ""), (None, None), ("nonsense", "rubbish")]:
            with self.subTest(start=start):
                self.assertEqual(len(date_range(start, end)), 1)

    def test_the_asked_for_length_is_reported_uncapped(self):
        # So a parent can be told "that's 30 days" rather than being handed
        # seven without explanation.
        self.assertEqual(days_between("2026-09-01", "2026-09-30"), 30)


class TheFormAsksForTwoDatesTest(unittest.TestCase):
    def test_an_end_date_survives_the_form(self):
        form = read_form(MultiDict([("trip_date", "2026-09-14"),
                                    ("end_date", "2026-09-18")]))
        self.assertEqual(form["end_date"], "2026-09-18")
        self.assertEqual(len(trip_dates(form)), 5)

    def test_no_end_date_is_a_day_out(self):
        form = read_form(MultiDict([("trip_date", "2026-09-14")]))
        self.assertEqual(form["end_date"], "")
        self.assertEqual(trip_dates(form), ["2026-09-14"])

    def test_an_end_before_the_start_is_dropped_rather_than_kept(self):
        # Kept, it would render back into the field as a date the form would
        # refuse, with nothing saying why.
        form = read_form(MultiDict([("trip_date", "2026-09-18"),
                                    ("end_date", "2026-09-14")]))
        self.assertEqual(form["end_date"], "")

    def test_too_long_is_named_not_clamped(self):
        self.assertEqual(trip_too_long({"trip_date": "2026-09-01",
                                        "end_date": "2026-09-30"}), 30)

    def test_a_range_we_can_plan_is_not_flagged(self):
        self.assertIsNone(trip_too_long({"trip_date": "2026-09-14",
                                         "end_date": "2026-09-18"}))


class DaysDoNotRepeatEachOtherTest(unittest.TestCase):
    def test_a_venue_another_day_took_is_not_offered_again(self):
        first = generate_plans(POOL, BASE)[0]
        second = generate_plans(POOL, {**BASE, "used_names": set(_names(first))})[0]
        self.assertFalse(set(_names(first)) & set(_names(second)),
                         (_names(first), _names(second)))

    def test_and_the_second_day_is_still_a_real_day(self):
        # Paired with the test above: "no overlap" is also satisfied by an
        # empty second day, which is not the answer.
        first = generate_plans(POOL, BASE)[0]
        second = generate_plans(POOL, {**BASE, "used_names": set(_names(first))})[0]
        self.assertEqual(len(_names(second)), 3, _names(second))

    def test_with_nothing_taken_the_day_is_unchanged(self):
        # The one-day path: an empty exclusion set must not perturb anything.
        plain = _names(generate_plans(POOL, BASE)[0])
        empty = _names(generate_plans(POOL, {**BASE, "used_names": set()})[0])
        self.assertEqual(plain, empty)


class PlanDaysIsALoopTest(unittest.TestCase):
    """One call to the existing planner per date, and nothing else."""

    def setUp(self):
        patcher = mock.patch.object(plan_module, "PlanningAgent")
        agent = patcher.start()
        self.addCleanup(patcher.stop)
        agent.return_value.adjust_plan.side_effect = \
            plan_module.PlanningAgentError("no ai in tests")

    def _days(self, dates, **over):
        return plan_module.plan_days(
            dates, destination="Vancouver", age_months=36, transit="car",
            stop_count=3, dining="on_the_go", **over)

    def test_one_plan_per_date(self):
        days = self._days(["2026-09-14", "2026-09-15", "2026-09-16"])
        self.assertEqual(len(days), 3)

    def test_each_plan_knows_its_own_date(self):
        days = self._days(["2026-09-14", "2026-09-15"])
        self.assertEqual([d["trip_date"] for d in days],
                         ["2026-09-14", "2026-09-15"])

    def test_each_plan_knows_its_position(self):
        days = self._days(["2026-09-14", "2026-09-15"])
        self.assertEqual([d["day_index"] for d in days], [0, 1])

    def test_no_venue_appears_on_two_days(self):
        days = self._days(["2026-09-14", "2026-09-15", "2026-09-16"])
        seen = [s["venue"]["name"] for d in days for s in d["stops"]
                if s.get("venue")]
        self.assertEqual(len(seen), len(set(seen)), seen)

    def test_a_single_date_is_one_call_to_the_planner(self):
        # The invariant the whole design rests on: a day out did not become a
        # trip of one that takes a different route through the code.
        with mock.patch.object(plan_module, "plan_trip",
                               return_value={"stops": []}) as planner:
            plan_module.plan_days(["2026-09-14"], destination="Vancouver",
                                  age_months=36)
        planner.assert_called_once()
        self.assertEqual(planner.call_args.kwargs["trip_date"], "2026-09-14")

    def test_the_planner_is_told_what_earlier_days_took(self):
        calls = []

        def spy(**kwargs):
            calls.append(set(kwargs["used_names"]))
            return {"stops": [{"time": "9:00 AM", "kind": "activity",
                               "venue": {"name": f"V{len(calls)}"},
                               "reason": ""}]}

        with mock.patch.object(plan_module, "plan_trip", side_effect=spy):
            plan_module.plan_days(["a", "b", "c"], destination="Vancouver",
                                  age_months=36)
        # Nothing on day one, then one more name each day.
        self.assertEqual(calls, [set(), {"V1"}, {"V1", "V2"}])

    def test_each_day_is_planned_for_its_own_date(self):
        # Hours are resolved from the date, so a Sunday in the middle of a week
        # is planned as a Sunday. This is what makes that true.
        with mock.patch.object(plan_module, "plan_trip",
                               return_value={"stops": []}) as planner:
            plan_module.plan_days(["2026-09-14", "2026-09-15"],
                                  destination="Vancouver", age_months=36)
        self.assertEqual([c.kwargs["trip_date"] for c in planner.call_args_list],
                         ["2026-09-14", "2026-09-15"])


class TheModelHoldsDaysTest(unittest.TestCase):
    def _trip(self, dates):
        days = [Day(date=d, index=i, original=Plan("L", "b", []))
                for i, d in enumerate(dates)]
        return Trip(destination="Vancouver", transit="walk", days=days)

    def test_a_one_day_trip_is_not_multi_day(self):
        self.assertFalse(self._trip(["2026-09-14"]).is_multi_day)

    def test_two_days_are(self):
        self.assertTrue(self._trip(["2026-09-14", "2026-09-15"]).is_multi_day)

    def test_the_trip_date_is_the_first_day(self):
        self.assertEqual(self._trip(["2026-09-14", "2026-09-15"]).trip_date,
                         "2026-09-14")

    def test_a_day_out_of_range_is_none_rather_than_an_error(self):
        self.assertIsNone(self._trip(["2026-09-14"]).day(4))

    def test_each_day_keeps_its_own_versions(self):
        trip = self._trip(["2026-09-14", "2026-09-15"])
        trip.days[0].add_version(Plan("Updated", "b", []))
        self.assertEqual(len(trip.days[0].versions), 2)
        self.assertEqual(len(trip.days[1].versions), 1)

    def test_accommodation_lives_on_the_day_not_the_trip(self):
        # The form asks once and writes the same answer to every day. This is
        # what lets that become a different answer per day without the planner,
        # the page or the table changing.
        day = Day(date="2026-09-14", original=Plan("L", "b", []),
                  accommodation="Sylvia Hotel", accommodation_lat=49.28,
                  accommodation_lng=-123.14)
        self.assertEqual(Day.from_dict(day.to_dict()).accommodation,
                         "Sylvia Hotel")
        self.assertFalse(hasattr(Trip(destination="V", transit="walk"),
                                 "accommodation"))

    def test_a_day_lists_the_venues_it_visits(self):
        # What the next day is told to avoid, and where a visited-venue tick
        # will read from.
        plan = Plan("L", "b", [{"time": "9:00 AM", "kind": "activity",
                                "venue": {"name": "A Park"}, "reason": ""},
                               {"time": "12:00 PM", "kind": "meal",
                                "venue": None, "reason": ""}])
        self.assertEqual(Day(date="d", original=plan).venue_names(), ["A Park"])


class TheRouteRendersEveryDayTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, **extra):
        day = {"label": "L", "blurb": "b", "stops": [], "source": "rule",
               "adjusted": True, "changed": False, "hours": None,
               "out_of_range": []}
        dates = extra.pop("_dates", ["2026-09-14"])
        plans = [dict(day, trip_date=d) for d in dates]
        with mock.patch.object(web_planning, "plan_days",
                               return_value=plans) as planned:
            page = self.client.post("/plan", data={
                "generate": "1", "destination": "Vancouver", "age_years": "3",
                "age_months": "0", **extra})
        return page.get_data(as_text=True), planned

    def test_the_dates_reach_the_planner(self):
        _html, planned = self._post(trip_date="2026-09-14",
                                    end_date="2026-09-16",
                                    _dates=["2026-09-14", "2026-09-15",
                                            "2026-09-16"])
        self.assertEqual(planned.call_args.args[0],
                         ["2026-09-14", "2026-09-15", "2026-09-16"])

    def test_one_card_per_day(self):
        html, _ = self._post(trip_date="2026-09-14", end_date="2026-09-16",
                             _dates=["2026-09-14", "2026-09-15", "2026-09-16"])
        self.assertEqual(len(re.findall(r'data-day="', html)), 3)

    def test_a_day_out_still_renders_one_card_and_says_day(self):
        html, _ = self._post(trip_date="2026-09-14")
        self.assertEqual(len(re.findall(r'data-day="', html)), 1)
        self.assertIn("Start this day", html)
        self.assertNotIn("Start this trip", html)

    def test_a_visit_says_trip(self):
        html, _ = self._post(trip_date="2026-09-14", end_date="2026-09-15",
                             _dates=["2026-09-14", "2026-09-15"])
        self.assertIn("Start this trip", html)

    def test_the_actions_appear_once_however_many_days(self):
        # They act on the visit, and their ids have to stay unique.
        html, _ = self._post(trip_date="2026-09-14", end_date="2026-09-16",
                             _dates=["2026-09-14", "2026-09-15", "2026-09-16"])
        self.assertEqual(html.count('id="plan-revise-block"'), 1)
        self.assertEqual(html.count('id="plan-reject-btn"'), 1)

    def test_every_day_is_posted_onward_with_its_date(self):
        html, _ = self._post(trip_date="2026-09-14", end_date="2026-09-15",
                             _dates=["2026-09-14", "2026-09-15"])
        posted = json.loads(re.search(
            r'<textarea name="plans" hidden>(.*?)</textarea>',
            html, re.S).group(1).replace("&#34;", '"'))
        self.assertEqual([p["trip_date"] for p in posted],
                         ["2026-09-14", "2026-09-15"])


class TooLongIsRefusedTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _post(self, **extra):
        # A real plan shape, not a bare Mock: the route renders what it gets,
        # and a Mock reaching tojson turns a refusal into a 500 that still
        # satisfies assert_called_once.
        day = {"label": "L", "blurb": "b", "stops": [], "source": "rule",
               "adjusted": True, "changed": False, "hours": None,
               "out_of_range": [], "trip_date": "2026-09-14"}
        with mock.patch.object(web_planning, "plan_days",
                               return_value=[day]) as planned:
            page = self.client.post("/plan", data={
                "generate": "1", "destination": "Vancouver", "age_years": "3",
                "age_months": "0", **extra})
        self.assertEqual(page.status_code, 200)
        return page.get_data(as_text=True), planned

    def test_a_month_plans_nothing(self):
        _html, planned = self._post(trip_date="2026-09-01",
                                    end_date="2026-09-30")
        planned.assert_not_called()

    def test_and_says_how_long_it_was_and_what_we_allow(self):
        html, _ = self._post(trip_date="2026-09-01", end_date="2026-09-30")
        self.assertIn("30 days", html)
        self.assertIn(f"{MAX_TRIP_DAYS} at a time", html)

    def test_a_week_is_fine(self):
        _html, planned = self._post(trip_date="2026-09-14",
                                    end_date="2026-09-20")
        planned.assert_called_once()


class SavingAVisitTest(unittest.TestCase):
    """One row per day, sharing a group. Every existing query still reads a
    row as a day, which is why the dashboard and the delete button did not
    have to change."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                    os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect_sqlite()) as conn:
            schema.create_schema(conn)
        self.parent_id = db.add_parent("p@example.com", "hash", name="P")
        self.client = app_module.app.test_client()
        with self.client.session_transaction() as session:
            session["parent_id"] = self.parent_id

    def _plans(self, *dates):
        return [{"label": f"Day {i}", "blurb": "b", "stops": [],
                 "source": "rule", "trip_date": d}
                for i, d in enumerate(dates)]

    def _save(self, plans, **form):
        self.client.post("/save-trip", data={
            "plans": json.dumps(plans),
            "trip_form": json.dumps({"destination": "Vancouver",
                                     "transit": "car", **form})})
        return db.get_trips_for_parent(self.parent_id)

    def test_three_days_save_as_three_rows(self):
        rows = self._save(self._plans("2026-09-14", "2026-09-15", "2026-09-16"))
        self.assertEqual(len(rows), 3)

    def test_they_share_one_group(self):
        rows = self._save(self._plans("2026-09-14", "2026-09-15"))
        self.assertEqual(len({r["trip_group_id"] for r in rows}), 1)

    def test_each_row_keeps_its_own_date_and_position(self):
        rows = self._save(self._plans("2026-09-14", "2026-09-15", "2026-09-16"))
        by_index = {r["day_index"]: r["trip_date"] for r in rows}
        self.assertEqual(by_index, {0: "2026-09-14", 1: "2026-09-15",
                                    2: "2026-09-16"})

    def test_a_day_out_saves_one_row_that_still_has_a_group(self):
        # A group of one, so reading a trip back is never two shapes.
        rows = self._save(self._plans("2026-09-14"))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["trip_group_id"])
        self.assertEqual(rows[0]["day_index"], 0)

    def test_the_older_single_plan_field_still_works(self):
        # Anything that posts one plan rather than a list: a saved snapshot, a
        # bookmarked form, a test written before this.
        self.client.post("/save-trip", data={
            "plan": json.dumps({"label": "L", "blurb": "b", "stops": [],
                                "trip_date": "2026-09-14"}),
            "trip_form": json.dumps({"destination": "Vancouver"})})
        rows = db.get_trips_for_parent(self.parent_id)
        self.assertEqual(len(rows), 1)

    def test_the_group_id_is_not_taken_from_the_post(self):
        # It decides which rows read back as one trip. Accepting one from the
        # client would let a parent staple their day onto another group.
        rows = self._save(self._plans("2026-09-14"),
                          trip_group_id="somebody-elses-trip")
        self.assertNotEqual(rows[0]["trip_group_id"], "somebody-elses-trip")

    def test_a_visit_saved_per_child_keeps_the_days_together(self):
        child_id = db.add_child(self.parent_id, "Kid", "2023-01-01")
        rows = self._save(self._plans("2026-09-14", "2026-09-15"),
                          child_ids=[str(child_id)])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len({r["trip_group_id"] for r in rows}), 1)
        self.assertEqual({r["child_id"] for r in rows}, {child_id})


class ReopeningAVisitTest(SavingAVisitTest):
    def _open(self, trip_id):
        page = self.client.get(f"/trip/{trip_id}").get_data(as_text=True)
        return json.loads(re.search(
            r'id="trip-data" type="application/json">(.*?)</script>',
            page, re.S).group(1))

    def test_opening_one_day_opens_the_whole_visit(self):
        rows = self._save(self._plans("2026-09-14", "2026-09-15", "2026-09-16"))
        trip = self._open(rows[0]["id"])
        self.assertEqual([d["date"] for d in trip["days"]],
                         ["2026-09-14", "2026-09-15", "2026-09-16"])

    def test_the_days_come_back_in_order(self):
        # By day_index, not by insertion or by date string: an index cannot be
        # ambiguous and a missing date should not sort a day to the front.
        rows = self._save(self._plans("2026-09-14", "2026-09-15", "2026-09-16"))
        trip = self._open(rows[0]["id"])
        self.assertEqual([d["index"] for d in trip["days"]], [0, 1, 2])

    def test_it_opens_on_the_day_that_was_clicked(self):
        rows = self._save(self._plans("2026-09-14", "2026-09-15", "2026-09-16"))
        middle = [r for r in rows if r["day_index"] == 1][0]
        page = self.client.get(f"/trip/{middle['id']}").get_data(as_text=True)
        self.assertIn("Math.min(1,", page)

    def test_a_row_saved_before_groups_existed_reads_as_one_day(self):
        # Every trip already in the live database has no group id.
        rows = self._save(self._plans("2026-09-14"))
        with closing(db.connect_sqlite()) as conn:
            with conn:
                conn.execute("UPDATE trips SET trip_group_id = NULL, "
                             "day_index = NULL WHERE id = ?", (rows[0]["id"],))
        trip = self._open(rows[0]["id"])
        self.assertEqual(len(trip["days"]), 1)
        self.assertEqual(trip["days"][0]["date"], "2026-09-14")


class TheInTripPageSwitchesDaysTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _page(self, *dates):
        plans = [{"label": "L", "blurb": "b", "stops": [], "source": "rule",
                  "trip_date": d} for d in dates]
        context = {"destination": "Vancouver", "transit": "car",
                   "trip_date": dates[0], "age_years": "3", "age_months": "0"}
        if len(dates) > 1:
            context["end_date"] = dates[-1]
        return self.client.post("/trip", data={
            "plans": json.dumps(plans),
            "context": json.dumps(context)}).get_data(as_text=True)

    def _trip(self, html):
        return json.loads(re.search(
            r'id="trip-data" type="application/json">(.*?)</script>',
            html, re.S).group(1))

    def test_every_posted_day_reaches_the_page(self):
        trip = self._trip(self._page("2026-09-14", "2026-09-15", "2026-09-16"))
        self.assertEqual([d["date"] for d in trip["days"]],
                         ["2026-09-14", "2026-09-15", "2026-09-16"])

    def test_each_day_starts_with_its_own_single_version(self):
        trip = self._trip(self._page("2026-09-14", "2026-09-15"))
        self.assertEqual([len(d["plans"]) for d in trip["days"]], [1, 1])

    def test_a_day_out_is_a_trip_of_one(self):
        trip = self._trip(self._page("2026-09-14"))
        self.assertEqual(len(trip["days"]), 1)

    def test_the_page_carries_a_day_picker_to_hide_or_show(self):
        self.assertIn('id="day-picker"', self._page("2026-09-14"))

    def test_a_single_posted_plan_still_opens(self):
        # The older hand-off shape, which posts one plan rather than a list.
        page = self.client.post("/trip", data={
            "plan": json.dumps({"label": "L", "blurb": "b", "stops": []}),
            "context": json.dumps({"destination": "Vancouver",
                                   "trip_date": "2026-09-14"})})
        self.assertEqual(page.status_code, 200)
        self.assertEqual(len(self._trip(page.get_data(as_text=True))["days"]), 1)

    def test_a_post_with_no_plan_at_all_goes_back_to_planning(self):
        page = self.client.post("/trip", data={"plans": json.dumps([])})
        self.assertEqual(page.status_code, 302)


class TheColumnsAreRegisteredEverywhereTest(unittest.TestCase):
    """The failure this closes took down five pages in commit ece7a4d: a column
    added to SCHEMA reached SQLite and never reached Supabase."""

    def test_the_schema_has_them(self):
        for column in ("trip_group_id", "day_index"):
            with self.subTest(column=column):
                self.assertIn(column, schema.SCHEMA)

    def test_add_trip_will_write_them(self):
        for column in ("trip_group_id", "day_index"):
            with self.subTest(column=column):
                self.assertIn(column, db.TRIP_FIELDS)

    def test_supabase_gets_them_too(self):
        registered = {(t, c) for t, c, _ in schema.POSTGRES_ADDED_COLUMNS}
        self.assertIn(("trips", "trip_group_id"), registered)
        self.assertIn(("trips", "day_index"), registered)

    def test_an_existing_sqlite_database_is_migrated(self):
        # A database exactly like the live one before this change: everything
        # else in place, these two columns absent.
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "old.db")
            with mock.patch.object(db, "DB_PATH", path):
                with closing(db.connect_sqlite()) as conn:
                    schema.create_schema(conn)
                    with conn:
                        conn.execute("ALTER TABLE trips DROP COLUMN trip_group_id")
                        conn.execute("ALTER TABLE trips DROP COLUMN day_index")
                    before = {r["name"] for r in
                              conn.execute("PRAGMA table_info(trips)")}
                    schema._ensure_columns(conn)
                    after = {r["name"] for r in
                             conn.execute("PRAGMA table_info(trips)")}
        # Both halves: the fixture really was missing them, and the migration
        # really put them back. Without the first, this passes on a database
        # that never lost them.
        self.assertNotIn("trip_group_id", before)
        self.assertIn("trip_group_id", after)
        self.assertIn("day_index", after)


if __name__ == "__main__":
    unittest.main()
