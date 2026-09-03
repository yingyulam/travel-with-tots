"""Every way into Find Nearby runs the same component.

Three entry points reach this feature: the chat workflow, the agent's tool for
a phrasing the classifier misses, and the trip page's need panel. Two of them
used to call `interactions.find_nearby` instead, which has no location
narrowing and no web fallback, and the trip page reported source "curated"
having consulted neither.
"""

import unittest
from src.web import trip as web_trip
from src.web import devpages
from src.web import guards
from unittest import mock

import app as app_module
from src import agent
from src.components.find_nearby import searchable
from src.data_loader import SUPPORTED_CITIES

FOUND = {"places": [{"name": "Science World", "neighbourhood": "False Creek",
                     "type": "attraction", "distance_km": None,
                     "maps_url": "https://maps.example/sw"}],
         "source": "search", "need": "nursing_room", "city": "Vancouver",
         "neighbourhood": "", "maps_search_url": None}


class TheAgentStartsTheWorkflowTest(unittest.TestCase):
    """The agent's tool starts the workflow; the workflow calls the component.

    It used to call the component itself, which is why the agent path had no
    coordinates -- it passed only a need and a city, where the workflow passes
    lat, lng and the resolved place name. Starting the workflow instead means
    one implementation reaches the component, and the browser's location
    reaches it too.
    """

    def _tool(self):
        name = agent._slug("Find a nearby place")
        return next(t for t in agent.TOOLS if t.name == name)

    def test_the_workflow_has_a_tool(self):
        self.assertIsNotNone(self._tool())

    def test_the_tool_takes_no_arguments(self):
        # The whole point. A tool that needed a `need` could not be called
        # before the parent had named one, so a bare "I need something nearby"
        # started nothing at all.
        self.assertEqual(self._tool().args, {})

    def test_it_runs_the_workflow_on_the_parents_own_words(self):
        with mock.patch.object(agent, "run_workflow_turn",
                               return_value={"reply": "ok"}) as ran:
            token = agent._TURN_MESSAGE.set("I need a nursing room")
            try:
                self._tool().func()
            finally:
                agent._TURN_MESSAGE.reset(token)
        self.assertEqual(ran.call_args.args,
                         ("Find a nearby place", "I need a nursing room"))

    def test_the_request_context_reaches_the_workflow(self):
        # Coordinates live here. Without them the component cannot rank by
        # distance, which is what the old tool silently lost.
        here = {"lat": 49.2, "lng": -123.1}
        with mock.patch.object(agent, "run_workflow_turn",
                               return_value={"reply": "ok"}) as ran:
            token = agent._TURN_CONTEXT.set(here)
            try:
                self._tool().func()
            finally:
                agent._TURN_CONTEXT.reset(token)
        self.assertEqual(ran.call_args.kwargs["context"], here)


class TheTripPagePanelUsesTheComponentTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_the_no_location_branch_reports_the_source_it_used(self):
        # It used to return a hardcoded "curated" without consulting anything
        # but the sample venue list.
        with mock.patch.object(web_trip, "find_nearby_component",
                               return_value=FOUND) as component:
            response = self.client.post("/find_nearby", json={"need": "nursing_room"})
        # Every field, the same ones the test page passes, so the two cannot
        # answer the same question differently.
        self.assertEqual(component.call_args.kwargs,
                         {"need": "nursing_room", "city": SUPPORTED_CITIES[0],
                          "neighbourhood": "", "place_name": "",
                          "lat": None, "lng": None,
                          # Both only matter to lunch, but they are passed for
                          # every need rather than branching in the route.
                          "transit": "", "near_place": ""})
        body = response.get_json()
        self.assertEqual(body["source"], "search")
        self.assertEqual(body["venues"], FOUND["places"])

    def test_the_response_keys_the_trip_page_reads_are_unchanged(self):
        with mock.patch.object(web_trip, "find_nearby_component",
                               return_value=FOUND):
            body = self.client.post("/find_nearby",
                                    json={"need": "nursing_room"}).get_json()
        self.assertEqual(set(body),
                         {"need", "venues", "source", "location",
                          "maps_search_url"})

    def test_the_trip_page_can_anchor_the_lunch_handoff_on_a_stop(self):
        # Sent by the trip page as the stop the parent is standing at, so a
        # Maps search has somewhere to sit when the browser shared no location.
        with mock.patch.object(web_trip, "find_nearby_component",
                               return_value=FOUND) as component:
            self.client.post("/find_nearby",
                             json={"need": "restaurant", "transit": "walk",
                                   "near_place": "Science World"})
        self.assertEqual(component.call_args.kwargs["near_place"], "Science World")
        self.assertEqual(component.call_args.kwargs["transit"], "walk")


