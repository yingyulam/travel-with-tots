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
from ..data_loader import maps_url
from ..geo import haversine_km
from .search_web import WebSearchError, search_web

NEED_LABELS = dict(interactions.NEED_OPTIONS)
DEFAULT_LIMIT = 2


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


def _curated_places(need, city, neighbourhood, limit, lat=None, lng=None):
    """Curated venues in `city` matching `need`, nearest first. The venues
    table has no maps_url column (data_loader only attaches it to the JSON
    copy), so it's built here from the same helper. Each place gets a
    `distance_km` when it can be computed, and None when it can't."""
    venues = _rank_by_proximity(
        [dict(row) for row in db.get_venues_in_city(city)], neighbourhood, lat, lng)
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
                 lat=None, lng=None, limit=DEFAULT_LIMIT) -> dict:
    """Places matching `need` near a resolved location. Returns
    {"places", "source", "need", "city", "neighbourhood"} where source is
    "curated", "search", or "none". Given `lat`/`lng`, curated results are
    ranked by real distance and each carries a `distance_km`. Never raises
    for an empty result: no match is an answer, not a failure. A failing web
    search also degrades to an empty list rather than raising, so a missing
    or blocked Tavily key can't break the panel when curated already had
    nothing to offer.

    Coordinates alone are enough: with them, venues are searched across every
    city and ranked by distance, so sharing a location works even with no
    geocoding key configured at all. `city` only narrows the SQL when that is
    the only thing known about where the parent is."""
    has_coords = lat is not None and lng is not None
    places = (_curated_places(need, city, neighbourhood, limit, lat, lng)
              if city or has_coords else [])
    source = "curated" if places else "none"

    if not places:
        try:
            places = _search_places(need, place_name or city or "me", limit)
            source = "search" if places else "none"
        except (WebSearchError, requests.exceptions.RequestException, KeyError) as e:
            print(f"Find-nearby search fallback skipped: {e}")

    return {"places": places, "source": source, "need": need,
            "city": city, "neighbourhood": neighbourhood}
