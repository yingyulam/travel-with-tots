import unittest
from src.web import guards
from unittest import mock

import requests

from src.components.place_search import (
    FIELD_MASK,
    PLACES_SEARCH_URL,
    PlaceSearchError,
    search_places,
)


class _FakeResponse:
    """Stands in for one third-party HTTP response only; the component's own
    parsing and normalising run for real."""

    def __init__(self, body, ok=True):
        self._body = body
        self._ok = ok

    def raise_for_status(self):
        if not self._ok:
            raise requests.exceptions.HTTPError("boom")

    def json(self):
        if self._body is _UNREADABLE:
            raise ValueError("not json")
        return self._body


_UNREADABLE = object()

_ONE_PLACE = {"places": [{
    "displayName": {"text": "Science World"},
    "formattedAddress": "1455 Quebec St, Vancouver, BC",
    "location": {"latitude": 49.2733, "longitude": -123.1038},
    "primaryType": "science_museum",
    "addressComponents": [
        {"longText": "Vancouver", "types": ["locality", "political"]},
        {"longText": "False Creek", "types": ["neighborhood"]},
    ],
}]}


def _run(body, ok=True, **kwargs):
    captured = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, body=json)
        return _FakeResponse(body, ok)

    with mock.patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key"}), \
         mock.patch("src.components.place_search.requests.post", side_effect=fake_post):
        return search_places("science world", **kwargs), captured


class SearchPlacesTest(unittest.TestCase):
    def test_a_result_is_flattened_into_what_the_form_needs(self):
        places, _ = _run(_ONE_PLACE)
        self.assertEqual(places, [{
            "name": "Science World",
            "address": "1455 Quebec St, Vancouver, BC",
            "lat": 49.2733,
            "lng": -123.1038,
            "city": "Vancouver",
            "neighbourhood": "False Creek",
            "type": "science museum",
        }])

    def test_googles_underscores_become_words(self):
        # "science_museum" is Google's vocabulary; a parent reads the field.
        places, _ = _run(_ONE_PLACE)
        self.assertEqual(places[0]["type"], "science museum")

    def test_no_match_is_an_answer_not_a_failure(self):
        # Google answers 200 with no "places" key when nothing matched.
        places, _ = _run({})
        self.assertEqual(places, [])

    def test_a_missing_neighbourhood_falls_back_to_sublocality(self):
        body = {"places": [{
            "displayName": {"text": "Somewhere"},
            "addressComponents": [{"longText": "Kits", "types": ["sublocality"]}],
        }]}
        places, _ = _run(body)
        self.assertEqual(places[0]["neighbourhood"], "Kits")

    def test_a_sparse_result_does_not_crash(self):
        places, _ = _run({"places": [{}]})
        self.assertEqual(places[0]["name"], "")
        self.assertIsNone(places[0]["lat"])


class RequestShapeTest(unittest.TestCase):
    def test_the_key_travels_in_a_header_not_the_url(self):
        # Unlike the Geocoding API, so a logged URL cannot carry the key.
        _, captured = _run(_ONE_PLACE)
        self.assertEqual(captured["url"], PLACES_SEARCH_URL)
        self.assertNotIn("test-key", captured["url"])
        self.assertEqual(captured["headers"]["X-Goog-Api-Key"], "test-key")

    def test_a_field_mask_is_always_sent(self):
        # Google rejects the request outright without one.
        _, captured = _run(_ONE_PLACE)
        self.assertEqual(captured["headers"]["X-Goog-FieldMask"], FIELD_MASK)

    def test_coordinates_bias_the_search(self):
        _, captured = _run(_ONE_PLACE, lat=49.28, lng=-123.12)
        centre = captured["body"]["locationBias"]["circle"]["center"]
        self.assertAlmostEqual(centre["latitude"], 49.28)

    def test_no_coordinates_means_no_bias(self):
        _, captured = _run(_ONE_PLACE)
        self.assertNotIn("locationBias", captured["body"])


class FailureTest(unittest.TestCase):
    def test_a_missing_key_raises_key_error(self):
        # The route turns this into a "needs a key" message rather than a 500.
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(KeyError):
                search_places("anything")

    def test_an_http_failure_becomes_a_component_error(self):
        with self.assertRaises(PlaceSearchError):
            _run(_ONE_PLACE, ok=False)

    def test_an_unreadable_body_becomes_a_component_error(self):
        with self.assertRaises(PlaceSearchError):
            _run(_UNREADABLE)

    def test_a_network_failure_never_leaks_the_key(self):
        leaky = requests.exceptions.ConnectionError(
            "Max retries exceeded: X-Goog-Api-Key: test-key")
        with mock.patch.dict("os.environ", {"GOOGLE_MAPS_API_KEY": "test-key"}), \
             mock.patch("src.components.place_search.requests.post", side_effect=leaky):
            with self.assertRaises(PlaceSearchError) as caught:
                search_places("anything")
        self.assertNotIn("test-key", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)


class SearchRouteTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.parent = {"id": 1, "is_admin": False, "name": "P", "email": "p@b.com"}

    def _as_parent(self):
        return mock.patch.object(guards, "current_parent",
                                 return_value=self.parent)

    def test_a_query_is_required(self):
        with self._as_parent():
            self.assertEqual(
                self.client.post("/log-place/search", json={}).status_code, 400)

    def test_results_come_back_as_json(self):
        with self._as_parent(), \
             mock.patch.object(self.app_module, "search_places",
                               return_value=[{"name": "Science World"}]):
            resp = self.client.post("/log-place/search", json={"query": "science"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["places"][0]["name"], "Science World")

    def test_a_missing_key_says_so_rather_than_500ing(self):
        with self._as_parent(), \
             mock.patch.object(self.app_module, "search_places",
                               side_effect=KeyError("GOOGLE_MAPS_API_KEY")):
            resp = self.client.post("/log-place/search", json={"query": "x"})
        self.assertEqual(resp.status_code, 503)
        self.assertIn("key", resp.get_json()["error"].lower())

    def test_the_component_has_its_own_page(self):
        # Every other invokable component is testable in isolation; without
        # this you can only reach the search through the log-a-place form, so a
        # wrong address can't be pinned on the search or the form.
        admin = {**self.parent, "is_admin": True}
        with mock.patch.object(guards, "current_parent", return_value=admin):
            resp = self.client.get("/place-search")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Place Search", resp.get_data(as_text=True))

    def test_the_component_page_is_admin_only(self):
        with self._as_parent():
            self.assertEqual(self.client.get("/place-search").status_code, 302)

    def test_the_components_page_links_to_it(self):
        admin = {**self.parent, "is_admin": True}
        with mock.patch.object(guards, "current_parent", return_value=admin):
            html = self.client.get("/components").get_data(as_text=True)
        self.assertIn('href="/place-search"', html)

    def test_the_component_run_route_returns_results(self):
        admin = {**self.parent, "is_admin": True}
        with mock.patch.object(guards, "current_parent", return_value=admin), \
             mock.patch.object(self.app_module, "search_places",
                               return_value=[{"name": "Science World"}]):
            resp = self.client.post("/place-search/run", json={"query": "science"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["places"][0]["name"], "Science World")

    def test_searching_needs_a_login(self):
        with mock.patch.object(guards, "current_parent", return_value=None):
            self.assertEqual(
                self.client.post("/log-place/search", json={"query": "x"}).status_code,
                302)


if __name__ == "__main__":
    unittest.main()
