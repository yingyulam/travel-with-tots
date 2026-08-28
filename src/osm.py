"""Opening hours from OpenStreetMap, via Overpass.

Used to check our stored hours against an outside source, not to replace them: a
finding goes to a person, who decides. See scripts/verify_hours.py.

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


def _query(names):
    south, west, north, east = BBOX
    box = f"{south},{west},{north},{east}"
    clauses = "".join(
        f'node["name"~"{key}",i]({box});way["name"~"{key}",i]({box});'
        for key in (_name_key(n) for n in names) if key)
    return f"[out:json][timeout:90];({clauses});out tags;"


def opening_hours_for(names):
    """{venue name as given: the OSM opening_hours string} for what OSM knows.

    A name OSM has no hours for is absent from the result rather than present
    and empty, so a caller cannot mistake "we did not find it" for "it has no
    hours".
    """
    names = [n for n in names if _name_key(n)]
    found = {}
    for start in range(0, len(names), NAMES_PER_QUERY):
        batch = names[start:start + NAMES_PER_QUERY]
        if start:
            time.sleep(BETWEEN_QUERIES_SECONDS)
        try:
            response = session.post(OVERPASS_URL, data={"data": _query(batch)},
                                    timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            elements = response.json().get("elements", [])
        except requests.exceptions.RequestException as e:
            # Never print the exception: none of these are keyed, but the habit
            # is the rule (see scripts/geocode_venues.py).
            raise OverpassError(
                f"Overpass request failed ({type(e).__name__})") from None
        except ValueError:
            raise OverpassError("Overpass returned an unreadable body") from None

        tagged = [(e["tags"]["name"], e["tags"]["opening_hours"])
                  for e in elements
                  if (e.get("tags") or {}).get("name")
                  and (e.get("tags") or {}).get("opening_hours")]
        for wanted in batch:
            key = _name_key(wanted).lower()
            for osm_name, hours in tagged:
                if key and key in osm_name.lower():
                    found.setdefault(wanted, hours)
                    break
    return found


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
    if osm_hours.strip() == "24/7":
        return "agrees" if (our_open, our_close) == ("00:00", "23:59") else "differs"
    ranges = [(_pad(a), _pad(b)) for a, b in _RANGE.findall(osm_hours)]
    if not ranges:
        return "unverifiable"
    if _DAY_SPECIFIC.search(osm_hours):
        return "more_detail"
    return "agrees" if (our_open, our_close) in ranges else "differs"


def _pad(text):
    """"9:00" as "09:00", so a comparison is not defeated by a leading zero."""
    hour, _, minute = text.partition(":")
    return f"{int(hour):02d}:{minute}"