class NothingKnownMeansTheCityWeCoverTest(unittest.TestCase):
    """find_nearby treats "nothing known" as "search the whole web", which is
    the right general contract and the wrong answer for this app: asked for a
    kid-friendly restaurant with no location, it returned Austin, Texas."""

    def test_an_unresolved_location_gets_the_supported_city(self):
        self.assertEqual(
            searchable({"city": "", "neighbourhood": "", "formatted_address": "",
                        "lat": None, "lng": None})["city"],
            SUPPORTED_CITIES[0])

    def test_a_resolved_city_is_left_alone(self):
        where = searchable({"city": "Richmond", "neighbourhood": "",
                            "formatted_address": "7360 St Albans Rd",
                            "lat": None, "lng": None})
        self.assertEqual(where["city"], "Richmond")

    def test_coordinates_alone_are_left_alone(self):
        # Coordinates search every venue by real distance. Naming a city here
        # would narrow the search to one we never resolved.
        where = searchable({"city": "", "neighbourhood": "",
                            "formatted_address": "", "lat": 49.1, "lng": -123.1})
        self.assertEqual(where["city"], "")

    def test_the_component_page_applies_it_too(self):
        # The page and the chat differed on exactly this: same question, no
        # location, one answered Vancouver and the other answered Texas.
        client = app_module.app.test_client()
        with mock.patch.object(guards, "current_parent",
                               return_value={"id": 1, "is_admin": 1}), \
             mock.patch.object(devpages, "find_nearby_component",
                               return_value=FOUND) as component:
            client.post("/find-nearby/run", json={"need": "restaurant"})
        self.assertEqual(component.call_args.kwargs["city"], SUPPORTED_CITIES[0])


class TheRequestCarriesCoordinatesTest(unittest.TestCase):
    """`location` is client-supplied, like `conversation` before it. Asserted
    on the coordinate keys rather than the whole context, which carries other
    things a request knows and will carry more."""

    @staticmethod
    def _coords(body):
        context = app_module._message_context(body)
        return {k: v for k, v in context.items() if k in ("lat", "lng")}

    def test_a_real_pair_is_passed_through(self):
        self.assertEqual(
            self._coords({"location": {"lat": 49.27, "lng": -123.1}}),
            {"lat": 49.27, "lng": -123.1})

    def test_anything_else_becomes_no_location(self):
        for location in ("49,-123", {"lat": "49", "lng": "-123"}, {"lat": 49},
                         {"lat": None, "lng": None}, {"lat": 999, "lng": 0},
                         {"lat": 0, "lng": 999}, {"lat": True, "lng": True}, None):
            with self.subTest(location=location):
                self.assertEqual(self._coords({"location": location}), {})

    def test_a_missing_key_is_fine(self):
        self.assertEqual(self._coords({}), {})

    def test_a_bad_location_does_not_take_the_rest_of_the_context_with_it(self):
        # They are read independently: an unusable location must not also lose
        # the flag saying a trip is open.
        self.assertTrue(app_module._message_context(
            {"on_trip": True, "location": "nonsense"})["on_trip"])


if __name__ == "__main__":
    unittest.main()
