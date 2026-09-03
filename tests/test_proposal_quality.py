"""What reaches the review page, and what a reviewer can do to it wrongly.

Two failures this file exists to prevent, both seen in a live batch:

1. The proposer generated `type: "activity"` four times, plus `cafe` and
   `restaurant` for venues the prompt tells it to skip. The review form
   rendered the unrecognised value as the *selected* option, and approving
   without opening the dropdown wrote it into venues.type -- where
   is_nap_friendly does not fail on it, it silently answers False forever.
2. A candidate was cited to a Portland guide to Vancouver, *Washington*, and
   another to a Facebook group post, both shown as an unlabelled "Source" link.
"""

import json
import unittest
from src.web import venues as web_venues
from unittest import mock

import app as app_module
from src import candidates, nominatim, osm
from src.data_loader import CITIES, NEIGHBOURHOODS, SETTINGS, VENUE_TYPES
from src.workflows import propose_venues as pv


class GeneratedValuesTest(unittest.TestCase):
    """Stop the bad value being generated, not just displayed."""

    def _schema_enum(self, field):
        return (pv.PROPOSAL_RESPONSE_FORMAT["json_schema"]["schema"]
                ["properties"]["venues"]["items"]["properties"][field]["enum"])

    def test_the_model_is_given_the_enum_not_a_description_of_it(self):
        self.assertEqual(self._schema_enum("type"), [*VENUE_TYPES, None])
        self.assertEqual(self._schema_enum("neighbourhood"),
                         [*NEIGHBOURHOODS, None])

    def test_the_words_the_live_batch_invented_are_not_available(self):
        for invented in ("activity", "cafe", "restaurant"):
            self.assertNotIn(invented, self._schema_enum("type"))

    def test_null_stays_allowed_because_the_results_may_not_say(self):
        self.assertIn(None, self._schema_enum("type"))
        self.assertIn(None, self._schema_enum("neighbourhood"))

    def test_the_prompt_carries_the_same_lists_as_the_schema(self):
        prompt = pv._messages([{"title": "t", "url": "u", "snippet": "s"}],
                              set())[0]["content"]
        self.assertNotIn("{types}", prompt)
        self.assertNotIn("{neighbourhoods}", prompt)
        for value in VENUE_TYPES:
            self.assertIn(value, prompt)

    def test_a_value_outside_the_enum_is_blanked_not_written(self):
        # Behind the schema, because a model can ignore one. Blank asks the
        # reviewer a question; wrong tells them an answer.
        said = {"roundhouse", "community", "centre", "yaletown", "toddler"}
        kept = pv._grounded({"name": "Roundhouse Community Centre",
                             "type": "activity",
                             "neighbourhood": "Yaletown"}, said)
        self.assertEqual(kept["type"], "")
        self.assertEqual(kept["neighbourhood"], "Yaletown")

    def test_a_value_inside_the_enum_survives(self):
        kept = pv._grounded({"name": "Bloedel Conservatory", "type": "garden",
                             "neighbourhood": "Riley Park"},
                            {"bloedel", "conservatory", "riley", "park"})
        self.assertEqual(kept["type"], "garden")


