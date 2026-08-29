"""Opening hours and official websites from OpenStreetMap, via Overpass.

Two callers, both of which hand what they find to a person rather than acting
on it: scripts/verify_hours.py checks our stored hours against an outside
source, and workflows/propose_venues.py fills a candidate's hours in before
review so the reviewer confirms instead of typing.

OSM rather than a commercial API for two reasons. It is openly licensed, so a
result can be stored and shown (with attribution) rather than only glanced at,
which is the restriction that took Google Places out of the proposal path. And
it costs nothing, which matters for something meant to run repeatedly.

The trade is coverage: measured, about half of Vancouver's museums carry an
`opening_hours` tag. A venue OSM knows nothing about is reported as
unverifiable, never as agreeing.

Overpass is shared infrastructure and rate-limits hard: querying venue by venue
earned a 429 within about thirty requests. So names are batched into one query
and callers are expected to be a script, not a page.
"""

import re
import time

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "travel-with-tots/1.0 (venue hours check)"
REQUEST_TIMEOUT_SECONDS = 120
# What a caller that somebody is waiting on should allow instead. The proposal
# page runs one batched query for a whole batch, and a slow Overpass there
# should cost the batch its prefilled hours, not hold the page open. Every
# caller degrades to blank hours on a failure, so a timeout is a real option.
PAGE_TIMEOUT_SECONDS = 25

# Metro Vancouver, generously drawn, as (south, west, north, east) for Overpass.
BBOX = (49.00, -123.40, 49.45, -122.70)

# Overpass asks for restraint, and a 429 costs the whole run.
BETWEEN_QUERIES_SECONDS = 2.0

# How many venue names to put in one query. Large enough that a normal run is a
# couple of requests, small enough that a timeout loses little.
NAMES_PER_QUERY = 12

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


class OverpassError(Exception):
    """Overpass could not be reached, or refused."""


def _name_key(name):
    """A venue name reduced to something safe to put in an Overpass regex."""
    trimmed = re.sub(r"\s*\(.*?\)", " ", name or "").strip()
    return re.sub(r'[^A-Za-z0-9 \-\']', "", trimmed)[:28].strip()


