"""`setting`: where a visit is spent.

The one fact `type` provably cannot carry. `attraction` is a legitimate
residual bucket -- a place that fits none of the other types -- and its eight
venues split four indoor, four outdoor, so no amount of redrawing the type list
removes the need for this.

What it must never absorb, since every field in this app has drifted at least
once: nap suitability, calm, admission, whether the hours are real, or
seasonality. Two readers, both about shelter.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import json
import unittest
from datetime import date
from pathlib import Path

from src import data_loader, importers
from src.data_loader import OPEN_AIR, SETTINGS, SHELTERED, suits_weather
from src.workflows import propose_venues as pv

SEED = json.loads((Path(data_loader.__file__).parent.parent / "data"
                   / "venues.json").read_text(encoding="utf-8"))


class SeedDataTest(unittest.TestCase):
    def test_every_seeded_venue_has_a_setting(self):
        missing = [v["name"] for v in SEED if not v.get("setting")]
        self.assertEqual(missing, [])

    def test_every_value_is_one_we_know(self):
        bad = [(v["name"], v["setting"]) for v in SEED
               if v["setting"] not in SETTINGS]
        self.assertEqual(bad, [])

    def test_the_residual_type_really_does_mix_settings(self):
        # If this ever became one-valued, `setting` would be derivable from
        # `type` and this column would be redundant. It is not.
        attractions = {v["setting"] for v in SEED if v["type"] == "attraction"}
        self.assertGreater(len(attractions), 1)

    def test_both_is_rare_because_it_is_the_hard_call(self):
        # Only where either mode is a real visit on its own. If this grows
        # large, the definition has been loosened to "has a roof somewhere".
        both = [v["name"] for v in SEED if v["setting"] == "both"]
        self.assertLessEqual(len(both), 4)
        self.assertIn("Grouse Mountain", both)

    def test_a_gift_shop_does_not_make_a_place_both(self):
        # Capilano has a shop and a cafe. Nobody goes there in the rain to
        # stand in the shop, so the visit is outdoor.
        byname = {v["name"]: v for v in SEED}
        self.assertEqual(byname["Capilano Suspension Bridge Park"]["setting"],
                         "outdoor")

    def test_a_glass_dome_is_indoor_even_though_it_is_a_garden(self):
        byname = {v["name"]: v for v in SEED}
        self.assertEqual(byname["Bloedel Conservatory"]["type"], "garden")
        self.assertEqual(byname["Bloedel Conservatory"]["setting"], "indoor")

    def test_the_corrected_types_are_accurate(self):
        # These were typed for the planning behaviour the old type list bought,
        # not for what they are: four beaches and a seawall were `park`,
        # a market was a `mall`, and the botanical gardens were `park`.
        byname = {v["name"]: v["type"] for v in SEED}
        self.assertEqual(byname["English Bay Beach"], "beach")
        self.assertEqual(byname["Stanley Park Seawall"], "seawall")
        self.assertEqual(byname["VanDusen Botanical Garden"], "garden")
        self.assertEqual(byname["Granville Island Public Market"], "market")
        self.assertEqual(byname["Vancouver Aquarium"], "aquarium")


class TwoTierRuleTest(unittest.TestCase):
    """Three tiers measurably drops every "both" venue below all 222 imported
    parks, overriding the curator's seed_rank with a weaker heuristic. Two
    tiers also confines the field's ambiguity to where it cannot matter."""

    def test_both_counts_as_sheltered_and_as_open_air(self):
        self.assertIn("both", SHELTERED)
        self.assertIn("both", OPEN_AIR)

    def test_indoor_and_both_are_interchangeable_when_shelter_is_wanted(self):
        # So misjudging one for the other costs nothing -- which is the whole
        # answer to "is this too subjective for an admin to assign".
        self.assertEqual(suits_weather({"setting": "indoor"}, wet=True),
                         suits_weather({"setting": "both"}, wet=True))

    def test_only_the_unambiguous_call_changes_anything(self):
        self.assertNotEqual(suits_weather({"setting": "indoor"}, wet=True),
                            suits_weather({"setting": "outdoor"}, wet=True))

    def test_dry_weather_accepts_anything(self):
        # Weather only ever pushes towards shelter: rain makes indoors better,
        # dry weather does not make outdoors obligatory.
        for value in SETTINGS:
            self.assertTrue(suits_weather({"setting": value}, wet=False))

    def test_an_unknown_forecast_behaves_exactly_like_dry(self):
        # The property that lets a forecast be added later without changing how
        # a day with no forecast is planned today.
        for value in (*SETTINGS, None, ""):
            self.assertTrue(suits_weather({"setting": value}, wet=False))

    def test_an_unset_setting_is_not_treated_as_shelter(self):
        # Not knowing is a reason to leave a venue out of a wet slot, never to
        # include it -- the same rule the app applies to unknown hours.
        self.assertFalse(suits_weather({"setting": None}, wet=True))
        self.assertFalse(suits_weather({}, wet=True))