class LocatedValuesTest(unittest.TestCase):
    """The enum has to hold wherever a value enters, not only at the model.

    _locate runs after _grounded and writes a neighbourhood and a city from the
    place lookup. A geocoder does not know our list, so the first fix -- aimed
    at the model -- left this open, and "Central Vancouver" reached the review
    queue through it.
    """

    def _located(self, hit):
        with mock.patch.object(nominatim, "locate", return_value=hit):
            return pv._locate("Somewhere")

    HIT = {"lat": 49.27, "lng": -123.12, "address": "1 Main St, Vancouver, BC",
           "area": "Yaletown", "external_id": "osm:node/1"}

    def test_an_area_the_lookup_invents_arrives_blank(self):
        # Nominatim answers "Olympic Village" and "Financial District" where
        # our list says neither.
        found = self._located({**self.HIT, "area": "Olympic Village"})
        self.assertEqual(found["neighbourhood"], "")

    def test_a_real_area_survives(self):
        self.assertEqual(self._located(self.HIT)["neighbourhood"], "Yaletown")

    def test_the_city_is_the_one_we_plan(self):
        self.assertEqual(self._located(self.HIT)["city"], pv.CITY)

    def test_the_coordinates_are_still_kept(self):
        # Only the names are held to the enum. A coordinate is not a label.
        found = self._located({**self.HIT, "area": "Nowhere"})
        self.assertEqual((found["lat"], found["lng"]), (49.27, -123.12))

    def test_the_identity_is_carried_through(self):
        self.assertEqual(self._located(self.HIT)["external_id"], "osm:node/1")

    def test_a_result_outside_metro_vancouver_is_dropped_not_kept(self):
        found = self._located({**self.HIT, "lat": 45.63, "lng": -122.66})
        self.assertEqual(found, {"out_of_area": True})

    def test_nothing_found_keeps_the_candidate_without_coordinates(self):
        self.assertEqual(self._located(None), {})

    def test_the_guard_is_shared_with_the_models_answer(self):
        self.assertEqual(pv.in_enum("Yaletown", NEIGHBOURHOODS), "Yaletown")
        self.assertEqual(pv.in_enum("Central Vancouver", NEIGHBOURHOODS), "")
        self.assertEqual(pv.in_enum(None, NEIGHBOURHOODS), "")


class WrongVancouverTest(unittest.TestCase):
    """There are two Vancouvers, 500km apart, and search reaches both.

    A live run of the retargeted queries returned a Portland listicle
    ("8-indoor-playgrounds-portland") and took two Washington venues from it,
    Dizzy Castle and Play Street Museum. The coordinate guard could not catch
    them: it only fires on a *located* candidate, and a Washington venue is
    one Nominatim searching Metro Vancouver never finds, so both arrived with
    no coordinates and sailed straight past the bounds check.
    """

    def _keeps(self, url, title=""):
        return pv.in_region({"url": url, "title": title})

    def test_the_portland_listicle_that_caused_this_is_dropped(self):
        self.assertFalse(self._keeps(
            "https://www.kristinagraffphotography.com/blog/8-indoor-playgrounds-portland"))

    def test_the_earlier_vancouver_washington_citation_is_dropped(self):
        # This one reached the review queue attached to Vancouver Public
        # Library: a real venue carrying a citation about another country.
        self.assertFalse(self._keeps(
            "https://pdx.eater.com/maps/best-kid-friendly-restaurants-vancouver-washington"))

    def test_a_title_naming_the_other_vancouver_is_dropped(self):
        self.assertFalse(self._keeps("https://example.com/x",
                                     "Best of Vancouver, WA for toddlers"))

    def test_real_vancouver_sources_are_kept(self):
        for url in ("https://www.vancouversnorthshore.com/attractions/maplewood-farm",
                    "https://vancouver.kidsoutandabout.com/content/west-point-grey",
                    "https://www.cascadiakids.com/family-friendly-vancouver"):
            with self.subTest(url=url):
                self.assertTrue(self._keeps(url))

    def test_a_washington_street_in_vancouver_bc_is_not_a_false_positive(self):
        # Which is why the pattern does not simply match "washington".
        self.assertTrue(self._keeps("https://example.com/washington-street-park",
                                    "Washington Street Park, Vancouver BC"))

    def test_only_the_url_and_title_are_read_not_the_snippet(self):
        # A snippet can mention Portland in passing; a URL or headline that
        # does is what the article is about.
        self.assertTrue(pv.in_region(
            {"url": "https://example.com/vancouver-parks",
             "title": "Vancouver parks",
             "snippet": "Better than anything in Portland."}))


