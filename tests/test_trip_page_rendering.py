"""Structural guards on the trip page's client-side rendering.

The page renders a plan that arrives in the request body: POST /trip takes it
verbatim and Plan.from_dict does not validate it, and /save-trip persists it
unchanged. So venue names, reasons and the blurb are caller-chosen text. While
this page built HTML by concatenating that text and assigning innerHTML, the
browser ran it as markup rather than showing it.

These tests read the template rather than executing it, so they cannot prove the
rendering is correct, only that the shape which made it unsafe has not come
back. Worth having anyway: this is inline JS with no unit-test harness, so a
future edit reaching for a template literal would otherwise go unnoticed.
"""

import re
import unittest

TRIP_TEMPLATE = "templates/trip.html"


def _template() -> str:
    with open(TRIP_TEMPLATE) as f:
        return f.read()


def _script() -> str:
    """The page's own script block, which is the last one: the earlier two hold
    JSON data rather than code."""
    return re.findall(r"<script>(.*?)</script>", _template(), re.DOTALL)[-1]


class NoHtmlStringSinksTest(unittest.TestCase):
    def test_nothing_is_assigned_to_innerhtml(self):
        # Even a literal-only assignment is worth refusing here: it is the
        # shape that invites the next interpolation.
        self.assertEqual(re.findall(r"\.innerHTML\s*=", _script()), [],
                         "innerHTML is assigned in trip.html")

    def test_no_other_html_parsing_sink(self):
        for sink in ("outerHTML", "insertAdjacentHTML", "document.write",
                     "createContextualFragment"):
            with self.subTest(sink=sink):
                self.assertNotIn(sink, _script())

    def test_no_interpolated_href_attribute(self):
        # An interpolated href was attribute injection as well as a route to a
        # non-http scheme, neither of which escaping the text would have fixed.
        self.assertNotRegex(_script(), r"href=[\"']\$\{")


class LinkGuardTest(unittest.TestCase):
    def test_a_url_scheme_guard_exists(self):
        self.assertIn("function safeUrl(", _script())

    def test_the_guard_allows_only_http_and_https(self):
        # Read the pattern the guard uses rather than trusting its name.
        body = re.search(r"function safeUrl\([^)]*\)\s*\{(.*?)\n  \}",
                         _script(), re.DOTALL)
        self.assertIsNotNone(body, "safeUrl's body not found")
        self.assertIn("^https?://", body.group(1).replace("\\", ""))

    def test_every_link_target_goes_through_the_guard(self):
        script = _script()
        self.assertEqual(len(re.findall(r"\.href\s*=", script)), 1,
                         "an href is set somewhere other than mapsLink")
        maps_link = re.search(r"function mapsLink\(.*?\n  \}", script, re.DOTALL)
        self.assertIsNotNone(maps_link, "mapsLink not found")
        self.assertIn("safeUrl(", maps_link.group(0))


class CurrentTimeFieldTest(unittest.TestCase):
    """The editable current-time field is how a replan is tested: it decides
    which stops count as done, so being able to set it by hand is the only way
    to exercise a mid-day situation without waiting for the clock. Guarded
    because it is easy to mistake for decoration when tidying the header."""

    def test_the_field_is_editable_and_present(self):
        self.assertRegex(_template(), r'<input\s+type="time"\s+id="current-time"')

    def test_editing_it_re_renders_the_timeline(self):
        self.assertIn('timeField.addEventListener("input", renderTimeline)', _script())

    def test_its_value_is_what_a_replan_is_anchored_to(self):
        self.assertIn("current_time: timeField.value", _script())


class DelegatedHandlerContractTest(unittest.TestCase):
    """The "Why" toggle is delegated on the timeline host and finds its panel
    with btn.nextElementSibling, so the button and panel have to be built as
    adjacent siblings. Rewriting the renderer could silently break that."""

    def test_the_handler_still_relies_on_the_sibling(self):
        self.assertIn("nextElementSibling", _script())

    def test_the_button_and_panel_are_built_together(self):
        why = re.search(r"function whyBlock\(.*?\n  \}", _script(), re.DOTALL)
        self.assertIsNotNone(why, "whyBlock not found")
        self.assertIn("stop-why-btn", why.group(0))
        self.assertIn("stopWhyDetail(", why.group(0))


if __name__ == "__main__":
    unittest.main()
