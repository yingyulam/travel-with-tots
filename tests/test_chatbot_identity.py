"""Who the chat thinks it is talking to.

The bubble is on every page, including pages nobody is logged in for, so the
chat has to work with no parent at all. When there is one, every recall is
scoped by their id, which makes where that id comes from a security property
rather than a convenience: it is read from the session and never from the
request body.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import os
import unittest
from src.web import chat as web_chat
from src.web import guards
from unittest import mock

os.environ.setdefault("SECRET_KEY", "test-only")

import app as app_module

# A reply shaped like handle_message's, so the route can finish and this file
# only ever asserts on the context it was handed.
REPLY = {
    "reply": "ok", "sources": [], "model": "m", "response_time": 0,
    "input_tokens": 0, "output_tokens": 0, "workflow": None,
    "conversation": None, "choices": None, "choose_many": False, "form": None,
    "place_form": None, "replan_request": None, "open_form": False,
    "places": [], "source": None, "ask_location": False, "cancel_choice": "x",
}


class ChatIdentityTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def _context(self, body, parent_id=None):
        """The context the route handed to handle_message for one POST.

        `parent_id` puts a real value in the session cookie, for the tests that
        exercise current_parent rather than mocking it.
        """
        seen = {}

        def capture(message, **kwargs):
            seen.update(kwargs.get("context") or {})
            return dict(REPLY)

        patches = [
            mock.patch.object(web_chat, "handle_message", side_effect=capture),
            mock.patch.object(web_chat.rag, "get_status",
                              return_value={"state": "ready"}),
        ]
        if parent_id is not None:
            with self.client.session_transaction() as session:
                session["parent_id"] = parent_id
        with patches[0], patches[1]:
            self.client.post("/chatbot", json={"message": "hi", **body})
        return seen

    def test_an_anonymous_chat_has_no_parent(self):
        self.assertIsNone(self._context({})["parent_id"])

    def test_a_logged_in_parent_reaches_the_context(self):
        with mock.patch.object(guards, "current_parent",
                               return_value={"id": 7}):
            self.assertEqual(self._context({})["parent_id"], 7)

    def test_a_parent_id_in_the_body_is_ignored_when_anonymous(self):
        # The whole point: an id from the request would read someone else's
        # children and saved trips.
        self.assertIsNone(self._context({"parent_id": 999})["parent_id"])

    def test_a_parent_id_in_the_body_cannot_impersonate_another_parent(self):
        with mock.patch.object(guards, "current_parent",
                               return_value={"id": 7}):
            context = self._context({"parent_id": 999})
        self.assertEqual(context["parent_id"], 7)

    def test_the_real_cookie_path_works_not_just_the_mock(self):
        # Exercises current_parent and the session for real, so a change to
        # either is caught rather than mocked over.
        with mock.patch.object(guards, "get_parent",
                               return_value={"id": 4, "is_admin": 0}):
            self.assertEqual(self._context({}, parent_id=4)["parent_id"], 4)

    def test_a_cookie_naming_a_parent_who_is_gone_has_no_parent(self):
        # SQLite reuses row ids, so a stale cookie must not resolve to whoever
        # holds that id now. get_parent returning None is that case.
        with mock.patch.object(guards, "get_parent", return_value=None):
            self.assertIsNone(self._context({}, parent_id=4)["parent_id"])

    def test_the_other_context_keys_still_arrive(self):
        with mock.patch.object(guards, "current_parent", return_value=None):
            context = self._context({"on_trip": True,
                                     "location": {"lat": 49.2, "lng": -123.1}})
        self.assertTrue(context["on_trip"])
        self.assertEqual(context["lat"], 49.2)


if __name__ == "__main__":
    unittest.main()
