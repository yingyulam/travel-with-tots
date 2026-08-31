import unittest
from unittest import mock

import requests
from langchain_core.messages import AIMessage, ToolMessage

from src.components.extract_form import FormExtractionError
from src.agent import (
    TOOLS,
    answer_faq_tool,
    extract_form_tool,
    run_agent,
)


def _fake_result(reply, tool_messages=()):
    """What create_react_agent().invoke() hands back: a message list ending in
    the assistant's reply, with any ToolMessages earlier in it."""
    return {"messages": [*tool_messages, AIMessage(reply)]}


class ToolArtifactTest(unittest.TestCase):
    """The load-bearing mechanism. A tool returning a plain dict has it
    JSON-stringified onto ToolMessage.content by LangGraph, so the caller can
    only recover text. These tools return (text, dict) so the dict survives on
    .artifact, which is what carries the form and the FAQ's citations back."""

    def test_extractor_result_survives_as_a_dict(self):
        extracted = {"form": {"destination": "Vancouver"}, "found": ["destination"]}
        with mock.patch("src.agent.extract_form", return_value=extracted):
            message = extract_form_tool.invoke(
                {"args": {"description": "a day in Vancouver"}, "id": "1",
                 "name": "extract_form_tool", "type": "tool_call"})
        self.assertIsInstance(message, ToolMessage)
        self.assertEqual(message.artifact, extracted)
        self.assertIsInstance(message.content, str)
        self.assertIn("destination", message.content)  # the model-facing summary

    def test_faq_result_survives_with_its_sources(self):
        answer = {"reply": "Tap Save this plan.", "sources": [{"index": 1}],
                  "model": "m", "response_time": 1.0,
                  "input_tokens": 10, "output_tokens": 5}
        with mock.patch("src.agent.ask_website_chatbot", return_value=answer):
            message = answer_faq_tool.invoke(
                {"args": {"question": "how do I save a plan?"}, "id": "1",
                 "name": "answer_faq_tool", "type": "tool_call"})
        self.assertEqual(message.artifact["sources"], [{"index": 1}])
        self.assertEqual(message.content, "Tap Save this plan.")


class ToolErrorHandlingTest(unittest.TestCase):
    """The chat route catches only KeyError and OpenAIError, so anything else
    raised inside a tool escapes as a 500. extract_form does raise, unlike the
    older tools, so each tool has to swallow its own failures."""

    def _invoke_extractor(self):
        return extract_form_tool.invoke(
            {"args": {"description": "x"}, "id": "1",
             "name": "extract_form_tool", "type": "tool_call"})

    def test_extraction_failure_becomes_a_readable_result(self):
        for error in (FormExtractionError("bad json"),
                      requests.exceptions.RequestException("down"),
                      KeyError("OPENROUTER_API_KEY")):
            with self.subTest(error=type(error).__name__):
                with mock.patch("src.agent.extract_form", side_effect=error):
                    message = self._invoke_extractor()
                self.assertIn("Couldn't read a form", message.content)
                self.assertEqual(message.artifact, {})

    def test_faq_failure_becomes_a_readable_result(self):
        with mock.patch("src.agent.ask_website_chatbot",
                        side_effect=requests.exceptions.RequestException("down")):
            message = answer_faq_tool.invoke(
                {"args": {"question": "x"}, "id": "1",
                 "name": "answer_faq_tool", "type": "tool_call"})
        self.assertIn("unavailable", message.content)
        self.assertEqual(message.artifact, {})


