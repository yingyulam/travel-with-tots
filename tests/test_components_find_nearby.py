import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

import requests

from src import db
from src.components.find_nearby import find_nearby
from src.components.geocode import GeocodeError, geocode, reverse_geocode


def _insert_venue(conn, name, *, city="Vancouver", neighbourhood="Downtown",
                   venue_type="park", source="curated",
                   lat=None, lng=None, **flags):
    """A venue, with any amenities recorded as reports rather than columns.

    Amenities are not columns any more: they live in venue_reports, so a claim
    carries an author and a date and an unexamined field reads as absent rather
    than as "no". `can_eat` is still a column -- it follows the kind of place
    and nobody reports it.
    """
    can_eat = int(bool(flags.pop("can_eat", 0)))
    cur = conn.execute(
        "INSERT INTO venues (name, city, neighbourhood, type, source, "
        "can_eat, lat, lng) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (name, city, neighbourhood, venue_type, source, can_eat, lat, lng))
    venue_id = cur.lastrowid
    for field, value in flags.items():
        # nap_friendly is derived from type, and kid_friendly is an admission
        # rule; neither was ever a thing to set here.
        if field in db.REPORTABLE_FIELDS:
            conn.execute(
                "INSERT INTO venue_reports (venue_id, field, value, reported_by) "
                "VALUES (?, ?, ?, NULL)", (venue_id, field, int(bool(value))))
    return venue_id


class _FakeResponse:
    """Stands in for one third-party HTTP response only -- everything below
    it (geocode's own parsing, find_nearby, interactions.find_nearby, the
    real SQLite query) runs for real."""
    status_code = 200

    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


class FindNearbyCuratedTest(unittest.TestCase):
    """Exercises the real curated path end to end: a real SQLite database and
    the app's real interactions.find_nearby() matching, no stand-ins."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            db.create_schema(conn)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def test_curated_hit_reports_curated_source(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Nursing Spot", has_nursing_room=1)
        result = find_nearby(need="nursing_room", city="Vancouver")
        self.assertEqual(result["source"], "curated")
        self.assertEqual([p["name"] for p in result["places"]], ["Nursing Spot"])

    def test_city_filter_excludes_other_cities(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Vancouver Spot", has_nursing_room=1)
            _insert_venue(conn, "Toronto Spot", city="Toronto", has_nursing_room=1)
        result = find_nearby(need="nursing_room", city="Vancouver")
        self.assertEqual([p["name"] for p in result["places"]], ["Vancouver Spot"])

    def test_need_predicate_is_respected(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Has Nursing", has_nursing_room=1)
            _insert_venue(conn, "No Nursing", has_nursing_room=0)
        result = find_nearby(need="nursing_room", city="Vancouver")
        self.assertEqual([p["name"] for p in result["places"]], ["Has Nursing"])

    def test_a_restaurant_need_is_answered_from_the_table(self):
        # This used to assert the opposite. The table still holds attractions
        # rather than restaurants, but `can_eat` says which of them serve food,
        # and a mall food court is a real answer to a hungry toddler. What the
        # table cannot do -- enumerate the restaurants of a city -- goes to
        # Google Maps rather than to a web search that returns pages, not
        # places. See tests/test_lunch_nearby.py.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "A Mall", can_eat=1)
            _insert_venue(conn, "A Park")
        with mock.patch("src.components.find_nearby.search_web") as searched:
            result = find_nearby(need="restaurant", city="Vancouver")
        self.assertEqual(result["source"], "curated")
        self.assertEqual([p["name"] for p in result["places"]], ["A Mall"])
        searched.assert_not_called()

    def test_a_need_the_table_cannot_answer_escalates(self):
        # "other" is free text, so the table has no filter for it: returning an
        # arbitrary nearby venue is worse than admitting it does not know, and
        # the web is the only place left to look. "restaurant" used to be here
        # too and is now answered above.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "A Park")
            _insert_venue(conn, "A Mall", can_eat=1)
        # search_web is mocked: escalating is the point of the test, and a real
        # call would make the suite hit the network.
        with mock.patch("src.components.find_nearby.search_web",
                        return_value=[]) as searched:
            result = find_nearby(need="other", city="Vancouver")
        self.assertNotEqual(result["source"], "curated")
        searched.assert_called_once()

    def test_matching_neighbourhood_wins_when_no_coordinates(self):
        # The fallback path: no venue has coordinates, so neighbourhood is the
        # only proximity signal available. Still load-bearing, since only some
        # venues resolve from open data and user-submitted rows never will.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Far A", neighbourhood="Far", has_nursing_room=1)
            _insert_venue(conn, "Far B", neighbourhood="Far", has_nursing_room=1)
            _insert_venue(conn, "Close One", neighbourhood="Kitsilano", has_nursing_room=1)
        result = find_nearby(need="nursing_room", city="Vancouver",
                             neighbourhood="Kitsilano", limit=1)
        self.assertEqual([p["name"] for p in result["places"]], ["Close One"])

    def test_real_distance_beats_neighbourhood_when_coordinates_exist(self):
        # "Wrong Hood" is physically closest but in a different neighbourhood,
        # so it only wins if real distance is being used, not the name proxy.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Wrong Hood", neighbourhood="Elsewhere",
                          has_nursing_room=1, lat=49.2755, lng=-123.1535)
            _insert_venue(conn, "Right Hood Far", neighbourhood="Kitsilano",
                          has_nursing_room=1, lat=49.2100, lng=-123.1160)
        result = find_nearby(need="nursing_room", city="Vancouver",
                             neighbourhood="Kitsilano", limit=1,
                             lat=49.2753, lng=-123.1532)
        self.assertEqual([p["name"] for p in result["places"]], ["Wrong Hood"])

    def test_distance_km_reported_only_when_computable(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Has Coords", has_nursing_room=1,
                          lat=49.2800, lng=-123.1200)
            _insert_venue(conn, "No Coords", has_nursing_room=1)
        by_name = {p["name"]: p for p in find_nearby(
            need="nursing_room", city="Vancouver", limit=5,
            lat=49.2753, lng=-123.1532)["places"]}
        self.assertIsInstance(by_name["Has Coords"]["distance_km"], float)
        self.assertIsNone(by_name["No Coords"]["distance_km"])

    def test_venues_with_coordinates_rank_before_those_without(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "No Coords", has_nursing_room=1)
            _insert_venue(conn, "Has Coords", has_nursing_room=1,
                          lat=49.2800, lng=-123.1200)
        result = find_nearby(need="nursing_room", city="Vancouver", limit=1,
                             lat=49.2753, lng=-123.1532)
        self.assertEqual([p["name"] for p in result["places"]], ["Has Coords"])

    def test_open_data_source_is_visible_but_user_submitted_is_not(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "From Open Data", has_nursing_room=1,
                          source="municipal_open_data")
            _insert_venue(conn, "From A Parent", has_nursing_room=1,
                          source="user_submitted")
        names = {p["name"] for p in find_nearby(
            need="nursing_room", city="Vancouver", limit=5)["places"]}
        self.assertEqual(names, {"From Open Data"})

    def test_curated_places_carry_a_maps_url(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Nursing Spot", has_nursing_room=1)
        place = find_nearby(need="nursing_room", city="Vancouver")["places"][0]
        self.assertIn("google.com/maps", place["maps_url"])
        self.assertIn("Nursing+Spot", place["maps_url"])

    def test_empty_curated_falls_through_to_search(self):
        # No venues inserted at all, so curated has nothing to offer.
        with mock.patch("src.components.find_nearby.search_web", return_value=[
                {"title": "A web result", "url": "https://example.com", "snippet": "s"}]):
            result = find_nearby(need="nursing_room", city="Vancouver")
        self.assertEqual(result["source"], "search")
        self.assertEqual(result["places"][0]["name"], "A web result")
        self.assertEqual(result["places"][0]["maps_url"], "https://example.com")

    def test_both_empty_is_not_an_error(self):
        with mock.patch("src.components.find_nearby.search_web", return_value=[]):
            result = find_nearby(need="nursing_room", city="Vancouver")
        self.assertEqual(result["source"], "none")
        self.assertEqual(result["places"], [])

    def test_failing_search_degrades_instead_of_raising(self):
        with mock.patch("src.components.find_nearby.search_web",
                        side_effect=KeyError("TAVILY_API_KEY")):
            result = find_nearby(need="nursing_room", city="Vancouver")
        self.assertEqual(result["source"], "none")
        self.assertEqual(result["places"], [])

    def test_no_city_and_no_coordinates_skips_curated_and_searches(self):
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Nursing Spot", has_nursing_room=1)
        with mock.patch("src.components.find_nearby.search_web", return_value=[]) as searched:
            result = find_nearby(need="nursing_room", city="")
        self.assertEqual(result["places"], [])
        searched.assert_called_once()

    def test_coordinates_alone_search_curated_without_a_city(self):
        # No city means no geocoder was available, but shared coordinates are
        # enough on their own -- this is what lets the feature work with no
        # Google Maps key configured at all.
        with closing(db.connect()) as conn, conn:
            _insert_venue(conn, "Near", has_nursing_room=1,
                          lat=49.2755, lng=-123.1535)
            _insert_venue(conn, "Far", has_nursing_room=1,
                          lat=49.2100, lng=-123.1160)
        result = find_nearby(need="nursing_room", city="", limit=1,
                            lat=49.2753, lng=-123.1532)
        self.assertEqual(result["source"], "curated")
        self.assertEqual([p["name"] for p in result["places"]], ["Near"])


class GeocodeTest(unittest.TestCase):
    """Only Google's HTTP response is faked; geocode.py's own parsing runs."""

    OK_BODY = {
        "status": "OK",
        "results": [{
            "address_components": [
                {"long_name": "Kitsilano", "types": ["neighborhood", "political"]},
                {"long_name": "Vancouver", "types": ["locality", "political"]},
                {"long_name": "British Columbia", "types": ["administrative_area_level_1"]},
            ],
            "formatted_address": "Kitsilano, Vancouver, BC, Canada",
            "geometry": {"location": {"lat": 49.2688, "lng": -123.1685}},
        }],
    }

    def setUp(self):
        self.key_patcher = mock.patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": "test-key"})
        self.key_patcher.start()

    def tearDown(self):
        self.key_patcher.stop()

    def test_reverse_geocode_parses_city_and_neighbourhood(self):
        with mock.patch("src.components.geocode.requests.get",
                        return_value=_FakeResponse(self.OK_BODY)):
            place = reverse_geocode(49.2688, -123.1685)
        self.assertEqual(place["city"], "Vancouver")
        self.assertEqual(place["neighbourhood"], "Kitsilano")
        self.assertEqual(place["formatted_address"], "Kitsilano, Vancouver, BC, Canada")
        self.assertEqual(place["lat"], 49.2688)

    def test_geocode_by_address_uses_the_address_param(self):
        with mock.patch("src.components.geocode.requests.get",
                        return_value=_FakeResponse(self.OK_BODY)) as got:
            geocode("Kitsilano, Vancouver")
        self.assertEqual(got.call_args.kwargs["params"]["address"], "Kitsilano, Vancouver")

    def test_missing_neighbourhood_is_empty_not_an_error(self):
        body = {"status": "OK", "results": [{
            "address_components": [{"long_name": "Vancouver", "types": ["locality"]}],
            "formatted_address": "Vancouver, BC", "geometry": {"location": {}}}]}
        with mock.patch("src.components.geocode.requests.get",
                        return_value=_FakeResponse(body)):
            place = reverse_geocode(49.0, -123.0)
        self.assertEqual(place["city"], "Vancouver")
        self.assertEqual(place["neighbourhood"], "")

    def test_non_ok_google_status_raises(self):
        # Google answers HTTP 200 for these, so the body's status is the check.
        for status in ("ZERO_RESULTS", "REQUEST_DENIED", "OVER_QUERY_LIMIT"):
            with mock.patch("src.components.geocode.requests.get",
                            return_value=_FakeResponse({"status": status, "results": []})):
                with self.assertRaises(GeocodeError):
                    reverse_geocode(0, 0)

    def test_missing_key_raises_key_error(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(KeyError):
                reverse_geocode(49.0, -123.0)

    def test_network_failure_never_leaks_the_api_key(self):
        # Google needs the key as a query param, and a requests network error
        # embeds the whole request URL in its message -- so the raised error
        # must carry neither the key nor the original exception chain.
        leaky = requests.exceptions.ConnectionError(
            "Max retries exceeded with url: /maps/api/geocode/json?key=test-key")
        with mock.patch("src.components.geocode.requests.get", side_effect=leaky):
            with self.assertRaises(GeocodeError) as caught:
                reverse_geocode(49.0, -123.0)
        self.assertNotIn("test-key", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


if __name__ == "__main__":
    unittest.main()
