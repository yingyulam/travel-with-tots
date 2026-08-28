import json
import unittest
from unittest import mock

import requests

from src.components.extract_form import (
    EXTRACTED_FORM_PROPERTIES,
    EXTRACTED_FORM_RESPONSE_FORMAT,
    FormExtractionError,
    extract_form,
)
from src.form_helpers import (
    ASSUMED_NAP_DURATION_MIN,
    DEFAULTS,
    MAX_AGE_YEARS,
    MAX_NAPS,
    STOP_COUNT_FORM_MAX,
)

EXTRACTABLE_FIELDS = (
    "wake_up", "bedtime", "age_years", "age_months", "destination",
    "accommodation", "stop_count", "preferred_lunch_time", "strict_schedule",
    "transit", "dining", "transit_nap", "features", "themes", "naps",
    "nap_notes", "extra_notes",
)


def _reply(**overrides):
    """A model reply with everything null but the given fields, which is the
    shape strict mode forces: every key present, absence expressed as null."""
    body = {field: None for field in EXTRACTABLE_FIELDS}
    body.update(overrides)
    return json.dumps(body)


# What the parent is taken to have said. The fake reply stands in for the
# model, so the description has to be able to account for it: _grounded drops
# a value the description cannot support, and a fixture claiming a nap time
# from the words "a description" is exactly the fabrication it exists to
# catch. Tests about grounding itself pass their own description.
_SAID = ("Vancouver, Kitsilano, 2 years old, up at 7, bed at 19:00, naps at "
         "13:00 for 45 minutes, 3 stops, lunch at 12:00, a light sleeper in "
         "the stroller, hates loud crowded places")


def _run(reply, elapsed=1.0, description=_SAID):
    """Fake only the OpenRouter boundary. The real schema, the real read_form,
    the real grounding and the real option lists all run."""
    with mock.patch("src.components.extract_form.call_openrouter",
                    return_value=(reply, {}, elapsed)) as call:
        return extract_form(description), call


