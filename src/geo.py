"""Distance between two coordinates, and whether one is in the right region.
No routing, just straight-line geometry."""

from math import asin, cos, radians, sin, sqrt

EARTH_RADIUS_KM = 6371.0

# Metro Vancouver, generously drawn, as (south, north, west, east). A guard on
# results, not a search box: any geocoder asked about "Vancouver" can return
# Vancouver, Washington, and a live proposal run once accepted Fort Vancouver
# at latitude 45.6, in another country.
#
# Deliberately not shared with osm.BBOX, which is a tighter box in a different
# tuple order because it bounds an Overpass *query*: widening it to this would
# make every Overpass call scan more of the map for no benefit.
METRO_VANCOUVER_BOUNDS = (48.9, 49.6, -123.5, -122.5)


def in_metro_vancouver(lat, lng):
    """Whether a coordinate is plausibly in Metro Vancouver.

    False for a missing coordinate: not knowing where something is cannot count
    as knowing it is here.
    """
    if lat is None or lng is None:
        return False
    south, north, west, east = METRO_VANCOUVER_BOUNDS
    return south <= lat <= north and west <= lng <= east


def haversine_km(lat1, lng1, lat2, lng2):
    """Great-circle distance in kilometres between two points.

    Straight-line, so it under-reports real walking or transit distance --
    fine for ranking which of several venues is nearest, which is all the
    app uses it for. Real travel time would need a routing API.
    """
    lat1, lng1, lat2, lng2 = map(radians, (lat1, lng1, lat2, lng2))
    d_lat, d_lng = lat2 - lat1, lng2 - lng1
    a = sin(d_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(d_lng / 2) ** 2
    return 2 * EARTH_RADIUS_KM * asin(sqrt(a))
