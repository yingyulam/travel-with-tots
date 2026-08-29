"""How the nap window is planned.

The model, decided deliberately: the planner structures the day around the nap
the parent *expects*, and does not try to predict where or how the child will
actually sleep. A child's nap does not follow a plan, so what really happens is
handled by Replan on the Go ("nap happened here"), which extends the current
stop and re-times the rest of the day.

Three defects this locks out, all found in real output:

1. Nap-friendliness was a hard filter with a fallback that only fired when
   *nothing* was nap-friendly. One nap-friendly venue in the pool excluded
   every other option, and if that venue was shut at nap time the stop
   vanished from the day.
2. The parent's nap length was collected and discarded: a flat 45 minutes, so
   a thirty-minute nap and a three-hour one were checked identically and a
   park closing at two could host either.
3. The reason told every parent it was a "nap on the go" stop, including one
   who had said their child needs a proper place, and called whatever it
   picked "Nap-friendly" even when the fallback had handed it a pool.
"""

import unittest
from datetime import date

from src import itinerary

ON = date(2026, 9, 15)
BASE = {"wake_up": "07:00", "bedtime": "19:30", "transit_nap": "yes",
        "destination": "Vancouver", "transit": "car", "stop_count": "2",
        "dining": "on_the_go", "preferred_lunch_time": "11:30", "features": [],
        "age_months": 18, "age_years": 1, "interest": [],
        "trip_date": ON.isoformat()}


def _venue(name, venue_type, nap_friendly, opens="09:00", closes="18:00"):
    return {"id": abs(hash(name)) % 9999, "name": name, "type": venue_type,
            "neighbourhood": "Downtown", "open": opens, "close": closes,
            "hours_source": "default", "can_eat": False,
            "nap_friendly": nap_friendly, "lat": 49.28, "lng": -123.12,
            "maps_url": ""}


# The morning activity slot picks before the nap slot does and marks its venue
# used, so a two-venue pool leaves the nap whatever is left over rather than
# whatever it prefers. This venue exists only to absorb the 9am slot, and is
# shut by noon so it can never be chosen for the nap itself.
FILLER = ("Morning Filler", "museum", False, "09:00", "12:00")


def _plan(pool, minutes=90, **over):
    inputs = {**BASE, "naps": [{"start": "13:00", "duration_min": minutes}],
              **over}
    return itinerary.generate_plans([_venue(*FILLER)] + list(pool),
                                    inputs)[0].stops


def _nap(pool, minutes=90, **over):
    return next((s for s in _plan(pool, minutes, **over) if s["kind"] == "nap"),
                None)


class NapIsAPreferenceTest(unittest.TestCase):
    def test_a_nap_prefers_a_restful_venue_when_one_is_open(self):
        pool = [_venue("A Gallery", "museum", False),
                _venue("A Park", "park", True)]
        self.assertEqual(_nap(pool)["venue"]["name"], "A Park")

    def test_a_nap_still_happens_when_nothing_is_restful(self):
        # Previously the `or activities` fallback covered this, so it worked --
        # but only because *nothing* qualified. See the next test.
        pool = [_venue("A Gallery", "museum", False),
                _venue("A Pool", "pool", False)]
        self.assertIsNotNone(_nap(pool))

    def test_the_nap_stop_survives_its_only_restful_venue_being_shut(self):
        # The real failure. One nap-friendly venue was enough to exclude the
        # gallery, and because the park shuts at noon the slot was dropped
        # silently: two stops requested, one returned.
        pool = [_venue("Park Shuts At Noon", "park", True, "06:00", "12:00"),
                _venue("Gallery Open Late", "museum", False, "09:00", "18:00")]
        nap = _nap(pool)
        self.assertIsNotNone(nap)
        self.assertEqual(nap["venue"]["name"], "Gallery Open Late")

    def test_a_nap_prefers_what_the_parent_asked_for_among_restful_options(self):
        # Nap-friendliness first, interest second. Two malls, because the
        # morning activity slot picks before the nap slot does and takes the
        # first interest match -- so with one mall the nap only ever gets the
        # park, whatever the tiebreak says.
        pool = [_venue("A Park", "park", True),
                _venue("Mall One", "mall", True),
                _venue("Mall Two", "mall", True)]
        self.assertEqual(_nap(pool, interest=["mall"])["venue"]["type"], "mall")


