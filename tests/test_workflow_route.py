"""POST /workflows/<name>/run: the workflow test pages' own backend.

Their own route rather than /chatbot with a flag, so each route has one
orchestrator: /chatbot is the parent's front door and the agent decides there,
this is a demo surface and a named workflow decides here. Both go through
agent.run_workflow_turn, so a workflow cannot answer two different ways.

It also closes something force_workflow left open. That flag arrived in the
body of a public route, so any caller could choose which workflow their message
reached. This route is admin-only.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import json
import os
import tempfile
import unittest
from src.web import chat as web_chat
from src.web import guards
from contextlib import closing
from unittest import mock

from src import db


class WorkflowRouteTest(unittest.TestCase):
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
            db.create_schema(conn)
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        self.admin = db.add_parent("a@example.com", "h", name="A")

    def _as(self, is_admin=True):
        patcher = mock.patch.object(guards, "current_parent",
            return_value={"id": self.admin, "is_admin": is_admin,
                          "name": "A", "email": "a@example.com"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _post(self, name="Find a nearby place", **body):
        return self.client.post(f"/workflows/{name}/run",
                                json={"message": "a nursing room", **body})

    def test_an_admin_can_run_a_workflow_by_name(self):
        self._as()
        reply = self._post()
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json()["workflow"], "Find a nearby place")

    def test_it_returns_the_shape_the_widget_already_renders(self):
        self._as()
        body = self._post().get_json()
        for key in ("reply", "workflow", "conversation", "choices", "places",
                    "model", "sources", "tool_calls"):
            self.assertIn(key, body)

    def test_a_workflow_nobody_offers_says_so_rather_than_answering(self):
        # The page exists to watch one workflow run. Quietly answering as
        # something else would be it reporting a pass on a test it never ran.
        self._as()
        reply = self.client.post("/workflows/Not%20A%20Workflow/run",
                                 json={"message": "hello"})
        self.assertEqual(reply.status_code, 404)
        self.assertIn("error", reply.get_json())

    def test_an_empty_message_is_refused(self):
        self._as()
        self.assertEqual(self._post(message="   ").status_code, 400)

    def test_an_over_long_message_is_refused(self):
        self._as()
        long = "x" * (web_chat.MAX_MESSAGE_CHARS + 1)
        self.assertEqual(self._post(message=long).status_code, 413)

    def test_a_non_dict_conversation_is_dropped_not_handed_over(self):
        # Client-controlled: a string would reach the workflow as an attribute
        # error.
        self._as()
        self.assertEqual(self._post(conversation="not a dict").status_code, 200)

    def test_a_signed_in_parent_who_is_not_an_admin_cannot_run_one(self):
        self._as(is_admin=False)
        self.assertNotEqual(self._post().status_code, 200)

    def test_a_stranger_cannot_run_one(self):
        # force_workflow was accepted from the body of a public route, so this
        # is stricter than what it replaces.
        with mock.patch.object(guards, "current_parent",
                               return_value=None):
            self.assertNotEqual(self._post().status_code, 200)


class OneOrchestratorEachTest(unittest.TestCase):
    """A workflow runs from the workflow route, and from nowhere else."""

    def test_the_route_calls_run_workflow_turn(self):
        import app as app_module
        app_module.app.config["TESTING"] = True
        with mock.patch.object(guards, "current_parent",
                               return_value={"id": 1, "is_admin": True,
                                             "name": "A", "email": "a@e.com"}), \
             mock.patch.object(web_chat, "run_workflow_turn",
                               return_value={"reply": "ok"}) as ran:
            app_module.app.test_client().post(
                "/workflows/Find a nearby place/run", json={"message": "hi"})
        self.assertEqual(ran.call_args.args[0], "Find a nearby place")
        self.assertIs(ran.call_args.kwargs["forced"], True)

    def test_the_chat_route_runs_no_workflow_at_all(self):
        # The other half of "one orchestrator each". /chatbot reached
        # run_workflow_turn too while the classifier still routed; it does not
        # now, and a workflow appearing there again would be the dual router
        # coming back.
        from src import agent
        with mock.patch.object(agent, "run_workflow_turn") as ran, \
             mock.patch.object(agent, "run_agent",
                               return_value={"reply": "ok", "tool_calls": []}), \
             mock.patch.object(agent, "log_decision"):
            agent.handle_message("a nursing room")
        ran.assert_not_called()


if __name__ == "__main__":
    unittest.main()
