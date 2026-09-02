"""The Vancouver Open Data importer.

Every record below is a real captured response body, trimmed to the rows the
assertions need. That is deliberate: the field names are the part of this
feature most likely to be wrong, and a hand-written fixture would only ever
prove the importer agrees with itself.
"""

import os
import tempfile
import unittest
from src.web import guards
from contextlib import closing
from unittest import mock

import app as app_module
from src import db, importers, opendata

# parks. Chosen for what each one proves:
#   Arbutus Village Park          hyphenated local area; washrooms "N"
#   Trafalgar Park                washrooms "Y", and a washrooms-dataset row
#   Queen Elizabeth Park          already in data/venues.json under this name
#   John Hendry (Trout Lake) Park the curator calls it the other way round
#   English Bay Beach Park        the curator drops the "Park"
#   Riley Park                    washrooms "N", yet a fieldhouse is listed.
#                                 One of the 9 places the City contradicts
#                                 itself, and the reason the join exists.
PARKS = [
    {"parkid": 1, "name": "Arbutus Village Park", "washrooms": "N",
     "streetnumber": "4202", "streetname": "Valley Drive",
     "neighbourhoodname": "Arbutus-Ridge", "hectare": 1.41,
     "googlemapdest": {"lon": -123.15525, "lat": 49.249783}},
    {"parkid": 8, "name": "Trafalgar Park", "washrooms": "Y",
     "streetnumber": "2610", "streetname": "W 23rd Avenue",
     "neighbourhoodname": "Arbutus-Ridge", "hectare": 4.85,
     "googlemapdest": {"lon": -123.161879, "lat": 49.25046}},
    {"parkid": 167, "name": "Queen Elizabeth Park", "washrooms": "Y",
     "streetnumber": "4600", "streetname": "Cambie Street",
     "neighbourhoodname": "Riley Park", "hectare": 52.98,
     "googlemapdest": {"lon": -123.112028, "lat": 49.240978}},
    {"parkid": 85, "name": "John Hendry (Trout Lake) Park", "washrooms": "Y",
     "streetnumber": "3300", "streetname": "Victoria Drive",
     "neighbourhoodname": "Kensington-Cedar Cottage", "hectare": 27.31,
     "googlemapdest": {"lon": -123.062242, "lat": 49.255808}},
    {"parkid": 201, "name": "English Bay Beach Park", "washrooms": "Y",
     "streetnumber": "1700", "streetname": "Beach Avenue",
     "neighbourhoodname": "West End", "hectare": 9.83,
     "googlemapdest": {"lon": -123.143317, "lat": 49.287118}},
    {"parkid": 168, "name": "Riley Park", "washrooms": "N",
     "streetnumber": "50", "streetname": "E 30th Avenue",
     "neighbourhoodname": "Riley Park", "hectare": 2.7,
     "googlemapdest": {"lon": -123.104361, "lat": 49.241987}},
]

CENTRES = [
    {"name": "Hastings", "address": "3096 E Hastings St",
     "urllink": "http://vancouver.ca/parks/cc/hastings/index.htm",
     "geo_local_area": "Hastings-Sunrise",
     "geo_point_2d": {"lon": -123.0393, "lat": 49.2809}},
]

WASHROOMS = [
    {"park_name": "Trafalgar Park", "type": "Park - Field House",
     "location": "North side, fieldhouse", "geo_local_area": "Arbutus Ridge",
     "summer_hours": "Dawn to Dusk", "winter_hours": "Dawn to Dusk"},
    {"park_name": "Hastings", "type": "Community Center",
     "location": "3096 E Hastings St", "geo_local_area": "Hastings-Sunrise",
     "summer_hours": "as per CC operating hours",
     "winter_hours": "as per CC operating hours"},
    {"park_name": "Riley Park", "type": "Park - Field House",
     "location": "Fieldhouse", "geo_local_area": "Riley Park",
     "summer_hours": "Dawn to Dusk", "winter_hours": "Dawn to Dusk"},
    # A row with no park_name, which 13 of the 147 have. Must not become a
    # blank-named place the join then matches everything against.
    {"park_name": None, "location": "Vancouver Art Gallery",
     "geo_local_area": "Downtown", "summer_hours": "6am - 10pm"},
]


