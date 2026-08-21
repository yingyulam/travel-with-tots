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
from .search_web import WebSearchError, search_web

NEED_LABELS = dict(interactions.NEED_OPTIONS)
DEFAULT_LIMIT = 2


def _curated_places(need, city, neighbourhood, limit):
    """Curated venues in `city` matching `need`. Same-neighbourhood venues
    are moved to the front before matching, so when the need has more
    matches than `limit` the closest ones win -- neighbourhood is the app's
    only proximity proxy, since the venue data has no lat/lng. The venues
    table has no maps_url column (data_loader only attaches it to the JSON
    copy), so it's built here from the same helper."""
    venues = [dict(row) for row in db.get_venues_in_city(city)]
    if neighbourhood:
        venues.sort(key=lambda v: v["neighbourhood"] != neighbourhood)
    places = interactions.find_nearby(need, venues, limit)
    for venue in places:
        venue["maps_url"] = maps_url(venue["name"], venue["city"] or city)
    return places


def _search_places(need, place_name, limit):
    """Web-search results normalized into the same shape as curated places,
    so the caller renders one list either way."""
    label = NEED_LABELS.get(need, need or "kid-friendly place")
    results = search_web(f"{label} near {place_name} kid friendly")
    return [
        {"name": r["title"], "neighbourhood": "", "type": "web result",
         "reason": r["snippet"], "maps_url": r["url"]}
        for r in results[:limit]
    ]


def find_nearby(*, need, city="", neighbourhood="", place_name="",
                 limit=DEFAULT_LIMIT) -> dict:
    """Places matching `need` near a resolved location. Returns
    {"places", "source", "need", "city", "neighbourhood"} where source is
    "curated", "search", or "none". Never raises for an empty result: no
    match is an answer, not a failure. A failing web search also degrades to
    an empty list rather than raising, so a missing or blocked Tavily key
    can't break the panel when curated already had nothing to offer."""
    places = _curated_places(need, city, neighbourhood, limit) if city else []
    source = "curated" if places else "none"

    if not places:
        try:
            places = _search_places(need, place_name or city or "me", limit)
            source = "search" if places else "none"
        except (WebSearchError, requests.exceptions.RequestException, KeyError) as e:
            print(f"Find-nearby search fallback skipped: {e}")

    return {"places": places, "source": source, "need": need,
            "city": city, "neighbourhood": neighbourhood}
