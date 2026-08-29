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


# How far apart two consecutive stops may reasonably sit, by how the family gets
# between them. A judgment about a day out with a small child, not a routing
# calculation: no schedules, no transfers, no waiting, and no attempt to model
# that a SkyTrain covers more ground than a bus.
#
# 1.5km on foot is about 26 minutes pushing a stroller, which fits even the
# tightest gap the nap anchoring produces -- so getting the selection right
# leaves the clock alone. See itinerary._pick.
REACH_KM = {"walk": 1.5, "transit": 5.0, "car": 8.0}

# An unrecognised mode -- a trip saved before this existed, a hand-made post --
# takes the tightest reach. A clustered day is fine for a family with a car;
# a spread-out one is not fine for a family on foot.
DEFAULT_REACH_KM = REACH_KM["walk"]


def reach_km(mode):
    """How far the next stop may reasonably be, for one transport mode.

    Tolerates the old list shape, taking the widest: `trips.transit` held a JSON
    array before the form became one question, and generate_plans can be handed
    a dict directly by a component or a test. Ticking several always meant
    "take the widest", so that is what a legacy list resolves to.
    """
    if isinstance(mode, (list, tuple, set)):
        return max((reach_km(m) for m in mode), default=DEFAULT_REACH_KM)
    return REACH_KM.get(mode, DEFAULT_REACH_KM)


def within_reach(venue, anchor, reach):
    """Whether `venue` is close enough to `anchor` to be the next stop.

    True when either coordinate is missing, on purpose. Penalising a venue for
    incomplete data is the wrong direction: the cost of getting this wrong is a
    longer walk, not a wrong answer, and four curated venues have no coordinates
    yet -- including both Granville Island markets.
    """
    if anchor is None:
        return True
    for point in (venue, anchor):
        if point.get("lat") is None or point.get("lng") is None:
            return True
    return haversine_km(anchor["lat"], anchor["lng"],
                        venue["lat"], venue["lng"]) <= reach


def as_point(lat, lng):
    """A {"lat", "lng"} point from two untrusted values, or None.

    One place to turn whatever arrived -- a form string, a JSON number, a NULL
    column, a hand-made post -- into something the planner can measure from.
    Anything that is not a real coordinate becomes None rather than raising,
    because an accommodation is optional: a day still plans without one, it just
    has no start and end anchor.

    Range-checked but not region-checked. A family staying outside Metro
    Vancouver is a real family, and the anchor only ever sorts, so a distant
    pin costs the day nothing. METRO_VANCOUVER_BOUNDS guards *proposals*, where
    a wrong hit gets written to the venues table.
    """
    try:
        lat, lng = float(lat), float(lng)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return {"lat": lat, "lng": lng}


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
