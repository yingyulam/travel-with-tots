"""The proposal loop. Two invariants matter more than anything else here:

1. It never writes a venue. Only a person approving one does.
2. A rejected place is never proposed again, so the loop converges instead of
   spending a reviewer's capacity on the same rejections.
"""

import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from src import candidates, db, nominatim, osm
from src.workflows import propose_venues

RESULTS = [
    {"title": "Rainy day Vancouver with kids", "url": "https://example.org/rain",
     "snippet": "The Beaty Biodiversity Museum has a blue whale skeleton that "
                "toddlers love, and it is indoors."},
    {"title": "Cafes for families", "url": "https://example.org/cafes",
     "snippet": "Kokomo Foods in Kitsilano has room for strollers."},
]


def _reply(*venues):
    import json
    return json.dumps({"venues": [
        {"name": None, "type": None,
         "neighbourhood": None, "evidence": None, **v} for v in venues]})


class ProposeVenuesTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for target, attr, value in (
                (db, "DB_PATH", os.path.join(self._tmp.name, "app.db")),
                (candidates, "CANDIDATES_PATH",
                 Path(self._tmp.name) / "venue_candidates.csv")):
            patcher = mock.patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            db.create_schema(conn)
        # Enrichment reaches two networks. Stubbed for the whole class rather
        # than per test, so a new test cannot accidentally call Overpass for
        # real: it does not fail, it just takes a minute and hammers a shared
        # public endpoint. Individual tests set .return_value to say what OSM
        # knows.
        self.osm_facts = self._patch(osm, "venue_facts", {})
        self.official = self._patch(propose_venues, "official_site", "")

    def _patch(self, target, attr, value):
        patcher = mock.patch.object(target, attr, return_value=value)
        mocked = patcher.start()
        self.addCleanup(patcher.stop)
        return mocked

    def _run(self, reply, batch_size=5, located=None):
        # `located` is a nominatim.locate return value, or None for "found
        # nothing" -- the proposal path is keyless now, so there is no Places
        # call left to patch.
        with mock.patch.object(propose_venues, "search_web", return_value=RESULTS), \
             mock.patch.object(propose_venues, "call_openrouter",
                               return_value=(reply, {}, 0.4)), \
             mock.patch.object(nominatim, "locate", return_value=located):
            return propose_venues.propose(batch_size=batch_size)

    def _venue_count(self):
        with closing(db.connect()) as conn:
            return conn.execute("SELECT COUNT(*) FROM venues").fetchone()[0]

    def test_it_never_writes_to_the_venues_table(self):
        before = self._venue_count()
        self._run(_reply({"name": "Beaty Biodiversity Museum"}))
        self.assertEqual(self._venue_count(), before)
        self.assertTrue(candidates.load())

    def test_a_proposal_the_search_did_not_mention_is_dropped(self):
        # Invention is the failure that makes a reviewer stop trusting a batch.
        result = self._run(_reply({"name": "Totally Fabricated Play Barn"}))
        self.assertEqual(result["proposed"], 0)
        self.assertGreater(result["skipped"], 0)
        self.assertEqual(candidates.load(), [])

    def test_a_proposal_the_search_named_is_kept(self):
        result = self._run(_reply({"name": "Beaty Biodiversity Museum",
                                   "category": "activity", "type": "museum"}))
        self.assertEqual(result["proposed"], 1)
        self.assertEqual(candidates.load()[0]["name"], "Beaty Biodiversity Museum")

    def test_a_listicle_title_is_not_a_venue(self):
        result = self._run(_reply({"name": "Best rainy day things to do in Vancouver"}))
        self.assertEqual(result["proposed"], 0)

    def test_a_neighbourhood_the_search_never_mentioned_is_dropped(self):
        # Otherwise it is a guess from the name, presented as a finding.
        self._run(_reply({"name": "Beaty Biodiversity Museum", "neighbourhood": "Yaletown"}))
        self.assertEqual(candidates.load()[0]["neighbourhood"], "")

    def test_a_neighbourhood_the_search_mentioned_is_kept(self):
        self._run(_reply({"name": "Kokomo Foods", "neighbourhood": "Kitsilano"}))
        self.assertEqual(candidates.load()[0]["neighbourhood"], "Kitsilano")

    def test_every_candidate_carries_a_url(self):
        self._run(_reply({"name": "Beaty Biodiversity Museum"}))
        self.assertTrue(candidates.load()[0]["source_url"].startswith("https://"))

    def test_a_venue_already_in_the_database_is_not_proposed(self):
        db.add_venue("Beaty Biodiversity Museum", source="curated", city="Vancouver")
        result = self._run(_reply({"name": "Beaty Biodiversity Museum",
                                   "category": "activity"}))
        self.assertEqual(result["proposed"], 0)

    def test_a_rejected_venue_is_not_proposed_again(self):
        reply = _reply({"name": "Beaty Biodiversity Museum"})
        self.assertEqual(self._run(reply)["proposed"], 1)
        candidates.set_status(candidates.load()[0]["id"], candidates.REJECTED,
                              decided_by=1)
        self.assertEqual(self._run(reply)["proposed"], 0)

    def test_the_batch_size_is_a_ceiling(self):
        reply = _reply(
            {"name": "Beaty Biodiversity Museum"},
            {"name": "Kokomo Foods"})
        self.assertEqual(self._run(reply, batch_size=1)["proposed"], 1)

    def test_a_location_lookup_failure_costs_coordinates_not_the_candidate(self):
        with mock.patch.object(propose_venues, "search_web", return_value=RESULTS), \
             mock.patch.object(propose_venues, "call_openrouter",
                               return_value=(_reply({"name": "Beaty Biodiversity Museum"}), {}, 0.4)), \
             mock.patch.object(nominatim, "locate",
                               side_effect=nominatim.NominatimError("down")):
            result = propose_venues.propose(batch_size=1)
        self.assertEqual(result["proposed"], 1)
        self.assertEqual(candidates.load()[0]["lat"], "")

    def test_coordinates_are_attached_when_the_lookup_works(self):
        self._run(_reply({"name": "Beaty Biodiversity Museum"}),
                  located={"lat": 49.2646, "lng": -123.25,
                           "address": "2212 Main Mall, Vancouver, BC",
                           "area": "Point Grey",
                           "external_id": "osm:node/123"})
        row = candidates.load()[0]
        self.assertEqual(row["address"], "2212 Main Mall, Vancouver, BC")
        self.assertEqual(float(row["lat"]), 49.2646)
        # Identity Places never gave us, so a re-proposal is recognisable.
        self.assertEqual(row["external_id"], "osm:node/123")

    def test_a_search_failure_raises_rather_than_writing_a_partial_batch(self):
        from src.components.search_web import WebSearchError
        with mock.patch.object(propose_venues, "search_web",
                               side_effect=WebSearchError("429")):
            with self.assertRaises(propose_venues.ProposalError):
                propose_venues.propose(batch_size=5)
        self.assertEqual(candidates.load(), [])

    def test_an_unusable_model_reply_skips_that_query_without_failing(self):
        result = self._run("not json at all")
        self.assertEqual(result["proposed"], 0)

    def test_gap_queries_target_the_indoor_shortage(self):
        # The measure changed with the City import. It used to be geographic --
        # fewest venues wins -- which was right at 38 venues and wrong at 260,
        # because every City area now has somewhere outdoors. What a family
        # cannot find is somewhere under cover.
        for i in range(7):
            db.add_venue(f"Park {i}", source="curated", city="Vancouver",
                         neighbourhood="Downtown", setting="outdoor")
        db.add_venue("A Museum", source="curated", city="Vancouver",
                     neighbourhood="Marpole", setting="indoor")
        queries = " ".join(propose_venues.gap_queries(limit=1))
        # Seven parks and no roof is the gap, even though it is the busiest
        # neighbourhood; Marpole already has shelter.
        self.assertIn("Downtown", queries)
        self.assertNotIn("Marpole", queries)
        self.assertIn("indoor", queries)

    def test_an_area_with_outdoor_venues_outranks_one_with_none(self):
        # Somewhere families already go, with no shelter, is a better search
        # than somewhere with nothing at all.
        db.add_venue("A Park", source="curated", city="Vancouver",
                     neighbourhood="Kitsilano", setting="outdoor")
        first = propose_venues.gap_queries(limit=1)[0]
        self.assertIn("Kitsilano", first)

    def test_an_area_with_no_venues_is_still_reachable(self):
        # The old version iterated only neighbourhoods already in the data, so
        # an area with zero venues never entered the counts and the biggest
        # gaps were the ones it could not see.
        queries = " ".join(propose_venues.gap_queries(limit=40))
        self.assertIn("Killarney", queries)

    def test_a_venue_outside_metro_vancouver_is_dropped(self):
        # A search for "Vancouver" reaches Vancouver, Washington. A live run
        # proposed Fort Vancouver, at latitude 45.6, in another country.
        result = self._run(
            _reply({"name": "Beaty Biodiversity Museum"}),
            batch_size=1,
            located={"lat": 45.6261838, "lng": -122.6566053,
                     "address": "1501 E Evergreen Blvd, Vancouver, WA",
                     "area": "Hudson Bay", "external_id": "osm:node/9"})
        self.assertEqual(result["proposed"], 0)
        self.assertEqual(candidates.load(), [])

    def test_a_name_that_is_only_a_kind_of_place_is_dropped(self):
        # A live run proposed "Library". No reviewer can act on that.
        self.assertEqual(self._run(_reply({"name": "Library"}))["proposed"], 0)

    def test_a_name_identical_to_its_type_is_dropped(self):
        self.assertEqual(self._run(_reply({"name": "Museum", "type": "museum"}))["proposed"], 0)

    def test_a_spelling_variant_of_an_existing_venue_is_not_proposed(self):
        db.add_venue("Beaty Biodiversity Museum", source="curated", city="Vancouver")
        result = self._run(_reply({"name": "Beaty  Biodiversity-Museum"}))
        self.assertEqual(result["proposed"], 0)

    def test_the_city_name_alone_is_not_evidence(self):
        # A live run accepted "Vancouver Public Library" cited to an article
        # about Vancouver, Washington restaurants: a real venue with a citation
        # that says nothing about it. The only shared word was the city.
        results = [{"title": "Best kid-friendly restaurants in Vancouver, Washington",
                    "url": "https://example.org/wa",
                    "snippet": "The family-friendly suburb of Vancouver, "
                               "Washington offers many places for kids to dine."}]
        with mock.patch.object(propose_venues, "search_web", return_value=results), \
             mock.patch.object(propose_venues, "call_openrouter",
                               return_value=(_reply({"name": "Vancouver Public Library",
                                                     "type": "library"}), {}, 0.4)), \
             mock.patch.object(nominatim, "locate", return_value=None):
            result = propose_venues.propose(batch_size=1)
        self.assertEqual(result["proposed"], 0)

    def test_a_name_whose_distinctive_words_appear_is_still_kept(self):
        results = [{"title": "Vancouver library story times", "url": "https://example.org/l",
                    "snippet": "The Vancouver Public Library central branch runs "
                               "free story times for toddlers."}]
        with mock.patch.object(propose_venues, "search_web", return_value=results), \
             mock.patch.object(propose_venues, "call_openrouter",
                               return_value=(_reply({"name": "Vancouver Public Library",
                                                     "type": "library"}), {}, 0.4)), \
             mock.patch.object(nominatim, "locate", return_value=None):
            result = propose_venues.propose(batch_size=1)
        self.assertEqual(result["proposed"], 1)

    def test_it_is_kept_out_of_the_chat_router(self):
        # A parent never asks for this, and offering it to the intent classifier
        # would let a message trigger a batch of API calls.
        from src.workflows import runnable_message_workflows
        names = [w["name"] for w, _run in runnable_message_workflows()]
        self.assertNotIn(propose_venues.WORKFLOW["name"], names)


if __name__ == "__main__":
    unittest.main()
