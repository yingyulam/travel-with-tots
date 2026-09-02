"""A replan is a proposal, not something that happens to the day.

Before this, pressing "it's raining" rebuilt the afternoon and installed it as
an "Updated" tab. Nothing was lost -- the original stayed at index 0 and only
"Save this plan" wrote anything down -- but the page said the day had been
updated, and the parent had agreed to nothing. That is the wrong default in a
trip of several days, where accepting one day's change is what licenses
touching the days after it.

So: the result goes into slot 1, the tab reads "Proposed", the panel says in
words what would differ, and it is the parent who says yes.

The differences are computed here in Python rather than in the page, because
getting a pairing wrong at an edge -- a venue kept but retimed, a swap that
reads as a drop and an add -- is exactly the kind of bug a browser hides.
"""

import re
import unittest
from unittest import mock

import app as app_module
from src.components import replan_trip as replan_module
from src.plan_diff import describe_changes, summarise


def _stop(time, name=None, kind="activity"):
    return {"time": time, "kind": kind, "reason": "",
            "venue": {"id": abs(hash(name or kind)) % 999, "name": name}
                     if name else None}


MORNING = [_stop("9:00 AM", "Stanley Park Seawall"),
           _stop("12:00 PM", kind="meal"),
           _stop("12:45 PM", "Science World"),
           _stop("4:15 PM", "Second Beach")]


class TheDiffSaysWhatWouldChangeTest(unittest.TestCase):
    def _texts(self, before, after):
        return [c["text"] for c in describe_changes(before, after)]

    def _kinds(self, before, after):
        return [c["kind"] for c in describe_changes(before, after)]

    def test_an_unchanged_day_has_no_changes(self):
        # The common outcome when the adjuster agrees with the draft, and one
        # the page has to be able to say out loud.
        self.assertEqual(describe_changes(MORNING, MORNING), [])

    def test_a_replacement_at_the_same_time_is_one_change_not_two(self):
        after = [s if s["time"] != "12:45 PM" else _stop("12:45 PM", "Vancouver Aquarium")
                 for s in MORNING]
        self.assertEqual(self._kinds(MORNING, after), ["swapped"])
        self.assertEqual(self._texts(MORNING, after),
                         ["Vancouver Aquarium replaces Science World at 12:45 PM"])

    def test_a_dropped_stop_says_where_it_went_from(self):
        after = [s for s in MORNING if s["time"] != "4:15 PM"]
        self.assertEqual(self._texts(MORNING, after),
                         ["Second Beach is dropped from 4:15 PM"])

    def test_an_added_stop_says_when(self):
        after = MORNING + [_stop("6:00 PM", "Kitsilano Beach")]
        self.assertEqual(self._texts(MORNING, after),
                         ["Kitsilano Beach is added at 6:00 PM"])

    def test_a_kept_stop_at_a_new_time_moves_rather_than_vanishing(self):
        after = [_stop("10:00 AM", "Stanley Park Seawall")] + MORNING[1:]
        self.assertEqual(self._kinds(MORNING, after), ["moved"])
        self.assertEqual(
            self._texts(MORNING, after),
            ["Stanley Park Seawall moves from 9:00 AM to 10:00 AM"])

    def test_stops_are_matched_by_what_they_are_not_by_position(self):
        # Dropping the first stop shifts every later one. Matching on position
        # would report the whole day as changed.
        after = MORNING[1:]
        self.assertEqual(self._texts(MORNING, after),
                         ["Stanley Park Seawall is dropped from 9:00 AM"])

    def test_a_venue_less_block_is_named_by_what_it_is(self):
        # Lunch is a block with a handoff rather than a place, and it can still
        # move or disappear.
        after = [s for s in MORNING if s["kind"] != "meal"]
        self.assertEqual(self._texts(MORNING, after),
                         ["lunch is dropped from 12:00 PM"])

    def test_changes_are_listed_in_the_order_the_day_happens(self):
        after = [_stop("9:00 AM", "Stanley Park Seawall"),
                 _stop("12:00 PM", kind="meal"),
                 _stop("12:45 PM", "Vancouver Aquarium"),
                 _stop("6:00 PM", "Kitsilano Beach")]
        self.assertEqual(self._kinds(MORNING, after),
                         ["swapped", "dropped", "added"])

    def test_an_unreadable_time_still_reports_a_change(self):
        # Rather than raising, or being silently dropped and leaving the parent
        # agreeing to something nobody listed.
        after = MORNING[:-1] + [_stop("half four", "Second Beach")]
        self.assertTrue(self._texts(MORNING, after))

    def test_the_summary_counts_them(self):
        self.assertEqual(summarise([]),
                         "Nothing would change: this is already the best we can do.")
        self.assertEqual(summarise([{"kind": "added", "text": "x"}]),
                         "One change to the rest of your day:")
        self.assertIn("2 changes", summarise([{"kind": "added", "text": "x"}] * 2))


