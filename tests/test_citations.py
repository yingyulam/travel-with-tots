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