class RunAgentContractTest(unittest.TestCase):
    """run_agent has to return what the chat widget already consumes, or
    citations stop rendering and ratings stop recording model and timing."""

    def _run(self, reply="ok", tool_messages=(), model=None):
        agent = mock.Mock()
        agent.invoke.return_value = _fake_result(reply, tool_messages)
        with mock.patch("src.agent._build_agent", return_value=agent) as build:
            result = run_agent("hello", model=model) if model else run_agent("hello")
        return result, build

    def test_returns_every_key_the_widget_uses(self):
        result, _ = self._run()
        for key in ("reply", "sources", "model", "response_time",
                    "input_tokens", "output_tokens", "tool_calls"):
            self.assertIn(key, result)

    def test_faq_citations_and_usage_are_surfaced(self):
        faq = ToolMessage(content="Tap Save.", name="answer_faq_tool",
                          tool_call_id="1",
                          artifact={"sources": [{"index": 1}], "response_time": 2.5,
                                    "input_tokens": 11, "output_tokens": 3})
        result, _ = self._run(reply="Tap Save.", tool_messages=(faq,))
        self.assertEqual(result["sources"], [{"index": 1}])
        self.assertEqual(result["response_time"], 2.5)
        self.assertEqual(result["input_tokens"], 11)

    def test_a_direct_answer_reports_no_tools_and_no_sources(self):
        result, _ = self._run(reply="I can't help with that.")
        self.assertEqual(result["tool_calls"], [])
        self.assertEqual(result["sources"], [])

    def test_tool_calls_carry_name_text_and_data(self):
        extraction = ToolMessage(content="Filled in: destination.",
                                 name="extract_form_tool", tool_call_id="1",
                                 artifact={"form": {"destination": "Vancouver"},
                                           "found": ["destination"]})
        result, _ = self._run(tool_messages=(extraction,))
        call = result["tool_calls"][0]
        self.assertEqual(call["name"], "extract_form_tool")
        self.assertIn("Filled in", call["output"])
        self.assertEqual(call["data"]["form"]["destination"], "Vancouver")

    def test_the_chosen_model_is_used_and_reported(self):
        result, build = self._run(model="nvidia/nemotron-3-super-120b-a12b:free")
        # Every build, not one: a turn may build the agent twice, once to stop
        # at the tool and once to word the answer from it. The model must be
        # the parent's choice on both, and pinning the call count instead made
        # this fail for a change that never touched model selection.
        self.assertTrue(build.call_args_list)
        for call in build.call_args_list:
            self.assertEqual(call.args[0], "nvidia/nemotron-3-super-120b-a12b:free")
        self.assertEqual(result["model"], "nvidia/nemotron-3-super-120b-a12b:free")


class ChatBubbleContractTest(unittest.TestCase):
    """The bubble now talks to the agent instead of ask_website_chatbot. If
    this regresses, citations stop rendering and a rating stops recording model,
    tokens, and timing, which is silent data loss rather than a visible break."""

    def setUp(self):
        import app as app_module
        self.client = app_module.app.test_client()

    def _ask(self, message="how do I save a plan?"):
        faq = ToolMessage(
            content="Tap Save this plan. [Source 1]", name="answer_faq_tool",
            tool_call_id="1",
            artifact={"reply": "Tap Save this plan. [Source 1]",
                      "sources": [{"index": 1, "section": "Saving",
                                   "score": 0.9, "text": "..."}],
                      "model": "openai/gpt-4o-mini", "response_time": 2.1,
                      "input_tokens": 120, "output_tokens": 18})
        agent = mock.Mock()
        agent.invoke.return_value = _fake_result(
            "Tap Save this plan. [Source 1]", (faq,))
        # classify_intent too: /chatbot routes through it before reaching the
        # agent, so without this a unit test makes a real, paid model call to
        # be told the message matches no workflow.
        with mock.patch("src.agent._build_agent", return_value=agent), \
             mock.patch("src.agent.classify_intent", return_value="none"), \
             mock.patch("src.rag.get_status", return_value={"state": "ready"}):
            return self.client.post("/chatbot", json={"message": message,
                                                      "model": "openai/gpt-4o-mini"})

    def test_a_question_still_returns_everything_the_widget_renders(self):
        resp = self._ask()
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertIn("[Source 1]", body["reply"])
        self.assertEqual(body["sources"][0]["index"], 1)
        for key in ("model", "response_time", "input_tokens", "output_tokens"):
            self.assertIsNotNone(body[key], f"{key} lost, ratings would degrade")

    def test_an_empty_message_is_still_rejected(self):
        self.assertEqual(
            self.client.post("/chatbot", json={"message": "  "}).status_code, 400)

    def test_still_waits_for_the_knowledge_base(self):
        with mock.patch("src.rag.get_status", return_value={"state": "indexing"}):
            resp = self.client.post("/chatbot", json={"message": "hi"})
        self.assertEqual(resp.status_code, 503)


class ToolRegistrationTest(unittest.TestCase):
    def test_the_agent_can_answer_questions_and_extract_forms(self):
        # Without the FAQ tool the bubble would lose knowledge-base answers the
        # moment it started talking to the agent instead.
        names = {t.name for t in TOOLS}
        self.assertIn("answer_faq_tool", names)
        self.assertIn("extract_form_tool", names)


if __name__ == "__main__":
    unittest.main()
