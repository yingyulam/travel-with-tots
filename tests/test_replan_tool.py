"""The agent's replan tool: collecting a replan, never performing one.

The division matters more than the tool does. The itinerary, its versions and
the current time live on the trip page, and runReplan there is the one
implementation; replanning here would be a second one, producing a version the
page's switcher never sees. So this collects a request and hands it over for
one button, exactly as the Replan on the go workflow does.

That "exactly as" is asserted rather than assumed: both read the same message
through interactions.read_replan_request, so the chat agent and the workflow
cannot collect the same request two different ways.
"""

import unittest
from unittest import mock

from src import agent, interactions
from src.workflows import replan_on_the_go


class ReplanToolTest(unittest.TestCase):
    def _call(self, message, on_trip=True):
        token = agent._TURN_ON_TRIP.set(on_trip)
        try:
            return agent.replan_tool.func(message)
        finally:
            agent._TURN_ON_TRIP.reset(token)

    def test_it_collects_a_request_rather_than_replanning(self):
        content, artifact = self._call("the nap ran long")
        self.assertEqual(artifact["replan_request"]["situation"], "nap_happened")
        self.assertIn("Collected", content)

    def test_it_reads_how_long_for_a_timed_situation(self):
        _content, artifact = self._call("the nap ran 90 minutes over")
        self.assertEqual(artifact["replan_request"]["minutes"], 90)

    def test_their_own_words_ride_along(self):
        _content, artifact = self._call("it started pouring")
        request = artifact["replan_request"]
        self.assertEqual(request["situation"], "weather_rain")
        self.assertEqual(request["note"], "it started pouring")

    def test_it_refuses_when_no_trip_is_open(self):
        # Collecting a situation nothing can act on would waste the turn, and
        # the workflow already refuses for the same reason.
        content, artifact = self._call("the nap ran long", on_trip=False)
        self.assertEqual(artifact, {})
        self.assertIn("already started", content)

    def test_whether_a_trip_is_open_is_not_the_models_to_decide(self):
        # It comes from the request, so the tool takes only the situation text.
        self.assertEqual(list(agent.replan_tool.args), ["situation"])

    def test_it_hands_back_an_artifact_not_a_stringified_dict(self):
        self.assertEqual(agent.replan_tool.response_format, "content_and_artifact")

    def test_it_is_registered(self):
        self.assertIn("replan_tool", [t.name for t in agent.TOOLS])


class OneImplementationTest(unittest.TestCase):
    """The tool and the workflow must read a message identically."""

    MESSAGES = (
        "the nap ran long",
        "we napped for 45 mins",
        "it's raining",
        "skip the next stop",
        "we finished early",
        "running behind",
        "Nap happened here",          # a tapped chip, not typed words
    )

    def test_the_tool_and_the_workflow_agree_on_every_message(self):
        token = agent._TURN_ON_TRIP.set(True)
        self.addCleanup(agent._TURN_ON_TRIP.reset, token)
        for message in self.MESSAGES:
            with self.subTest(message=message):
                _content, artifact = agent.replan_tool.func(message)
                from_workflow = replan_on_the_go.run(
                    message, {"stage": replan_on_the_go.STAGE_SITUATION,
                              "values": {}}, {"on_trip": True})
                self.assertEqual(artifact["replan_request"],
                                 from_workflow["replan_request"])

    def test_words_that_name_no_situation_get_the_chips(self):
        """The workflow's first turn offers six chips rather than guessing, and
        so does the tool. Collecting "the ferry was cancelled" as a note would
        skip the useful thing to show somebody who has not said yet."""
        token = agent._TURN_ON_TRIP.set(True)
        self.addCleanup(agent._TURN_ON_TRIP.reset, token)
        content, artifact = agent.replan_tool.func("the ferry was cancelled")
        self.assertEqual(artifact["choices"], interactions.SITUATION_CHIP_LABELS)
        self.assertNotIn("replan_request", artifact)
        self.assertEqual(content, interactions.SITUATION_QUESTION)

        # And the chip they tap comes back as a real situation.
        _content, artifact = agent.replan_tool.func("Nap happened here")
        self.assertEqual(artifact["replan_request"]["situation"], "nap_happened")

    def test_the_workflow_reads_through_interactions_now(self):
        # The readers moved so agent.py never imports from workflows/. If the
        # workflow grows its own copy again, this fails.
        self.assertIs(replan_on_the_go.read_situation, interactions.read_situation)
        self.assertIs(replan_on_the_go.read_minutes, interactions.read_minutes)


class TurnContextTest(unittest.TestCase):
    def test_handle_message_sets_on_trip_from_the_request(self):
        seen = {}

        def fake_agent(message, history=None, model=None):
            seen["on_trip"] = agent._TURN_ON_TRIP.get()
            return {"reply": "ok"}

        with mock.patch.object(agent, "classify_intent", return_value="none"), \
             mock.patch.object(agent, "run_agent", side_effect=fake_agent):
            agent.handle_message("hello", context={"on_trip": True})
        self.assertIs(seen["on_trip"], True)

    def test_it_defaults_to_no_trip_when_the_request_says_nothing(self):
        seen = {}

        def fake_agent(message, history=None, model=None):
            seen["on_trip"] = agent._TURN_ON_TRIP.get()
            return {"reply": "ok"}

        with mock.patch.object(agent, "classify_intent", return_value="none"), \
             mock.patch.object(agent, "run_agent", side_effect=fake_agent):
            agent.handle_message("hello")
        self.assertIs(seen["on_trip"], False)


if __name__ == "__main__":
    unittest.main()