def _match_key(name):
    """A name reduced to letters and digits, for deciding whether two names are
    the same place. Separate from _name_key, which has to survive being put
    inside an Overpass regex."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _query(names):
    south, west, north, east = BBOX
    box = f"{south},{west},{north},{east}"
    clauses = "".join(
        f'node["name"~"{key}",i]({box});way["name"~"{key}",i]({box});'
        for key in (_name_key(n) for n in names) if key)
    return f"[out:json][timeout:90];({clauses});out tags;"


def venue_facts(names, timeout=REQUEST_TIMEOUT_SECONDS):
    """{venue name as given: {"osm_name", "opening_hours", "website"}}.

    Only what OSM actually holds: a name it knows nothing about is absent from
    the result, and a key is absent rather than empty when that tag is missing,
    so a caller can never mistake "we did not find it" for "it has none".

    **Matched on the whole name, not a substring.** The Overpass query has to
    use a regex, which matches loosely, so the results are filtered again here.
    That filter is the difference between a fact and a plausible wrong answer:
    querying loosely returned "The Granville Island Toy Company" for Granville
    Island and the Kerrisdale branch for Vancouver Public Library, both of
    which would have been written into a candidate's hours as if checked. It
    also finds more, not less -- "Maplewood Farm" was being shadowed by
    "Maplewood Farm Livestock Barn", whichever came back first.

    `osm_name` is returned so a reviewer can see which OSM entry answered. A
    match this code accepts can still be the wrong branch of something, and the
    name is what makes that visible before it is approved.
    """
    names = [n for n in names if _name_key(n)]
    found = {}
    for start in range(0, len(names), NAMES_PER_QUERY):
        batch = names[start:start + NAMES_PER_QUERY]
        if start:
            time.sleep(BETWEEN_QUERIES_SECONDS)
        tags = _fetch_tags(batch, timeout)
        for wanted in batch:
            key = _match_key(wanted)
            same = [t for t in tags if _match_key(t.get("name")) == key]
            fact = {}
            for tag in same:
                for ours, theirs in (("opening_hours", "opening_hours"),
                                     ("website", "website"),
                                     ("website", "contact:website")):
                    if tag.get(theirs) and ours not in fact:
                        fact[ours] = tag[theirs]
                        fact.setdefault("osm_name", tag["name"])
            # Only when OSM actually held something. A name match on its own
            # says the place exists, which the caller already believed, and
            # returning it would read as "OSM answered" to anyone checking
            # membership rather than the keys.
            if fact:
                found[wanted] = fact
    return found


def _fetch_tags(names, timeout=REQUEST_TIMEOUT_SECONDS):
    """The tag dicts Overpass returns for one batch of names."""
    try:
        response = session.post(OVERPASS_URL, data={"data": _query(names)},
                                timeout=timeout)
        response.raise_for_status()
        elements = response.json().get("elements", [])
    except requests.exceptions.RequestException as e:
        # Never print the exception: none of these are keyed, but the habit
        # is the rule (see scripts/geocode_venues.py).
        raise OverpassError(
            f"Overpass request failed ({type(e).__name__})") from None
    except ValueError:
        raise OverpassError("Overpass returned an unreadable body") from None
    return [e["tags"] for e in elements if (e.get("tags") or {}).get("name")]


def opening_hours_for(names):
    """{venue name as given: the OSM opening_hours string}, for verify_hours."""
    return {name: fact["opening_hours"]
            for name, fact in venue_facts(names).items()
            if fact.get("opening_hours")}


# One unambiguous pair, and nothing else. A candidate's hours are prefilled
# from this, so it has to refuse anything a single open/close cannot hold: a
# string naming particular days, a second range, a closure. Those still reach
# the reviewer as the raw string, which is where they belong.
def single_pair(osm_hours):
    """(open, close) when OSM says exactly one plain range, else (None, None).

    "Mo-Su 12:00-22:00" is one pair and safe to prefill. "We,Th 12:00-14:30,
    16:30-20:45; Fr ..." is not, and guessing which half to use would be
    inventing an answer -- the thing this whole path exists to avoid.
    """
    if not osm_hours:
        return None, None
    text = osm_hours.strip()
    if text == "24/7":
        return "00:00", "23:59"
    # "Mo-Su" names days but excludes none, so it holds no more than one pair
    # does. Stripped before the day check, or every venue open all week would
    # be refused for saying so. "Mo-Fr" is left alone: it is a real exclusion.
    text = _ALL_WEEK.sub(" ", text)
    if _DAY_SPECIFIC.search(text):
        return None, None
    ranges = _RANGE.findall(text)
    if len(ranges) != 1:
        return None, None
    opens, closes = ranges[0]
    return _pad(opens), _pad(closes)


_ALL_WEEK = re.compile(r"\bMo\s*-\s*Sun?\b", re.IGNORECASE)


# A bare "HH:MM-HH:MM". Enough to tell whether our single pair appears in what
# OSM says, without implementing the opening_hours grammar, which is a rabbit
# hole and not what a person needs in order to adjudicate.
_RANGE = re.compile(r"\b(\d{1,2}:\d{2})\s*-\s*(\d{1,2}:\d{2})")

# Signs that OSM holds more than one pair can express, which is itself worth
# telling a reviewer: it means a season or day slot is waiting to be filled in.
_DAY_SPECIFIC = re.compile(
    r"\b(Mo|Tu|We|Th|Fr|Sa|Su|PH|SH)\b|\boff\b|\bclosed\b", re.IGNORECASE)


def compare(our_open, our_close, osm_hours):
    """What to tell a reviewer about the gap between our pair and OSM's string.

    Returns one of:
      "agrees"      our pair appears in what OSM says, and OSM says no more.
      "more_detail" OSM names particular days, which one pair cannot hold.
      "differs"     OSM's times are not ours.
      "unverifiable" OSM has nothing, or nothing readable as a time.

    Deliberately shallow. A reviewer reads the raw string and decides; this only
    has to be right about whether the string is worth their attention.
    """
    if not osm_hours:
        return "unverifiable"
    text = osm_hours.strip()
    if text == "24/7":
        return "agrees" if (our_open, our_close) == ("00:00", "23:59") else "differs"
    # "Mo-Su" names every day and excludes none, so it holds no more than one
    # pair does. Stripped first, exactly as single_pair does -- without this a
    # venue open the same hours all week was reported as "more detail" rather
    # than compared, which was four of seven real findings and two outright
    # wrong: Science World agreed with us exactly and was flagged anyway.
    text = _ALL_WEEK.sub(" ", text)
    ranges = [(_pad(a), _pad(b)) for a, b in _RANGE.findall(text)]
    if not ranges:
        return "unverifiable"
    if _DAY_SPECIFIC.search(text):
        return "more_detail"
    return "agrees" if (our_open, our_close) in ranges else "differs"


def _pad(text):
    """"9:00" as "09:00", so a comparison is not defeated by a leading zero."""
    hour, _, minute = text.partition(":")
    return f"{int(hour):02d}:{minute}"
