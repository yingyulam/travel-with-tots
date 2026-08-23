"""Every way into Find Nearby runs the same component.

Three entry points reach this feature: the chat workflow, the agent's tool for
a phrasing the classifier misses, and the trip page's need panel. Two of them
used to call `interactions.find_nearby` instead, which has no location
narrowing and no web fallback, and the trip page reported source "curated"
having consulted neither.
"""

import unittest
from unittest import mock

import app as app_module
from src import agent
from src.data_loader import SUPPORTED_CITIES

FOUND = {"places": [{"name": "Science World", "neighbourhood": "False Creek",
                     "type": "attraction", "distance_km": None,
                     "maps_url": "https://maps.example/sw"}],
         "source": "search", "need": "nursing_room", "city": "Vancouver",
         "neighbourhood": ""}


class TheAgentToolUsesTheComponentTest(unittest.TestCase):
    def test_it_calls_the_component_with_the_supported_city(self):
        with mock.patch.object(agent, "find_nearby_component",
                               return_value=FOUND) as component:
            content, artifact = agent.find_nearby_tool.func("nursing_room")
        self.assertEqual(component.call_args.kwargs,
                         {"need": "nursing_room", "city": SUPPORTED_CITIES[0]})
        self.assertIn("Science World", content)
        self.assertEqual(artifact["places"], FOUND["places"])

    def test_the_places_survive_as_an_artifact(self):
        # A plain dict return is JSON-stringified into the tool message, so the
        # caller could not render links from it. content_and_artifact is why
        # the agent path shows the same cards the workflow does.
        self.assertEqual(agent.find_nearby_tool.response_format,
                         "content_and_artifact")

    def test_a_failure_is_answered_not_raised(self):
        # The chat route only catches KeyError and OpenAIError, so anything
        # escaping a tool is a 500.
        with mock.patch.object(agent, "find_nearby_component",
                               side_effect=KeyError("TAVILY_API_KEY")):
            content, artifact = agent.find_nearby_tool.func("nursing_room")
        self.assertIn("Couldn't look that up", content)
        self.assertEqual(artifact, {})


class TheTripPagePanelUsesTheComponentTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_the_no_location_branch_reports_the_source_it_used(self):
        # It used to return a hardcoded "curated" without consulting anything
        # but the sample venue list.
        with mock.patch.object(app_module, "find_nearby_component",
                               return_value=FOUND) as component:
            response = self.client.post("/find_nearby", json={"need": "nursing_room"})
        self.assertEqual(component.call_args.kwargs,
                         {"need": "nursing_room", "city": SUPPORTED_CITIES[0]})
        body = response.get_json()
        self.assertEqual(body["source"], "search")
        self.assertEqual(body["venues"], FOUND["places"])

    def test_the_response_keys_the_trip_page_reads_are_unchanged(self):
        with mock.patch.object(app_module, "find_nearby_component",
                               return_value=FOUND):
            body = self.client.post("/find_nearby",
                                    json={"need": "nursing_room"}).get_json()
        self.assertEqual(set(body), {"need", "venues", "source", "location"})


class TheRequestCarriesCoordinatesTest(unittest.TestCase):
    """`location` is client-supplied, like `conversation` before it."""

    def test_a_real_pair_is_passed_through(self):
        self.assertEqual(
            app_module._message_context({"location": {"lat": 49.27, "lng": -123.1}}),
            {"lat": 49.27, "lng": -123.1})

    def test_anything_else_becomes_no_location(self):
        for location in ("49,-123", {"lat": "49", "lng": "-123"}, {"lat": 49},
                         {"lat": None, "lng": None}, {"lat": 999, "lng": 0},
                         {"lat": 0, "lng": 999}, {"lat": True, "lng": True}, None):
            with self.subTest(location=location):
                self.assertEqual(
                    app_module._message_context({"location": location}), {})

    def test_a_missing_key_is_fine(self):
        self.assertEqual(app_module._message_context({}), {})


if __name__ == "__main__":
    unittest.main()
