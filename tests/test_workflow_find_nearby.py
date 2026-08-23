"""The Find a nearby place workflow.

The bug this exists for: asking the chat "find the nearest nursing room" was
answered by `interactions.find_nearby`, the deterministic placeholder, and
logged as no workflow at all. These pin both halves, that the message routes to
a registered workflow and that the workflow runs the real component.
"""

import unittest
from unittest import mock

from src import agent, interactions
from src.data_loader import SUPPORTED_CITIES
from src.workflows import find_nearby_place
from src.workflows.find_nearby_place import (
    NEED_QUESTION,
    STAGE_NEED,
    WORKFLOW,
    read_need,
    run,
)

HERE = {"lat": 49.2734, "lng": -123.1027}
FOUND = {"places": [{"name": "Science World", "neighbourhood": "False Creek",
                     "type": "attraction", "distance_km": 0.19,
                     "maps_url": "https://maps.example/science-world"}],
         "source": "curated", "need": "nursing_room", "city": "Vancouver",
         "neighbourhood": ""}
NOTHING = {"places": [], "source": "none", "need": "nursing_room",
           "city": "Vancouver", "neighbourhood": ""}


def _run(message, state=None, context=None, result=None):
    with mock.patch.object(find_nearby_place, "find_nearby",
                           return_value=result or FOUND) as component:
        answer = run(message, state, context)
    return answer, component


class ReadingTheNeedTest(unittest.TestCase):
    def test_each_need_from_how_a_parent_would_say_it(self):
        for message, expected in (
            ("find the nearest nursing room", "nursing_room"),
            ("where can I breastfeed", "nursing_room"),
            ("where can I change a nappy", "changing_table"),
            ("I need a changing table", "changing_table"),
            ("is there a family room near here", "family_room"),
            ("somewhere to eat with a toddler", "restaurant"),
            ("she's hungry", "restaurant"),
            ("somewhere quiet for a meltdown", "quiet_spot"),
        ):
            with self.subTest(message=message):
                self.assertEqual(read_need(message), expected)

    def test_feeding_beats_quiet(self):
        # Both sets of words are in this sentence, and only the order of
        # NEED_WORDS decides. A quiet spot has nowhere to feed a baby.
        self.assertEqual(read_need("a quiet place to feed the baby"),
                         "nursing_room")

    def test_a_chip_label_is_matched_exactly(self):
        # Tapping a chip sends the button's own label, so every one must parse.
        for key, label in interactions.NEED_OPTIONS:
            with self.subTest(label=label):
                self.assertEqual(read_need(label), key)

    def test_an_unreadable_need_is_not_guessed(self):
        self.assertIsNone(read_need("we need somewhere to go right now"))


class AskingForTheNeedTest(unittest.TestCase):
    def test_an_unreadable_need_asks_with_the_six_options(self):
        answer, component = _run("we need somewhere to go right now")
        component.assert_not_called()
        self.assertEqual(answer["reply"], NEED_QUESTION)
        self.assertEqual(answer["choices"],
                         [label for _, label in interactions.NEED_OPTIONS])
        self.assertEqual(answer["state"]["stage"], STAGE_NEED)

    def test_it_does_not_ask_twice(self):
        # Asking again for something already asked is how a conversation stops
        # being useful, so an unreadable second answer falls back to anything
        # kid-friendly rather than looping.
        answer, component = _run("still not sure", {"stage": STAGE_NEED})
        self.assertEqual(component.call_args.kwargs["need"], "other")
        self.assertIsNone(answer["state"])

    def test_the_answer_to_the_question_is_used(self):
        _, component = _run("Nursing room", {"stage": STAGE_NEED})
        self.assertEqual(component.call_args.kwargs["need"], "nursing_room")


