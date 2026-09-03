"""Map lookups shared by more than one blueprint.

Both answer JSON to whichever map is asking. They are here rather than on a
blueprint because the pages that use them differ only in who may call them,
and that part stays on the routes.
"""

from flask import jsonify, request

from src.components.geocode import resolve_location
from src.components.place_search import PlaceSearchError, search_places

def resolve_body_location(data):
    """A request body's location, resolved. The resolving itself lives in the
    Geocode component, so this page, the trip panel and the chat workflow all
    centre on the same place given the same coordinates."""
    return resolve_location(lat=data.get("lat"), lng=data.get("lng"),
                            address=data.get("address") or "")


def place_search_response():
    """Find a place by name, as JSON, for whichever map is asking.

    Biased toward wherever that map is currently looking, so "the library"
    resolves to a nearby one rather than a famous namesake. Server-side, so the
    Google key stays out of the browser even though the maps themselves need
    none. Two pages and a component test page share this; they differ only in
    who is allowed to call them, which is what stays on the routes.
    """
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    if not query:
        return jsonify({"error": "query is required"}), 400
    try:
        places = search_places(query, lat=data.get("lat"), lng=data.get("lng"))
    except KeyError:
        return jsonify({"error": "Searching by name needs a Google Maps API key."}), 503
    except PlaceSearchError as e:
        print(f"Place search failed: {e}")
        return jsonify({"error": "Couldn't search for that right now."}), 502
    return jsonify({"query": query, "places": places})