def _park(name):
    return next(p for p in PARKS if p["name"] == name)


class MappingTest(unittest.TestCase):
    """Pure record-to-venue mapping. No database, no network."""

    def test_a_park_becomes_a_plannable_venue(self):
        entry = importers.park_entry(_park("Trafalgar Park"))
        self.assertEqual(entry["name"], "Trafalgar Park")
        self.assertEqual(entry["external_id"], "vanopendata:parks/8")
        self.assertIn("parks", entry["source_url"])
        self.assertIn("refine.parkid=8", entry["source_url"])
        fields = entry["fields"]
        self.assertEqual(fields["type"], "park")
        self.assertEqual(fields["city"], "Vancouver")
        self.assertEqual(fields["address"], "2610 W 23rd Avenue")
        self.assertEqual((fields["lat"], fields["lng"]), (49.25046, -123.161879))
        self.assertEqual((fields["open_time"], fields["close_time"]),
                         importers.PARK_HOURS)
        self.assertEqual(fields["can_eat"], 0)
        self.assertIs(entry["washroom"], True)

    def test_the_citys_hyphenated_local_area_becomes_ours(self):
        # The only disagreement in 22 areas, and it would otherwise put every
        # Arbutus Ridge park in a cluster of its own.
        entry = importers.park_entry(_park("Arbutus Village Park"))
        self.assertEqual(entry["fields"]["neighbourhood"], "Arbutus Ridge")

    def test_an_unknown_local_area_is_left_null_not_written(self):
        # neighbourhood is how get_candidate_venues keeps a day's stops near
        # each other, so a value outside the enum is a cluster of one.
        record = dict(_park("Riley Park"), neighbourhoodname="Somewhere New")
        self.assertIsNone(importers.park_entry(record)["fields"]["neighbourhood"])

    def test_a_park_with_no_washrooms_says_so_rather_than_saying_nothing(self):
        self.assertIs(importers.park_entry(_park("Riley Park"))["washroom"], False)

    def test_a_community_centre_is_named_as_one_and_arrives_without_hours(self):
        entry = importers.centre_entry(CENTRES[0])
        self.assertEqual(entry["name"], "Hastings Community Centre")
        self.assertEqual(entry["external_id"],
                         "vanopendata:community-centres/hastings")
        self.assertEqual(entry["fields"]["type"], "community centre")
        self.assertEqual(entry["fields"]["neighbourhood"], "Hastings-Sunrise")
        # The City publishes the address and the coordinates, not the hours.
        # Inventing a pair would be a guess about whether a family can get in.
        self.assertIsNone(entry["fields"]["open_time"])
        self.assertIsNone(entry["fields"]["close_time"])

    def test_a_multi_word_centre_name_slugs_without_spaces(self):
        entry = importers.centre_entry(dict(CENTRES[0], name="False Creek"))
        self.assertEqual(entry["external_id"],
                         "vanopendata:community-centres/false-creek")

    def test_the_curators_spelling_wins_so_the_row_is_matched_not_duplicated(self):
        # Renaming the entry is what makes the exact-name match in
        # upsert_imported_venue hit. `name` is not an import field, so the
        # stored name is the curator's either way.
        self.assertEqual(importers.park_entry(
            _park("John Hendry (Trout Lake) Park"))["name"],
            "Trout Lake (John Hendry Park)")
        self.assertEqual(importers.park_entry(
            _park("English Bay Beach Park"))["name"], "English Bay Beach")

    def test_the_preview_agrees_with_what_will_be_written(self):
        # The dry run once reported only the parks dataset's own Y/N, which
        # understated washrooms by the 9 parks the City contradicts itself on:
        # a preview that disagrees with the write is worse than no preview.
        names = importers.washroom_places(WASHROOMS)
        self.assertIs(importers.resolved_washroom(
            importers.park_entry(_park("Riley Park")), names), True)
        self.assertIs(importers.resolved_washroom(
            importers.park_entry(_park("Arbutus Village Park")), names), False)
        self.assertIsNone(importers.resolved_washroom(
            importers.centre_entry(dict(CENTRES[0], name="Nowhere")), names))

    def test_a_washroom_row_without_a_park_name_is_skipped(self):
        names = importers.washroom_places(WASHROOMS)
        self.assertEqual(names, {"Trafalgar Park", "Hastings", "Riley Park"})
        self.assertNotIn("", names)


