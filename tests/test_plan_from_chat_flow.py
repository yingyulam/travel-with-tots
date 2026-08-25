import unittest
from unittest import mock

from src import agent
from src.form_helpers import DEFAULTS
from src.data_loader import SUPPORTED_CITIES
from src.intent import matches_only
from src.workflows import plan_from_chat
from src.workflows.plan_from_chat import (
    CONFIRM_CHOICE,
    EXTRAS_QUESTION,
    NOTHING_CHOICE,
    OPENING_QUESTION,
    QUESTION_CHOICES,
    QUESTIONS,
    REQUIRED,
    STAGE_COLLECTING,
    STAGE_CONFIRMING,
    STAGE_EXTRAS,
    STAGE_OFFERED,
    _NOTHING,
    _YES,
    run,
)

# Everything the four required fields need, in one message.
ALL_REQUIRED = dict(destination="Vancouver", age_years="2", wake_up="07:00",
                    bedtime="19:30", naps=[{"start": "13:00", "duration_min": 60}])

WORKFLOW_NAME = "Fill the form from a chat message"


def _extraction(**supplied):
    """What extract_form returns: a complete form, plus the fields this
    particular message actually supplied."""
    form = dict(DEFAULTS)
    form.update(supplied)
    return {"form": form, "found": sorted(supplied), "model": "m",
            "response_time": 1.0}


def _turn(message, state, **supplied):
    with mock.patch.object(plan_from_chat, "extract_form",
                           return_value=_extraction(**supplied)):
        return run(message, state)


class OfferTest(unittest.TestCase):
    def test_a_bare_intent_offers_the_two_ways(self):
        # This replaces a test that asserted the extractor was *not* called on
        # the first message, on the reasoning that an opening message is only
        # ever an intent. It is not: a parent who opens with their whole day
        # had all of it thrown away. The extractor runs on this turn now, and
        # what it finds decides the reply.
        result = _turn("I want to plan a day", None)
        self.assertEqual(result["state"]["stage"], STAGE_OFFERED)
        self.assertEqual(len(result["choices"]), 2)

    def test_a_first_message_describing_the_day_is_not_thrown_away(self):
        # The reported bug, stated directly.
        result = _turn("Vancouver, she's 2, up at 7, bed at 7:30, naps at 1",
                       None, destination="Vancouver", age_years="2",
                       wake_up="07:00", bedtime="19:30",
                       naps=[{"start": "13:00", "duration_min": 60}])
        self.assertEqual(result["state"]["form"]["destination"], "Vancouver")
        self.assertEqual(result["reply"], EXTRAS_QUESTION)

    def test_a_partial_first_message_asks_only_for_the_rest(self):
        result = _turn("plan a day in Vancouver", None, destination="Vancouver")
        self.assertEqual(result["state"]["form"]["destination"], "Vancouver")
        self.assertEqual(result["reply"], QUESTIONS["age"])
        # The offer is skipped: describing a day is choosing chat by doing it.
        self.assertNotEqual(result["state"]["stage"], STAGE_OFFERED)

    def test_a_failing_extractor_on_the_first_turn_still_offers_the_choice(self):
        # An unreachable model must degrade to the old behaviour, not a dead end.
        with mock.patch.object(plan_from_chat, "extract_form",
                               side_effect=RuntimeError("model down")):
            result = run("I want to plan a day")
        self.assertEqual(result["state"]["stage"], STAGE_OFFERED)

    def test_choosing_the_form_ends_the_flow(self):
        result = run("fill out the form myself", {"stage": STAGE_OFFERED})
        self.assertIsNone(result["state"])
        self.assertTrue(result["open_form"])

    def test_choosing_chat_starts_collecting_by_asking(self):
        result = run("plan through chat", {"stage": STAGE_OFFERED})
        self.assertEqual(result["state"]["stage"], STAGE_COLLECTING)
        self.assertIn("city", result["reply"].lower())

    def test_you_do_it_is_not_read_as_the_form(self):
        # "yourself" contains "you", so matching on the chat words first sent
        # this to the form. The form words are tested for instead.
        result = run("you do it for me", {"stage": STAGE_OFFERED})
        self.assertEqual(result["state"]["stage"], STAGE_COLLECTING)

    def test_fill_it_out_yourself_is_read_as_the_form(self):
        result = run("I'll fill it out myself", {"stage": STAGE_OFFERED})
        self.assertTrue(result["open_form"])


