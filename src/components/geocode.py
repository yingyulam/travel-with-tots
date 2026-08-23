"""Geocode component: turn a location into a place name, via Google Geocoding.

Self-contained, one file per component (see /components). One job only:
coordinates or a typed address in, a normalized place out. It never decides
what to do with that place -- find_nearby.py does the matching.

Server-side on purpose: the browser's own navigator.geolocation supplies
coordinates for free with no key, so the Google key never has to reach the
client and stays a normal server-side secret like the app's other keys.
"""

import os

import requests

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
REQUEST_TIMEOUT_SECONDS = 10

# Google returns the place's parts as a flat list of typed components; these
# are the types worth pulling out, most specific first for neighbourhood.
CITY_TYPES = ("locality", "postal_town", "administrative_area_level_2")
NEIGHBOURHOOD_TYPES = ("neighborhood", "sublocality", "sublocality_level_1")


class GeocodeError(Exception):
    """Raised when the Google Geocoding API call fails or finds nothing.

    Every requests-level failure is converted into this, deliberately
    dropping the original exception and its chain: Google needs the key as a
    query parameter, and a requests network error embeds the full request URL
    (key included) in its message, so re-raising or logging it would leak the
    key into the server log."""


def _pick_component(components: list, wanted_types: tuple) -> str:
    """First component matching any of `wanted_types`, in that order of
    preference (not the order Google happens to return them in)."""
    for wanted in wanted_types:
        for component in components:
            if wanted in component.get("types", []):
                return component.get("long_name", "")
    return ""


def _first_result(params: dict) -> dict:
    """Call the Geocoding API and return its first result. Raises KeyError if
    GOOGLE_MAPS_API_KEY isn't set, or GeocodeError if Google reports a
    non-OK status -- it answers with HTTP 200 even for ZERO_RESULTS or
    REQUEST_DENIED, so the body's own status is the real check."""
    api_key = os.environ["GOOGLE_MAPS_API_KEY"]
    try:
        response = requests.get(
            GEOCODE_URL, params={**params, "key": api_key},
            timeout=REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
    except requests.exceptions.RequestException:
        # `from None`: see GeocodeError's docstring -- the original message
        # carries the key.
        raise GeocodeError("Couldn't reach Google Geocoding.") from None

    try:
        body = response.json()
    except ValueError:
        raise GeocodeError("Google Geocoding returned an unreadable response.") from None
    status = body.get("status")
    if status != "OK" or not body.get("results"):
        detail = body.get("error_message") or status or "no results"
        raise GeocodeError(f"Google Geocoding couldn't resolve that location: {detail}")
    return body["results"][0]


def _normalize(result: dict) -> dict:
    """Flatten a Geocoding result into the shape find_nearby expects."""
    components = result.get("address_components", [])
    location = result.get("geometry", {}).get("location", {})
    return {
        "city": _pick_component(components, CITY_TYPES),
        "neighbourhood": _pick_component(components, NEIGHBOURHOOD_TYPES),
        "formatted_address": result.get("formatted_address", ""),
        "lat": location.get("lat"),
        "lng": location.get("lng"),
    }


def reverse_geocode(lat, lng) -> dict:
    """Coordinates (e.g. from the browser) to {"city", "neighbourhood",
    "formatted_address", "lat", "lng"}."""
    return _normalize(_first_result({"latlng": f"{lat},{lng}"}))


def geocode(address: str) -> dict:
    """A typed address or place name to the same shape as reverse_geocode,
    for when a parent sets their location by hand instead of sharing it."""
    return _normalize(_first_result({"address": address}))


# "We don't know where they are", in the shape a resolved location has. Named
# rather than repeated so the places that need it cannot drift.
UNKNOWN_LOCATION = {"city": "", "neighbourhood": "", "formatted_address": "",
                    "lat": None, "lng": None}


def resolve_location(lat=None, lng=None, address="") -> dict:
    """The place a find-nearby request is centred on: browser coordinates, a
    typed address, or nothing at all, as one resolved shape.

    Lives here rather than in a route because it is not a route's job and more
    than one caller needs it: the component's test page, the trip page's need
    panel, and the chat workflow all have to resolve a location the same way,
    or they answer the same question differently. Raises what geocode raises,
    so each caller can decide what a failure costs it.
    """
    if lat is not None and lng is not None:
        return reverse_geocode(lat, lng)
    address = (address or "").strip()
    if address:
        return geocode(address)
    return dict(UNKNOWN_LOCATION)