class _WithDatabase(unittest.TestCase):
    """A schema on a throwaway file, plus the helpers every case below wants.
    No tests of its own, so subclassing does not re-run someone else's."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        self.washroom_names = importers.washroom_places(WASHROOMS)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _seed_curated(self, name, **fields):
        """A row as data/venues.json seeds one: curated, ranked, no citation."""
        fields.setdefault("seed_rank", 3)
        fields.setdefault("open_time", "06:00")
        fields.setdefault("close_time", "22:00")
        columns = ", ".join(("name", "source") + tuple(fields))
        placeholders = ", ".join("?" for _ in range(len(fields) + 2))
        with closing(db.connect()) as conn, conn:
            return conn.execute(
                f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
                (name, "curated", *fields.values())).lastrowid

    def _count(self):
        with closing(db.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0]

    def _row(self, venue_id):
        with closing(db.connect()) as conn:
            return conn.execute("SELECT * FROM venues WHERE id = ?",
                                (venue_id,)).fetchone()

    def _import_all(self):
        for record in PARKS:
            importers.store(importers.park_entry(record), self.washroom_names)
        for record in CENTRES:
            importers.store(importers.centre_entry(record), self.washroom_names)

    def _store_park(self, park_name):
        """Import one park. Returns (venue_id, what the washroom resolved to)."""
        entry = importers.park_entry(_park(park_name))
        _, washroom = importers.store(entry, self.washroom_names)
        with closing(db.connect()) as conn:
            row = conn.execute("SELECT id FROM venues WHERE name = ?",
                               (entry["name"],)).fetchone()
        return row["id"], washroom

    def _flags(self, venue_id):
        return db.reported_flags([venue_id]).get(venue_id, {})


class ImportTest(_WithDatabase):
    def test_a_park_lands_with_its_citation_and_can_be_planned_around(self):
        importers.store(importers.park_entry(_park("Trafalgar Park")),
                        self.washroom_names)
        venue = db.get_candidate_venues("Vancouver")[0]
        self.assertEqual(venue["name"], "Trafalgar Park")
        self.assertEqual(venue["source"], importers.SOURCE)
        self.assertIn(importers.SOURCE, db.VERIFIED_SOURCES)
        self.assertIn("refine.parkid=8", venue["source_url"])
        # No human checked it. Trust comes from the source, not from a stamp.
        self.assertIsNone(venue["verified_at"])

    def test_running_it_twice_changes_nothing(self):
        self._import_all()
        first = self._count()
        self._import_all()
        self.assertEqual(self._count(), first)

    def test_a_seeded_park_is_upgraded_in_place_not_duplicated(self):
        seeded = self._seed_curated("Queen Elizabeth Park", type="park",
                                    city="Vancouver", seed_rank=3)
        before = self._count()
        action, _ = importers.store(
            importers.park_entry(_park("Queen Elizabeth Park")),
            self.washroom_names)
        self.assertEqual(action, importers.UPGRADED)
        self.assertEqual(self._count(), before)
        row = self._row(seeded)
        self.assertEqual(row["external_id"], "vanopendata:parks/167")
        self.assertIn("refine.parkid=167", row["source_url"])
        # The curator's ordering is real signal: without it the two best
        # toddler venues in the city drop to near-last alphabetically.
        self.assertEqual(row["seed_rank"], 3)
        # And source is untouched, so the row does not move between queues as
        # a side effect of an unattended script.
        self.assertEqual(row["source"], "curated")

    def test_a_park_the_curator_names_differently_is_still_matched(self):
        trout = self._seed_curated("Trout Lake (John Hendry Park)", type="park")
        bay = self._seed_curated("English Bay Beach", type="park")
        before = self._count()
        for name in ("John Hendry (Trout Lake) Park", "English Bay Beach Park"):
            action, _ = importers.store(importers.park_entry(_park(name)),
                                        self.washroom_names)
            self.assertEqual(action, importers.UPGRADED)
        self.assertEqual(self._count(), before)
        self.assertEqual(self._row(trout)["external_id"], "vanopendata:parks/85")
        self.assertEqual(self._row(bay)["external_id"], "vanopendata:parks/201")

    def test_an_import_never_overwrites_hours_a_person_corrected(self):
        # set_venue_default_hours is the only path by which an approved venue's
        # hours change, and an unattended script must not undo it.
        seeded = self._seed_curated("Queen Elizabeth Park", type="park")
        db.set_venue_default_hours(seeded, "07:30", "19:45")
        importers.store(importers.park_entry(_park("Queen Elizabeth Park")),
                        self.washroom_names)
        row = self._row(seeded)
        self.assertEqual((row["open_time"], row["close_time"]), ("07:30", "19:45"))

    def test_an_import_fills_a_blank_a_seed_left(self):
        seeded = self._seed_curated("Queen Elizabeth Park", type="park",
                                    lat=None, lng=None)
        importers.store(importers.park_entry(_park("Queen Elizabeth Park")),
                        self.washroom_names)
        row = self._row(seeded)
        self.assertEqual((row["lat"], row["lng"]), (49.240978, -123.112028))

    def test_the_dry_runs_answer_matches_what_the_write_does(self):
        # What makes "would this duplicate the seeded parks?" answerable before
        # touching the database, so the printed counts are the real counts.
        self._seed_curated("Queen Elizabeth Park", type="park")
        entries = [importers.park_entry(r) for r in PARKS]
        with closing(db.connect()) as conn:
            existing = conn.execute(
                "SELECT id, name, source, external_id FROM venues").fetchall()
        predicted = [importers.classify(e, existing) for e in entries]
        actual = [importers.store(e, self.washroom_names)[0] for e in entries]
        self.assertEqual(predicted, actual)
        self.assertEqual(predicted.count(importers.UPGRADED), 1)

    def test_the_dry_run_sees_its_own_earlier_run(self):
        entry = importers.park_entry(_park("Trafalgar Park"))
        importers.store(entry, self.washroom_names)
        with closing(db.connect()) as conn:
            existing = conn.execute(
                "SELECT id, name, source, external_id FROM venues").fetchall()
        self.assertEqual(importers.classify(entry, existing),
                         importers.UNCHANGED)


class WashroomReportTest(_WithDatabase):
    def test_the_citys_yes_becomes_a_report_by_nobody(self):
        venue_id, _ = self._store_park("Trafalgar Park")
        self.assertIs(self._flags(venue_id)["has_washroom"], True)
        with closing(db.connect()) as conn:
            rows = conn.execute(
                "SELECT reported_by FROM venue_reports WHERE venue_id = ?",
                (venue_id,)).fetchall()
        # No author, so a real parent's report outranks it whatever its age.
        self.assertTrue(all(r["reported_by"] is None for r in rows))

    def test_the_citys_no_is_a_report_too_and_differs_from_silence(self):
        # "The City says there is no washroom" and "nobody has said" were the
        # same value before venue_reports existed. This is the difference.
        no_washroom, _ = self._store_park("Arbutus Village Park")
        self.assertIs(self._flags(no_washroom)["has_washroom"], False)

        silent = self._seed_curated("Somewhere Unreported", type="park")
        self.assertNotIn("has_washroom", self._flags(silent))

    def test_the_more_specific_record_wins_when_the_city_contradicts_itself(self):
        # Riley Park is flagged washrooms "N" in the parks dataset while the
        # public-washrooms dataset lists a fieldhouse there. 9 parks are like
        # this. Insert order is the resolution: the point-level row goes last.
        venue_id, washroom = self._store_park("Riley Park")
        self.assertIs(washroom, True)
        self.assertIs(self._flags(venue_id)["has_washroom"], True)

    def test_a_community_centre_gets_its_washroom_from_the_same_join(self):
        # The dataset names 134 washroom rows after a park or a centre, so the
        # name join answers this for centres as well, with no extra mechanism.
        entry = importers.centre_entry(CENTRES[0])
        importers.store(entry, self.washroom_names)
        with closing(db.connect()) as conn:
            venue_id = conn.execute(
                "SELECT id FROM venues WHERE name = ?",
                ("Hastings Community Centre",)).fetchone()["id"]
        self.assertIs(self._flags(venue_id)["has_washroom"], True)


class MissingHoursTest(_WithDatabase):
    def test_a_centre_without_hours_is_stored_and_kept_out_of_plans(self):
        importers.store(importers.centre_entry(CENTRES[0]), self.washroom_names)
        self.assertEqual(self._count(), 1)
        self.assertEqual(db.get_candidate_venues("Vancouver"), [])
        missing = db.get_venues_missing_hours()
        self.assertEqual([row["name"] for row in missing],
                         ["Hastings Community Centre"])

    def test_setting_hours_is_what_lets_it_be_planned_around(self):
        importers.store(importers.centre_entry(CENTRES[0]), self.washroom_names)
        venue_id = db.get_venues_missing_hours()[0]["id"]
        db.set_venue_default_hours(venue_id, "09:00", "21:00")
        self.assertEqual(db.get_venues_missing_hours(), [])
        self.assertEqual([v["name"] for v in db.get_candidate_venues("Vancouver")],
                         ["Hastings Community Centre"])

    def test_a_park_never_lands_in_the_queue(self):
        importers.store(importers.park_entry(_park("Trafalgar Park")),
                        self.washroom_names)
        self.assertEqual(db.get_venues_missing_hours(), [])

    def test_imported_venues_stay_out_of_the_confirm_backlog(self):
        # 245 of them, and confirming "the City lists this park" is review as
        # theatre. The backlog is for what only a person can vouch for.
        self._seed_curated("Science World", type="museum", city="Vancouver")
        self._import_all()
        self.assertEqual([row["name"] for row in db.get_unverified_venues()],
                         ["Science World"])


class SetHoursRouteTest(_WithDatabase):
    def setUp(self):
        super().setUp()
        self.client = app_module.app.test_client()
        importers.store(importers.centre_entry(CENTRES[0]), self.washroom_names)
        self.venue_id = db.get_venues_missing_hours()[0]["id"]

    def _as(self, is_admin=True):
        return mock.patch.object(guards, "current_parent",
            return_value={"id": 1, "is_admin": is_admin,
                          "name": "A", "email": "a@b.com"})

    def test_an_admin_can_finish_the_row(self):
        with self._as():
            response = self.client.post(
                f"/venues/{self.venue_id}/hours",
                data={"open_time": "09:00", "close_time": "21:00"},
                follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.get_venues_missing_hours(), [])

    def test_half_an_answer_writes_nothing(self):
        with self._as():
            self.client.post(f"/venues/{self.venue_id}/hours",
                             data={"open_time": "09:00", "close_time": ""},
                             follow_redirects=True)
        self.assertEqual(len(db.get_venues_missing_hours()), 1)

    def test_a_time_that_would_break_the_planner_is_refused(self):
        # itinerary.venue_hours parses these with int() and no fallback, so one
        # bad value stored on a venue makes every plan in that city raise. The
        # <input type="time"> stops it in a browser; a hand-made POST does not.
        for bad in ("javascript:x", "25:00", "09:70", "9am", "09-00", ""):
            with self._as():
                self.client.post(f"/venues/{self.venue_id}/hours",
                                 data={"open_time": bad, "close_time": "21:00"},
                                 follow_redirects=True)
            self.assertEqual(len(db.get_venues_missing_hours()), 1, bad)

    def test_a_non_admin_cannot_set_hours(self):
        with self._as(is_admin=False):
            self.client.post(f"/venues/{self.venue_id}/hours",
                             data={"open_time": "09:00", "close_time": "21:00"},
                             follow_redirects=True)
        self.assertEqual(len(db.get_venues_missing_hours()), 1)


class GuardTest(_WithDatabase):
    def test_an_unknown_import_field_raises_rather_than_being_dropped(self):
        with self.assertRaises(ValueError):
            db.upsert_imported_venue("x:1", "Somewhere", source=importers.SOURCE,
                                     source_url="https://example.com",
                                     seed_rank=0)

    def test_the_import_source_is_one_the_planner_trusts(self):
        self.assertIn(importers.SOURCE, db.VERIFIED_SOURCES)

    def test_the_client_refuses_to_page_forever(self):
        # A guard, not a limit: the largest dataset here is 218 rows.
        full = [{"n": i} for i in range(opendata.PAGE_SIZE)]
        response = mock.Mock(status_code=200)
        response.json.return_value = {"results": full}
        with mock.patch.object(opendata.session, "get", return_value=response):
            with self.assertRaises(RuntimeError):
                opendata.records("parks")