class TheReplanCarriesItsChangesTest(unittest.TestCase):
    """The component computes the diff against the day as it stands."""

    def _replan(self, before, after):
        with mock.patch.object(replan_module, "get_venues", return_value=[]), \
             mock.patch.object(replan_module, "replan",
                               return_value={"label": "L", "blurb": "b",
                                             "from_time": "1:00 PM",
                                             "stops": after}), \
             mock.patch.object(replan_module, "ReplanningAgent") as agent:
            agent.return_value.adjust_replan.side_effect = \
                replan_module.ReplanningAgentError("no ai in tests")
            return replan_module.replan_trip(
                plan={"stops": before}, situation="weather_rain",
                current_time="12:00")

    def test_the_result_lists_what_would_change(self):
        after = [s if s["time"] != "4:15 PM" else _stop("4:15 PM", "Science Centre")
                 for s in MORNING]
        result = self._replan(MORNING, after)
        self.assertEqual([c["text"] for c in result["changes"]],
                         ["Science Centre replaces Second Beach at 4:15 PM"])

    def test_and_summarises_them(self):
        after = [s for s in MORNING if s["time"] != "4:15 PM"]
        self.assertEqual(self._replan(MORNING, after)["change_summary"],
                         "One change to the rest of your day:")

    def test_an_agreeing_replan_says_nothing_would_change(self):
        result = self._replan(MORNING, MORNING)
        self.assertEqual(result["changes"], [])
        self.assertIn("Nothing would change", result["change_summary"])

    def test_the_diff_is_against_the_plan_that_was_sent_in(self):
        # Not against the original day. On a second replan the parent is
        # deciding about the day as it now stands.
        current = [_stop("9:00 AM", "Somewhere Else")]
        after = [_stop("9:00 AM", "A Third Place")]
        self.assertEqual([c["text"] for c in self._replan(current, after)["changes"]],
                         ["A Third Place replaces Somewhere Else at 9:00 AM"])


class TheRouteReturnsTheProposalTest(unittest.TestCase):
    def setUp(self):
        self.client = app_module.app.test_client()

    def test_the_endpoint_hands_back_the_changes(self):
        with mock.patch.object(app_module, "replan_trip",
                               return_value={"label": "L", "blurb": "b",
                                             "stops": [], "adjusted": True,
                                             "changed": True,
                                             "changes": [{"kind": "added",
                                                          "text": "X is added"}],
                                             "change_summary": "One change"}):
            body = self.client.post("/replan/adjust", json={
                "plan": {"stops": []}, "situation": "nap_here",
                "current_time": "12:00"}).get_json()
        self.assertEqual(body["changes"], [{"kind": "added", "text": "X is added"}])
        self.assertEqual(body["change_summary"], "One change")


