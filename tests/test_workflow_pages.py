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
    "log_place_from_chat_page": ("/workflows/log-a-place",
                                 "static/log-place-from-chat.js"),
}

TEMPLATES = {
    "plan_from_chat_page": "templates/plan_from_chat.html",
    "find_nearby_place_page": "templates/find_nearby_place.html",
    "log_place_from_chat_page": "templates/log_place_from_chat.html",
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


class EveryPageArmsTheSameWayTest(unittest.TestCase):
    """Workflow pages that armed themselves differently would be several
    things to learn, so there is one machine and they all call it."""

    WATCHER = "static/workflow-watch.js"

    def test_the_machine_lives_in_one_place(self):
        source = _script(self.WATCHER)
        self.assertIn('let mode = "off";', source)
        self.assertIn('setMode("once")', source)
        self.assertIn('mode === "many" ? "off" : "many"', source)
        self.assertIn('if (mode === "off") return;', source)

    def test_run_spans_one_execution_and_listen_spans_several(self):
        # "Once" is one execution, not one turn: a conversational workflow asks
        # follow-ups, so Run holds through them and lets go when the workflow
        # finishes. Listen never lets go on its own.
        source = _script(self.WATCHER)
        self.assertIn("const finished = !event.detail.conversation;", source)
        self.assertIn('if (mode === "once" && finished) setMode("off", true);',
                      source)

    def test_arming_routes_messages_to_this_workflow(self):
        # The chat is both a workflow's input and the general front door, so
        # without this a page cannot reach its own workflow when the classifier
        # prefers another.
        source = _script(self.WATCHER)
        self.assertIn("window.twtForceWorkflow = mode === \"off\" ? null : workflow;",
                      source)
        self.assertIn("force_workflow: window.twtForceWorkflow || null,",
                      _script("static/chatbot.js"))

    def test_every_page_names_the_workflow_it_arms(self):
        for _, (_, path) in WORKFLOW_PAGES.items():
            with self.subTest(path=path):
                self.assertIn("workflow: WORKFLOW_NAME,", _script(path))

    def test_no_page_keeps_a_copy_of_it(self):
        # Three copies is what prompted the extraction; a fourth would undo it.
        for _, (_, path) in WORKFLOW_PAGES.items():
            with self.subTest(path=path):
                self.assertNotIn('let mode = "off";', _script(path))

    def test_every_page_calls_the_shared_watcher(self):
        for _, (_, path) in WORKFLOW_PAGES.items():
            with self.subTest(path=path):
                self.assertIn("watchChatReplies({", _script(path))

    def test_every_page_loads_it_before_its_own_script(self):
        # Order matters: the helper is a plain global, so a page loading its
        # own script first would call an undefined function.
        for endpoint, (_, path) in WORKFLOW_PAGES.items():
            template = TEMPLATES[endpoint]
            with self.subTest(template=template):
                markup = _script(template)
                own = path.split("/")[-1]
                self.assertLess(markup.index("workflow-watch.js"),
                                markup.index(own))


if __name__ == "__main__":
    unittest.main()
