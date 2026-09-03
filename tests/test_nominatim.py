"""The keyless geocoder that replaced Google Places on the proposal path.

Why it exists: Places terms allow keeping a place *id* but restrict retaining
returned content, and everything `_locate` writes lands in
`data/venue_candidates.csv`, which is tracked in git -- so a public repo was
redistributing Google's addresses and coordinates. Nominatim is ODbL.

The trade is precision, so two guards do the work a paid API would: a bare-name
hit is accepted only when the result is in British Columbia, and the caller
checks Metro Vancouver's bounds.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import unittest
from unittest import mock

import requests

from src import nominatim
from src.geo import in_metro_vancouver


def _hit(**over):
    base = {"lat": "49.2721", "lon": "-123.1044",
            "display_name": "Science World, Vancouver, BC, Canada",
            "osm_type": "node", "osm_id": 8594006462,
            "address": {"state": "British Columbia", "suburb": "Olympic Village"}}
    base.update(over)
    return base


class LocateTest(unittest.TestCase):
    def _locate(self, hits, name="Science World", area=None):
        """`hits` is a list of per-request JSON bodies, in call order."""
        responses = []
        for body in hits:
            r = mock.Mock(status_code=200)
            r.json.return_value = body
            r.raise_for_status.return_value = None
            responses.append(r)
        with mock.patch.object(nominatim.session, "get", side_effect=responses) as got, \
             mock.patch.object(nominatim.time, "sleep") as slept:
            found = nominatim.locate(name, area)
        return found, got, slept

    def test_a_hit_becomes_a_location(self):
        found, _, _ = self._locate([[_hit()]])
        self.assertEqual((found["lat"], found["lng"]), (49.2721, -123.1044))
        self.assertIn("Science World", found["address"])
        self.assertEqual(found["area"], "Olympic Village")

    def test_the_osm_id_is_carried_as_a_stable_identity(self):
        # Places gave us nothing storable. An OSM id is openly licensed and
        # durable, so a re-proposal of the same venue is recognisable.
        found, _, _ = self._locate([[_hit()]])
        self.assertEqual(found["external_id"], "osm:node/8594006462")

    def test_a_hit_without_an_osm_id_still_locates(self):
        found, _, _ = self._locate([[_hit(osm_type=None, osm_id=None)]])
        self.assertEqual(found["external_id"], "")
        self.assertIsNotNone(found["lat"])

    def test_nothing_found_is_none_not_a_guess(self):
        found, _, _ = self._locate([[]])
        self.assertIsNone(found)

    def test_an_area_hint_is_tried_first(self):
        found, got, _ = self._locate([[_hit()]], area="False Creek")
        self.assertEqual(got.call_count, 1)
        self.assertIn("False Creek", got.call_args.kwargs["params"]["q"])
        self.assertIsNotNone(found)

    def test_the_bare_name_is_tried_when_the_area_hint_finds_nothing(self):
        found, got, _ = self._locate([[], [_hit()]], area="False Creek")
        self.assertEqual(got.call_count, 2)
        self.assertNotIn("False Creek", got.call_args.kwargs["params"]["q"])
        self.assertIsNotNone(found)

    def test_a_bare_name_hit_outside_bc_is_refused(self):
        # Otherwise a bare name silently resolves to a same-named place in
        # another province or country.
        found, _, _ = self._locate(
            [[_hit(address={"state": "Washington"})]])
        self.assertIsNone(found)

    def test_an_area_qualified_hit_is_not_held_to_the_province_check(self):
        # It was already pinned to Vancouver by the query itself, and
        # Nominatim does not always return a state.
        found, _, _ = self._locate([[_hit(address={"suburb": "Yaletown"})]],
                                   area="Yaletown")
        self.assertIsNotNone(found)
        self.assertEqual(found["area"], "Yaletown")

    def test_a_parenthetical_alias_is_dropped_from_the_query(self):
        _, got, _ = self._locate([[_hit()]], name="Trout Lake (John Hendry Park)")
        self.assertNotIn("John Hendry", got.call_args.kwargs["params"]["q"])
        self.assertIn("Trout Lake", got.call_args.kwargs["params"]["q"])

    def test_a_blank_name_makes_no_request(self):
        with mock.patch.object(nominatim.session, "get") as got:
            self.assertIsNone(nominatim.locate("   "))
        got.assert_not_called()


class UsagePolicyTest(unittest.TestCase):
    """Nominatim is donation-funded and asks for one request per second."""

    def test_the_delay_honours_the_policy(self):
        self.assertGreaterEqual(nominatim.DELAY_SECONDS, 1.0)

    def test_it_sleeps_after_every_request(self):
        r = mock.Mock(status_code=200)
        r.json.return_value = []
        r.raise_for_status.return_value = None
        with mock.patch.object(nominatim.session, "get", return_value=r), \
             mock.patch.object(nominatim.time, "sleep") as slept:
            nominatim.locate("Somewhere", area="Downtown")
        # Two queries were tried, so two waits.
        self.assertEqual(slept.call_count, 2)
        for call in slept.call_args_list:
            self.assertEqual(call.args[0], nominatim.DELAY_SECONDS)

    def test_it_sleeps_even_when_the_request_failed(self):
        # A request that errored still reached the service.
        with mock.patch.object(nominatim.session, "get",
                               side_effect=requests.exceptions.Timeout()), \
             mock.patch.object(nominatim.time, "sleep") as slept:
            with self.assertRaises(nominatim.NominatimError):
                nominatim.locate("Somewhere")
        self.assertEqual(slept.call_count, 1)

    def test_it_identifies_itself(self):
        agent = nominatim.session.headers["User-Agent"]
        self.assertIn("travel-with-tots", agent)

    def test_it_asks_for_one_result(self):
        self.assertEqual(nominatim.RESULT_LIMIT, 1)


class FailureTest(unittest.TestCase):
    def test_a_request_failure_raises_without_leaking_the_exception(self):
        with mock.patch.object(nominatim.session, "get",
                               side_effect=requests.exceptions.ConnectionError("secret")), \
             mock.patch.object(nominatim.time, "sleep"):
            with self.assertRaises(nominatim.NominatimError) as caught:
                nominatim.locate("Somewhere")
        self.assertNotIn("secret", str(caught.exception))
        self.assertIn("ConnectionError", str(caught.exception))

    def test_an_unreadable_body_raises(self):
        r = mock.Mock(status_code=200)
        r.json.side_effect = ValueError("not json")
        r.raise_for_status.return_value = None
        with mock.patch.object(nominatim.session, "get", return_value=r), \
             mock.patch.object(nominatim.time, "sleep"):
            with self.assertRaises(nominatim.NominatimError):
                nominatim.locate("Somewhere")


class BoundsTest(unittest.TestCase):
    """The guard that caught Fort Vancouver at latitude 45.6, now shared."""

    def test_vancouver_washington_is_out(self):
        self.assertFalse(in_metro_vancouver(45.6261838, -122.6566053))

    def test_vancouver_bc_is_in(self):
        self.assertTrue(in_metro_vancouver(49.2721, -123.1044))

    def test_north_shore_and_richmond_are_in(self):
        self.assertTrue(in_metro_vancouver(49.3862, -123.0761))   # Grouse
        self.assertTrue(in_metro_vancouver(49.1666, -123.1336))   # Richmond

    def test_a_missing_coordinate_is_not_treated_as_here(self):
        self.assertFalse(in_metro_vancouver(None, None))
        self.assertFalse(in_metro_vancouver(49.27, None))

    def test_the_overpass_box_is_deliberately_separate(self):
        # osm.BBOX bounds a query rather than validating a result, and is
        # tighter on purpose: widening it would make every Overpass call scan
        # more of the map for no benefit.
        from src import geo, osm
        self.assertNotEqual(tuple(geo.METRO_VANCOUVER_BOUNDS), tuple(osm.BBOX))


class NoPlacesOnTheProposalPathTest(unittest.TestCase):
    def test_the_proposer_does_not_import_the_google_client(self):
        from src.workflows import propose_venues
        self.assertFalse(hasattr(propose_venues, "search_places"))
        self.assertFalse(hasattr(propose_venues, "PlaceSearchError"))

    def test_the_google_client_still_exists_for_the_parent_facing_lookups(self):
        # /place-search, /log-place/search, Log a Place and find-nearby all use
        # it; only the proposal path moved off.
        from src.components import place_search
        self.assertTrue(callable(place_search.search_places))


if __name__ == "__main__":
    unittest.main()
