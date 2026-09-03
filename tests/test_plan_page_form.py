"""Guards on the planning form's own inputs.

Client-side constraints on this form must not be stricter than the server's:
a value the browser refuses is a value the parent cannot enter at all, and
there is no error message explaining a rule the server does not have.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import re
import unittest

from src.form_helpers import (
    NAP_DURATION_MAX_MINUTES,
    NAP_DURATION_MIN_MINUTES,
    clamp_int,
)

PLAN_TEMPLATE = "templates/plan.html"


def _template() -> str:
    with open(PLAN_TEMPLATE) as f:
        return f.read()


def _nap_inputs() -> list:
    return re.findall(r'<input type="number" name="nap_duration"[^>]*>',
                      _template(), re.DOTALL)


class NapDurationAcceptsAnyMinuteTest(unittest.TestCase):
    def test_forty_minutes_is_valid_to_the_server(self):
        # The value that started this: the server has always accepted it.
        self.assertEqual(clamp_int(40, NAP_DURATION_MIN_MINUTES,
                                    NAP_DURATION_MAX_MINUTES, 60), 40)

    def test_the_input_does_not_step_in_quarter_hours(self):
        # step="15" made the browser refuse 40, and 20, and 50: every value
        # off the quarter-hour grid, none of which the server minds.
        for field in _nap_inputs():
            with self.subTest(field=field):
                self.assertIn('step="1"', field)

    def test_both_the_rendered_and_the_added_row_agree(self):
        # One input is server-rendered, the other built in JS by "Add a nap".
        # They drifted apart before; a nap added later must take 40 too.
        self.assertEqual(len(_nap_inputs()), 2)

    def test_the_bounds_come_from_the_server_constants(self):
        for field in _nap_inputs():
            with self.subTest(field=field):
                self.assertIn("{{ nap_duration_min }}", field)
                self.assertIn("{{ nap_duration_max }}", field)

    def test_the_route_supplies_those_bounds(self):
        # The /plan route, which is in src/web/planning.py since app.py was
        # split into blueprints.
        with open("src/web/planning.py") as f:
            source = f.read()
        self.assertIn("nap_duration_min=NAP_DURATION_MIN_MINUTES", source)
        self.assertIn("nap_duration_max=NAP_DURATION_MAX_MINUTES", source)


class GeneratingSaysItIsWorkingTest(unittest.TestCase):
    """Building a day is one AI call of several seconds and this page stays on
    screen for all of it, so silence reads as a button that did nothing."""

    def test_there_is_a_progress_message_for_the_main_submit(self):
        self.assertIn('id="plan-progress"', _template())

    def test_it_is_shown_on_submit_not_on_click(self):
        # submit fires only once the browser has accepted the form, so a
        # rejected nap length cannot leave progress running under a page that
        # never went anywhere.
        handler = re.search(r'planForm\.addEventListener\("submit".*?\}\);',
                            _template(), re.DOTALL).group(0)
        self.assertIn('plan-progress").hidden = false', handler)


if __name__ == "__main__":
    unittest.main()
