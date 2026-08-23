"""Structural guards on the chat widget's persistence.

The widget is included on every page, so a full page navigation destroys its
JavaScript state. The transcript is therefore mirrored into sessionStorage and
replayed on load, and "End chat" is the only thing that clears it.

These read the file rather than executing it, so they cannot prove the replay
is correct, only that its contract has not quietly gone away: this is browser
code with no runner in this project. Behaviour was verified separately against
a DOM shim.
"""

import re
import unittest

CHATBOT_JS = "static/chatbot.js"


def _source() -> str:
    with open(CHATBOT_JS) as f:
        return f.read()


class TranscriptSurvivesNavigationTest(unittest.TestCase):
    def test_the_transcript_is_written_to_session_storage(self):
        source = _source()
        self.assertIn("sessionStorage.setItem(SESSION_STORAGE_KEY", source)
        self.assertIn("turns: turns.slice(-MAX_STORED_TURNS)", source)

    def test_it_is_read_back_on_load(self):
        source = _source()
        self.assertIn("sessionStorage.getItem(SESSION_STORAGE_KEY)", source)
        # Restoring has to actually run at load, not merely be defined.
        self.assertRegex(source, r"\n\s*restore\(\);")

    def test_the_conversation_and_the_open_panel_travel_with_it(self):
        # Without these, the transcript comes back but a half-filled form is
        # forgotten and the panel is found closed mid-answer.
        saved = re.search(r"sessionStorage\.setItem\(SESSION_STORAGE_KEY.*?\}\)\);",
                          _source(), re.DOTALL).group(0)
        for key in ("open:", "greeted,", "planOffered,", "conversation,",
                    "history,", "turns:"):
            with self.subTest(key=key):
                self.assertIn(key, saved)


class OnlyEndChatClearsItTest(unittest.TestCase):
    def test_end_chat_removes_the_stored_chat(self):
        end_handler = re.search(r'endBtn\.addEventListener\("click".*?\}\);',
                                _source(), re.DOTALL).group(0)
        self.assertIn("sessionStorage.removeItem(SESSION_STORAGE_KEY)", end_handler)

    def test_nothing_else_clears_it_except_an_unreadable_value(self):
        # Two removals only: End chat, and discarding a value that will not
        # parse. A third would be something quietly ending the chat.
        source = _source()
        self.assertEqual(
            source.count("sessionStorage.removeItem(SESSION_STORAGE_KEY)"), 2)


class RestoredTranscriptIsNotMarkupTest(unittest.TestCase):
    def test_innerhtml_is_only_ever_cleared_never_assigned_content(self):
        # The stored transcript is caller-influenced text. Replaying it through
        # innerHTML would run it as markup, which is the shape trip.html was
        # rewritten to remove.
        assignments = re.findall(r"\.innerHTML\s*=\s*([^;]+);", _source())
        self.assertTrue(assignments, "expected the existing clearing assignments")
        for value in assignments:
            with self.subTest(value=value):
                self.assertEqual(value.strip(), '""')

    def test_no_other_html_parsing_sink(self):
        for sink in ("outerHTML", "insertAdjacentHTML", "document.write",
                     "createContextualFragment"):
            with self.subTest(sink=sink):
                self.assertNotIn(sink, _source())


if __name__ == "__main__":
    unittest.main()
