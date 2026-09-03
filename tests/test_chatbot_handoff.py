"""The chat hands the day over; it never builds one.

The reply carries a hidden form that posts the collected fields to /plan, with
one button: "Open the form". No `generate` marker, so the route fills the boxes
in and stops, and Plan my day on the planning page is the one control that
builds an itinerary.

That is a rule about where a day lives, not a preference. An itinerary built in
the chat sits outside the planner: no version switcher, no situation buttons,
no replanning, and a second one generated as soon as the parent opens the form
they were just handed and presses Plan my day.

The JavaScript guards are read rather than executed, like the others in this
file. Behaviour was verified separately against a DOM shim, posting the fields
the widget really builds into the real route.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import re
import unittest

from src.agent import TOOLS

CHATBOT_JS = "static/chatbot.js"


class TheChatCannotBuildADayTest(unittest.TestCase):
    def test_no_tool_can_generate_an_itinerary(self):
        # The other half of removing the button. With the tool on offer the
        # agent could still build a day and print it into the bubble, which is
        # the same itinerary-outside-the-planner by another route.
        self.assertNotIn("plan_trip_tool", [t.name for t in TOOLS])

    def test_the_planner_still_has_the_component(self):
        # Only the chat gives it up. /plan and the component test page build
        # days exactly as before.
        from src.components.plan_trip import plan_trip
        self.assertTrue(callable(plan_trip))


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

    def test_the_card_cannot_ask_for_a_day_to_be_built(self):
        # There is one button and it opens the form. /plan builds a day only
        # when the post carries `generate`, so with no field naming it the card
        # can only fill the boxes in. An itinerary generated from here would sit
        # outside the planner, with no version switcher and no replanning, and
        # a second one would follow the moment Plan my day was pressed.
        handoff = _handoff()
        self.assertNotIn('name = "generate"', handoff)
        self.assertNotIn("Generate my day", handoff)
        self.assertIn("Open the form", handoff)


class SlowGenerateIsVisibleTest(unittest.TestCase):
    def test_it_submits_in_this_tab(self):
        # A background tab shows a blank page for the whole generate, with
        # nothing to say it is working, which reads as a button that did
        # nothing. In this tab the browser's own loading indicator says it.
        self.assertNotIn("_blank", _handoff())

    def test_the_button_says_it_is_working(self):
        handoff = _handoff()
        self.assertIn('el.classList.add("working")', handoff)
        self.assertIn("Opening the form", handoff)

    def test_the_submitter_is_not_disabled(self):
        # Disabling the submitter mid-submit can drop its name from the post.
        # The row is locked with a class instead.
        self.assertEqual(re.findall(r"\.disabled\s*=", _handoff()), [])


if __name__ == "__main__":
    unittest.main()
