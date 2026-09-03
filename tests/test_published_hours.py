"""Reading a venue's opening hours off its own web page.

The proposer never asked the model for hours, on the reasoning that a search
snippet cannot establish them. That is true, and it was generalised into "hours
are unfindable", which is not: Maplewood Farm publishes four plain lines on its
homepage, and the proposer had already found that homepage and stored the link
without reading it.

So hours may now be read from the page, and the whole design is about not
trusting the reading. Every time the model reports has to appear on the page,
all seven days have to be accounted for, and what survives is labelled
unconfirmed for a person to check.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import json
import unittest
from unittest import mock

import requests

from src import osm, webpage
from src.workflows import propose_venues as pv

# The real page, in the shape a browser gets it.
MAPLEWOOD_HTML = """
<html><head><style>p{font-size:14px}</style></head><body>
  <h1>Maplewood Farm</h1>
  <p>Farm Hours:</p>
  <ul>
    <li>Mon-Thu: 10:00am&#8211;4:00pm</li>
    <li>Fri-Sun: 8:30am&nbsp;-&nbsp;4:00pm</li>
  </ul>
  <script>var analytics = 1;</script>
</body></html>
"""
MAPLEWOOD_TEXT = webpage.to_text(MAPLEWOOD_HTML)

MO, TU, WE, TH, FR, SA, SU = range(7)


def _answer(days=(), closed=(), note=None, states=True):
    """One model reply, in the schema's shape."""
    return {
        "states_hours": states,
        "days": [{"day": pv._DAY_ENUM[d], "open": o, "close": c}
                 for d, o, c in days],
        "closed_days": [pv._DAY_ENUM[d] for d in closed],
        "note": note,
    }


def _read(page, answer, name="A Venue"):
    """(week, note) from hours_from_page, with the model's reply stubbed.

    Drops the third value, `missing`, which the tests that care about it read
    from `hours_from_page` directly. Never touches the network.
    """
    week, note, _missing = _read_full(page, answer, name)
    return week, note


def _read_full(page, answer, name="A Venue"):
    with mock.patch.object(pv, "call_openrouter",
                           return_value=(json.dumps(answer), {}, 0.1)):
        return pv.hours_from_page(name, page)


class ReadingTheTextTest(unittest.TestCase):
    def test_scripts_and_styles_are_not_prose(self):
        # A stylesheet's "font-size:14px" would otherwise arrive as text and
        # read like data.
        self.assertNotIn("font-size", MAPLEWOOD_TEXT)
        self.assertNotIn("analytics", MAPLEWOOD_TEXT)

    def test_list_items_do_not_run_together(self):
        # Without a line break at </li>, "4:00pmFri-Sun" is one token and the
        # hours become unreadable.
        self.assertIn("Mon-Thu: 10:00am-4:00pm", MAPLEWOOD_TEXT)
        self.assertIn("Fri-Sun: 8:30am - 4:00pm", MAPLEWOOD_TEXT)

    def test_a_url_that_is_not_a_web_address_is_refused_before_fetching(self):
        with self.assertRaises(webpage.PageError):
            webpage.fetch_text("javascript:alert(1)")


class TimesOnThePageTest(unittest.TestCase):
    """The anti-invention guard: what the page actually says, as arithmetic."""

    def test_it_normalises_every_notation_the_page_uses(self):
        self.assertEqual(pv.page_times(MAPLEWOOD_TEXT),
                         {"08:30", "10:00", "16:00"})

    def test_a_bare_number_is_not_a_time(self):
        # Otherwise "3 year old" grounds an opening at 03:00.
        self.assertEqual(
            pv.page_times("great for a 3 year old, 2 hours of fun, 45 minutes"),
            set())

    def test_dots_and_24_hour_notation_are_read(self):
        self.assertEqual(pv.page_times("open 09.30 to 17:00, late night 8pm"),
                         {"09:30", "17:00", "20:00"})

    def test_midnight_and_noon(self):
        self.assertEqual(pv.page_times("12:00am to 12:00pm"), {"00:00", "12:00"})


