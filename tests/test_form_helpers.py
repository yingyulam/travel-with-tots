import unittest
from unittest import mock

from src.data_loader import CITIES, SUPPORTED_CITIES
from src.form_helpers import (
    ASSUMED_NAP_DURATION_MIN,
    DEFAULTS,
    MAX_AGE_YEARS,
    MAX_NAPS,
    NAP_DURATION_MAX_MINUTES,
    NAP_DURATION_MIN_MINUTES,
    clamp_int,
    read_form,
    resolve_plan_child,
)


class _Form(dict):
    """Minimal stand-in for Flask's request.form (a MultiDict) -- supports
    the .get/.getlist calls read_form makes."""
    def getlist(self, key):
        value = self.get(key, [])
        return value if isinstance(value, list) else [value]


def _child(id, date_of_birth):
    return {"id": id, "name": f"child-{id}", "date_of_birth": date_of_birth}


class ClampIntTest(unittest.TestCase):
    def test_within_range_is_unchanged(self):
        self.assertEqual(clamp_int("3", 0, 6, 1), 3)

    def test_below_low_is_clamped_up(self):
        self.assertEqual(clamp_int("-5", 0, 6, 1), 0)

    def test_above_high_is_clamped_down(self):
        self.assertEqual(clamp_int("99", 0, 6, 1), 6)

    def test_non_numeric_uses_fallback(self):
        self.assertEqual(clamp_int("not-a-number", 0, 6, 1), 1)
        self.assertEqual(clamp_int(None, 0, 6, 1), 1)


class ReadFormTest(unittest.TestCase):
    def test_missing_fields_fall_back_to_defaults(self):
        result = read_form(_Form())
        self.assertEqual(result["destination"], DEFAULTS["destination"])
        self.assertEqual(result["wake_up"], DEFAULTS["wake_up"])
        self.assertEqual(result["dining"], DEFAULTS["dining"])

    def test_age_is_capped_at_the_ceiling(self):
        result = read_form(_Form(age_years="9", age_months="6"))
        self.assertEqual(result["age_years"], str(MAX_AGE_YEARS))
        self.assertEqual(result["age_months"], "0")

    def test_naps_are_read_and_capped(self):
        starts = [f"{h}:00" for h in range(MAX_NAPS + 2)]
        durations = ["30"] * len(starts)
        result = read_form(_Form(nap_start=starts, nap_duration=durations))
        self.assertEqual(len(result["naps"]), MAX_NAPS)

    def test_empty_nap_start_is_skipped(self):
        result = read_form(_Form(nap_start=["", "9:00"], nap_duration=["30", "45"]))
        self.assertEqual(len(result["naps"]), 1)
        self.assertEqual(result["naps"][0]["start"], "9:00")

    def test_a_blank_nap_duration_becomes_the_assumed_one(self):
        # The manual form's duration input has no default value, so a parent
        # can add a nap row with a time and leave the length empty.
        result = read_form(_Form(nap_start=["13:00"], nap_duration=[""]))
        self.assertEqual(result["naps"],
                         [{"start": "13:00", "duration_min": ASSUMED_NAP_DURATION_MIN}])

    def test_a_nap_duration_is_clamped_to_its_bounds(self):
        result = read_form(_Form(nap_start=["9:00", "13:00"],
                                 nap_duration=["1", "600"]))
        self.assertEqual([nap["duration_min"] for nap in result["naps"]],
                         [NAP_DURATION_MIN_MINUTES, NAP_DURATION_MAX_MINUTES])

    def test_checkbox_lists_pass_through(self):
        result = read_form(_Form(features=["kid_friendly", "stroller_accessible"]))
        self.assertEqual(result["features"], ["kid_friendly", "stroller_accessible"])


class ResolvePlanChildTest(unittest.TestCase):
    def test_no_parent_is_a_noop(self):
        form = read_form(_Form())
        result = resolve_plan_child(form, None)
        self.assertEqual(result["age_years"], DEFAULTS["age_years"])

    def test_parent_with_no_children_is_a_noop(self):
        with mock.patch("src.form_helpers.get_children", return_value=[]):
            form = read_form(_Form())
            result = resolve_plan_child(form, {"id": 1})
        self.assertEqual(result["age_years"], DEFAULTS["age_years"])

    def test_defaults_to_the_youngest_checked_child(self):
        # Child 2, born later, is younger.
        children = [_child(1, "2020-01-01"), _child(2, "2023-01-01")]
        with mock.patch("src.form_helpers.get_children", return_value=children):
            form = read_form(_Form())
            result = resolve_plan_child(form, {"id": 1})
        self.assertEqual(result["plan_child_id"], "2")
        self.assertEqual(sorted(result["child_ids"]), ["1", "2"])

    def test_respects_an_explicit_plan_child_id_among_checked(self):
        children = [_child(1, "2020-01-01"), _child(2, "2023-01-01")]
        with mock.patch("src.form_helpers.get_children", return_value=children):
            form = read_form(_Form(child_ids=["1", "2"], plan_child_id="1"))
            result = resolve_plan_child(form, {"id": 1})
        self.assertEqual(result["plan_child_id"], "1")

    def test_unchecked_plan_child_id_is_ignored(self):
        children = [_child(1, "2020-01-01"), _child(2, "2023-01-01")]
        with mock.patch("src.form_helpers.get_children", return_value=children):
            # plan_child_id "3" isn't in the checked child_ids, so it's
            # ignored in favor of the youngest-checked-child fallback (child 2).
            form = read_form(_Form(child_ids=["1", "2"], plan_child_id="3"))
            result = resolve_plan_child(form, {"id": 1})
        self.assertEqual(result["plan_child_id"], "2")

class TheDestinationIsAClosedListTest(unittest.TestCase):
    """The app plans one city, because the venue table holds one city.

    Free text invited a destination nothing could satisfy: the planner never
    filters on it, so "Seattle" produced a Vancouver day labelled Seattle. The
    dropdown says so, and read_form enforces it, because a select is not a
    guard against a stale page or a hand-made post. That gap has been closed
    once for `interest` and once for `transit` already.
    """

    def _destination(self, **data):
        return read_form(_Form(data))["destination"]

    def test_a_supported_city_is_kept(self):
        self.assertEqual(self._destination(destination="Vancouver"), "Vancouver")

    def test_an_unsupported_city_falls_back_rather_than_being_carried(self):
        self.assertEqual(self._destination(destination="Seattle"),
                         DEFAULTS["destination"])

    def test_a_neighbourhood_is_not_a_destination(self):
        # The extractor's prompt has always said so; nothing enforced it.
        self.assertEqual(self._destination(destination="Kitsilano"),
                         DEFAULTS["destination"])

    def test_a_venue_city_is_still_not_a_trip_destination(self):
        # CITIES holds Burnaby and North Vancouver because venues are there.
        # SUPPORTED_CITIES is a different question: where a day can be planned.
        self.assertIn("Burnaby", CITIES)
        self.assertNotIn("Burnaby", SUPPORTED_CITIES)
        self.assertEqual(self._destination(destination="Burnaby"),
                         DEFAULTS["destination"])

    def test_nothing_given_is_the_default(self):
        self.assertEqual(self._destination(), DEFAULTS["destination"])

    def test_the_default_is_itself_supported(self):
        # Otherwise every form would fall back to a value it then rejects.
        self.assertIn(DEFAULTS["destination"], SUPPORTED_CITIES)



if __name__ == "__main__":
    unittest.main()
