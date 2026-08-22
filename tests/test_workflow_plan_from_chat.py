import unittest
from unittest import mock

from src.workflows.plan_from_chat import WORKFLOW, run


def _agent_result(reply="ok", tool_calls=()):
    """run_agent's shape, which is what `run` consumes."""
    return {"reply": reply, "sources": [], "model": "openrouter/free",
            "response_time": 1.0, "input_tokens": 1, "output_tokens": 1,
            "tool_calls": list(tool_calls)}


def _extraction(form=None, found=None):
    return {"name": "extract_form_tool", "output": "Filled in: destination.",
            "data": {"form": form or {"destination": "Vancouver"},
                     "found": found or ["destination"]}}


class RunTest(unittest.TestCase):
    def test_returns_the_form_when_the_extractor_ran(self):
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(tool_calls=[_extraction()])):
            result = run("a day in Vancouver")
        self.assertEqual(result["form"]["destination"], "Vancouver")
        self.assertEqual(result["found"], ["destination"])
        self.assertEqual(result["tool_calls"], ["extract_form_tool"])

    def test_form_is_none_when_the_agent_answered_instead(self):
        # None rather than {} on purpose: a caller must be able to tell "no
        # form was extracted" from "an empty form was extracted", or the page
        # shows blank fields as though they were read from the message.
        faq = {"name": "answer_faq_tool", "output": "Tap Save.",
               "data": {"sources": [{"index": 1}]}}
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(reply="Tap Save.",
                                                   tool_calls=[faq])):
            result = run("how do I save a plan?")
        self.assertIsNone(result["form"])
        self.assertEqual(result["found"], [])
        self.assertEqual(result["tool_calls"], ["answer_faq_tool"])

    def test_form_is_none_when_no_tool_ran(self):
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(reply="I can't help.")):
            result = run("hello")
        self.assertIsNone(result["form"])
        self.assertEqual(result["tool_calls"], [])

    def test_a_failed_extraction_is_not_mistaken_for_a_form(self):
        # The tool swallows its own errors and returns an empty artifact, which
        # must not read as a successfully extracted form.
        failed = {"name": "extract_form_tool",
                  "output": "Couldn't read a form from that.", "data": {}}
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(tool_calls=[failed])):
            result = run("a day in Vancouver")
        self.assertIsNone(result["form"])

    def test_a_failed_extraction_reads_differently_from_a_different_tool(self):
        # Observed live: the extractor failed and the agent went on to plan the
        # trip anyway. Reporting that as "used another tool" would hide the
        # failure, so the two cases must give different notes.
        failed = {"name": "extract_form_tool",
                  "output": "Couldn't read a form from that.", "data": {}}
        planned = {"name": "plan_trip_tool", "output": "A plan.", "data": {}}
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(tool_calls=[failed, planned])):
            failure = run("a day in Vancouver")
        self.assertIn("Couldn't read a form", failure["note"])

        faq = {"name": "answer_faq_tool", "output": "Tap Save.", "data": {"sources": []}}
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(tool_calls=[faq])):
            other = run("how do I save a plan?")
        self.assertIn("answer_faq_tool", other["note"])
        self.assertNotEqual(failure["note"], other["note"])

    def test_no_note_when_a_form_came_back(self):
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(tool_calls=[_extraction()])):
            self.assertIsNone(run("a day in Vancouver")["note"])

    def test_passes_the_chosen_model_through(self):
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result()) as agent:
            run("hello", model="nvidia/nemotron-3-super-120b-a12b:free")
        self.assertEqual(agent.call_args.kwargs["model"],
                         "nvidia/nemotron-3-super-120b-a12b:free")

    def test_relays_the_agents_reply(self):
        with mock.patch("src.workflows.plan_from_chat.run_agent",
                        return_value=_agent_result(reply="Got it.")):
            self.assertEqual(run("x")["reply"], "Got it.")


class DeclarationTest(unittest.TestCase):
    def test_the_declaration_points_at_its_test_page(self):
        self.assertEqual(WORKFLOW["page"], "plan_from_chat_page")

    def test_every_step_is_built_now(self):
        self.assertTrue(all(step["built"] for step in WORKFLOW["steps"]))


class PageTest(unittest.TestCase):
    def setUp(self):
        import app as app_module
        self.app_module = app_module
        self.client = app_module.app.test_client()
        self.admin = {"id": 1, "is_admin": True, "name": "A", "email": "a@b.com"}

    def test_page_renders_for_an_admin(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            resp = self.client.get("/workflows/plan-from-chat")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Plan from chat", resp.get_data(as_text=True))

    def test_page_is_admin_only(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=None):
            self.assertEqual(
                self.client.get("/workflows/plan-from-chat").status_code, 302)

    def test_the_workflows_page_links_to_it(self):
        with mock.patch.object(self.app_module, "_current_parent", return_value=self.admin):
            html = self.client.get("/workflows").get_data(as_text=True)
        self.assertIn("/workflows/plan-from-chat", html)


if __name__ == "__main__":
    unittest.main()