class ExtractFormTest(unittest.TestCase):
    def test_uses_the_strict_schema(self):
        _, call = _run(_reply(destination="Vancouver"))
        self.assertIs(call.call_args[0][2], EXTRACTED_FORM_RESPONSE_FORMAT)

    def test_populates_what_the_description_supplied(self):
        result, _ = _run(_reply(
            wake_up="07:30", bedtime="19:00", destination="Kitsilano",
            transit=["stroller"], dining="on_the_go", themes=["Outdoorsy"]))
        form = result["form"]
        self.assertEqual(form["wake_up"], "07:30")
        self.assertEqual(form["bedtime"], "19:00")
        self.assertEqual(form["destination"], "Kitsilano")
        self.assertEqual(form["transit"], ["stroller"])
        self.assertEqual(form["dining"], "on_the_go")
        self.assertEqual(form["themes"], ["Outdoorsy"])

    def test_reports_only_the_fields_actually_supplied(self):
        result, _ = _run(_reply(destination="Vancouver", nap_notes="light sleeper"))
        self.assertEqual(result["found"], ["destination", "nap_notes"])

    def test_unmentioned_fields_fall_back_to_defaults(self):
        result, _ = _run(_reply(destination="Vancouver"))
        form = result["form"]
        for field in ("wake_up", "bedtime", "stop_count", "dining"):
            with self.subTest(field=field):
                self.assertEqual(form[field], DEFAULTS[field])
                self.assertNotIn(field, result["found"])

    def test_out_of_range_values_are_clamped_by_the_real_validator(self):
        # The point of routing through read_form rather than reimplementing
        # limits: a model emitting nonsense degrades to something sane.
        result, _ = _run(_reply(age_years=99, age_months=40, stop_count=40))
        form = result["form"]
        self.assertEqual(form["age_years"], str(MAX_AGE_YEARS))
        self.assertEqual(form["age_months"], "0")  # capped years force 0 months
        self.assertEqual(form["stop_count"], str(STOP_COUNT_FORM_MAX))

    def test_naps_are_flattened_into_the_form_shape(self):
        result, _ = _run(_reply(naps=[
            {"start": "13:00", "duration_min": 90},
            {"start": "16:00", "duration_min": 30},
        ]))
        self.assertEqual(result["form"]["naps"], [
            {"start": "13:00", "duration_min": 90},
            {"start": "16:00", "duration_min": 30},
        ])

    def test_an_unstated_duration_becomes_the_assumed_one(self):
        # The schema used to require duration_min as a plain integer, so the
        # model had to invent a number to answer at all: 15 minutes one run
        # and an hour the next, from a description that gave neither. Null now
        # means "they didn't say", and the assumed length is applied here
        # rather than guessed by the model.
        result, _ = _run(_reply(naps=[{"start": "13:00", "duration_min": None}]))
        self.assertEqual(result["form"]["naps"],
                         [{"start": "13:00", "duration_min": ASSUMED_NAP_DURATION_MIN}])

    def test_a_stated_duration_still_wins(self):
        result, _ = _run(_reply(naps=[{"start": "13:00", "duration_min": 90}]))
        self.assertEqual(result["form"]["naps"][0]["duration_min"], 90)

    def test_a_nap_is_still_reported_as_found_without_a_duration(self):
        # The parent did mention a nap, so it must not read as a default.
        result, _ = _run(_reply(naps=[{"start": "13:00", "duration_min": None}]))
        self.assertIn("naps", result["found"])

    def test_the_schema_lets_the_model_say_it_does_not_know(self):
        duration = (EXTRACTED_FORM_PROPERTIES["naps"]["items"]
                    ["properties"]["duration_min"])
        self.assertIn("null", duration["type"])

    def test_too_many_naps_are_capped(self):
        many = [{"start": f"{9 + i}:00", "duration_min": 30}
                for i in range(MAX_NAPS + 3)]
        result, _ = _run(_reply(naps=many))
        self.assertEqual(len(result["form"]["naps"]), MAX_NAPS)

    def test_strict_schedule_becomes_a_real_boolean(self):
        on, _ = _run(_reply(strict_schedule=True))
        self.assertIs(on["form"]["strict_schedule"], True)
        off, _ = _run(_reply(strict_schedule=False))
        self.assertIs(off["form"]["strict_schedule"], False)
        self.assertNotIn("strict_schedule", off["found"])

    def test_reports_the_model_and_timing(self):
        result, _ = _run(_reply(destination="Vancouver"), elapsed=2.345)
        self.assertEqual(result["response_time"], 2.345)
        self.assertTrue(result["model"])

    def test_unusable_reply_raises_rather_than_returning_junk(self):
        with self.assertRaises(FormExtractionError):
            _run("not json at all")
        with self.assertRaises(FormExtractionError):
            _run(json.dumps(["a", "list", "not", "an", "object"]))

    def test_transport_errors_are_not_swallowed(self):
        # The route turns this into a 502; the component must not hide it.
        with mock.patch("src.components.extract_form.call_openrouter",
                        side_effect=requests.exceptions.RequestException("down")):
            with self.assertRaises(requests.exceptions.RequestException):
                extract_form("a description")