class ImportedSettingTest(unittest.TestCase):
    PARK = {"parkid": 1, "name": "A Park", "washrooms": "Y",
            "neighbourhoodname": "Kitsilano", "streetnumber": "1",
            "streetname": "Main St",
            "googlemapdest": {"lat": 49.2, "lon": -123.1}}
    CENTRE = {"name": "Hastings", "address": "1 E Hastings St",
              "geo_local_area": "Hastings-Sunrise",
              "geo_point_2d": {"lat": 49.2, "lon": -123.1}}

    def test_a_city_park_is_outdoor_without_anyone_deciding(self):
        self.assertEqual(importers.park_entry(self.PARK)["fields"]["setting"],
                         "outdoor")

    def test_a_community_centre_is_the_building_not_its_playground(self):
        self.assertEqual(importers.centre_entry(self.CENTRE)["fields"]["setting"],
                         "indoor")

    def test_the_import_can_write_it(self):
        from src.store import db
        self.assertIn("setting", db.IMPORT_FIELDS)


class ProposedSettingTest(unittest.TestCase):
    def _enum(self):
        return (pv.PROPOSAL_RESPONSE_FORMAT["json_schema"]["schema"]["properties"]
                ["venues"]["items"]["properties"]["setting"]["enum"])

    def test_the_model_picks_from_the_enum(self):
        self.assertEqual(self._enum(), [*SETTINGS, None])

    def test_the_prompt_carries_the_same_values(self):
        prompt = pv._messages([{"title": "t", "url": "u", "snippet": "s"}],
                              set())[0]["content"]
        self.assertNotIn("{settings}", prompt)
        for value in SETTINGS:
            self.assertIn(value, prompt)

    def test_a_value_outside_the_enum_is_blanked(self):
        said = {"beaty", "biodiversity", "museum", "whale"}
        kept = pv._grounded({"name": "Beaty Biodiversity Museum",
                             "type": "museum", "setting": "inside"}, said)
        self.assertEqual(kept["setting"], "")

    def test_a_real_value_survives(self):
        said = {"beaty", "biodiversity", "museum", "whale"}
        kept = pv._grounded({"name": "Beaty Biodiversity Museum",
                             "type": "museum", "setting": "indoor"}, said)
        self.assertEqual(kept["setting"], "indoor")


class PlannerExposureTest(unittest.TestCase):
    def test_setting_reaches_the_venue_dicts_the_planners_read(self):
        self.assertIn("setting", data_loader.VENUE_KEYS)
        venues = data_loader.get_venues("Vancouver", on_date=date(2026, 9, 15))
        self.assertTrue(venues)
        self.assertTrue(all("setting" in v for v in venues))

    def test_it_is_not_coerced_to_a_boolean(self):
        # BOOL_KEYS turns 0/1 into real booleans; setting is a string and must
        # not be swept into that.
        self.assertNotIn("setting", data_loader.BOOL_KEYS)


if __name__ == "__main__":
    unittest.main()