class NapDurationTest(unittest.TestCase):
    # The park is the restful choice but shuts at 14:00. A gallery is open
    # until 18:00 and is not restful. The nap starts at 13:00.
    POOL = [_venue("Park Shuts At Two", "park", True, "06:00", "14:00"),
            _venue("Gallery Open Till Six", "museum", False, "09:00", "18:00")]

    def test_a_short_nap_fits_the_venue_that_shuts_early(self):
        self.assertEqual(_nap(self.POOL, 30)["venue"]["name"], "Park Shuts At Two")

    def test_a_nap_ending_exactly_at_closing_still_fits(self):
        self.assertEqual(_nap(self.POOL, 60)["venue"]["name"], "Park Shuts At Two")

    def test_a_long_nap_will_not_be_sent_somewhere_that_shuts_partway(self):
        # The whole point: before this, a three-hour nap was checked as 45
        # minutes, so a family was sent to a park closing two hours in.
        self.assertEqual(_nap(self.POOL, 180)["venue"]["name"],
                         "Gallery Open Till Six")

    def test_a_missing_length_falls_back_rather_than_guessing(self):
        for bad in (None, "", "ninety", 0, -30):
            with self.subTest(duration=bad):
                nap = _nap(self.POOL, bad)
                self.assertEqual(nap["venue"]["name"], "Park Shuts At Two")

    def test_an_implausible_length_is_capped_not_trusted(self):
        # generate_plans takes a plain dict, so a caller that skipped
        # form_helpers' clamp must not make the nap stop disappear.
        long_day = [_venue("Open All Day", "park", True, "06:00", "23:00")]
        self.assertIsNotNone(_nap(long_day, 10_000))
        self.assertEqual(itinerary.MAX_NAP_STOP_MIN, 180)


class NapReasonTest(unittest.TestCase):
    def test_the_reason_no_longer_claims_how_the_child_sleeps(self):
        pool = [_venue("A Park", "park", True)]
        reason = _nap(pool)["reason"]
        self.assertNotIn("on the go", reason)
        self.assertIn("nap you expect", reason)

    def test_the_reason_is_the_same_whatever_the_parent_said_about_transit(self):
        # transit_nap is a judgment, left to the LLM adjuster (plan_adjust.txt),
        # so nothing deterministic should read it -- including the wording.
        pool = [_venue("A Park", "park", True)]
        reasons = {_nap(pool, transit_nap=tn)["reason"]
                   for tn in ("yes", "sometimes", "no")}
        self.assertEqual(len(reasons), 1)

    def test_a_venue_that_is_not_restful_is_not_described_as_restful(self):
        pool = [_venue("A Pool", "pool", False)]
        self.assertNotIn("rest fits easily", _nap(pool)["reason"])

    def test_a_restful_venue_says_so(self):
        pool = [_venue("A Park", "park", True)]
        self.assertIn("rest fits easily", _nap(pool)["reason"])


class TransitNapStaysSoftTest(unittest.TestCase):
    """Locks the decision that transit_nap belongs to the adjuster, not here.

    Recorded as a test because the deterministic planner ignoring a collected
    field looks like a bug, and someone will be tempted to "fix" it by adding
    branching the model already handles better.
    """

    POOL = [_venue("A Gallery", "museum", False),
            _venue("A Park", "park", True),
            _venue("A Mall", "mall", True)]

    def test_the_whole_plan_is_identical_across_every_answer(self):
        plans = {}
        for tn in ("yes", "sometimes", "no"):
            plans[tn] = [(s["time"], s["kind"], (s.get("venue") or {}).get("name"))
                         for s in _plan(self.POOL, transit_nap=tn)]
        self.assertEqual(plans["yes"], plans["sometimes"])
        self.assertEqual(plans["yes"], plans["no"])

    def test_the_adjuster_prompt_is_where_it_is_handled(self):
        from pathlib import Path
        prompt = (Path(itinerary.__file__).parent / "prompts"
                  / "plan_adjust.txt").read_text(encoding="utf-8")
        self.assertIn("{transit_nap}", prompt)


if __name__ == "__main__":
    unittest.main()