class ExtractionRegressionTest(unittest.TestCase):
    """Cases from a real reported failure. The prompt is what actually fixes
    these, so these assert the component carries the model's answer through
    intact rather than dropping it on the way to the form."""

    def test_a_nap_with_a_start_but_no_duration_survives(self):
        # The reported bug: "naps at around 1:30 pm" was dropped entirely
        # because the prompt demanded a duration too. read_form defaults the
        # duration, so a start alone is a perfectly usable nap.
        result, _ = _run(_reply(naps=[{"start": "13:30", "duration_min": 0}]))
        naps = result["form"]["naps"]
        self.assertEqual(len(naps), 1)
        self.assertEqual(naps[0]["start"], "13:30")
        self.assertGreater(naps[0]["duration_min"], 0)  # defaulted, not dropped
        self.assertIn("naps", result["found"])

    def test_several_themes_are_kept(self):
        # "a park and a museum" is two themes, not one.
        result, _ = _run(_reply(themes=["Outdoorsy", "Culture"]))
        self.assertEqual(result["form"]["themes"], ["Outdoorsy", "Culture"])

    def test_a_city_destination_reaches_real_venues(self):
        # The quiet one: a destination the venue table cannot match leaves the
        # AI adjuster with nothing to swap in and Find Nearby with no curated
        # venues. Only a bare city name matches.
        from src import db
        result, _ = _run(_reply(destination="Vancouver"))
        self.assertTrue(db.get_candidate_venues(
            result["form"]["destination"], age_months=18))
        self.assertFalse(db.get_candidate_venues(
            "downtown Vancouver", age_months=18))

    def test_the_prompt_states_the_rules_these_failures_needed(self):
        # Cheap guard: the fixes live in the prompt, so a future edit that
        # drops the guidance would silently reintroduce the bugs above.
        with open("src/prompts/extract_form.txt") as f:
            prompt = f.read()
        # Stronger than "optional": the schema now allows null, and the
        # instruction has to forbid picking a number, because a model that
        # invents one produces a plausible wrong nap rather than a blank.
        self.assertIn("never a number you picked yourself", prompt)
        self.assertIn("only the city", prompt)
        self.assertIn("a park is Outdoorsy", prompt)
        # Known unfixed: rule 1 ("nothing may be dropped") beats rule 2 in
        # practice, so a clause already captured by stop_count, themes and
        # features is copied into extra_notes as well. Rewording rule 2 to
        # quote the offending sentence made it worse, not better: the model
        # echoed the quoted string back and started duplicating transit too.
        self.assertIn("Do not repeat what a structured field already holds", prompt)


class VocabularyGuardTest(unittest.TestCase):
    """The schema constrains the choice fields, but strict-mode support for a
    nullable enum varies by provider and read_form does not validate these
    either, so the component drops out-of-vocabulary values itself."""

    def test_invented_single_choice_is_dropped(self):
        result, _ = _run(_reply(dining="michelin_starred", transit_nap="maybe"))
        self.assertEqual(result["form"]["dining"], DEFAULTS["dining"])
        self.assertEqual(result["form"]["transit_nap"], DEFAULTS["transit_nap"])
        self.assertNotIn("dining", result["found"])
        self.assertNotIn("transit_nap", result["found"])

    def test_invented_list_values_are_dropped_but_valid_ones_kept(self):
        result, _ = _run(_reply(
            transit=["stroller", "helicopter"],
            themes=["Outdoorsy", "Extreme Sports"]))
        self.assertEqual(result["form"]["transit"], ["stroller"])
        self.assertEqual(result["form"]["themes"], ["Outdoorsy"])

    def test_a_wholly_invented_list_is_not_reported_as_found(self):
        result, _ = _run(_reply(transit=["helicopter", "submarine"]))
        self.assertEqual(result["form"]["transit"], [])
        self.assertNotIn("transit", result["found"])


class FreeTextRoutingTest(unittest.TestCase):
    """The requirement most likely to regress quietly: a parent's own words
    must survive into the form, and must not be duplicated once a structured
    field already holds them."""

    def test_prose_no_field_can_hold_survives_in_extra_notes(self):
        result, _ = _run(_reply(
            destination="Vancouver",
            extra_notes="she hates loud crowded places"))
        self.assertIn("loud crowded places", result["form"]["extra_notes"])

    def test_sleep_prose_survives_in_nap_notes(self):
        result, _ = _run(_reply(
            naps=[{"start": "13:00", "duration_min": 90}],
            nap_notes="wakes if moved out of the stroller"))
        self.assertIn("stroller", result["form"]["nap_notes"])
        self.assertEqual(len(result["form"]["naps"]), 1)

    def test_structurally_captured_prose_is_not_duplicated(self):
        # The prompt tells the model not to restate a structured field in free
        # text. If it obeys, transit holds the stroller and extra_notes does
        # not mention it, so the planner reads the constraint once.
        result, _ = _run(_reply(
            transit=["stroller"], extra_notes="no long walks please"))
        self.assertEqual(result["form"]["transit"], ["stroller"])
        self.assertNotIn("stroller", result["form"]["extra_notes"])

    def test_free_text_fields_reach_the_planner_contract(self):
        # Both fields are rendered into plan_adjust.txt, so anything routed
        # here is acted on rather than merely stored.
        with open("src/prompts/plan_adjust.txt") as f:
            prompt = f.read()
        self.assertIn("{nap_notes}", prompt)
        self.assertIn("{extra_notes}", prompt)


