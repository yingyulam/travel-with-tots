import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import unittest
from src.web import guards
from unittest import mock

from src.workflows.plan_from_chat import WORKFLOW


class DeclarationTest(unittest.TestCase):
    def test_the_declaration_points_at_its_test_page(self):
        self.assertEqual(WORKFLOW["page"], "devpages.plan_from_chat_page")

    def test_every_step_is_built_now(self):
        self.assertTrue(all(step["built"] for step in WORKFLOW["steps"]))

    def test_the_chain_stops_at_the_form(self):
        # It used to claim a third, planning step it never ran. Building the day
        # is a separate step, so a planner here would be advertising a chain the
        # workflow does not have.
        components = [step["component"] for step in WORKFLOW["steps"]]
        self.assertEqual(components, ["AI Agent (OpenRouter)", "Form extractor"])

    def test_the_name_says_it_fills_the_form(self):
        self.assertIn("form", WORKFLOW["name"].lower())
        self.assertNotIn("plan", WORKFLOW["name"].lower())


class PageTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.admin = {"id": 1, "is_admin": True, "name": "A", "email": "a@b.com"}

    def test_page_renders_for_an_admin(self):
        with mock.patch.object(guards, "current_parent", return_value=self.admin):
            resp = self.client.get("/workflows/plan-from-chat")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Fill the form from chat", resp.get_data(as_text=True))

    def test_page_is_admin_only(self):
        with mock.patch.object(guards, "current_parent", return_value=None):
            self.assertEqual(
                self.client.get("/workflows/plan-from-chat").status_code, 302)

    def test_the_workflows_page_links_to_it(self):
        with mock.patch.object(guards, "current_parent", return_value=self.admin):
            html = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn("/workflows/plan-from-chat", html)


if __name__ == "__main__":
    unittest.main()
