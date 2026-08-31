"""Find nearby component: kid-friendly places matching an immediate need.

Self-contained entry point, one file per component (see /components). One
job: narrowing by location. The need-matching itself is not reimplemented
here -- it calls interactions.find_nearby(), the same function the live
/trip panel and the AI Agent's tool already use, just handing it a
location-filtered candidate list instead of every venue. Falls back to a
web search only when the curated table has nothing, the same
"deterministic first, then escalate" shape plan_trip.py uses.
"""

import requests

from .. import db, interactions
from ..data_loader import SUPPORTED_CITIES, maps_search_url, maps_url
from ..geo import haversine_km, reach_km, within_reach
from .search_web import WebSearchError, search_web

NEED_LABELS = dict(interactions.NEED_OPTIONS)
DEFAULT_LIMIT = 2

# The need the venue table can partly answer and Google Maps can finish. Kept
# as a constant because three rules key off it: it is distance-capped, it never
# reaches the web, and it always carries a Maps link.
LUNCH_NEED = "restaurant"

# What the Maps handoff searches for. "kid friendly" because that is the whole
# question a parent is asking at noon with a toddler, and Maps ranks on it.
LUNCH_QUERY = "kid friendly restaurants"


def searchable(where: dict) -> dict:
    """A resolved location, with the city this app covers filled in when it
    resolved to nothing at all.

    find_nearby below treats "nothing known" as "search the whole web", which
    is the right general contract and the wrong answer here: it replied to a
    Vancouver app's question with restaurants in Austin. Every caller wants the
    same fallback instead, so the rule lives once. Applied to the resolved
    location rather than inside find_nearby, which keeps its honest contract
    and its test.
    """
    if where.get("city") or where.get("lat") is not None:
        return where
    return {**where, "city": SUPPORTED_CITIES[0]}


def _rank_by_proximity(venues, neighbourhood, lat, lng):
    """Order venues nearest-first, so that when a need has more matches than
    the caller wants, the closest ones survive the cut.

    Real distance when both sides have coordinates. Not every venue does:
    only some resolve from open data, and user-submitted rows never will, so
    the neighbourhood-match fallback is load-bearing rather than legacy.
    Venues without coordinates sort after those with them, but still ahead of
    other-neighbourhood venues, so partial coordinate coverage degrades
    gracefully instead of hiding venues."""
    if lat is None or lng is None:
        return sorted(venues, key=lambda v: v["neighbourhood"] != neighbourhood) \
            if neighbourhood else list(venues)

    def key(venue):
        if venue.get("lat") is None or venue.get("lng") is None:
            return (1, venue["neighbourhood"] != neighbourhood, 0.0)
        return (0, False, haversine_km(lat, lng, venue["lat"], venue["lng"]))

    return sorted(venues, key=key)


def _within_lunch_reach(venues, lat, lng, transit):
    """Venues close enough to be lunch, by how the family gets between stops.

    A filter, where the rest of this file only sorts, and the exception is
    earned: elsewhere dropping a venue means a thinner day, but here anything
    left out is still reachable through the Maps link, so the cost of being
    strict is nothing and the cost of being loose is sending a family 8km for
    a sandwich.

    `within_reach` keeps a venue whose coordinates are unknown, which is
    deliberate and load-bearing: four curated venues have none, including both
    Granville Island markets, which are exactly the kind of place this should
    surface.
    """
    if lat is None or lng is None:
        return venues
    anchor = {"lat": lat, "lng": lng}
    return [v for v in venues if within_reach(v, anchor, reach_km(transit))]