class SourceTrustTest(unittest.TestCase):
    def test_a_domain_is_read_off_any_shape_of_url(self):
        self.assertEqual(pv.domain("https://www.roundhouse.ca/x?y=1"),
                         "roundhouse.ca")
        self.assertEqual(pv.domain("https://pdx.eater.com/maps/x"),
                         "pdx.eater.com")
        self.assertEqual(pv.domain("not a url"), "")
        self.assertEqual(pv.domain(None), "")

    def test_somewhere_anyone_can_post_is_marked(self):
        self.assertTrue(pv.is_low_trust(
            "https://www.facebook.com/groups/778886723163363/posts/1"))
        self.assertTrue(pv.is_low_trust("https://m.yelp.com/biz/x"))
        self.assertFalse(pv.is_low_trust("https://roundhouse.ca/"))

    def test_a_venues_own_domain_is_official_and_an_article_is_not(self):
        self.assertTrue(pv._looks_official("https://roundhouse.ca/",
                                           "Roundhouse Community Centre"))
        self.assertTrue(pv._looks_official("https://maplewoodfarm.bc.ca/",
                                           "Maplewood Farm"))
        # A good article about a venue is not the venue's site.
        self.assertFalse(pv._looks_official("https://vancouvermom.ca/little-nest",
                                            "Little Nest"))

    def test_the_city_name_alone_does_not_make_a_site_official(self):
        # Otherwise every Vancouver blog is every Vancouver venue's homepage.
        self.assertFalse(pv._looks_official("https://vancouver-guide.com/",
                                            "Vancouver Aquarium"))

    def test_a_venues_own_facebook_page_is_still_not_its_website(self):
        self.assertFalse(pv._looks_official(
            "https://www.facebook.com/sushiaboard", "Sushi Aboard"))

    def test_only_the_root_is_kept(self):
        with mock.patch.object(pv, "search_web", return_value=[
                {"title": "t", "url": "https://www.sushiaboard.ca/menu?tab=1",
                 "snippet": "s"}]):
            self.assertEqual(pv.official_site("Sushi Aboard"),
                             "https://sushiaboard.ca/")

    def test_osm_answers_first_so_no_search_is_spent(self):
        with mock.patch.object(pv, "search_web") as searched:
            found = pv.official_site("Maplewood Farm",
                                     "https://maplewoodfarm.bc.ca/")
            searched.assert_not_called()
        self.assertEqual(found, "https://maplewoodfarm.bc.ca/")

    def test_a_wrong_osm_website_falls_through_to_the_search(self):
        # OSM tagged granvilleisland with a toy shop's site before the exact
        # name match landed. The domain check is the second line of defence.
        with mock.patch.object(pv, "search_web", return_value=[
                {"title": "t", "url": "https://granvilleisland.com/",
                 "snippet": "s"}]):
            self.assertEqual(
                pv.official_site("Granville Island", "https://toycompany.ca/"),
                "https://granvilleisland.com/")

    def test_nothing_official_found_is_blank_not_a_guess(self):
        with mock.patch.object(pv, "search_web", return_value=[
                {"title": "t", "url": "https://vancouvermom.ca/x", "snippet": "s"}]):
            self.assertEqual(pv.official_site("Little Nest"), "")

    def test_a_failed_search_costs_the_field_not_the_run(self):
        with mock.patch.object(pv, "search_web",
                               side_effect=pv.WebSearchError("down")):
            self.assertEqual(pv.official_site("Anywhere"), "")


