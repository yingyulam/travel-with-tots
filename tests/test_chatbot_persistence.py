"""Structural guards on the chat widget's persistence.

The widget is included on every page, so a full page navigation destroys its
JavaScript state. The transcript is therefore mirrored into sessionStorage and
replayed on load, and "End chat" is the only thing that clears it.

These read the file rather than executing it, so they cannot prove the replay
is correct, only that its contract has not quietly gone away: this is browser
code with no runner in this project. Behaviour was verified separately against
a DOM shim.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

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


class LinkTargetsAreCheckedTest(unittest.TestCase):
    """The widget renders links to found places, and a place found by web
    search carries a URL nobody in this project chose."""

    def test_there_is_an_http_only_allowlist(self):
        source = _source()
        self.assertIn("function twtSafeUrl(", source)
        allowlist = re.search(r"function twtSafeUrl\(.*?\n\}", source,
                              re.DOTALL).group(0)
        self.assertIn("^https?:", allowlist)

    def test_every_href_is_a_literal_or_checked(self):
        # Escaping is not enough for a link target: as a property it could
        # still be "javascript:". Anything not plain http(s) must lose its
        # link rather than be rendered.
        for value in re.findall(r"\.href\s*=\s*([^;]+);", _source()):
            with self.subTest(value=value):
                self.assertTrue(
                    value.strip().startswith('"') or value.strip() == "href",
                    f"unchecked href assignment: {value}")

    def test_the_checked_href_comes_from_the_allowlist(self):
        source = _source()
        self.assertRegex(source, r"const href = twtSafeUrl\(")
        self.assertRegex(source, r"if \(href\) \{")


class TheInputGrowsWithTheMessageTest(unittest.TestCase):
    """One line for a question, taller for a described day. Behaviour was
    verified against a DOM shim; these guard what is easy to lose."""

    def setUp(self):
        with open("templates/_chatbot_widget.html") as f:
            self.markup = f.read()
        with open("static/chatbot.css") as f:
            self.css = f.read()

    def test_it_is_a_textarea_that_starts_at_one_row(self):
        # A one-line input cannot grow at all, and more than one starting row
        # would make a short question look like a form.
        self.assertIn("<textarea", self.markup)
        self.assertIn('rows="1"', self.markup)

    def test_the_height_is_cleared_before_it_is_measured(self):
        # Without this it can only ever grow: scrollHeight would report the
        # box it is already in rather than the text inside it.
        grow = re.search(r"function fitInput\(\).*?\n    \}",
                         _source(), re.DOTALL).group(0)
        self.assertLess(grow.index('input.style.height = "auto"'),
                        grow.index("input.scrollHeight"))

    def test_it_regrows_on_every_keystroke_and_after_sending(self):
        source = _source()
        self.assertIn('input.addEventListener("input", fitInput)', source)
        # Sending empties the box, which has to shrink it back to one line.
        send = re.search(r'input\.value = "";\n\s*fitInput\(\);', source)
        self.assertIsNotNone(send, "the box must shrink after a message is sent")

    def test_it_stops_growing_and_starts_scrolling(self):
        # Otherwise a long paste pushes the send button off the panel.
        box = re.search(r"\.twt-chatbot-form textarea \{.*?\}",
                        self.css, re.DOTALL).group(0)
        self.assertIn("max-height:", box)
        self.assertIn("overflow-y: auto", box)

    def test_it_has_no_drag_handle_of_its_own(self):
        # The panel is already resizable; a second handle inside it that only
        # stretched this box would fight with that one.
        box = re.search(r"\.twt-chatbot-form textarea \{.*?\}",
                        self.css, re.DOTALL).group(0)
        self.assertIn("resize: none", box)


class EnterSendsTest(unittest.TestCase):
    """A textarea makes Enter a newline, so without this the box could only be
    sent with the button."""

    def test_enter_sends_and_shift_enter_does_not(self):
        handler = re.search(r'input\.addEventListener\("keydown".*?\n    \}\);',
                            _source(), re.DOTALL).group(0)
        self.assertIn('event.key !== "Enter" || event.shiftKey', handler)
        self.assertIn("event.preventDefault()", handler)
        self.assertIn("send(input.value.trim())", handler)

    def test_the_placeholder_says_so(self):
        # A textarea normally means Enter is a newline, so the box has to say
        # that it is not.
        with open("templates/_chatbot_widget.html") as f:
            self.assertIn("Enter to send", f.read())


class TheParentCanResizeItTest(unittest.TestCase):
    """A fixed panel is the wrong size for somebody. Behaviour was verified
    against a DOM shim; these guard the parts that are easy to lose."""

    def test_the_size_is_a_variable_the_stylesheet_can_default(self):
        # Set as a custom property rather than assigned outright, so an
        # untouched panel keeps the size the stylesheet chose and a stored one
        # overrides it without the script knowing what the default was.
        with open("static/chatbot.css") as f:
            css = f.read()
        self.assertIn("var(--twt-chat-width,", css)
        self.assertIn("var(--twt-chat-height,", css)
        self.assertIn('panel.style.setProperty("--twt-chat-width"', _source())

    def test_it_is_clamped_at_both_ends(self):
        source = _source()
        # A floor, or the input row becomes unusable; a ceiling, or a size
        # dragged on a wide screen opens off the edge of a narrow one.
        self.assertIn("MIN_SIZE", source)
        self.assertIn("window.innerWidth", source)
        self.assertIn("window.innerHeight", source)

    def test_a_stored_size_is_clamped_on_the_way_back_in(self):
        # Restoring has to go through the same clamp, not straight to the
        # style, since the window it is restored into may be smaller.
        restore = re.search(r"function restoreSize\(\).*?\n    \}",
                            _source(), re.DOTALL).group(0)
        self.assertIn("applySize(", restore)

    def test_the_handle_can_be_used_without_a_pointer(self):
        # A drag-only control is unusable by keyboard, and this one is a real
        # button so it can be tabbed to.
        source = _source()
        self.assertIn('resizeHandle.addEventListener("keydown"', source)
        for key in ("ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"):
            with self.subTest(key=key):
                self.assertIn(key, source)

    def test_the_handle_is_a_labelled_button(self):
        with open("templates/_chatbot_widget.html") as f:
            markup = f.read()
        self.assertIn('class="twt-chatbot-resize"', markup)
        self.assertIn('type="button"', markup)
        self.assertRegex(markup, r'twt-chatbot-resize"\s+aria-label="[^"]+"')

    def test_the_gesture_is_not_lost_to_scrolling(self):
        # Without touch-action the browser claims the drag as a scroll on a
        # touchscreen and pointermove never fires.
        with open("static/chatbot.css") as f:
            css = f.read()
        handle = re.search(r"\.twt-chatbot-resize \{.*?\}", css, re.DOTALL).group(0)
        self.assertIn("touch-action: none", handle)

    def test_the_size_outlives_the_tab_but_the_chat_does_not(self):
        # A preference, like the model, so localStorage; the transcript is
        # sessionStorage because it dies with the conversation.
        source = _source()
        self.assertIn('localStorage.setItem(SIZE_STORAGE_KEY', source)
        self.assertNotIn('sessionStorage.setItem(SIZE_STORAGE_KEY', source)


if __name__ == "__main__":
    unittest.main()
