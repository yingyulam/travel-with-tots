"""A [Source N] marker has to have a source behind it.

The widget renders every marker as a button and looks the number up in
`sources`; a marker with nothing behind it is a dead chip reading "Source
details unavailable". Worse than dead, it presents a claim as cited when
nothing was retrieved to support it, which is what this retrieval path exists
to prevent.

Reported from a real transcript: a reply badged "answered directly" -- no tool
called, so no sources -- carrying a [2]. The model had answered a follow-up
from the conversation and copied a marker out of its own earlier answer.
"""

import unittest
from unittest import mock

from langchain_core.messages import AIMessage, ToolMessage

from src.agent import _only_earned_citations, run_agent


class OnlyEarnedCitationsTest(unittest.TestCase):
    def test_a_marker_with_no_sources_at_all_goes(self):
        self.assertEqual(
            _only_earned_citations("It plans a day [Source 2].", []),
            "It plans a day.")

    def test_a_marker_outside_the_retrieved_set_goes(self):
        # Retrieval ran and returned one chunk; a citation of a third does not
        # become true because the rest of the answer was grounded.
        self.assertEqual(
            _only_earned_citations("Tap Save [Source 1]. Or not [Source 3].",
                                   [{"index": 1}]),
            "Tap Save [Source 1]. Or not.")

    def test_real_markers_are_left_exactly_as_they_are(self):
        text = "It plans a day [Source 1] [Source 2]."
        self.assertEqual(
            _only_earned_citations(text, [{"index": 1}, {"index": 2}]), text)

    def test_the_space_before_a_dropped_marker_goes_with_it(self):
        # Otherwise the sentence ends " ." and reads like a typo.
        self.assertEqual(_only_earned_citations("A day [Source 2].", []),
                         "A day.")

    def test_a_bracket_that_is_not_a_citation_is_untouched(self):
        for text in ("Try [outsourced] tools.", "See [1] below."):
            with self.subTest(text=text):
                self.assertEqual(_only_earned_citations(text, []), text)

    def test_an_answer_with_no_markers_is_unchanged(self):
        self.assertEqual(_only_earned_citations("Hello there.", []),
                         "Hello there.")


class ThroughRunAgentTest(unittest.TestCase):
    def _run(self, messages):
        graph = mock.Mock()
        graph.invoke.return_value = {"messages": messages}
        with mock.patch("src.agent._build_agent", return_value=graph):
            return run_agent("hello")

    def test_the_reported_case_no_tool_but_a_citation(self):
        result = self._run([AIMessage("You can plan a day [Source 2].")])
        self.assertEqual(result["reply"], "You can plan a day.")
        self.assertEqual(result["sources"], [])

    def test_a_retrieved_answer_keeps_its_citations(self):
        faq = ToolMessage(content="Tap Save [Source 1].", name="answer_faq_tool",
                          tool_call_id="1",
                          artifact={"sources": [{"index": 1, "section": "FAQs"}],
                                    "reply": "Tap Save [Source 1]."})
        result = self._run([faq])
        self.assertEqual(result["reply"], "Tap Save [Source 1].")
        self.assertEqual(len(result["sources"]), 1)


if __name__ == "__main__":
    unittest.main()


class AlwaysGroundedTest(unittest.TestCase):
    """A question the knowledge base can answer is answered from it.

    Measured: by the third turn of a conversation the model stopped calling
    answer_faq_tool, 4 times out of 4, because two knowledge-base answers were
    already in the transcript. The replies were correct and ungrounded, which
    is the guarantee retrieval exists to give. Naming it in the prompt took it
    to 1 in 4; this takes it to 0.

    What decides whether a message is about the site is retrieval, not the
    model: MIN_SIMILARITY already separates off-topic (~0.11) from real
    questions (~0.31), so a tool-less turn is offered to the knowledge base and
    kept only if something comes back.
    """

    def _turn(self, retrieved, faq_reply="Grounded. [Source 1]"):
        graph = mock.Mock()
        graph.invoke.return_value = {"messages": [AIMessage("From memory.")]}
        with mock.patch("src.agent._build_agent", return_value=graph), \
             mock.patch("src.agent.rag.retrieve", return_value=retrieved), \
             mock.patch("src.agent.ask_website_chatbot",
                        return_value={"reply": faq_reply,
                                      "sources": retrieved,
                                      "response_time": 1.0,
                                      "input_tokens": 1, "output_tokens": 1}):
            return run_agent("how does replanning work")

    def test_a_tool_less_turn_the_knowledge_base_can_answer_is_grounded(self):
        result = self._turn([{"index": 1, "section": "Re-planning"}])
        self.assertEqual(result["reply"], "Grounded. [Source 1]")
        self.assertEqual(len(result["sources"]), 1)

    def test_it_is_reported_as_the_tool_that_answered(self):
        # The badge and data/intents.jsonl both read tool_calls, and the FAQ
        # tool is what produced this answer.
        result = self._turn([{"index": 1, "section": "Re-planning"}])
        self.assertEqual([c["name"] for c in result["tool_calls"]],
                         ["answer_faq_tool"])

    def test_a_greeting_is_left_alone(self):
        # Retrieval finds nothing above the threshold, so the direct answer
        # stands. Dragging "hello" into the knowledge base would be worse.
        result = self._turn([])
        self.assertEqual(result["reply"], "From memory.")
        self.assertEqual(result["tool_calls"], [])

    def test_a_retrieval_blip_leaves_the_direct_answer(self):
        # The agent already has a reply; losing it to a network error would be
        # a worse turn than an ungrounded one.
        import requests
        graph = mock.Mock()
        graph.invoke.return_value = {"messages": [AIMessage("From memory.")]}
        with mock.patch("src.agent._build_agent", return_value=graph), \
             mock.patch("src.agent.rag.retrieve",
                        side_effect=requests.exceptions.RequestException("down")):
            result = run_agent("how does replanning work")
        self.assertEqual(result["reply"], "From memory.")
