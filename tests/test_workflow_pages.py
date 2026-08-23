"""Each workflow's "Try it" link goes to a page that runs that workflow.

The bug this exists for: "Find a nearby place" pointed at the Find Nearby
component's page, which calls the component directly and never runs the
workflow. The dashboard offered "Try it" and took you somewhere that did not.
"""

import re
import unittest
from unittest import mock

import app as app_module
from src.workflows import WORKFLOWS

ADMIN = {"id": 1, "email": "a@b.c", "name": "A", "is_admin": True}
PARENT = {"id": 2, "email": "p@b.c", "name": "P", "is_admin": False}

# Every workflow test page: the endpoint, and the script that drives it.
WORKFLOW_PAGES = {
    "plan_from_chat_page": ("/workflows/plan-from-chat", "static/plan-from-chat.js"),
    "find_nearby_place_page": ("/workflows/find-nearby-place",
                               "static/find-nearby-place.js"),
}


def _script(path):
    with open(path) as f:
        return f.read()


class EveryWorkflowPageRunsItsOwnWorkflowTest(unittest.TestCase):
    def test_find_nearby_no_longer_points_at_the_component_page(self):
        workflow = next(w for w in WORKFLOWS if w["name"] == "Find a nearby place")
        self.assertEqual(workflow["page"], "find_nearby_place_page")
        self.assertNotEqual(workflow["page"], "find_nearby_page",
                            "that page calls the component, not the workflow")

    def test_every_page_key_names_a_real_endpoint(self):
        # url_for raises at render time otherwise, which the dashboard test
        # would catch, but only for a workflow that has a page at all.
        endpoints = {rule.endpoint for rule in app_module.app.url_map.iter_rules()}
        for workflow in WORKFLOWS:
            if workflow.get("page"):
                with self.subTest(workflow=workflow["name"]):
                    self.assertIn(workflow["page"], endpoints)


class WorkflowPagesAreAdminOnlyTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_an_admin_gets_the_page(self):
        for endpoint, (url, _) in WORKFLOW_PAGES.items():
            with self.subTest(endpoint=endpoint), \
                 mock.patch.object(app_module, "_current_parent", return_value=ADMIN):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_anonymous_and_non_admin_are_turned_away(self):
        for endpoint, (url, _) in WORKFLOW_PAGES.items():
            for who in (None, PARENT):
                with self.subTest(endpoint=endpoint, who=who), \
                     mock.patch.object(app_module, "_current_parent", return_value=who):
                    self.assertEqual(self.client.get(url).status_code, 302)


class ThePageOffersRunAndListenTest(unittest.TestCase):
    """The course pattern: run the workflow once, or keep handling messages."""

    def setUp(self):
        with mock.patch.object(app_module, "_current_parent", return_value=ADMIN):
            self.html = app_module.app.test_client().get(
                "/workflows/find-nearby-place").get_data(as_text=True)

    def test_both_buttons_are_there(self):
        self.assertIn("▶ Run once", self.html)
        self.assertIn("👂 Listen", self.html)

    def test_the_status_banner_starts_disarmed(self):
        self.assertIn('data-state="off"', self.html)
        self.assertIn("Not watching", self.html)

    def test_the_page_loads_its_own_script(self):
        self.assertIn("find-nearby-place.js", self.html)


class TheScriptIsSafeTest(unittest.TestCase):
    """Places can come from a live web search, so their URLs are chosen by
    nobody in this project."""

    def setUp(self):
        self.source = _script("static/find-nearby-place.js")

    def test_every_href_is_a_literal_or_checked(self):
        for value in re.findall(r"\.href\s*=\s*([^;]+);", self.source):
            with self.subTest(value=value):
                self.assertTrue(value.strip().startswith('"')
                                or value.strip() == "href",
                                f"unchecked href assignment: {value}")

    def test_the_link_target_goes_through_the_allowlist(self):
        self.assertIn("const href = twtSafeUrl(place.maps_url);", self.source)

    def test_nothing_is_assigned_to_innerhtml(self):
        self.assertEqual(re.findall(r"\.innerHTML\s*=", self.source), [])

    def test_no_other_html_parsing_sink(self):
        for sink in ("outerHTML", "insertAdjacentHTML", "document.write",
                     "createContextualFragment"):
            with self.subTest(sink=sink):
                self.assertNotIn(sink, self.source)


class BothPagesArmTheSameWayTest(unittest.TestCase):
    """Two workflow pages that armed themselves differently would be two
    things to learn, so the mode machine is deliberately identical."""

    def test_each_script_has_the_three_state_mode_machine(self):
        for _, (_, path) in WORKFLOW_PAGES.items():
            source = _script(path)
            with self.subTest(path=path):
                self.assertIn('let mode = "off";', source)
                self.assertIn('setMode("once")', source)
                self.assertIn('mode === "many" ? "off" : "many"', source)
                self.assertIn('setMode(mode === "once" ? "off" : "many")', source)

    def test_each_ignores_replies_while_off(self):
        for _, (_, path) in WORKFLOW_PAGES.items():
            with self.subTest(path=path):
                self.assertIn('if (mode === "off") return;', _script(path))


if __name__ == "__main__":
    unittest.main()