def _curated_places(need, city, neighbourhood, limit, lat=None, lng=None,
                    transit=""):
    """Curated venues in `city` matching `need`, nearest first. The venues
    table has no maps_url column (data_loader only attaches it to the JSON
    copy), so it's built here from the same helper. Each place gets a
    `distance_km` when it can be computed, and None when it can't."""
    rows = db.get_venues_in_city(city)
    # Overlay what somebody actually observed. Without this the need filters
    # matched on the venues columns, which no longer carry current answers: a
    # parent who reported "the nursing room is gone" changed nothing, and the
    # next parent asking for one was still sent to the same mall. Reports are
    # what every other read of an amenity has used since venue_reports landed;
    # this path was simply missed.
    reported = db.reported_flags([row["id"] for row in rows])
    venues = _rank_by_proximity(
        [{**dict(row), **reported.get(row["id"], {})} for row in rows],
        neighbourhood, lat, lng)
    if need == LUNCH_NEED:
        venues = _within_lunch_reach(venues, lat, lng, transit)
    places = interactions.find_nearby(need, venues, limit)
    for venue in places:
        venue["maps_url"] = maps_url(venue["name"], venue["city"] or city)
        venue["distance_km"] = (
            round(haversine_km(lat, lng, venue["lat"], venue["lng"]), 2)
            if lat is not None and lng is not None
            and venue.get("lat") is not None and venue.get("lng") is not None
            else None)
    return places


def _search_places(need, place_name, limit):
    """Web-search results normalized into the same shape as curated places,
    so the caller renders one list either way."""
    label = NEED_LABELS.get(need, need or "kid-friendly place")
    results = search_web(f"{label} near {place_name} kid friendly")
    return [
        {"name": r["title"], "neighbourhood": "", "type": "web result",
         "reason": r["snippet"], "maps_url": r["url"], "distance_km": None}
        for r in results[:limit]
    ]


def find_nearby(*, need, city="", neighbourhood="", place_name="",
                 lat=None, lng=None, limit=DEFAULT_LIMIT, transit="",
                 near_place="") -> dict:
    """Places matching `need` near a resolved location. Returns
    {"places", "source", "need", "city", "neighbourhood", "maps_search_url"}
    where source is "curated", "search", or "none". Given `lat`/`lng`, curated
    results are ranked by real distance and each carries a `distance_km`. Never
    raises for an empty result: no match is an answer, not a failure. A failing
    web search also degrades to an empty list rather than raising, so a missing
    or blocked Tavily key can't break the panel when curated already had
    nothing to offer.

    **Lunch is answered differently, and deliberately.** It is capped to what is
    reachable by `transit` (see `_within_lunch_reach`) and it never reaches the
    web: enumerating the restaurants of a city is not something this table can
    do, and search results are pages rather than places -- no distance, no
    hours, nothing a parent standing on a street corner can act on. What we know
    is offered, and the rest is handed to Google Maps through
    `maps_search_url`, which has live hours and reviews we never will.

    Every other need keeps its web fallback. Maps is a poor answer for "nursing
    room", and for those needs a web result is the only answer there is.

    `near_place` is what the Maps link falls back to when the browser shared no
    location: the stop the parent is standing at. Never the city -- a search for
    restaurants near a city is the whole map.

    Coordinates alone are enough: with them, venues are searched across every
    city and ranked by distance, so sharing a location works even with no
    geocoding key configured at all. `city` only narrows the SQL when that is
    the only thing known about where the parent is."""
    has_coords = lat is not None and lng is not None
    places = (_curated_places(need, city, neighbourhood, limit, lat, lng, transit)
              if city or has_coords else [])
    source = "curated" if places else "none"
    is_lunch = need == LUNCH_NEED

    if not places and not is_lunch:
        try:
            places = _search_places(need, place_name or city or "me", limit)
            source = "search" if places else "none"
        except (WebSearchError, requests.exceptions.RequestException, KeyError) as e:
            print(f"Find-nearby search fallback skipped: {e}")

    # Offered alongside whatever was found, not only when nothing was: "here is
    # what we know, and here is where to look for more" reads as a handoff,
    # where the same link under an empty list reads as an apology.
    #
    # Anchored on where the parent actually is, never on the city. "Restaurants
    # near Vancouver" is not an answer to a hungry toddler -- it is the whole
    # map, and it would send somebody at Stanley Park a list starting downtown.
    # So: their coordinates if the browser shared them, else the address that
    # resolved to, else the stop they are standing at. If none of those is
    # known there is nothing worth linking to, and no link is offered.
    handoff = (maps_search_url(LUNCH_QUERY, lat, lng,
                               near=place_name or near_place)
               if is_lunch and (has_coords or place_name or near_place) else None)

    return {"places": places, "source": source, "need": need,
            "city": city, "neighbourhood": neighbourhood,
            "maps_search_url": handoff}
