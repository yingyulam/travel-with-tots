"""Coordinates for a place name, from OpenStreetMap's Nominatim.

A keyless, openly-licensed client, sibling to src/opendata.py and src/osm.py
rather than a component: nothing a parent touches uses it, so there is no
admin test page to isolate.

Why this exists rather than the Places lookup it replaced. Google Maps Platform
terms allow storing a place *id* but restrict retaining returned content, and
the proposal path wrote Places addresses and coordinates into
data/venue_candidates.csv, which is tracked in git -- so a public repo
redistributed them. Nominatim is ODbL: a result can be stored and shown with
attribution, which is the same reason src/osm.py was chosen for hours.

The trade is precision. Nominatim is free-text geocoding, good at landmarks,
museums and malls, and it has no notion of "the branch nearest here". Two
guards do the work a paid API would: a name-only hit is accepted only when the
result is actually in British Columbia, and every result is checked against
Metro Vancouver's bounds by the caller, because a search for "Vancouver"
reaches Vancouver, Washington and a live run once proposed Fort Vancouver at
latitude 45.6.

Lifted from scripts/geocode_venues.py, which had the working version and the
right pacing but sits outside src/ and so could never be imported or tested.
"""

import re
import time

import requests

SEARCH_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "travel-with-tots/1.0 (venue geocoding for a trip planner)"
# Nominatim's usage policy asks for at most one request per second. Paid for by
# donations, so the limit is a courtesy rather than a quota, and honoured here
# by sleeping after every call rather than only between batches.
DELAY_SECONDS = 1.1
REQUEST_TIMEOUT_SECONDS = 20
# Nothing here needs a second candidate: the caller wants one coordinate or
# none, and a wrong one is worse than a missing one.
RESULT_LIMIT = 1

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


class NominatimError(Exception):
    """Nominatim could not be reached, or refused."""


def _clean(name):
    """A venue name without its parenthetical alias: "Trout Lake (John Hendry
    Park)" searches better as "Trout Lake"."""
    return re.sub(r"\s*\(.*?\)", "", name or "").strip()


def locate(name, area=None):
    """{"lat", "lng", "address", "area"} for `name`, or None.

    The area-qualified query goes first because it pins down which of several
    similarly named places is meant. The bare name is tried second, and a hit
    from it is only accepted when Nominatim puts the result in British
    Columbia -- otherwise a bare name silently resolves to a same-named place
    in another province or another country.

    `area` is passed through as the parent or the agent spelled it; it is only
    a search hint, so an unrecognised one costs nothing.
    """
    cleaned = _clean(name)
    if not cleaned:
        return None
    queries = [f"{cleaned}, Vancouver, BC, Canada"]
    if area:
        queries.insert(0, f"{cleaned}, {area}, Vancouver, BC, Canada")

    for index, query in enumerate(queries):
        hit = _search(query)
        if not hit:
            continue
        address = hit.get("address") or {}
        # The bare-name query is always the last one, with or without an area
        # hint in front of it. Only its hits need the province check: an
        # area-qualified query has already been pinned to Vancouver.
        bare = index == len(queries) - 1
        if bare and address.get("state") != "British Columbia":
            continue
        return {
            "lat": float(hit["lat"]),
            "lng": float(hit["lon"]),
            "address": hit.get("display_name") or "",
            "area": (address.get("neighbourhood") or address.get("suburb")
                     or address.get("city_district") or ""),
            "external_id": _external_id(hit),
        }
    return None


def _external_id(hit):
    """A stable identity for the place Nominatim matched, or "".

    Places gave us nothing storable; OSM ids are openly licensed and durable,
    so a re-proposal of the same venue can be recognised rather than inserted
    a second time (see idx_venues_external_id).
    """
    kind, osm_id = hit.get("osm_type"), hit.get("osm_id")
    return f"osm:{kind}/{osm_id}" if kind and osm_id else ""


def _search(query):
    """The first Nominatim result for `query`, or None."""
    try:
        response = session.get(
            SEARCH_URL,
            params={"q": query, "format": "json", "limit": RESULT_LIMIT,
                    "addressdetails": 1},
            timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        hits = response.json()
    except requests.exceptions.RequestException as e:
        # Never print the exception itself: none of these are keyed, but the
        # habit is the rule (see src/osm.py).
        raise NominatimError(
            f"Nominatim request failed ({type(e).__name__})") from None
    except ValueError:
        raise NominatimError("Nominatim returned an unreadable body") from None
    finally:
        # After the call, and in the failure path too: a request that errored
        # still reached the service.
        time.sleep(DELAY_SECONDS)
    return hits[0] if hits else None
