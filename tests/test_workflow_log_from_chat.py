"""Logging a place by talking to the chat.

The reported bug: "Log this place" answered with the badge "no workflow" and
the agent asked for details it could do nothing with. The workflow's trigger
was "event", so the classifier's enum could not emit its name at any
confidence, and its `run` took `(parent_id, values)` rather than a message.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import unittest
from unittest import mock

from src import agent
from src.workflows import log_a_place, runnable_message_workflows
from src.workflows.log_a_place import (
    AMENITY_OPTIONS,
    NONE_CHOICE,
    STAGE_AMENITIES,
    STAGE_CONFIRMING,
    STAGE_NAME,
    STAGE_NOTES,
    WORKFLOW,
    read_amenities,
    run,
    split_name,
)

LABELS = [label for _, label in AMENITY_OPTIONS]


def _turn(message, state=None):
    return run(message, state)


def _state(stage, **values):
    return {"stage": stage, "values": values}


class ReadingSeveralFeaturesTest(unittest.TestCase):
    """A place is several things at once: a mall with a family room usually
    has a nursing room too. The matcher collects, it does not pick."""

    def test_two_features_in_one_answer(self):
        self.assertEqual(read_amenities("family room and nursing room"),
                         ["has_family_room", "has_nursing_room"])

    def test_all_four_at_once(self):
        self.assertEqual(read_amenities(", ".join(LABELS)),
                         [key for key, _ in AMENITY_OPTIONS])

    def test_it_never_stops_at_the_first_hit(self):
        # The difference from find_nearby_place.read_need, which returns one.
        self.assertGreater(len(read_amenities("Family room, Nursing room")), 1)

    def test_what_the_chips_send_reads_the_same_as_typing(self):
        # Done joins the picked labels with ", ", so both arrive here.
        for label, key in zip(LABELS, [k for k, _ in AMENITY_OPTIONS]):
            with self.subTest(label=label):
                self.assertEqual(read_amenities(label), [key])

    def test_nothing_mentioned_is_nothing_ticked(self):
        self.assertEqual(read_amenities("no idea really"), [])


class SplittingTheNameTest(unittest.TestCase):
    def test_a_trailing_comma_separates_the_area(self):
        self.assertEqual(split_name("Nourish Kitchen, Gastown"),
                         ("Nourish Kitchen", "Gastown"))

    def test_no_comma_is_all_name(self):
        self.assertEqual(split_name("Richmond Centre"), ("Richmond Centre", ""))

    def test_only_the_last_comma_splits(self):
        self.assertEqual(split_name("Cafe, Bar, Kitsilano"),
                         ("Cafe, Bar", "Kitsilano"))


class TheConversationTest(unittest.TestCase):
    def test_it_opens_by_asking_what_the_place_is_called(self):
        first = run("log this place")
        self.assertEqual(first["state"]["stage"], STAGE_NAME)
        self.assertIn("called", first["reply"])

    def test_the_opening_message_is_deliberately_not_read(self):
        # The planning chat reads its opening message; this one must not, and
        # the difference is the reader. split_name is a comma split with no way
        # to tell a place name from a sentence about wanting to log one, so
        # reading "I want to log a place" would store that as a venue name.
        # Do not "fix" this without a reader that can tell them apart.
        for message in ("log Richmond Centre in Richmond", "I want to log a place"):
            with self.subTest(message=message):
                answer = run(message)
                self.assertEqual(answer["state"]["stage"], STAGE_NAME)
                self.assertEqual(answer["state"]["values"], {})
                self.assertEqual(split_name(message)[0], message,
                                 "the reader cannot separate intent from name")

    def test_the_name_leads_to_the_features_question(self):
        turn = _turn("Richmond Centre, Richmond", _state(STAGE_NAME))
        self.assertEqual(turn["state"]["stage"], STAGE_AMENITIES)
        self.assertEqual(turn["state"]["values"]["name"], "Richmond Centre")
        self.assertEqual(turn["state"]["values"]["neighbourhood"], "Richmond")

    def test_the_features_question_asks_for_many(self):
        turn = _turn("Somewhere", _state(STAGE_NAME))
        self.assertTrue(turn["choose_many"])
        self.assertEqual(turn["choices"], LABELS)

    def test_an_empty_name_asks_again_rather_than_moving_on(self):
        # The one required field, matching store's single validation.
        turn = _turn("   ", _state(STAGE_NAME))
        self.assertEqual(turn["state"]["stage"], STAGE_NAME)

    def test_features_are_recorded_and_lead_to_notes(self):
        turn = _turn("family room and nursing room",
                     _state(STAGE_AMENITIES, name="Mall"))
        self.assertTrue(turn["state"]["values"]["has_family_room"])
        self.assertTrue(turn["state"]["values"]["has_nursing_room"])
        self.assertEqual(turn["state"]["stage"], STAGE_NOTES)

    def test_declining_the_features_ticks_none(self):
        turn = _turn(NONE_CHOICE, _state(STAGE_AMENITIES, name="Mall"))
        self.assertEqual(turn["state"]["values"], {"name": "Mall"})
        self.assertEqual(turn["state"]["stage"], STAGE_NOTES)

    def test_notes_are_optional(self):
        turn = _turn("No, that's everything", _state(STAGE_NOTES, name="Mall"))
        self.assertNotIn("notes", turn["state"]["values"])
        self.assertEqual(turn["state"]["stage"], STAGE_CONFIRMING)

    def test_the_summary_shows_everything_collected(self):
        turn = _turn("level 2", _state(STAGE_NOTES, name="Mall",
                                       neighbourhood="Richmond",
                                       has_family_room=True))
        for want in ("Mall", "Richmond", "Family room", "level 2"):
            with self.subTest(want=want):
                self.assertIn(want, turn["reply"])


class HandingOffTest(unittest.TestCase):
    """The chat collects; the Log a Place page stores. It has no parent to
    attach a submission to, and no way to drop a map pin."""

    def test_confirming_hands_over_the_values(self):
        turn = _turn("yes", _state(STAGE_CONFIRMING, name="Mall",
                                   has_family_room=True))
        self.assertIsNone(turn["state"])
        self.assertEqual(turn["place_form"]["name"], "Mall")

    def test_nothing_is_handed_over_before_confirming(self):
        for stage in (STAGE_NAME, STAGE_AMENITIES, STAGE_NOTES):
            with self.subTest(stage=stage):
                turn = _turn("Mall", _state(stage, name="Mall"))
                self.assertIsNone(turn.get("place_form"))

    def test_it_never_writes_to_the_database_itself(self):
        with mock.patch.object(log_a_place, "store") as stored, \
             mock.patch.object(log_a_place.db, "add_or_update_submission") as added:
            state = run("log this place")["state"]
            state = run("Mall, Richmond", state)["state"]
            state = run("family room", state)["state"]
            state = run("nothing", state)["state"]
            run("yes", state)
        stored.assert_not_called()
        added.assert_not_called()

    def test_the_keys_are_the_forms_own_field_names(self):
        # So `store` reads the handoff unchanged, with no translation layer.
        turn = _turn("yes", _state(STAGE_CONFIRMING, name="Mall",
                                   neighbourhood="Richmond", notes="level 2",
                                   has_family_room=True))
        form_fields = {"name", "neighbourhood", "notes", "venue_type",
                       *[key for key, _ in AMENITY_OPTIONS]}
        self.assertTrue(set(turn["place_form"]) <= form_fields,
                        f"unknown keys: {set(turn['place_form']) - form_fields}")


class ItIsRoutableNowTest(unittest.TestCase):
    def test_the_classifier_is_offered_it(self):
        # The bug in one assertion: trigger "event" kept it off this list.
        offered = [w["name"] for w, _ in runnable_message_workflows()]
        self.assertIn(WORKFLOW["name"], offered)

    def test_a_logging_message_names_the_workflow_in_the_reply(self):
        with \
             mock.patch.object(agent, "log_decision"), \
             mock.patch.object(agent, "run_agent") as fell_through:
            answer = agent.run_workflow_turn(WORKFLOW["name"], "Log this place")
        fell_through.assert_not_called()
        self.assertEqual(answer["workflow"], WORKFLOW["name"])

    def test_the_features_question_reaches_the_widget_as_multi_select(self):
        conversation = {"workflow": WORKFLOW["name"],
                        "state": _state(STAGE_NAME)}
        with mock.patch.object(agent, "log_decision"), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError(
                                   "fell through to the agent")):
            answer = agent.run_workflow_turn(WORKFLOW["name"], "Mall", conversation=conversation)
        self.assertTrue(answer["choose_many"])
        self.assertEqual(answer["choices"], LABELS)

    def test_the_place_form_reaches_the_widget(self):
        conversation = {"workflow": WORKFLOW["name"],
                        "state": _state(STAGE_CONFIRMING, name="Mall")}
        with mock.patch.object(agent, "log_decision"), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError(
                                   "fell through to the agent")):
            answer = agent.run_workflow_turn(WORKFLOW["name"], "yes", conversation=conversation)
        self.assertEqual(answer["place_form"]["name"], "Mall")

    def test_it_can_be_left_like_any_workflow(self):
        conversation = {"workflow": WORKFLOW["name"], "state": _state(STAGE_NAME)}
        with mock.patch.object(agent, "log_decision"), \
             mock.patch.object(agent, "run_agent",
                               side_effect=AssertionError(
                                   "fell through to the agent")):
            answer = agent.run_workflow_turn(WORKFLOW["name"], "never mind", conversation=conversation)
        self.assertIsNone(answer["conversation"])


if __name__ == "__main__":
    unittest.main()
