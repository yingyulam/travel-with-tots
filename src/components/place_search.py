"""Place Search component: find a place by name, via Google Places.

Self-contained, one file per component (see /components). One job: a text query
in, candidate places out. It never decides what to do with them.

Separate from geocode.py on purpose. That component turns an address or
coordinates into a location; this one answers "which place did you mean", which
is a different question and a different Google API. Geocoding is address-shaped
and will happily return a street for a cafe's name.

Server-side, like every other key in this app. Searching is a plain REST call,
so unlike an embedded map there is no reason for the key to reach the browser.
Note this API takes the key in a header rather than a query parameter, so a
network error cannot embed it in a URL the way the Geocoding API's can.
"""

import os

import requests

# The current Places API. The legacy /maps/api/place/textsearch endpoint is not
# available to projects created from 2025 on, so this is what a new key gets.
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
REQUEST_TIMEOUT_SECONDS = 10
RESULT_LIMIT = 5

# Google requires an explicit field mask and rejects the request without one.
# Asking for less is also cheaper: billing tiers are per requested field.
FIELD_MASK = ",".join((
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.primaryType",
    "places.addressComponents",
))

# How wide a bias around the map's centre counts as "near here", in metres.
# Generous enough to cover a city, tight enough that a name still resolves
# locally rather than to a famous namesake on another continent.
LOCATION_BIAS_RADIUS_M = 30000


class PlaceSearchError(Exception):
    """Raised when the Places call fails or the query matches nothing."""


def _pick_component(components: list, wanted_type: str) -> str:
    """The long name of the first address component of `wanted_type`."""
    for component in components:
        if wanted_type in component.get("types", []):
            return component.get("longText", "")
    return ""


def _normalize(place: dict) -> dict:
    """One Places result in the shape the log-a-place form needs: enough to
    fill in the name, the area, and the pin."""
    location = place.get("location", {})
    components = place.get("addressComponents", [])
    return {
        "name": place.get("displayName", {}).get("text", ""),
        "address": place.get("formattedAddress", ""),
        "lat": location.get("latitude"),
        "lng": location.get("longitude"),
        "city": _pick_component(components, "locality"),
        "neighbourhood": _pick_component(components, "neighborhood")
                         or _pick_component(components, "sublocality"),
        # Google's own category, so "kind of place" can be prefilled rather
        # than typed. Underscores are Google's; parents read words.
        "type": (place.get("primaryType") or "").replace("_", " "),
    }


def search_places(query: str, lat=None, lng=None, limit=RESULT_LIMIT) -> list[dict]:
    """Places matching `query`, nearest-first when coordinates bias the search.

    Returns a list of normalized dicts, empty when Google matched nothing.
    Raises KeyError if GOOGLE_MAPS_API_KEY isn't set, or PlaceSearchError if
    Google itself fails.
    """
    api_key = os.environ["GOOGLE_MAPS_API_KEY"]
    body = {"textQuery": query, "maxResultCount": limit}
    if lat is not None and lng is not None:
        body["locationBias"] = {"circle": {
            "center": {"latitude": lat, "longitude": lng},
            "radius": LOCATION_BIAS_RADIUS_M,
        }}

    try:
        response = requests.post(
            PLACES_SEARCH_URL,
            headers={
                "Content-Type": "application/json",
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": FIELD_MASK,
            },
            json=body,
            timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        # `from None` for the same reason geocode.py does it: never risk an
        # exception message carrying anything about the request.
        raise PlaceSearchError("Couldn't reach Google Places.") from None

    try:
        body = response.json()
    except ValueError:
        raise PlaceSearchError("Google Places returned an unreadable response.") from None

    # A query matching nothing comes back as 200 with no "places" key at all,
    # which is an answer rather than a failure.
    return [_normalize(place) for place in body.get("places", [])[:limit]]
