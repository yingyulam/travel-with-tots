import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from src import db
from src.components.geocode import GeocodeError
from src.workflows import log_a_place
from src.workflows.log_a_place import WORKFLOW


class _VenueDbTest(unittest.TestCase):
    """A real SQLite database on a temp file, so the schema, the CHECK on
    source and the VERIFIED_SOURCES filter all run for real."""

    def setUp(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.db_path = tmp.name
        self.patcher = mock.patch.object(db, "DB_PATH", self.db_path)
        self.patcher.start()
        with closing(db.connect()) as conn:
            conn.executescript(db.SCHEMA)
        self.parent_id = db.add_parent("p@example.com", "hash", name="P")
        self.other_id = db.add_parent("q@example.com", "hash", name="Q")

    def tearDown(self):
        self.patcher.stop()
        os.unlink(self.db_path)

    def _row(self, name):
        with closing(db.connect()) as conn:
            return conn.execute(
                "SELECT * FROM venues WHERE name = ?", (name,)).fetchone()


class AddVenueStorageTest(_VenueDbTest):
    """A submission is only worth verifying if it carries a location. Before
    this, add_venue's INSERT omitted city, lat and lng entirely, so a logged
    place could never be distance-ranked or matched by a city query."""

    def test_everything_a_verifier_needs_round_trips(self):
        db.add_venue("Nourish Kitchen", source="user_submitted",
                     city="Vancouver", lat=49.2634, lng=-123.1005,
                     neighbourhood="Mount Pleasant", has_nursing_room=True,
                     notes="room is behind the lifts", address="8 Main St")
        row = self._row("Nourish Kitchen")
        self.assertEqual(row["city"], "Vancouver")
        self.assertAlmostEqual(row["lat"], 49.2634)
        self.assertEqual(row["has_nursing_room"], 1)
        self.assertEqual(row["notes"], "room is behind the lifts")
        self.assertEqual(row["address"], "8 Main St")

    def test_a_submission_without_a_location_still_stores(self):
        # A geocoder that is unreachable must not cost the parent their entry.
        db.add_venue("Unresolved Cafe", source="user_submitted")
        row = self._row("Unresolved Cafe")
        self.assertIsNotNone(row)
        self.assertIsNone(row["lat"])
        self.assertIsNone(row["notes"])

    def test_a_logged_place_is_not_searchable_even_with_coordinates(self):
        # The guard that matters. A complete submission is still held back by
        # source alone, which is the human-in-the-loop gate.
        db.add_venue("Secret Playground", source="user_submitted",
                     city="Vancouver", lat=49.28, lng=-123.12, kid_friendly=True)
        self.assertNotIn("Secret Playground",
                         [v["name"] for v in db.get_venues_in_city("Vancouver")])
        self.assertNotIn("Secret Playground",
                         [v["name"] for v in
                          db.get_candidate_venues("Vancouver", age_months=24)])

    def test_the_gate_is_source_based_not_completeness_based(self):
        # Same row, promoted: proves the previous test failed on source rather
        # than on something missing from the record.
        db.add_venue("Promoted Park", source="curated", city="Vancouver",
                     lat=49.28, lng=-123.12, kid_friendly=True)
        self.assertIn("Promoted Park",
                      [v["name"] for v in db.get_venues_in_city("Vancouver")])


class UpdateVenueTest(_VenueDbTest):
    def setUp(self):
        super().setUp()
        self.place_id = db.add_venue(
            "Old Name", source="user_submitted", parent_id=self.parent_id,
            city="Vancouver", lat=49.28, lng=-123.12, kid_friendly=True)

    def test_an_owner_can_correct_their_own(self):
        db.update_venue(self.place_id, self.parent_id, name="New Name",
                        notes="quieter upstairs")
        row = self._row("New Name")
        self.assertIsNotNone(row)
        self.assertEqual(row["notes"], "quieter upstairs")

    def test_another_parent_cannot(self):
        db.update_venue(self.place_id, self.other_id, name="Hijacked")
        self.assertIsNone(self._row("Hijacked"))
        self.assertIsNotNone(self._row("Old Name"))

    def test_a_curated_row_cannot_be_edited_by_its_parent_id(self):
        # parent_id is nullable on this table, so a query keyed on id alone
        # would happily rewrite a seed row.
        curated = db.add_venue("Science World", source="curated",
                               parent_id=self.parent_id, city="Vancouver")
        db.update_venue(curated, self.parent_id, name="Not Science World")
        self.assertIsNone(self._row("Not Science World"))

    def test_source_is_not_editable(self):
        # The whole gate would be pointless if an edit could promote a row.
        with self.assertRaises(ValueError):
            db.update_venue(self.place_id, self.parent_id, source="curated")

    def test_an_unknown_field_fails_loudly(self):
        with self.assertRaises(ValueError):
            db.update_venue(self.place_id, self.parent_id, nmae="typo")

    def test_editing_leaves_it_unsearchable(self):
        db.update_venue(self.place_id, self.parent_id, name="Edited",
                        kid_friendly=1)
        self.assertNotIn("Edited",
                         [v["name"] for v in
                          db.get_candidate_venues("Vancouver", age_months=24)])


class DeleteVenueTest(_VenueDbTest):
    def setUp(self):
        super().setUp()
        self.place_id = db.add_venue("Mine", source="user_submitted",
                                     parent_id=self.parent_id)

    def test_an_owner_can_remove_their_own(self):
        db.delete_venue(self.place_id, self.parent_id)
        self.assertIsNone(self._row("Mine"))

    def test_another_parent_cannot(self):
        db.delete_venue(self.place_id, self.other_id)
        self.assertIsNotNone(self._row("Mine"))

    def test_a_curated_row_survives(self):
        curated = db.add_venue("Seeded", source="curated",
                               parent_id=self.parent_id)
        db.delete_venue(curated, self.parent_id)
        self.assertIsNotNone(self._row("Seeded"))

    def test_the_submitter_sees_only_their_own(self):
        db.add_venue("Theirs", source="user_submitted", parent_id=self.other_id)
        mine = [v["name"] for v in db.get_logged_venues_for_parent(self.parent_id)]
        self.assertEqual(mine, ["Mine"])


class RunTest(_VenueDbTest):
    """The workflow's own sequencing: work out where the place is, then store
    it. Lives in the workflow module rather than the route so it can be tested
    without Flask."""

    def test_a_pin_is_preferred_over_geocoding_the_name(self):
        # A playground has no address to look up, so the pin's coordinates are
        # the only thing locating it. Geocoding must not be consulted at all.
        with mock.patch.object(log_a_place, "geocode") as geocoded:
            record = log_a_place.run(self.parent_id, {
                "name": "The good playground", "lat": "49.2827",
                "lng": "-123.1207", "city": "Vancouver",
                "neighbourhood": "West End", "address": "Denman St",
            })
        geocoded.assert_not_called()
        self.assertAlmostEqual(record["lat"], 49.2827)
        self.assertEqual(record["address"], "Denman St")

    def test_without_a_pin_the_name_is_geocoded(self):
        resolved = {"city": "Vancouver", "neighbourhood": "Gastown",
                    "formatted_address": "1 Water St", "lat": 49.28, "lng": -123.1}
        with mock.patch.object(log_a_place, "geocode", return_value=resolved):
            record = log_a_place.run(self.parent_id,
                                     {"name": "Nourish", "neighbourhood": "Gastown"})
        self.assertEqual(record["address"], "1 Water St")
        self.assertAlmostEqual(record["lat"], 49.28)

    def test_a_half_filled_pin_falls_back_rather_than_crashing(self):
        with mock.patch.object(log_a_place, "geocode",
                               return_value=dict(log_a_place.UNRESOLVED_PLACE)):
            record = log_a_place.run(self.parent_id,
                                     {"name": "Somewhere", "lat": "", "lng": ""})
        self.assertIsNone(record["lat"])

    def test_a_failing_geocoder_does_not_cost_the_submission(self):
        with mock.patch.object(log_a_place, "geocode",
                               side_effect=GeocodeError("down")):
            record = log_a_place.run(self.parent_id, {"name": "Unresolvable"})
        self.assertIsNone(record["lat"])
        self.assertIsNotNone(self._row("Unresolvable"))

    def test_amenities_and_notes_are_stored(self):
        with mock.patch.object(log_a_place, "geocode",
                               return_value=dict(log_a_place.UNRESOLVED_PLACE)):
            log_a_place.run(self.parent_id, {
                "name": "Mall", "has_nursing_room": "on", "notes": "level 2",
            })
        row = self._row("Mall")
        self.assertEqual(row["has_nursing_room"], 1)
        self.assertEqual(row["has_family_room"], 0)
        self.assertEqual(row["notes"], "level 2")

    def test_a_nameless_submission_is_refused(self):
        with self.assertRaises(ValueError):
            log_a_place.run(self.parent_id, {"name": "   "})

    def test_what_it_stores_is_never_searchable(self):
        with mock.patch.object(log_a_place, "geocode",
                               return_value={"city": "Vancouver",
                                             "neighbourhood": "Downtown",
                                             "formatted_address": "1 Main",
                                             "lat": 49.28, "lng": -123.12}):
            log_a_place.run(self.parent_id, {"name": "Fresh", "kid_friendly": "on"})
        self.assertNotIn("Fresh", [v["name"] for v in
                                   db.get_candidate_venues("Vancouver", age_months=24)])


class DeclarationTest(unittest.TestCase):
    def test_the_card_points_at_the_real_page(self):
        # Not a separate admin copy: a test surface that exercises the page a
        # parent uses cannot drift away from it.
        self.assertEqual(WORKFLOW["page"], "log_place_page")

    def test_every_step_is_built_now(self):
        self.assertTrue(all(step["built"] for step in WORKFLOW["steps"]))

    def test_the_chain_is_input_geocode_database(self):
        self.assertEqual([s["component"] for s in WORKFLOW["steps"]],
                         ["User in-trip input", "Google Map handoff", "Venues DB"])


class PageTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.parent = {"id": 1, "is_admin": False, "name": "P", "email": "p@b.com"}

    def _as_parent(self):
        return mock.patch.object(self.app_module, "_current_parent",
                                 return_value=self.parent)

    def test_the_page_renders_for_any_logged_in_parent(self):
        # Parent-facing, not admin-only: this is the real feature now.
        with self._as_parent():
            resp = self.client.get("/log-place")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Log a Place", resp.get_data(as_text=True))

    def test_the_page_needs_a_login(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=None):
            self.assertEqual(self.client.get("/log-place").status_code, 302)

    def test_the_map_needs_no_api_key(self):
        # The whole reason for Leaflet over Google: no key reaches the browser.
        with self._as_parent():
            html = self.client.get("/log-place").get_data(as_text=True)
        self.assertIn("vendor/leaflet.js", html)
        self.assertNotIn("maps.googleapis.com", html)

    def test_the_page_says_it_is_not_searchable_yet(self):
        with self._as_parent():
            html = self.client.get("/log-place").get_data(as_text=True)
        self.assertIn("until an admin checks it", html)

    def test_the_nav_links_to_it(self):
        with self._as_parent():
            html = self.client.get("/log-place").get_data(as_text=True)
        self.assertIn('href="/log-place"', html)

    def test_the_workflows_card_links_to_it(self):
        admin = {**self.parent, "is_admin": True}
        with mock.patch.object(self.app_module, "_current_parent", return_value=admin):
            html = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn('href="/log-place"', html)

    def test_submitting_comes_back_showing_what_was_stored(self):
        # The chain is only observable if its output appears where it was run.
        # A bare redirect to the dashboard gave no confirmation at all.
        stored = {"id": 7, "name": "Science World", "type": "museum",
                  "neighbourhood": "False Creek", "city": "Vancouver",
                  "address": "1455 Quebec St", "lat": 49.2734, "lng": -123.1038,
                  "notes": "change table by the gift shop",
                  "kid_friendly": 1, "has_family_room": 0,
                  "has_nursing_room": 0, "stroller_accessible": 0}
        with self._as_parent(), \
             mock.patch.object(self.app_module.log_a_place, "run",
                               return_value={"id": 7}), \
             mock.patch.object(self.app_module, "_logged_place", return_value=stored):
            resp = self.client.post("/log-place", data={"name": "Science World"})
            self.assertEqual(resp.status_code, 302)
            self.assertIn("logged=7", resp.headers["Location"])
            html = self.client.get("/log-place?logged=7").get_data(as_text=True)
        self.assertIn("Science World", html)
        self.assertIn("1455 Quebec St", html)
        self.assertIn("49.27340", html)
        self.assertIn("awaiting verification", html)

    def test_a_place_that_is_not_yours_shows_nothing(self):
        # ?logged= is a query parameter, so it has to be ownership-checked.
        with self._as_parent(), \
             mock.patch.object(self.app_module, "_logged_place", return_value=None):
            html = self.client.get("/log-place?logged=999").get_data(as_text=True)
        self.assertNotIn("awaiting verification", html)

    def test_area_needs_coordinates(self):
        with self._as_parent():
            resp = self.client.post("/log-place/area", json={})
        self.assertEqual(resp.status_code, 400)


if __name__ == "__main__":
    unittest.main()