class WeekdayWeekendHoursTest(unittest.TestCase):
    """The case that prompted this: Maplewood Farm."""

    def test_the_week_is_read_and_the_split_is_kept(self):
        week, note = _read(MAPLEWOOD_TEXT, _answer(
            days=[(d, "10:00", "16:00") for d in (MO, TU, WE, TH)]
                 + [(d, "08:30", "16:00") for d in (FR, SA, SU)]))
        self.assertEqual(week[MO], ("10:00", "16:00"))
        self.assertEqual(week[FR], ("08:30", "16:00"))
        self.assertIsNone(note)

    def test_it_stores_as_a_week_rather_than_one_pair(self):
        proposal = {"name": "Maplewood Farm",
                    "official_url": "https://maplewoodfarm.bc.ca/"}
        week = {d: ("10:00", "16:00") for d in (MO, TU, WE, TH)}
        week.update({d: ("08:30", "16:00") for d in (FR, SA, SU)})
        with mock.patch.object(webpage, "fetch_text", return_value=MAPLEWOOD_TEXT), \
             mock.patch.object(pv, "hours_from_page",
                               return_value=(week, None, set())):
            pv._read_published_hours(proposal)
        self.assertEqual(proposal["hours_week"],
                         "Mo-Th 10:00-16:00; Fr-Su 08:30-16:00")
        # The representative pair too, or the venue lands under "no hours at
        # all" while carrying a full timetable.
        self.assertEqual(proposal["open_time"], "10:00")
        self.assertIn("maplewoodfarm.bc.ca", proposal["hours_source"])

    def test_a_partial_week_is_not_serialised_as_a_closure(self):
        # "Mo-Fr 09:00-17:00; Sa-Su off" reads back as a complete week with the
        # weekend shut, which is the inference this whole path refuses. A day
        # never established has to be left out entirely.
        part = {d: ("09:00", "17:00") for d in range(5)}
        written = osm.to_week_string(part, closed=set())
        self.assertEqual(written, "Mo-Fr 09:00-17:00")
        self.assertIsNone(osm.per_day_hours(written))       # still incomplete
        self.assertEqual(sorted(osm.partial_week(written)[1]), [0, 1, 2, 3, 4])

    def test_a_week_with_a_closure_is_not_uniform(self):
        # "Tuesday to Sunday 10am-4pm, closed Mondays" is six identical days.
        # Collapsing it to one pair reopens the venue on the day it shuts, and
        # this rule had been written three times with two of them wrong.
        shut_monday = {d: ("10:00", "17:00") for d in range(1, 7)}
        self.assertFalse(osm.is_uniform_week(shut_monday))
        self.assertTrue(osm.is_uniform_week({d: ("10:00", "17:00")
                                             for d in range(7)}))

    def test_a_closure_survives_the_whole_round_trip(self):
        proposal = {"name": "A Centre", "official_url": "https://example.org/"}
        shut_monday = {d: ("10:00", "16:00") for d in range(1, 7)}
        with mock.patch.object(webpage, "fetch_text", return_value="10am-4pm"), \
             mock.patch.object(pv, "hours_from_page",
                               return_value=(shut_monday, None, set())):
            pv._read_published_hours(proposal)
        self.assertEqual(proposal["hours_week"], "Mo off; Tu-Su 10:00-16:00")
        self.assertEqual(osm.per_day_hours(proposal["hours_week"]), shut_monday)

    def test_a_known_closure_is_still_written(self):
        shut_monday = {d: ("10:00", "17:00") for d in range(1, 7)}
        written = osm.to_week_string(shut_monday)
        self.assertEqual(written, "Mo off; Tu-Su 10:00-17:00")
        self.assertEqual(osm.per_day_hours(written), shut_monday)

    def test_the_stored_week_round_trips_through_the_shared_parser(self):
        # One notation for both sources: what is written here is what
        # osm.per_day_hours reads back on approval.
        week = {d: ("10:00", "16:00") for d in (MO, TU, WE, TH)}
        week.update({d: ("08:30", "16:00") for d in (FR, SA, SU)})
        self.assertEqual(osm.per_day_hours(osm.to_week_string(week)), week)


class IndividualDayHoursTest(unittest.TestCase):
    def test_a_single_late_day_survives(self):
        page = ("Open Mon to Thu 10:00am-5:00pm, Friday 10:00am-8:00pm, "
                "weekends 10:00am-5:00pm")
        week, _ = _read(page, _answer(
            days=[(d, "10:00", "17:00") for d in (MO, TU, WE, TH, SA, SU)]
                 + [(FR, "10:00", "20:00")]))
        self.assertEqual(week[FR], ("10:00", "20:00"))
        self.assertEqual(week[TH], ("10:00", "17:00"))

    def test_a_closed_day_is_accounted_for_without_being_opened(self):
        page = "Closed Mondays. Tuesday to Sunday 10:00am to 5:00pm."
        week, _ = _read(page, _answer(
            days=[(d, "10:00", "17:00") for d in (TU, WE, TH, FR, SA, SU)],
            closed=[MO]))
        self.assertNotIn(MO, week)          # absent means shut
        self.assertEqual(len(week), 6)

    def test_a_uniform_week_collapses_to_one_pair(self):
        proposal = {"name": "A Museum", "official_url": "https://example.org/"}
        week = {d: ("10:00", "17:00") for d in range(7)}
        with mock.patch.object(webpage, "fetch_text",
                               return_value="10:00am-5:00pm daily"), \
             mock.patch.object(pv, "hours_from_page",
                               return_value=(week, None, set())):
            pv._read_published_hours(proposal)
        self.assertEqual((proposal["open_time"], proposal["close_time"]),
                         ("10:00", "17:00"))
        self.assertNotIn("hours_week", proposal)


