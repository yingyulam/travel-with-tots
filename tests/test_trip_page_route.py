"""GET /trip/<id> against a real saved trip.

The suite tested the trip page's rendering helpers but never actually fetched
the route, so dropping a trips column broke reopening a saved itinerary with a
500 and every test still passed. This closes that.
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

PLAN = {
    "label": "Mixed", "blurb": "A day out.", "source": "rule",
    "stops": [
        {"time": "9:00 AM", "kind": "activity", "reason": "first",
         "venue": {"name": "A Park", "type": "park", "neighbourhood": "Downtown",
                   "has_washroom": True, "has_family_room": False,
                   "has_nursing_room": False, "stroller_accessible": True,
                   "has_highchair": False, "can_eat": False,
                   "nap_friendly": True, "open": "06:00", "close": "22:00",
                   "lat": 49.28, "lng": -123.12, "maps_url": "https://maps.example"}},
        {"time": "12:00 PM", "kind": "meal", "venue": None,
         "reason": "Find lunch near A Park.", "duration": "about 1.5 hours"},
    ],
}


class TripPageTest(unittest.TestCase):
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
        self.parent_id = db.add_parent("p@example.com", "h", name="P")
        self.child_id = db.add_child(self.parent_id, "Sam", "2024-01-01")
        self.trip_id = db.add_trip(
            self.parent_id, self.child_id,
            destination="Vancouver", bedtime="20:00", dining="dine_out",
            transit=json.dumps(["stroller"]), plan_label="Mixed",
            plan_json=json.dumps(PLAN))
        self.client = app_module.app.test_client()
        patcher = mock.patch.object(guards, "current_parent",
            return_value={"id": self.parent_id, "is_admin": False,
                          "name": "P", "email": "p@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_saved_trip_reopens(self):
        response = self.client.get(f"/trip/{self.trip_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Vancouver", response.get_data(as_text=True))

    def test_a_lunch_block_with_no_venue_does_not_break_the_page(self):
        # The handoff block: lunch names nowhere and offers a search instead.
        body = self.client.get(f"/trip/{self.trip_id}").get_data(as_text=True)
        self.assertIn("Find lunch", body)

    def test_another_parents_trip_is_not_viewable(self):
        other = db.add_parent("q@example.com", "h", name="Q")
        with mock.patch.object(guards, "current_parent",
                return_value={"id": other, "is_admin": False,
                              "name": "Q", "email": "q@example.com"}):
            self.assertEqual(
                self.client.get(f"/trip/{self.trip_id}").status_code, 302)


if __name__ == "__main__":
    unittest.main()
