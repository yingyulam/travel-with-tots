"""Saving a generated day, with or without a child on the account.

A trip is the parent's day out. child_id records whose age shaped it, which is
worth having and is not what makes the plan real, so it is optional. Requiring
one meant a parent who had logged nobody pressed Save and was redirected back
to /plan with nothing saved and no explanation.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import json
import os
import tempfile
import unittest
from src.web import guards
from contextlib import closing
from unittest import mock

from src.store import db, schema


PLAN = {"label": "Mixed", "stops": [{"time": "10:00", "venue": {"id": 1,
                                                                "name": "A Park"}}]}


class SaveTripTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import app as app_module
        self.app_module = app_module
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)
        self.parent = db.add_parent("p@example.com", "h", name="P")
        self.client = app_module.app.test_client()
        self._as(self.parent)

    def _as(self, parent_id):
        patcher = mock.patch.object(guards, "current_parent",
            return_value={"id": parent_id, "is_admin": False,
                          "name": "P", "email": "p@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _save(self, **form):
        body = {"plan": json.dumps(PLAN),
                "trip_form": json.dumps({"destination": "Vancouver", **form})}
        return self.client.post("/save-trip", data=body)

    def _trips(self):
        return db.get_trips_for_parent(self.parent)

    def test_a_parent_with_no_children_can_save(self):
        # The whole point. This used to redirect to /plan and save nothing.
        response = self._save()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/dashboard", response.headers["Location"])
        self.assertEqual(len(self._trips()), 1)

    def test_saving_without_a_child_leaves_child_id_null(self):
        self._save()
        self.assertIsNone(self._trips()[0]["child_id"])

    def test_the_plan_itself_is_stored_either_way(self):
        # A childless trip is a whole trip, not a placeholder.
        self._save()
        trip = self._trips()[0]
        self.assertEqual(json.loads(trip["plan_json"])["label"], "Mixed")
        self.assertEqual(trip["destination"], "Vancouver")

    def test_a_picked_child_is_still_recorded(self):
        child = db.add_child(self.parent, "Sam", "2024-01-01")
        self._save(child_ids=[str(child)])
        self.assertEqual(self._trips()[0]["child_id"], child)

    def test_two_children_still_save_one_trip_each(self):
        a = db.add_child(self.parent, "Sam", "2024-01-01")
        b = db.add_child(self.parent, "Ada", "2022-05-05")
        self._save(child_ids=[str(a), str(b)])
        self.assertEqual(sorted(t["child_id"] for t in self._trips()), sorted([a, b]))

    def test_another_parents_child_is_dropped_not_saved_against(self):
        # valid_ids filters it out; the fallback must be a childless trip, never
        # a trip pointing at somebody else's child.
        stranger = db.add_parent("z@example.com", "h", name="Z")
        theirs = db.add_child(stranger, "Not Yours", "2024-01-01")
        self._save(child_ids=[str(theirs)])
        trips = self._trips()
        self.assertEqual(len(trips), 1)
        self.assertIsNone(trips[0]["child_id"])

    def test_a_malformed_plan_still_saves_nothing(self):
        # Dropping the child requirement must not drop this guard with it.
        response = self.client.post("/save-trip",
                                    data={"plan": "not json", "trip_form": "{}"})
        self.assertIn("/plan", response.headers["Location"])
        self.assertEqual(self._trips(), [])


class SavePromptTest(unittest.TestCase):
    """What the pages offer a logged-in parent who has logged no child."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        import app as app_module
        self.app_module = app_module
        patcher = mock.patch.object(db, "DB_PATH",
                                   os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)
        self.parent = db.add_parent("p@example.com", "h", name="P")
        self.client = app_module.app.test_client()
        patcher = mock.patch.object(guards, "current_parent",
            return_value={"id": self.parent, "is_admin": False,
                          "name": "P", "email": "p@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_in_trip_page_offers_saving_with_no_child(self):
        html = self.client.post("/trip", data={
            "plan": json.dumps(PLAN),
            "context": json.dumps({"destination": "Vancouver"}),
        }).get_data(as_text=True)
        self.assertIn("Save this plan", html)
        self.assertNotIn("Add a child", html)

    def test_the_dashboard_names_no_child_rather_than_inventing_one(self):
        # "for a child no longer on your account" was fine when every trip had
        # had a child; it is a lie about a trip that never did, and child_id
        # cannot tell the two apart.
        db.add_trip(self.parent, None, destination="Vancouver",
                    plan_label="Mixed", plan_json=json.dumps(PLAN))
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn("Vancouver", html)
        self.assertNotIn("no longer on your account", html)
        self.assertNotIn("Vancouver for", html)


if __name__ == "__main__":
    unittest.main()