class HoursPrefillTest(unittest.TestCase):
    def test_one_plain_pair_is_safe_to_prefill(self):
        self.assertEqual(osm.single_pair("10:00-18:00"), ("10:00", "18:00"))
        self.assertEqual(osm.single_pair("9:00-17:30"), ("09:00", "17:30"))

    def test_open_all_week_is_still_one_pair(self):
        self.assertEqual(osm.single_pair("Mo-Su 12:00-22:00"), ("12:00", "22:00"))
        self.assertEqual(osm.single_pair("Mo-Sun 09:00-17:00"), ("09:00", "17:00"))

    def test_always_open_is_a_fact_not_a_guess(self):
        self.assertEqual(osm.single_pair("24/7"), ("00:00", "23:59"))

    def test_anything_one_pair_cannot_hold_is_refused(self):
        for rich in ("Mo-Fr 08:00-16:30",                     # a real exclusion
                     "We,Th 12:00-14:30,16:30-20:45",         # a lunch break
                     "Sep-May: Mo off; Tu-Su 10:00-17:00",    # seasonal
                     "Mo-Su 10:00-18:00; PH off",             # holiday closure
                     "Tu,Th 09:30-19:00; We,Fr 09:30-18:00"):
            self.assertEqual(osm.single_pair(rich), (None, None), rich)

    def test_enrichment_prefills_and_says_where_from(self):
        proposals = [{"name": "Maplewood Farm"}]
        with mock.patch.object(osm, "venue_facts", return_value={
                "Maplewood Farm": {"osm_name": "Maplewood Farm",
                                   "opening_hours": "Mo-Su 10:00-16:00"}}), \
             mock.patch.object(pv, "official_site", return_value=""):
            pv.enrich(proposals)
        self.assertEqual(proposals[0]["open_time"], "10:00")
        self.assertEqual(proposals[0]["close_time"], "16:00")
        # The evidence is what makes the prefill checkable.
        self.assertIn("Maplewood Farm", proposals[0]["hours_note"])
        self.assertIn("Mo-Su 10:00-16:00", proposals[0]["hours_note"])

    def test_hours_too_rich_to_prefill_still_reach_the_reviewer(self):
        proposals = [{"name": "Museum"}]
        with mock.patch.object(osm, "venue_facts", return_value={
                "Museum": {"osm_name": "Museum",
                           "opening_hours": "Tu-Su 10:00-17:00"}}), \
             mock.patch.object(pv, "official_site", return_value=""):
            pv.enrich(proposals)
        self.assertEqual(proposals[0].get("open_time", ""), "")
        self.assertIn("Tu-Su 10:00-17:00", proposals[0]["hours_note"])

    def test_overpass_being_down_costs_the_hours_not_the_batch(self):
        proposals = [{"name": "Somewhere"}]
        with mock.patch.object(osm, "venue_facts",
                               side_effect=osm.OverpassError("down")), \
             mock.patch.object(pv, "official_site", return_value=""):
            pv.enrich(proposals)
        self.assertEqual(proposals[0]["official_url"], "")

    def test_the_batch_costs_one_overpass_query_not_one_per_venue(self):
        # Querying venue by venue earned a 429 within about thirty requests,
        # and this runs from a page.
        proposals = [{"name": f"Venue {i}"} for i in range(8)]
        with mock.patch.object(osm, "venue_facts", return_value={}) as looked_up, \
             mock.patch.object(pv, "official_site", return_value=""):
            pv.enrich(proposals)
        self.assertEqual(looked_up.call_count, 1)

    def test_prefilled_hours_survive_into_the_candidate_file(self):
        # candidates.add copies a fixed column list, so hours the proposer
        # filled in were being silently dropped before PREFILLED_COLUMNS.
        self.assertIn("open_time", candidates.PREFILLED_COLUMNS)
        self.assertIn("open_time", candidates.COLUMNS)


class ExactOsmMatchTest(unittest.TestCase):
    """The Overpass query matches loosely; the filter afterwards must not."""

    def _facts(self, elements, name):
        with mock.patch.object(osm, "_fetch_tags", return_value=elements):
            return osm.venue_facts([name])

    def test_a_longer_name_is_a_different_place(self):
        # Loose matching gave Granville Island the toy shop's hours and website.
        found = self._facts([{"name": "The Granville Island Toy Company",
                              "opening_hours": "10:00-18:00",
                              "website": "https://toycompany.ca/"}],
                            "Granville Island")
        self.assertEqual(found, {})

    def test_a_branch_is_not_the_thing_itself(self):
        found = self._facts([{"name": "Vancouver Public Library Kerrisdale Branch",
                              "opening_hours": "Tu,Th 09:30-19:00"}],
                            "Vancouver Public Library")
        self.assertEqual(found, {})

    def test_the_exact_name_is_not_shadowed_by_a_longer_one(self):
        # "Maplewood Farm Livestock Barn" came back first and won under the old
        # rule, so the farm's own hours were never seen.
        found = self._facts([{"name": "Maplewood Farm Livestock Barn"},
                             {"name": "Maplewood Farm",
                              "opening_hours": "Mo-Su 10:00-16:00"}],
                            "Maplewood Farm")
        self.assertEqual(found["Maplewood Farm"]["opening_hours"],
                         "Mo-Su 10:00-16:00")

    def test_punctuation_and_case_do_not_stop_a_match(self):
        found = self._facts([{"name": "Sun Yat-Sen Garden",
                              "opening_hours": "10:00-16:00"}],
                            "sun yat sen garden")
        self.assertTrue(found)

    def test_a_place_with_no_tags_we_want_is_absent_not_empty(self):
        self.assertEqual(self._facts([{"name": "Nowhere"}], "Nowhere"), {})

    def test_the_hours_only_view_still_works_for_verify_hours(self):
        with mock.patch.object(osm, "_fetch_tags", return_value=[
                {"name": "Science World", "opening_hours": "10:00-17:00"},
                {"name": "Somewhere Else", "website": "https://x.com/"}]):
            self.assertEqual(osm.opening_hours_for(["Science World",
                                                    "Somewhere Else"]),
                             {"Science World": "10:00-17:00"})


