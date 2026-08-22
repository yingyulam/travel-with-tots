import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src import db
from src.workflows.log_a_place import WORKFLOW


class AddVenueStorageTest(unittest.TestCase):
    """A submission is only worth verifying if it carries a location. Before
    this, add_venue's INSERT omitted city, lat and lng entirely, so a logged
    place could never be distance-ranked or matched by a city query."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _row(self, name):
        with closing(db.connect()) as conn:
            return conn.execute(
                "SELECT * FROM venues WHERE name = ?", (name,)).fetchone()

    def test_coordinates_and_city_round_trip(self):
        db.add_venue("Nourish Kitchen", source="user_submitted",
                     city="Vancouver", lat=49.2634, lng=-123.1005,
                     neighbourhood="Mount Pleasant", has_nursing_room=True)
        row = self._row("Nourish Kitchen")
        self.assertEqual(row["city"], "Vancouver")
        self.assertAlmostEqual(row["lat"], 49.2634)
        self.assertAlmostEqual(row["lng"], -123.1005)
        self.assertEqual(row["has_nursing_room"], 1)

    def test_a_submission_without_a_location_still_stores(self):
        # A geocoder that is unreachable must not cost the parent their entry.
        db.add_venue("Unresolved Cafe", source="user_submitted")
        row = self._row("Unresolved Cafe")
        self.assertIsNotNone(row)
        self.assertIsNone(row["lat"])
        self.assertIsNone(row["city"])

    def test_a_logged_place_is_not_searchable_even_with_coordinates(self):
        # The guard that matters. A complete submission is still held back by
        # source alone, which is the human-in-the-loop gate: making a record
        # well formed must not accidentally publish it.
        db.add_venue("Secret Playground", source="user_submitted",
                     city="Vancouver", lat=49.28, lng=-123.12,
                     kid_friendly=True)
        in_city = [v["name"] for v in db.get_venues_in_city("Vancouver")]
        self.assertNotIn("Secret Playground", in_city)
        candidates = [v["name"] for v in
                      db.get_candidate_venues("Vancouver", age_months=24)]
        self.assertNotIn("Secret Playground", candidates)

    def test_the_gate_is_source_based_not_completeness_based(self):
        # Same row, promoted: proves the previous test failed on source rather
        # than on something missing from the record.
        db.add_venue("Promoted Park", source="curated", city="Vancouver",
                     lat=49.28, lng=-123.12, kid_friendly=True)
        in_city = [v["name"] for v in db.get_venues_in_city("Vancouver")]
        self.assertIn("Promoted Park", in_city)

    def test_the_submitter_can_see_their_own(self):
        parent_id = db.add_parent("p@example.com", "hash", name="P")
        db.add_venue("Mine", source="user_submitted", parent_id=parent_id,
                     city="Vancouver")
        mine = [v["name"] for v in db.get_logged_venues_for_parent(parent_id)]
        self.assertIn("Mine", mine)


class DeclarationTest(unittest.TestCase):
    def test_the_declaration_points_at_its_test_page(self):
        self.assertEqual(WORKFLOW["page"], "log_a_place_page")

    def test_every_step_is_built_now(self):
        self.assertTrue(all(step["built"] for step in WORKFLOW["steps"]))

    def test_the_chain_is_input_geocode_database(self):
        components = [step["component"] for step in WORKFLOW["steps"]]
        self.assertEqual(components,
                         ["User in-trip input", "Google Map handoff", "Venues DB"])

    def test_it_replaced_the_find_nearby_card(self):
        # "Find a nearby place" collapsed into one component, so declaring it
        # advertised sequencing no code performed.
        from src.workflows import WORKFLOWS
        names = [w["name"] for w in WORKFLOWS]
        self.assertIn(WORKFLOW["name"], names)
        self.assertNotIn("Find a nearby place", names)


class PageTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.admin = {"id": 1, "is_admin": True, "name": "A", "email": "a@b.com"}

    def test_page_renders_for_an_admin(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            resp = self.client.get("/workflows/log-a-place")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Log a place", resp.get_data(as_text=True))

    def test_the_page_says_it_is_not_searchable_yet(self):
        # Without this the page implies a submission takes effect immediately.
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            html = self.client.get("/workflows/log-a-place").get_data(as_text=True)
        self.assertIn("verif", html.lower())

    def test_page_is_admin_only(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=None):
            self.assertEqual(
                self.client.get("/workflows/log-a-place").status_code, 302)

    def test_the_workflows_page_links_to_it(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            html = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn("/workflows/log-a-place", html)

    def test_resolve_needs_a_name(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            resp = self.client.post("/workflows/log-a-place/resolve", json={})
        self.assertEqual(resp.status_code, 400)

    def test_area_needs_coordinates(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            resp = self.client.post("/workflows/log-a-place/area", json={})
        self.assertEqual(resp.status_code, 400)

    def test_resolve_reports_a_failed_lookup_without_erroring(self):
        # A geocoder that cannot answer is not a request failure: the parent
        # can still submit, just without coordinates.
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin), \
             mock.patch.object(self.app_module, "geocode",
                               side_effect=self.app_module.GeocodeError("nope")):
            resp = self.client.post("/workflows/log-a-place/resolve",
                                    json={"name": "Nowhere Cafe"})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.get_json()["resolved"])


if __name__ == "__main__":
    unittest.main()
