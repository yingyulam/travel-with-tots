"""Who writes the answer a parent reads, when a tool produced one already.

The FAQ tool hands back ask_website_chatbot's reply: grounded in retrieved
chunks and carrying [Source N] markers. Passing that to the model to word again
cost a whole round trip and left the citations resting on a line in the system
prompt asking it not to touch them.

So the graph stops at the tool, and only resumes when the tool's output was a
working note rather than an answer. "Filled in from their words: destination."
is not something to show anybody; the FAQ's reply is.
"""

import unittest
from unittest import mock

from langchain_core.messages import AIMessage, ToolMessage

from src.agent import FINAL_ANSWER_TOOLS, run_agent


def _faq_message(content="Tap Save on the plan page. [Source 1]"):
    return ToolMessage(content=content, name="answer_faq_tool", tool_call_id="1",
                       artifact={"sources": [{"index": 1, "section": "FAQs"}],
                                 "reply": content, "response_time": 1.0,
                                 "input_tokens": 5, "output_tokens": 2})


def _workflow_message():
    """A workflow tool's message: the workflow's own reply, in the widget's
    shape. Passed through whole, so the model never gets a turn to reword the
    question the chips sit under."""
    return ToolMessage(content="Happy to log it. What's the place called?",
                       name="log_a_place_we_don_t_have", tool_call_id="1",
                       artifact={"reply": "Happy to log it. What's the place called?",
                                 "workflow": "Log a place we don't have",
                                 "conversation": {"workflow": "Log a place we don't have",
                                                  "state": {"stage": "name"}},
                                 "choices": None, "form": None})


def _nearby_message():
    """A tool whose output is a working note, not an answer."""
    return ToolMessage(content="Found A Park, A Mall (curated).",
                       name="find_nearby_tool", tool_call_id="1",
                       artifact={"places": [{"name": "A Park"}], "source": "curated"})


class StopsAtTheToolTest(unittest.TestCase):
    def _run(self, *stages):
        """Each stage is one invoke() return value, in order."""
        built = []

        def build(model, stop_after_tools=False):
            graph = mock.Mock()
            graph.invoke.return_value = {"messages": stages[len(built)]}
            built.append(stop_after_tools)
            return graph

        with mock.patch("src.agent._build_agent", side_effect=build):
            return run_agent("hello"), built

    def test_the_faq_answer_is_returned_word_for_word(self):
        faq = _faq_message()
        result, built = self._run([faq])
        self.assertEqual(result["reply"], faq.content)
        self.assertIn("[Source 1]", result["reply"])

    def test_the_model_is_not_asked_to_word_it_again(self):
        # The saved round trip. One build, and it was the interrupting one.
        _result, built = self._run([_faq_message()])
        self.assertEqual(built, [True])

    def test_citations_still_reach_the_widget(self):
        result, _built = self._run([_faq_message()])
        self.assertEqual(result["sources"], [{"index": 1, "section": "FAQs"}])

    def test_a_working_note_is_handed_back_to_the_model(self):
        # "Found A Park, A Mall (curated)." is not something to show anybody,
        # so the second turn earns its cost. Two builds: stop at the tool, then
        # word the reply.
        nearby = _nearby_message()
        result, built = self._run([nearby], [nearby, AIMessage("Here are two.")])
        self.assertEqual(result["reply"], "Here are two.")
        self.assertEqual(built, [True, False])

    def test_resuming_does_not_run_the_tool_twice(self):
        # The resume is handed the messages so far, so the tool result is
        # already present and there is nothing for the model to call again.
        nearby = _nearby_message()
        result, _built = self._run([nearby], [nearby, AIMessage("ok")])
        self.assertEqual([c["name"] for c in result["tool_calls"]],
                         ["find_nearby_tool"])

    def test_an_answer_with_no_tool_needs_no_second_turn(self):
        result, built = self._run([AIMessage("Hi there!")])
        self.assertEqual(result["reply"], "Hi there!")
        self.assertEqual(built, [True])

    def test_a_failed_faq_lookup_is_still_shown_as_it_is(self):
        # The tool swallows the error into a sentence; that sentence is the
        # answer, so it must not be re-worded either.
        broken = ToolMessage(content="The knowledge base is unavailable right now.",
                             name="answer_faq_tool", tool_call_id="1", artifact={})
        result, built = self._run([broken])
        self.assertIn("unavailable", result["reply"])
        self.assertEqual(built, [True])

    def test_a_tool_that_asks_keeps_its_own_wording(self):
        # A question and the chips that answer it are one thing. Letting the
        # model reword "What do you need right now?" would put different words
        # above the same six buttons every time.
        asking = ToolMessage(content="Sure. What do you need right now?",
                             name="find_nearby_tool", tool_call_id="1",
                             artifact={"choices": ["Family room", "Other"]})
        result, built = self._run([asking])
        self.assertEqual(result["reply"], "Sure. What do you need right now?")
        self.assertEqual(result["choices"], ["Family room", "Other"])
        self.assertEqual(built, [True])

    def test_a_multi_select_row_stays_a_multi_select_row(self):
        asking = ToolMessage(content="What does it offer?",
                             name="log_place_tool", tool_call_id="1",
                             artifact={"choices": ["Family room"],
                                       "choose_many": True})
        result, _built = self._run([asking])
        self.assertIs(result["choose_many"], True)

    def test_a_tool_that_answers_carries_no_chips(self):
        result, _built = self._run([_nearby_message(), AIMessage("Got it.")])
        self.assertIsNone(result["choices"])
        self.assertIs(result["choose_many"], False)

    def test_a_workflow_keeps_its_own_wording(self):
        # The workflow wrote the question and its chips answer that exact
        # wording, so the model does not get a turn to reword it.
        started = _workflow_message()
        result, built = self._run([started])
        self.assertEqual(result["reply"], started.content)
        self.assertEqual(built, [True])

    def test_the_conversation_reaches_the_widget(self):
        """The bug this closes: the extractor ran, read the day correctly, and
        the form was dropped on the floor. The widget draws its handoff card
        from data.form, so the parent got a paragraph describing their own day
        back instead of a form to check."""
        # Without this the next turn would not be in flight and the agent
        # would re-decide a message the workflow already owns.
        result, _built = self._run([_workflow_message()])
        self.assertEqual(result["conversation"]["workflow"],
                         "Log a place we don't have")

    def test_a_faq_answer_starts_no_conversation(self):
        # run_agent carries the key only when a workflow started; the default
        # is handle_message's, which is where the widget's contract is met.
        result, _built = self._run([_faq_message()])
        self.assertFalse(result.get("conversation"))

    def test_which_tools_write_their_own_answers(self):
        # A guard on the list itself. A tool here has its raw output shown to a
        # parent and denies the model a turn, which is right for the FAQ (its
        # answer is already written and cited) and for the extractor (a turn
        # there is a turn to write an itinerary in).
        from src.workflows import runnable_message_workflows
        from src.agent import _slug
        self.assertEqual(
            set(FINAL_ANSWER_TOOLS),
            {"answer_faq_tool"} | {_slug(w["name"])
                                   for w, _ in runnable_message_workflows()})


if __name__ == "__main__":
    unittest.main()