class ThePageAsksBeforeChangingAnythingTest(unittest.TestCase):
    """The in-trip page's half. Read from the rendered source, because the
    behaviour is in the script and there is no JS harness in this project."""

    def setUp(self):
        import json
        self.source = app_module.app.test_client().post("/trip", data={
            "plan": json.dumps({"label": "L", "blurb": "b", "stops": []}),
            "context": json.dumps({"destination": "Vancouver",
                                   "trip_date": "2026-09-14"}),
        }).get_data(as_text=True)

    def test_there_is_a_panel_for_the_proposal(self):
        self.assertIn('id="replan-proposal"', self.source)
        self.assertIn('id="proposal-changes"', self.source)

    def test_it_offers_both_answers(self):
        self.assertIn('id="proposal-accept"', self.source)
        self.assertIn('id="proposal-discard"', self.source)

    def test_the_panel_starts_hidden(self):
        panel = self.source.split('id="replan-proposal"')[1].split(">")[0]
        self.assertIn("hidden", panel)

    def test_a_replan_marks_the_day_pending_rather_than_updated(self):
        self.assertIn("DAYS[activeDay].pending = true", self.source)

    def test_the_tab_reads_proposed_until_it_is_accepted(self):
        self.assertIn('return "Proposed"', self.source)

    def test_the_status_line_says_nothing_has_changed_yet(self):
        self.assertIn("Nothing has changed yet", self.source)
        # And the old wording, which claimed the opposite, is gone.
        self.assertNotIn("Updated ready from", self.source)

    def test_accepting_records_the_version_the_day_settled_on(self):
        self.assertIn("day.accepted = 1", self.source)

    def test_discarding_puts_the_day_back(self):
        self.assertIn("day.plans.length = 1", self.source)

    def test_saving_uses_the_accepted_version_not_the_one_on_screen(self):
        # Reading a proposal is not agreeing to it.
        self.assertIn("d.plans[d.accepted || 0]", self.source)

    def test_accepting_says_the_other_days_are_untouched(self):
        # True today because nothing cascades yet, and the sentence the next
        # stage has to keep honest when something does.
        self.assertIn("Your other days are unchanged", self.source)


class EachAnswerSitsUnderItsOwnQuestionTest(unittest.TestCase):
    """Where the status lines are, which turned out to matter.

    There was one, at the top of the card above the timeline, and every message
    went to it: replanning this day, accepting it, reworking the later days.
    Two consequences, both reported from real use. On a phone the reply landed
    a screen or two from the button that caused it, so accepting a change and
    then being asked a second question looked like nothing had happened. And
    "Updated" could mean this day or the four after it, with no way to tell.

    So: one line under the replan controls, one under the cascade controls.
    """

    def setUp(self):
        import json
        self.source = app_module.app.test_client().post("/trip", data={
            "plans": json.dumps([{"label": "L", "blurb": "b", "stops": [],
                                  "trip_date": d}
                                 for d in ("2026-09-14", "2026-09-15")]),
            "context": json.dumps({"destination": "Vancouver",
                                   "trip_date": "2026-09-14",
                                   "end_date": "2026-09-15"}),
        }).get_data(as_text=True)

    def _at(self, marker):
        return self.source.index(marker)

    def test_there_are_two_status_lines(self):
        self.assertIn('id="replan-status"', self.source)
        self.assertIn('id="cascade-status"', self.source)

    def test_the_replan_line_is_below_every_control_that_starts_one(self):
        # Both the situation chips and the Replan button, so wherever the
        # parent pressed, the answer is beneath their thumb.
        self.assertGreater(self._at('id="replan-status"'),
                           self._at('id="situation-bar"'))
        self.assertGreater(self._at('id="replan-status"'),
                           self._at('id="replan-note-go"'))

    def test_and_above_the_panels_that_answer_it(self):
        self.assertLess(self._at('id="replan-status"'),
                        self._at('id="replan-proposal"'))

    def test_it_is_no_longer_above_the_timeline(self):
        # The regression this closes: a reply rendered a screen away from the
        # question that produced it.
        self.assertGreater(self._at('id="replan-status"'),
                           self._at('id="timeline-host"'))

    def test_the_later_days_line_is_below_their_own_buttons(self):
        self.assertGreater(self._at('id="cascade-status"'),
                           self._at('id="cascade-offer"'))
        self.assertGreater(self._at('id="cascade-status"'),
                           self._at('id="cascade-proposal"'))

    def test_the_two_flows_do_not_share_a_line(self):
        # Otherwise "Updated" is ambiguous about which of them it describes.
        self.assertIn("say(cascadeStatus,", self.source)
        for later in ("Replanning your remaining", "Your later days are as they were",
                      "Left your later days alone"):
            with self.subTest(message=later):
                said = self.source.index(later)
                # The nearest preceding say() call must name the cascade line.
                before = self.source[:said]
                self.assertIn("say(cascadeStatus, ",
                              before[before.rindex("say("):])

    def test_accepting_stops_promising_the_other_days_are_untouched(self):
        # It was unconditional, and read as a contradiction when the very next
        # thing on screen offered to change them.
        self.assertIn('cascading ? "Updated." :', self.source)

    def test_switching_day_clears_both(self):
        self.assertIn("cascadeStatus.hidden = true", self.source)


if __name__ == "__main__":
    unittest.main()
