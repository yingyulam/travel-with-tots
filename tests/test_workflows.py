import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import unittest
from src.web import guards
from unittest import mock

from markupsafe import escape

from src.workflows import TRIGGERS, WORKFLOWS, workflows_by_trigger

TRIGGER_KEYS = {key for key, _ in TRIGGERS}


class WorkflowDeclarationTest(unittest.TestCase):
    """Guards the declaration shape. Without this, a workflow added with a
    field missing would only fail when someone loads the page."""

    def test_every_workflow_is_complete(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow.get("name")):
                for field in ("name", "emoji", "trigger", "description", "steps"):
                    self.assertTrue(workflow.get(field), f"missing {field}")

    def test_every_trigger_is_a_known_one(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow["name"]):
                self.assertIn(workflow["trigger"], TRIGGER_KEYS)

    def test_descriptions_are_one_to_three_sentences(self):
        for workflow in WORKFLOWS:
            with self.subTest(workflow=workflow["name"]):
                sentences = workflow["description"].count(".")
                self.assertGreaterEqual(sentences, 1)
                self.assertLessEqual(sentences, 3)

    def test_every_step_names_a_component_and_says_if_it_is_built(self):
        for workflow in WORKFLOWS:
            for step in workflow["steps"]:
                with self.subTest(workflow=workflow["name"], step=step):
                    self.assertTrue(step.get("component"))
                    self.assertIsInstance(step.get("built"), bool)

    def test_names_are_unique(self):
        names = [w["name"] for w in WORKFLOWS]
        self.assertEqual(len(names), len(set(names)))


class GroupByTriggerTest(unittest.TestCase):
    def test_groups_are_in_trigger_order(self):
        labels = [label for label, _ in workflows_by_trigger()]
        expected = [label for key, label in TRIGGERS
                    if any(w["trigger"] == key for w in WORKFLOWS)]
        self.assertEqual(labels, expected)

    def test_empty_triggers_are_skipped(self):
        # No workflow is scheduled today, so that heading must not render
        # with nothing under it.
        for label, workflows in workflows_by_trigger():
            self.assertTrue(workflows, f"{label} rendered with no workflows")

    def test_every_workflow_appears_exactly_once(self):
        grouped = [w for _, workflows in workflows_by_trigger() for w in workflows]
        self.assertEqual(len(grouped), len(WORKFLOWS))
        self.assertEqual({w["name"] for w in grouped},
                         {w["name"] for w in WORKFLOWS})


class WorkflowsPageTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.admin = {"id": 1, "is_admin": True, "name": "Admin", "email": "a@b.com"}

    def test_page_lists_every_workflow_and_its_components(self):
        with mock.patch.object(guards, "current_parent", return_value=self.admin):
            resp = self.client.get("/workflows")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Compared escaped, because the template escapes: a name containing an
        # apostrophe reaches the page as &#39; and a raw comparison would call
        # that missing. markupsafe rather than html.escape, since that is what
        # Jinja itself uses and the two differ on quotes (&#39; vs &#x27;).
        for workflow in WORKFLOWS:
            self.assertIn(str(escape(workflow["name"])), html)
            for step in workflow["steps"]:
                self.assertIn(str(escape(step["component"])), html)

    def test_unbuilt_steps_are_marked_pending(self):
        with mock.patch.object(guards, "current_parent", return_value=self.admin):
            html = self.client.get("/workflows").get_data(as_text=True)
        unbuilt = sum(1 for w in WORKFLOWS for s in w["steps"] if not s["built"])
        self.assertEqual(html.count("badge-pending"), unbuilt)

    def test_requires_an_admin(self):
        with mock.patch.object(guards, "current_parent", return_value=None):
            self.assertEqual(self.client.get("/workflows").status_code, 302)
        parent = {**self.admin, "is_admin": False}
        with mock.patch.object(guards, "current_parent", return_value=parent):
            self.assertEqual(self.client.get("/workflows").status_code, 302)


if __name__ == "__main__":
    unittest.main()
