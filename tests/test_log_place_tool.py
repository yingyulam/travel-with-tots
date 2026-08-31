"""The agent's log-a-place tool: collecting a submission, never writing one.

Same division the Log a Place workflow keeps. The chat has no parent to attach
a submission to and no way to drop a map pin, and a form post has both, so
log_a_place.store stays the one writer and this hands values to /log-place.

The interesting difference from the workflow is what the model can do that a
comma split cannot. The workflow deliberately refuses to read its opening
message, because split_name "cannot tell a place name from a sentence about
wanting to log one" and would store a venue called "I want to log a place".
Naming the place is a judgment, so the tool takes a name the model picked out
and guards the parts that are not judgments: the amenity vocabulary.
"""

import unittest

from src import db
from src.agent import TOOLS, log_place_tool
from src.workflows import log_a_place


def call(**kwargs):
    return log_place_tool.func(**kwargs)


class CollectsASubmissionTest(unittest.TestCase):
    def test_a_name_is_enough(self):
        _content, artifact = call(name="Nourish Kitchen", amenities=[])
        self.assertEqual(artifact["place_form"], {"name": "Nourish Kitchen"})

    def test_an_area_becomes_the_neighbourhood(self):
        # The key the form and store() already use, so a collected place looks
        # the same whichever path collected it.
        _content, artifact = call(name="Nourish Kitchen", area="Gastown", amenities=[])
        self.assertEqual(artifact["place_form"]["neighbourhood"], "Gastown")

    def test_amenities_are_set_as_flags(self):
        _content, artifact = call(name="A Mall",
                                  amenities=["has_family_room",
                                             "has_nursing_room"])
        form = artifact["place_form"]
        self.assertIs(form["has_family_room"], True)
        self.assertIs(form["has_nursing_room"], True)

    def test_an_amenity_nobody_defined_is_dropped(self):
        # A model naming a column that does not exist would reach the form as a
        # field nothing renders. Checked against the vocabulary, not trusted.
        _content, artifact = call(name="A Mall",
                                  amenities=["has_family_room", "has_ballpit"])
        self.assertNotIn("has_ballpit", artifact["place_form"])
        self.assertIs(artifact["place_form"]["has_family_room"], True)

    def test_notes_ride_along(self):
        _content, artifact = call(name="A Cafe", notes="tiny, but very calm", amenities=[])
        self.assertEqual(artifact["place_form"]["notes"], "tiny, but very calm")

    def test_nothing_empty_is_stored(self):
        # Blank strings would render as filled-in fields on the form.
        _content, artifact = call(name="A Cafe", area="  ", notes="  ", amenities=[])
        self.assertEqual(artifact["place_form"], {"name": "A Cafe"})

    def test_it_refuses_without_a_name(self):
        content, artifact = call(name="   ")
        self.assertEqual(artifact, {})
        self.assertIn("name", content)

    def test_it_asks_what_the_place_offers_when_nobody_has_said(self):
        # The workflow shows a multi-select row here, so the tool does too.
        content, artifact = call(name="A Cafe")
        self.assertEqual(artifact["choices"],
                         [label for _, label in db.AMENITY_OPTIONS])
        self.assertIs(artifact["choose_many"], True)
        self.assertNotIn("place_form", artifact)
        self.assertIn("offer", content)

    def test_saying_none_is_an_answer_and_does_not_ask_again(self):
        # The difference a missing argument carries and a falsy one cannot:
        # [] is "they said none", absent is "nobody has said". Without it the
        # chips would go out again on every turn.
        _content, artifact = call(name="A Cafe", amenities=[])
        self.assertEqual(artifact["place_form"], {"name": "A Cafe"})
        self.assertNotIn("choices", artifact)

    def test_it_hands_back_an_artifact(self):
        self.assertEqual(log_place_tool.response_format, "content_and_artifact")

    def test_it_is_registered(self):
        # The point of the tool. Unregistered, the agent cannot reach it, and
        # logging a place from the chat disappears the moment the classifier
        # stops routing to the workflow.
        self.assertIn("log_place_tool", [t.name for t in TOOLS])


class SharedVocabularyTest(unittest.TestCase):
    def test_the_tool_and_the_workflow_offer_one_list(self):
        self.assertIs(log_a_place.AMENITY_OPTIONS, db.AMENITY_OPTIONS)

    def test_every_amenity_the_tool_accepts_is_a_real_column(self):
        # The names are add_venue's parameters, so a drift here would be a
        # submission that silently loses a field.
        for key, _label in db.AMENITY_OPTIONS:
            self.assertIn(key, db.REPORTABLE_FIELDS)

    def test_the_form_shape_matches_what_the_workflow_hands_over(self):
        # Both end at /log-place, so the widget and store() must not have to
        # tell them apart.
        _content, artifact = call(name="Nourish Kitchen", area="Gastown",
                                  amenities=["has_family_room"],
                                  notes="calm at lunchtime")
        from_workflow = {"name": "Nourish Kitchen", "neighbourhood": "Gastown",
                         "has_family_room": True, "notes": "calm at lunchtime"}
        self.assertEqual(artifact["place_form"], from_workflow)


class ChipsMatchTheWorkflowTest(unittest.TestCase):
    """The chips the agent offers must be the chips the workflow offers.

    Two surfaces asking the same question with different buttons is how a
    parent learns the chat is two products. These are the same lists, read from
    the same constants, and this fails if either side grows its own.
    """

    def test_the_need_chips_match(self):
        from src.agent import find_nearby_tool
        from src.interactions import NEED_CHIP_LABELS
        from src.workflows import find_nearby_place
        _content, artifact = find_nearby_tool.func("")
        self.assertEqual(artifact["choices"], NEED_CHIP_LABELS)
        self.assertEqual(artifact["choices"],
                         find_nearby_place.run("help", None, None)["choices"])

    def test_the_situation_chips_match(self):
        from src import agent
        from src.workflows import replan_on_the_go
        token = agent._TURN_ON_TRIP.set(True)
        self.addCleanup(agent._TURN_ON_TRIP.reset, token)
        _content, artifact = agent.replan_tool.func("something happened")
        from_workflow = replan_on_the_go.run("something happened", None,
                                             {"on_trip": True})
        self.assertEqual(artifact["choices"], from_workflow["choices"])

    def test_the_amenity_chips_match(self):
        from src.agent import log_place_tool
        from src.workflows import log_a_place
        _content, artifact = log_place_tool.func(name="A Cafe")
        self.assertEqual(artifact["choices"],
                         [label for _, label in log_a_place.AMENITY_OPTIONS])
        self.assertIs(artifact["choose_many"], True)


if __name__ == "__main__":
    unittest.main()