class ExtractedFormDrivesThePlannerTest(unittest.TestCase):
    """The real contract: the extracted form has to be usable as a form. A
    shape mismatch here would pass every per-field assertion above."""

    def test_plan_trip_accepts_an_extracted_form(self):
        from src.agents import PlanningAgent, PlanningAgentError
        from src.components.plan_trip import plan_trip

        result, _ = _run(_reply(
            destination="Vancouver", wake_up="07:30", bedtime="19:30",
            stop_count=3, dining="dine_out", transit=["stroller"],
            naps=[{"start": "13:00", "duration_min": 90}],
            nap_notes="light sleeper", extra_notes="no long walks"))
        form = result["form"]
        age_months = int(form["age_years"]) * 12 + int(form["age_months"])

        with mock.patch.object(PlanningAgent, "adjust_plan",
                               side_effect=PlanningAgentError("skip the AI step")):
            plan = plan_trip(
                destination=form["destination"], age_months=age_months,
                wake_up=form["wake_up"], bedtime=form["bedtime"],
                stop_count=int(form["stop_count"]), dining=form["dining"],
                features=form["features"], naps=form["naps"],
                nap_notes=form["nap_notes"], extra_notes=form["extra_notes"],
                transit=form["transit"], themes=form["themes"])
        self.assertTrue(plan["stops"])


class ExtractFormRouteTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.admin = {"id": 1, "is_admin": True, "name": "Admin", "email": "a@b.com"}

    def test_page_renders_for_an_admin(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            resp = self.client.get("/extract-form")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Form Extractor", resp.get_data(as_text=True))

    def test_page_is_admin_only(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=None):
            self.assertEqual(self.client.get("/extract-form").status_code, 302)
        parent = {**self.admin, "is_admin": False}
        with mock.patch.object(self.app_module, "_current_parent", return_value=parent):
            self.assertEqual(self.client.get("/extract-form").status_code, 302)

    def test_empty_description_is_rejected(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            resp = self.client.post("/extract-form/run", json={"description": "   "})
        self.assertEqual(resp.status_code, 400)

    def test_run_returns_the_form_and_what_was_found(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin), \
             mock.patch("src.components.extract_form.call_openrouter",
                        return_value=(_reply(destination="Kitsilano"), {}, 1.0)):
            resp = self.client.post("/extract-form/run",
                                    json={"description": "a day in Kitsilano"})
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertEqual(body["form"]["destination"], "Kitsilano")
        self.assertEqual(body["found"], ["destination"])

    def test_missing_api_key_is_a_clean_500(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin), \
             mock.patch("src.components.extract_form.call_openrouter",
                        side_effect=KeyError("OPENROUTER_API_KEY")):
            resp = self.client.post("/extract-form/run", json={"description": "x"})
        self.assertEqual(resp.status_code, 500)

    def test_unusable_reply_is_a_clean_502(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin), \
             mock.patch("src.components.extract_form.call_openrouter",
                        return_value=("not json", {}, 1.0)):
            resp = self.client.post("/extract-form/run", json={"description": "x"})
        self.assertEqual(resp.status_code, 502)


if __name__ == "__main__":
    unittest.main()


class GroundingTest(unittest.TestCase):
    """A value the description cannot support is a fabrication, not an
    extraction, and must not be reported as something the parent supplied.

    Measured before this existed: asked to fill the form from the three words
    "Plan a trip", the pinned model returned up to ten non-null fields across
    repeated runs, every one lifted from an example in the prompt.
    """

    def test_a_bare_intent_supplies_nothing(self):
        # The reported bug, at the extractor's own boundary. Everything the
        # model claims here is invented, and all of it used to be reported as
        # the parent's own words.
        result, _ = _run(_reply(
            destination="Vancouver", age_years=1, age_months=6,
            wake_up="07:00", bedtime="20:00",
            naps=[{"start": "13:30", "duration_min": None}],
            accommodation="our hotel in downtown Vancouver"),
            description="Plan a trip")
        self.assertEqual(result["found"], [])

    def test_the_form_still_comes_back_whole(self):
        # Grounding drops claims, never the form: read_form's defaults are
        # what the planner runs on.
        result, _ = _run(_reply(wake_up="05:00"), description="Plan a trip")
        self.assertEqual(result["form"]["wake_up"], DEFAULTS["wake_up"])
        self.assertEqual(set(result["form"]), set(DEFAULTS))

    def test_a_time_needs_a_number_somewhere(self):
        ungrounded, _ = _run(_reply(bedtime="19:30"),
                             description="she goes to bed late")
        self.assertEqual(ungrounded["found"], [])
        grounded, _ = _run(_reply(bedtime="19:30"),
                           description="she goes to bed at 7:30")
        self.assertEqual(grounded["form"]["bedtime"], "19:30")

    def test_a_number_word_counts_as_a_number(self):
        # "up at seven" is a time, and a digit test alone would lose it.
        result, _ = _run(_reply(wake_up="07:00"),
                         description="we're up at seven")
        self.assertEqual(result["form"]["wake_up"], "07:00")

    def test_a_number_word_must_be_a_whole_word(self):
        # "one" inside "money" is not a number, so tokens are compared rather
        # than substrings.
        result, _ = _run(_reply(stop_count=3),
                         description="somewhere that doesn't cost money")
        self.assertEqual(result["found"], [])

    def test_a_nap_is_grounded_on_sleep_not_on_a_clock(self):
        # The prompt deliberately allows a nap with no stated time, so naps
        # cannot be grounded the way the other times are.
        result, _ = _run(_reply(naps=[{"start": "13:00", "duration_min": 90}]),
                         description="she naps after lunch")
        self.assertEqual(result["form"]["naps"][0]["start"], "13:00")

    def test_a_nap_nobody_mentioned_is_dropped(self):
        result, _ = _run(_reply(naps=[{"start": "13:00", "duration_min": 60}]),
                         description="a day out downtown")
        self.assertEqual(result["found"], [])
        self.assertEqual(result["form"]["naps"], [])

    def test_a_city_has_to_be_named(self):
        result, _ = _run(_reply(destination="Vancouver"),
                         description="we're staying in Kitsilano")
        self.assertEqual(result["found"], [])
        # The value is still the one the app plans in; only the claim that the
        # parent chose it is gone.
        self.assertEqual(result["form"]["destination"], DEFAULTS["destination"])

    def test_a_named_city_survives(self):
        result, _ = _run(_reply(destination="Vancouver"),
                         description="we'll be in vancouver in July")
        self.assertEqual(result["found"], ["destination"])

    def test_invented_free_text_is_dropped(self):
        result, _ = _run(_reply(extra_notes="she loves the aquarium"),
                         description="plan me a day")
        self.assertEqual(result["found"], [])

    def test_free_text_in_the_parents_own_words_survives(self):
        result, _ = _run(_reply(extra_notes="hates loud places"),
                         description="she hates loud places")
        self.assertEqual(result["form"]["extra_notes"], "hates loud places")

    def test_the_vocabulary_fields_are_deliberately_not_grounded(self):
        # "we'll drive" is legitimately car without sharing a word with it, so
        # there is nothing to compare these against. The prompt is what keeps
        # them honest. Asserted so nobody "fixes" this into dropping them.
        result, _ = _run(_reply(transit=["car"], transit_nap="yes",
                                themes=["Outdoorsy"]),
                         description="we'll drive and he sleeps in the buggy")
        self.assertEqual(result["form"]["transit"], ["car"])
        self.assertEqual(result["form"]["transit_nap"], "yes")
        self.assertEqual(result["form"]["themes"], ["Outdoorsy"])
