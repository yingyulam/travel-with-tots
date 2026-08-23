"""Structural guards on the chat's handoff to the planning page.

The confirmation reply carries a hidden form that posts the collected fields to
/plan: "Open the form" with a `prefill` marker so the route fills the boxes and
stops, "Generate my day" without it so the route's existing generate branch
runs. Generating is a real AI call of ten seconds and up, which is what shaped
the two rules here: it happens in this tab, and it says that it is working.

Read rather than executed, like the other guards on this file. Behaviour was
verified separately against a DOM shim, posting the fields the widget really
builds into the real route.
"""

import re
import unittest

CHATBOT_JS = "static/chatbot.js"


def _handoff() -> str:
    with open(CHATBOT_JS) as f:
        source = f.read()
    return re.search(r"function handoffForm\(collected\) \{.*?\n    \}",
                     source, re.DOTALL).group(0)


class PostsToThePlanningPageTest(unittest.TestCase):
    def test_it_posts_the_collected_fields_to_plan(self):
        handoff = _handoff()
        self.assertIn('el.method = "post"', handoff)
        self.assertIn('el.action = "/plan"', handoff)

    def test_generate_does_not_send_the_prefill_marker(self):
        # The only difference between the two buttons. If the generate button
        # gained a name, it would land on a filled form instead of a day.
        handoff = _handoff()
        self.assertIn('check.name = "prefill"', handoff)
        self.assertNotIn("generate.name", handoff)


class SlowGenerateIsVisibleTest(unittest.TestCase):
    def test_it_submits_in_this_tab(self):
        # A background tab shows a blank page for the whole generate, with
        # nothing to say it is working, which reads as a button that did
        # nothing. In this tab the browser's own loading indicator says it.
        self.assertNotIn("_blank", _handoff())

    def test_the_button_says_it_is_working(self):
        handoff = _handoff()
        self.assertIn('el.classList.add("working")', handoff)
        self.assertIn("Building your day", handoff)

    def test_the_submitter_is_not_disabled(self):
        # Disabling the submitter mid-submit can drop its name from the post,
        # and "Open the form" is nothing but its name. The row is locked with
        # a class instead.
        self.assertEqual(re.findall(r"\.disabled\s*=", _handoff()), [])


if __name__ == "__main__":
    unittest.main()
