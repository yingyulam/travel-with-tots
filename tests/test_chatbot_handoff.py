"""Structural guards on the chat's handoff to the planning page.

The confirmation reply carries a hidden form that posts the collected fields to
/plan: "Generate my day" with a `generate` marker so the route builds a day,
"Open the form" without it so the route just fills the boxes in and stops.
Generating is a real AI call of ten seconds and up, which is what shaped the
two rules here: it happens in this tab, and it says that it is working.

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

    def test_only_the_generate_button_is_named(self):
        # The only difference between the two buttons, and which one carries
        # the name is the whole safety property. /plan builds a day only when
        # asked, so a post that loses a submit button's name fills the form in
        # rather than spending a minute on an AI call nobody wanted. Naming
        # the safe button instead, which is how this started, inverts that.
        handoff = _handoff()
        self.assertIn('generate.name = "generate"', handoff)
        self.assertNotIn("check.name", handoff)


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
        # and "Generate my day" is nothing but its name. The row is locked with
        # a class instead.
        self.assertEqual(re.findall(r"\.disabled\s*=", _handoff()), [])


if __name__ == "__main__":
    unittest.main()