class KeepsPromptingTest(unittest.TestCase):
    """The point of the flow: the extractor runs on every message, and the
    assistant keeps asking until it has what it needs."""

    def test_a_partial_answer_asks_for_the_next_thing(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        result = _turn("Vancouver", state, destination="Vancouver")
        self.assertEqual(result["state"]["stage"], STAGE_COLLECTING)
        self.assertIn("old", result["reply"].lower())  # asks for the age next

    def test_answering_one_at_a_time_reaches_the_extras_question(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("Vancouver", state, destination="Vancouver")["state"]
        state = _turn("she's two", state, age_years="2")["state"]
        state = _turn("up at 7, bed at 7:30", state,
                      wake_up="07:00", bedtime="19:30")["state"]
        result = _turn("1pm for an hour", state,
                       naps=[{"start": "13:00", "duration_min": 60}])
        self.assertEqual(result["state"]["stage"], STAGE_EXTRAS)
        self.assertEqual(result["reply"], EXTRAS_QUESTION)

    def test_the_extras_answer_leads_to_the_confirmation(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("everything", state, **ALL_REQUIRED)["state"]
        result = _turn("she hates crowds", state,
                       extra_notes="She hates crowds.")
        self.assertEqual(result["state"]["stage"], STAGE_CONFIRMING)
        self.assertIn("She hates crowds.", result["state"]["form"]["extra_notes"])

    def test_the_extractor_runs_on_every_collecting_turn(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        with mock.patch.object(plan_from_chat, "extract_form",
                               return_value=_extraction(destination="Vancouver")) as extract:
            state = run("Vancouver", state)["state"]
            run("anything else", state)
        self.assertEqual(extract.call_count, 2)

    def test_a_failed_extraction_costs_one_turn_not_the_conversation(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS),
                 "found": ["destination"]}
        with mock.patch.object(plan_from_chat, "extract_form",
                               side_effect=RuntimeError("model down")):
            result = run("she's two", state)
        self.assertEqual(result["state"]["stage"], STAGE_COLLECTING)
        self.assertEqual(result["state"]["found"], ["destination"])


class MergeTest(unittest.TestCase):
    """Each extraction returns a complete form, so a plain update would let a
    later turn reset an earlier turn's answers back to their defaults."""

    def test_a_later_turn_does_not_reset_an_earlier_one(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("Kitsilano", state, destination="Kitsilano")["state"]
        state = _turn("she's two", state, age_years="2")["state"]
        self.assertEqual(state["form"]["destination"], "Kitsilano")
        self.assertIn("destination", state["found"])

    def test_only_supplied_fields_overwrite(self):
        state = {"stage": STAGE_COLLECTING,
                 "form": {**DEFAULTS, "destination": "Kitsilano"},
                 "found": ["destination"]}
        # The extraction carries a full form whose destination is the default.
        state = _turn("she's two", state, age_years="2")["state"]
        self.assertEqual(state["form"]["destination"], "Kitsilano")

    def test_notes_accumulate_rather_than_replacing(self):
        # The extractor sees one message at a time, so an earlier note it knows
        # nothing about used to be overwritten by the next thing said.
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("she hates crowds", state,
                      extra_notes="She hates loud crowded places.")["state"]
        state = _turn("and a highchair", state,
                      extra_notes="She needs a highchair wherever we eat.")["state"]
        self.assertEqual(
            state["form"]["extra_notes"],
            "She hates loud crowded places. She needs a highchair wherever we eat.")

    def test_both_note_fields_accumulate(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("a", state, nap_notes="Naps badly in a stroller.")["state"]
        state = _turn("b", state, nap_notes="She's teething.")["state"]
        self.assertEqual(state["form"]["nap_notes"],
                         "Naps badly in a stroller. She's teething.")

    def test_saying_the_same_thing_twice_does_not_double_it(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("a", state, extra_notes="She hates crowds.")["state"]
        state = _turn("a again", state, extra_notes="She hates crowds.")["state"]
        self.assertEqual(state["form"]["extra_notes"], "She hates crowds.")

    def test_only_notes_accumulate(self):
        # The regression this guards: destination must correct, not concatenate
        # into "Kitsilano Burnaby".
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("Kitsilano", state, destination="Kitsilano")["state"]
        state = _turn("actually Burnaby", state, destination="Burnaby")["state"]
        self.assertEqual(state["form"]["destination"], "Burnaby")

    def test_a_correction_replaces_the_earlier_value(self):
        state = {"stage": STAGE_COLLECTING,
                 "form": {**DEFAULTS, "destination": "Kitsilano"},
                 "found": ["destination"]}
        state = _turn("actually Burnaby", state, destination="Burnaby")["state"]
        self.assertEqual(state["form"]["destination"], "Burnaby")


class ConfirmTest(unittest.TestCase):
    def setUp(self):
        self.state = {
            "stage": STAGE_CONFIRMING,
            "form": {**DEFAULTS, "destination": "Vancouver"},
            "found": ["bedtime", "destination", "age_years", "wake_up"],
        }

    def test_yes_hands_the_form_over_and_ends_the_flow(self):
        result = run("yes", self.state)
        self.assertIsNone(result["state"])
        self.assertEqual(result["form"]["destination"], "Vancouver")

    def test_anything_else_is_a_correction_not_a_refusal(self):
        result = _turn("make it four stops", self.state, stop_count="4")
        self.assertIsNotNone(result["state"])
        self.assertEqual(result["state"]["form"]["stop_count"], "4")

    def test_the_summary_shows_both_theirs_and_the_defaults(self):
        # Explicitly required: nothing should reach the planner unseen.
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": [],
                 "asked_extras": True}
        reply = _turn("all of it", state, **ALL_REQUIRED)["reply"]
        self.assertIn("From what you told me", reply)
        self.assertIn("Using defaults", reply)
        self.assertIn("dining", reply)          # a default, with its value
        self.assertIn("dine_out", reply)

    def test_nothing_is_handed_over_before_confirmation(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        result = _turn("Vancouver", state, destination="Vancouver")
        self.assertIsNone(result.get("form"))


class OfferedButtonsWorkTest(unittest.TestCase):
    """The widget sends a button's own label back as the message, so a label
    this module cannot parse is a button that does nothing. That is exactly
    what "Yes, that's right" was: _YES held "yes" and "that's right"
    separately, so clicking it re-showed the same summary forever.
    """

    def _stages(self):
        """One turn per stage that offers buttons, with the state that made it."""
        collecting = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS),
                      "found": [], "skipped": [], "asked_extras": False}
        extras = _turn("everything", collecting, **ALL_REQUIRED)
        confirming = _turn("she hates crowds", extras["state"],
                           extra_notes="She hates crowds.")
        # The opening question is left out on purpose: it is open-ended, so
        # there is no short fixed answer a button could carry. The city
        # follow-up it leads to when nothing was supplied does have one.
        opener = run("plan through chat", {"stage": STAGE_OFFERED})
        city = _turn("hello", opener["state"])
        return [
            # Mocked: the opening turn reads the message now, and this one
            # supplies nothing, so it lands on the two-ways offer. Unmocked it
            # would be a real model call from a unit test.
            _turn("I want to plan a day", None),
            city,                               # which city?
            extras,                             # anything else?
            confirming,                         # yes, that's right
        ]

    def test_every_offered_button_moves_the_conversation_on(self):
        # Walked stage by stage rather than asserted per literal, so a button
        # added later is covered without anyone remembering to add a test.
        for offer in self._stages():
            for choice in offer.get("choices", []):
                with self.subTest(stage=offer["state"]["stage"], choice=choice):
                    # A chip that answers a field is worth what the extractor
                    # would read out of it, which for "Vancouver" is the city.
                    asking = offer["state"].get("asking")
                    supplied = {asking: choice} if asking in QUESTION_CHOICES else {}
                    after = _turn(choice, offer["state"], **supplied)
                    # "Where they were" is the question, not just the stage:
                    # four of the five questions share the collecting stage, so
                    # a stage comparison would call a repeat of the same
                    # question progress.
                    was = (offer["state"]["stage"], asking)
                    now = (after["state"] and
                           (after["state"]["stage"], after["state"].get("asking")))
                    self.assertNotEqual(now, was,
                                        f"{choice!r} left the parent where they were")

    def test_every_stage_that_asks_something_offers_a_button(self):
        # The city, "anything else" and the confirmation all have one obvious
        # answer, and typing it out is work the parent should not have to do.
        for offer in self._stages():
            with self.subTest(stage=offer["state"]["stage"]):
                self.assertTrue(offer.get("choices"))

    def test_the_confirmation_button_hands_the_form_over(self):
        state = {"stage": STAGE_CONFIRMING,
                 "form": {**DEFAULTS, "destination": "Vancouver"},
                 "found": ["destination"]}
        result = run(CONFIRM_CHOICE, state)
        self.assertIsNone(result["state"])
        self.assertEqual(result["form"]["destination"], "Vancouver")


class IsYesTest(unittest.TestCase):
    def test_whole_message_affirmations_are_accepted(self):
        for message in ("yes", "Yes!", "Yes, that's right", "yes please",
                        "sure, go ahead", "perfect, thanks", "looks good"):
            with self.subTest(message=message):
                self.assertTrue(matches_only(message, _YES))

    def test_a_yes_with_a_change_attached_is_not_consent(self):
        # The dangerous half: accepting these would hand over a form the
        # parent had just asked to change.
        for message in ("yes but make it four stops", "yes, make it four stops",
                        "ok, we're in Burnaby actually", "no", "change the bedtime"):
            with self.subTest(message=message):
                self.assertFalse(matches_only(message, _YES))


class RequiredFieldsTest(unittest.TestCase):
    def test_age_counts_as_answered_from_either_half(self):
        # Age is two form fields but one question.
        state = {"stage": STAGE_COLLECTING,
                 "form": {**DEFAULTS, "destination": "V", "wake_up": "07:00",
                          "bedtime": "19:30"},
                 "found": ["bedtime", "destination", "naps", "wake_up"]}
        result = _turn("18 months", state, age_months="6")
        self.assertEqual(result["state"]["stage"], STAGE_EXTRAS)

    def test_the_required_list_is_what_shapes_a_day(self):
        self.assertEqual(REQUIRED,
                         ("destination", "age", "wake_up", "bedtime", "naps"))


class TheQuestionsTest(unittest.TestCase):
    """What the flow actually asks, and in what order."""

    def _ask_all(self):
        """Every follow-up question, in order, by answering the opener with
        nothing and then supplying one field at a time."""
        state = run("plan through chat", {"stage": STAGE_OFFERED})["state"]
        turn = _turn("not sure yet", state)
        asked = [turn["reply"]]
        state = turn["state"]
        for supplied in ({"destination": "Vancouver"}, {"age_years": "2"},
                         {"wake_up": "07:00"}, {"bedtime": "19:30"},
                         {"naps": [{"start": "13:00", "duration_min": 60}]}):
            turn = _turn("answer", state, **supplied)
            asked.append(turn["reply"])
            state = turn["state"]
        return asked

    def test_it_opens_by_asking_for_everything_at_once(self):
        # One open question rather than the first of five: answering field by
        # field is the form again, only slower.
        first = run("plan through chat", {"stage": STAGE_OFFERED})
        self.assertEqual(first["reply"], OPENING_QUESTION)
        for asked_about in ("city", "old", "starts", "bedtime", "nap"):
            with self.subTest(asked_about=asked_about):
                self.assertIn(asked_about, first["reply"].lower())
        # Open-ended, so no button could carry the answer.
        self.assertIsNone(first.get("choices"))

    def test_only_what_is_missing_is_followed_up(self):
        opener = run("plan through chat", {"stage": STAGE_OFFERED})["state"]
        # Everything but the nap, in one message.
        after = _turn("Vancouver, she's 2, up at 7 and bed at 7:30", opener,
                      destination="Vancouver", age_years="2",
                      wake_up="07:00", bedtime="19:30")
        self.assertEqual(after["reply"], QUESTIONS["naps"])

    def test_the_city_follow_up_names_visiting_and_offers_the_supported_city(self):
        opener = run("plan through chat", {"stage": STAGE_OFFERED})["state"]
        city = _turn("no idea yet", opener)
        self.assertEqual(city["reply"], "Which city are you visiting?")
        self.assertEqual(city["choices"], ["Vancouver"])

    def test_the_offered_cities_come_from_the_venue_data(self):
        # Not a literal: offering a city the app has no venues for would be a
        # promise it cannot keep.
        self.assertEqual(QUESTION_CHOICES["destination"], list(SUPPORTED_CITIES))

    def test_the_nap_question_asks_for_the_time_and_the_length(self):
        # One question for both, because a nap time with no length is half an
        # answer and the planner needs the pair.
        nap_question = QUESTIONS["naps"].lower()
        self.assertIn("when", nap_question)
        self.assertIn("how long", nap_question)

    def test_anything_else_is_asked_last(self):
        asked = self._ask_all()
        self.assertEqual(asked[-1], EXTRAS_QUESTION)
        self.assertEqual(asked[:-1], [QUESTIONS[field] for field in REQUIRED])

    def test_anything_else_is_asked_once(self):
        state = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS), "found": []}
        state = _turn("everything", state, **ALL_REQUIRED)["state"]
        self.assertEqual(state["stage"], STAGE_EXTRAS)
        # Correcting at the confirmation must not restart the questions.
        state = _turn("nothing", state)["state"]
        self.assertEqual(state["stage"], STAGE_CONFIRMING)
        again = _turn("make it four stops", state, stop_count="4")
        self.assertEqual(again["state"]["stage"], STAGE_CONFIRMING)

    def test_nothing_to_add_skips_the_extractor_and_confirms(self):
        state = {"stage": STAGE_EXTRAS, "form": dict(DEFAULTS),
                 "found": ["destination"], "asked_extras": True}
        with mock.patch.object(plan_from_chat, "extract_form") as extract:
            result = run(NOTHING_CHOICE, state)
        # Running it on "no" would only append that to the notes.
        extract.assert_not_called()
        self.assertEqual(result["state"]["stage"], STAGE_CONFIRMING)

    def test_something_to_add_is_extracted(self):
        state = {"stage": STAGE_EXTRAS, "form": dict(DEFAULTS),
                 "found": ["destination"], "asked_extras": True}
        result = _turn("she is scared of dogs", state,
                       extra_notes="She is scared of dogs.")
        self.assertEqual(result["state"]["form"]["extra_notes"],
                         "She is scared of dogs.")


class AQuestionCanBeDeclinedTest(unittest.TestCase):
    """Without this, a question the parent cannot answer repeats forever: the
    extractor finds nothing, so the field stays missing, so it is asked again
    in the same words."""

    def _asking_naps(self):
        return {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS),
                "found": ["bedtime", "destination", "age_years", "wake_up"],
                "skipped": [], "asking": "naps", "asked_extras": False}

    def test_a_child_who_does_not_nap_can_move_on(self):
        result = _turn("she doesn't nap anymore", self._asking_naps())
        self.assertEqual(result["state"]["skipped"], ["naps"])
        self.assertEqual(result["state"]["stage"], STAGE_EXTRAS)

    def test_a_plain_no_moves_on_too(self):
        result = _turn("no", self._asking_naps())
        self.assertEqual(result["state"]["stage"], STAGE_EXTRAS)

    def test_declining_does_not_invent_an_answer(self):
        # Skipped is not found: the summary must still show naps as a default.
        result = _turn("no", self._asking_naps())
        self.assertNotIn("naps", result["state"]["found"])

    def test_the_ways_a_parent_says_there_is_no_nap(self):
        for message in ("she doesn't nap anymore", "he dropped his nap",
                        "no naps these days", "they stopped napping", "none"):
            with self.subTest(message=message):
                result = _turn(message, self._asking_naps())
                self.assertEqual(result["state"]["skipped"], ["naps"], message)

    def test_no_nap_wording_does_not_skip_a_different_question(self):
        # The phrase rule is the nap question's alone: "no" still works
        # everywhere, but "dropped the nap" is not an answer about a city.
        asking_city = {"stage": STAGE_COLLECTING, "form": dict(DEFAULTS),
                       "found": [], "skipped": [], "asking": "destination"}
        result = _turn("she dropped the nap", asking_city)
        self.assertEqual(result["state"]["skipped"], [])

    def test_a_real_answer_is_not_treated_as_a_refusal(self):
        result = _turn("1pm for an hour", self._asking_naps(),
                       naps=[{"start": "13:00", "duration_min": 60}])
        self.assertEqual(result["state"]["skipped"], [])
        self.assertIn("naps", result["state"]["found"])


class MidFlowRoutingTest(unittest.TestCase):
    """While a flow is running the classifier must not see the message: "yes"
    and "she's two" are answers, not intents."""

    def setUp(self):
        self.log = mock.patch.object(agent, "log_decision")
        self.log.start()

    def tearDown(self):
        self.log.stop()

    def test_mid_flow_skips_the_classifier_and_the_agent(self):
        conversation = {"workflow": WORKFLOW_NAME,
                        "state": {"stage": STAGE_COLLECTING,
                                  "form": dict(DEFAULTS), "found": []}}
        with mock.patch.object(agent, "classify_intent") as classify, \
             mock.patch.object(agent, "run_agent") as ran, \
             mock.patch.object(plan_from_chat, "extract_form",
                               return_value=_extraction(destination="Vancouver")):
            result = agent.handle_message("Vancouver", conversation=conversation)
        classify.assert_not_called()
        ran.assert_not_called()
        self.assertEqual(result["conversation"]["workflow"], WORKFLOW_NAME)

    def test_a_finished_flow_clears_the_conversation(self):
        conversation = {"workflow": WORKFLOW_NAME,
                        "state": {"stage": STAGE_CONFIRMING,
                                  "form": dict(DEFAULTS), "found": ["destination"]}}
        with mock.patch.object(agent, "classify_intent"):
            result = agent.handle_message("yes", conversation=conversation)
        self.assertIsNone(result["conversation"])
        self.assertIsNotNone(result["form"])

    def test_a_malformed_state_restarts_rather_than_crashing(self):
        # The widget echoes state back, so it is client-controlled. A non-dict
        # used to reach the workflow and raise an attribute error. The
        # extractor is mocked because restarting now reads the message, and a
        # test must not depend on a network call to prove it did not crash.
        conversation = {"workflow": WORKFLOW_NAME, "state": "not a dict"}
        with mock.patch.object(agent, "classify_intent"), \
             mock.patch.object(plan_from_chat, "extract_form",
                               return_value=_extraction(destination="Vancouver")):
            result = agent.handle_message("Vancouver", conversation=conversation)
        self.assertTrue(result["reply"])
        self.assertEqual(result["conversation"]["state"]["form"]["destination"],
                         "Vancouver")

    def test_an_unknown_workflow_name_falls_through_to_the_agent(self):
        with mock.patch.object(agent, "run_agent",
                               return_value={"reply": "hi"}) as ran:
            agent.handle_message("hello", conversation={"workflow": "nope"})
        ran.assert_called_once()

    def test_no_conversation_means_the_classifier_runs_as_before(self):
        with mock.patch.object(agent, "classify_intent",
                               return_value="none") as classify, \
             mock.patch.object(agent, "run_agent", return_value={"reply": "hi"}):
            agent.handle_message("how do I save a plan?")
        classify.assert_called_once()


class PrefillRouteTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()

    def test_prefill_fills_the_form_without_planning(self):
        with mock.patch.object(self.app_module, "plan_trip") as planned:
            resp = self.client.post("/plan", data={"prefill": "1",
                                                   "destination": "Burnaby",
                                                   "wake_up": "06:30"})
        planned.assert_not_called()
        html = resp.get_data(as_text=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Burnaby", html)
        self.assertIn("06:30", html)

    def test_the_handoff_shape_round_trips(self):
        # What the widget's hidden form posts: naps as parallel fields rather
        # than the array read_form returns, lists repeated, booleans as "on".
        posted = {"prefill": "1", "destination": "Burnaby",
                  "nap_start": "12:30", "nap_duration": "90",
                  "transit": ["bus", "stroller"],
                  "features": "kid_friendly", "strict_schedule": "on",
                  "stop_count": "4"}
        with mock.patch.object(self.app_module, "plan_trip") as planned:
            resp = self.client.post("/plan", data=posted)
        planned.assert_not_called()
        html = resp.get_data(as_text=True)
        for value in ("Burnaby", "12:30", "90", "bus", "stroller"):
            self.assertIn(value, html)

    def test_asking_for_a_day_still_generates_one(self):
        # The regression that matters in the other direction: the hand-off must
        # not have broken planning for the page's own form, which carries the
        # marker as a hidden field.
        plan = {"label": "L", "blurb": "b", "stops": [], "adjusted": True,
                "changed": True}
        with mock.patch.object(self.app_module, "plan_trip",
                               return_value=plan) as planned:
            resp = self.client.post("/plan", data={"destination": "Burnaby",
                                                   "generate": "1"})
        planned.assert_called_once()
        self.assertEqual(resp.status_code, 200)

    def test_a_post_that_asks_for_nothing_plans_nothing(self):
        # Generating is opt in, so a post that lost the marker fills the form
        # in rather than spending a minute on an AI call nobody asked for.
        # This is the direction that matters: the old flag meant the reverse,
        # and a lost name cost a plan.
        with mock.patch.object(self.app_module, "plan_trip") as planned:
            resp = self.client.post("/plan", data={"destination": "Burnaby"})
        planned.assert_not_called()
        self.assertEqual(resp.status_code, 200)



class SoftFieldsCannotSkipTheOfferTest(unittest.TestCase):
    """The reported bug: clicking "Plan a trip" jumped straight to a question
    instead of offering the two ways.

    The cause was not here. This guard was already correct: it skips the offer
    only when the message supplied a field the conversation asks about, and a
    theme or a transit mode alone does not count. What broke was the extractor,
    which claimed a destination, an age and a nap from the words "Plan a trip",
    all fabricated, all lifted from examples in its own prompt. See
    GroundingTest in test_components_extract_form.

    These pin the behaviour the fix relies on, so a later change here cannot
    quietly bring the symptom back.
    """

    def test_only_soft_fields_still_offers_the_two_ways(self):
        # What the model returns for a bare intent once the values it cannot
        # invent are grounded away: the vocabulary fields, which nothing can
        # ground, since "we'll drive" legitimately means car without sharing a
        # word with it.
        result = _turn("Plan a trip", None, themes=["Outdoorsy"],
                       transit=["car"], transit_nap="yes",
                       features=["kid_friendly"])
        self.assertEqual(result["state"]["stage"], STAGE_OFFERED)
        self.assertEqual(len(result["choices"]), 2)

    def test_a_described_day_still_skips_the_offer(self):
        result = _turn("Vancouver, she's 2", None,
                       destination="Vancouver", age_years="2")
        self.assertNotEqual(result["state"]["stage"], STAGE_OFFERED)

    def test_one_required_field_is_enough_to_skip_it(self):
        result = _turn("we're up at 7", None, wake_up="07:00")
        self.assertNotEqual(result["state"]["stage"], STAGE_OFFERED)

    def test_age_counts_even_though_it_is_two_fields(self):
        # age_years/age_months are form fields; "age" is the question _supplied
        # aliases them to. Getting that wrong would re-ask an age just given.
        result = _turn("she's 18 months", None, age_months="6", age_years="1")
        self.assertNotEqual(result["state"]["stage"], STAGE_OFFERED)


if __name__ == "__main__":
    unittest.main()