class HoursThatCannotBeFoundTest(unittest.TestCase):
    def test_a_page_that_states_no_hours_leaves_them_open(self):
        week, note = _read("Come and visit our lovely farm!",
                           _answer(states=False))
        self.assertEqual(week, {})
        self.assertIsNone(note)

    def test_a_partial_week_is_returned_but_names_what_is_missing(self):
        # The days that were read are worth showing a reviewer. Filling the gap
        # is what would claim hours nobody published, so `missing` says which
        # days still need a person and approval is refused until they have one.
        week, _, missing = _read_full("Open weekdays 09:00-17:00", _answer(
            days=[(d, "09:00", "17:00") for d in (MO, TU, WE, TH, FR)]))
        self.assertEqual(sorted(week), [MO, TU, WE, TH, FR])
        self.assertEqual(sorted(missing), [SA, SU])

    def test_an_unreachable_page_leaves_the_proposal_untouched(self):
        proposal = {"name": "A Farm", "official_url": "https://example.org/"}
        with mock.patch.object(webpage, "fetch_text",
                               side_effect=webpage.PageError("HTTP 404")):
            pv._read_published_hours(proposal)
        self.assertNotIn("open_time", proposal)
        self.assertNotIn("hours_week", proposal)

    def test_no_official_url_means_no_fetch_at_all(self):
        proposal = {"name": "A Farm", "official_url": ""}
        with mock.patch.object(webpage, "fetch_text") as fetched:
            pv._read_published_hours(proposal)
        fetched.assert_not_called()

    def test_a_note_survives_a_refused_week(self):
        # "Closed in January" is worth a reviewer's attention even when the
        # timetable itself is unusable.
        proposal = {"name": "A Farm", "official_url": "https://example.org/"}
        with mock.patch.object(webpage, "fetch_text", return_value="text"), \
             mock.patch.object(pv, "hours_from_page",
                               return_value=({}, "Closed in January", set(range(7)))):
            pv._read_published_hours(proposal)
        self.assertIn("Closed in January", proposal["hours_note"])
        self.assertIn("unconfirmed", proposal["hours_note"])
        self.assertNotIn("open_time", proposal)


class TheModelCannotInventHoursTest(unittest.TestCase):
    """The guard that makes reading a page safe at all."""

    def test_a_time_not_on_the_page_loses_the_whole_week(self):
        # The page says 10:00; the model claims 09:00. Not one day is kept:
        # a model inventing one time is not one to trust about the rest.
        week, _ = _read(MAPLEWOOD_TEXT, _answer(
            days=[(d, "09:00", "16:00") for d in range(7)]))
        self.assertEqual(week, {})

    def test_a_plausible_but_absent_closing_time_is_refused(self):
        week, _ = _read(MAPLEWOOD_TEXT, _answer(
            days=[(d, "10:00", "17:00") for d in range(7)]))
        self.assertEqual(week, {})

    def test_an_unpadded_time_is_not_treated_as_invented(self):
        # Measured on Maplewood's real page: the model answered "8:30" where
        # the scanner emits "08:30", and a correct week was refused as a
        # hallucination. The guard has to reject invention, not formatting.
        week, _ = _read(MAPLEWOOD_TEXT, _answer(
            days=[(d, "10:00", "16:00") for d in (MO, TU, WE, TH)]
                 + [(d, "8:30", "16:00") for d in (FR, SA, SU)]))
        self.assertEqual(week[FR], ("08:30", "16:00"))

    def test_a_time_that_is_not_a_clock_time_is_refused(self):
        week, _ = _read(MAPLEWOOD_TEXT, _answer(
            days=[(d, "morning", "16:00") for d in range(7)]))
        self.assertEqual(week, {})

    def test_the_real_times_are_accepted(self):
        # The same shape as the refusals above, differing only in being true,
        # so the guard is shown to reject invention rather than everything.
        week, _ = _read(MAPLEWOOD_TEXT, _answer(
            days=[(d, "10:00", "16:00") for d in range(7)]))
        self.assertEqual(len(week), 7)

    def test_an_unparseable_reply_leaves_hours_open(self):
        with mock.patch.object(pv, "call_openrouter",
                               return_value=("not json", {}, 0.1)):
            week, note, _ = pv.hours_from_page("A Farm", MAPLEWOOD_TEXT)
        self.assertEqual(week, {})
        self.assertIsNone(note)

    def test_a_transport_failure_leaves_hours_open(self):
        with mock.patch.object(pv, "call_openrouter",
                               side_effect=requests.exceptions.Timeout("slow")):
            week, _, _ = pv.hours_from_page("A Farm", MAPLEWOOD_TEXT)
        self.assertEqual(week, {})


if __name__ == "__main__":
    unittest.main()