class ItRunsTheComponentTest(unittest.TestCase):
    """Not interactions.find_nearby, which is the need-matching predicate with
    no location narrowing and no web fallback."""

    def test_the_city_is_always_passed_so_curated_is_consulted(self):
        # The component only searches the curated table given a city or
        # coordinates, so without this the no-location branch finds nothing.
        _, component = _run("find a nursing room")
        self.assertEqual(component.call_args.kwargs["city"], SUPPORTED_CITIES[0])

    def test_coordinates_reach_the_component(self):
        _, component = _run("find a nursing room", context=HERE)
        self.assertEqual(component.call_args.kwargs["lat"], HERE["lat"])
        self.assertEqual(component.call_args.kwargs["lng"], HERE["lng"])

    def test_without_coordinates_it_still_answers_and_offers_to_locate(self):
        answer, _ = _run("find a nursing room")
        self.assertTrue(answer["ask_location"])
        self.assertTrue(answer["places"])
        self.assertIn(SUPPORTED_CITIES[0], answer["reply"])

    def test_with_coordinates_it_does_not_ask_for_them(self):
        answer, _ = _run("find a nursing room", context=HERE)
        self.assertFalse(answer["ask_location"])
        self.assertIn("near you", answer["reply"])

    def test_junk_coordinates_are_ignored_rather_than_passed_on(self):
        # They come from the browser through the request body. A string would
        # reach haversine_km and produce a nonsense distance.
        for context in ({"lat": "49.2", "lng": "-123.1"}, {"lat": 49.2},
                        {"lat": None, "lng": None}, {}):
            with self.subTest(context=context):
                _, component = _run("find a nursing room", context=context)
                self.assertIsNone(component.call_args.kwargs["lat"])


class TheAnswerTest(unittest.TestCase):
    def test_places_come_back_as_data_not_as_urls_in_the_sentence(self):
        # The widget builds the anchors, so no URL belongs in the reply text.
        answer, _ = _run("find a nursing room", context=HERE)
        self.assertEqual(answer["places"], FOUND["places"])
        self.assertNotIn("http", answer["reply"])

    def test_a_web_result_says_so(self):
        answer, _ = _run("find a nursing room",
                         result={**FOUND, "source": "search"})
        self.assertIn("web search", answer["reply"])

    def test_finding_nothing_is_an_answer_not_an_error(self):
        answer, _ = _run("find a nursing room", result=NOTHING)
        self.assertEqual(answer["places"], [])
        self.assertIn("couldn't find", answer["reply"])

    def test_the_flow_ends_in_one_turn(self):
        # No state, so the next message goes back through the classifier.
        answer, _ = _run("find a nursing room", context=HERE)
        self.assertIsNone(answer["state"])


class RoutingTest(unittest.TestCase):
    """The reported bug, stated directly: this message showed "no workflow"."""

    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.log.start()
        self.addCleanup(self.log.stop)

    def test_a_nearby_message_is_named_in_the_reply(self):
        with mock.patch.object(agent, "classify_intent",
                               return_value=WORKFLOW["name"]), \
             mock.patch.object(find_nearby_place, "find_nearby",
                               return_value=FOUND), \
             mock.patch.object(agent, "run_agent") as fell_through:
            answer = agent.handle_message("find the nearest nursing room")
        fell_through.assert_not_called()
        self.assertEqual(answer["workflow"], WORKFLOW["name"])
        self.assertEqual(answer["places"], FOUND["places"])

    def test_coordinates_travel_from_the_request_to_the_workflow(self):
        with mock.patch.object(agent, "classify_intent",
                               return_value=WORKFLOW["name"]), \
             mock.patch.object(find_nearby_place, "find_nearby",
                               return_value=FOUND) as component:
            agent.handle_message("find a nursing room", context=HERE)
        self.assertEqual(component.call_args.kwargs["lat"], HERE["lat"])

    def test_it_is_offered_to_the_classifier(self):
        from src.workflows import runnable_message_workflows
        offered = [w["name"] for w, _ in runnable_message_workflows()]
        self.assertIn(WORKFLOW["name"], offered)


if __name__ == "__main__":
    unittest.main()