class ApprovalGuardTest(unittest.TestCase):
    READY = {"name": "X", "type": "museum", "setting": "indoor",
             "city": "Vancouver", "neighbourhood": "Downtown",
             "open_time": "09:00", "close_time": "17:00"}

    def test_a_ready_candidate_passes(self):
        self.assertEqual(web_venues._cannot_approve(dict(self.READY)), "")

    def test_a_type_outside_the_enum_cannot_be_approved(self):
        # The check that did not exist: is_nap_friendly answers False for a
        # type it does not know rather than failing, so nothing downstream
        # would ever have noticed.
        why = web_venues._cannot_approve({**self.READY, "type": "activity"})
        self.assertIn("activity", why)

    def test_a_neighbourhood_outside_the_enum_cannot_be_approved(self):
        why = web_venues._cannot_approve(
            {**self.READY, "neighbourhood": "Central Vancouver"})
        self.assertIn("Central Vancouver", why)

    def test_a_blank_neighbourhood_is_fine(self):
        self.assertEqual(
            web_venues._cannot_approve({**self.READY, "neighbourhood": ""}), "")

    def test_a_city_outside_the_enum_cannot_be_approved(self):
        self.assertIn("Toronto",
                      web_venues._cannot_approve({**self.READY, "city": "Toronto"}))

    def test_hours_are_still_required(self):
        self.assertIn("opening time",
                      web_venues._cannot_approve({**self.READY, "open_time": ""}))

    def test_every_enum_the_guard_checks_is_the_one_the_form_offers(self):
        self.assertEqual(dict(web_venues.APPROVAL_ENUMS)["type"], VENUE_TYPES)
        self.assertEqual(dict(web_venues.APPROVAL_ENUMS)["city"], CITIES)
        self.assertEqual(dict(web_venues.APPROVAL_ENUMS)["setting"], SETTINGS)


class UnknownValueDisplayTest(unittest.TestCase):
    def test_the_fields_a_reviewer_must_answer_are_named(self):
        row = {"type": "activity", "setting": "indoor",
               "neighbourhood": "Central Vancouver", "city": "Vancouver"}
        self.assertEqual(web_venues._unknown_values(row),
                         ["type", "neighbourhood"])

    def test_a_clean_row_asks_nothing(self):
        row = {"type": "museum", "setting": "indoor",
               "neighbourhood": "Downtown", "city": "Vancouver"}
        self.assertEqual(web_venues._unknown_values(row), [])

    def test_a_blank_is_not_an_unknown_value(self):
        self.assertEqual(web_venues._unknown_values(
            {"type": "", "setting": "", "neighbourhood": None,
             "city": "Vancouver"}), [])


class NameFoldingTest(unittest.TestCase):
    def test_an_american_spelling_is_the_same_place(self):
        # The agent proposed "Roundhouse Community Center"; the City publishes
        # "Roundhouse Community Centre". Approving would have duplicated it.
        self.assertEqual(candidates.normalize_name("Roundhouse Community Center"),
                         candidates.normalize_name("Roundhouse Community Centre"))

    def test_two_different_places_are_still_different(self):
        self.assertNotEqual(candidates.normalize_name("Science World"),
                            candidates.normalize_name("Science Centre"))


if __name__ == "__main__":
    unittest.main()
