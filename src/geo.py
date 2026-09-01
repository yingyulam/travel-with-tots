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


# How long a family will spend getting from one stop to the next, and how far
# that is. A budget in minutes rather than kilometres because minutes are what a
# parent can actually judge: "1.5 km" means nothing standing on a pavement with a
# stroller, and the comfortable limit differs enormously between families.
WALK_BUDGET_OPTIONS = (20, 30, 40)
DEFAULT_WALK_BUDGET_MIN = 20

# Pushing a stroller, with a small child, stopping. Not a brisk adult pace.
WALK_SPEED_KMH = 4.8

# Effective door-to-door speeds, waiting and parking included. Rough on purpose:
# these turn a straight line into a plausible number of minutes, and the moment
# the Routes API is enabled a real route replaces them (see estimate_minutes).
MODE_SPEED_KMH = {"walk": WALK_SPEED_KMH, "transit": 16.0, "car": 24.0}

# Street distance divided by straight-line distance, for an ordinary city grid.
# Walking distance is always at least the straight line, so haversine alone
# flatters every venue; 1.35 is the usual urban figure and keeps the estimate on
# the honest side of the real walk.
DETOUR_FACTOR = 1.35

def walk_budget_min(value):
    """A walking budget the form offered, or the default.

    Client-supplied, so an unrecognised value is the default rather than a
    number nobody chose: a hand-made post asking for 500 minutes is not a
    preference, and a trip saved before this control existed has none at all.
    """
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return DEFAULT_WALK_BUDGET_MIN
    return minutes if minutes in WALK_BUDGET_OPTIONS else DEFAULT_WALK_BUDGET_MIN


def route_km(a_lat, a_lng, b_lat, b_lng):
    """Estimated street distance, from the straight line.

    An estimate, and labelled one wherever it reaches a parent. It is the lower
    bound scaled up rather than a route: the Routes API is not enabled on this
    project, so nothing here can ask how the pavements actually run. When it is
    enabled this is the function a real matrix lookup replaces, and every caller
    keeps working.
    """
    return haversine_km(a_lat, a_lng, b_lat, b_lng) * DETOUR_FACTOR


def estimate_minutes(a_lat, a_lng, b_lat, b_lng, mode="walk"):
    """How long that leg takes, in minutes, for one transport mode."""
    speed = MODE_SPEED_KMH.get(mode, WALK_SPEED_KMH)
    return route_km(a_lat, a_lng, b_lat, b_lng) / speed * 60


def leg_minutes(origin, destination, mode="walk"):
    """Minutes between two points, or None when either has no coordinates.

    None rather than a number, and rather than zero: "we cannot measure this"
    has to be distinguishable from "this is close", because a filter reading a
    missing coordinate as nearby is how an unplaceable venue ends up in a day.
    """
    for point in (origin, destination):
        if point is None:
            return None
        if point.get("lat") is None or point.get("lng") is None:
            return None
    return estimate_minutes(origin["lat"], origin["lng"],
                            destination["lat"], destination["lng"], mode)


def within_budget(origin, destination, budget_min, mode="walk"):
    """Whether that leg fits the budget. Unmeasurable legs do not.

    The opposite of what within_reach did. As a ranking hint, "no coordinates"
    could safely mean "no opinion"; as a filter it would mean "always allowed",
    so the four venues with no coordinates would be the only ones exempt from
    the constraint. Out, and named separately to the parent.
    """
    minutes = leg_minutes(origin, destination, mode)
    return minutes is not None and minutes <= budget_min


# How far to look around a point for somewhere to eat, by how the family is
# getting there. A radius for the *nearby search*, not a constraint on the day:
# the planner works in minutes now (see within_budget), and these stay in
# kilometres because a search radius is all they were ever used for.
REACH_KM = {"walk": 1.5, "transit": 5.0, "car": 8.0}

# An unrecognised mode takes the tightest radius. A wide search is fine for a
# family with a car; it is not fine for a family on foot.
DEFAULT_REACH_KM = REACH_KM["walk"]


def reach_km(mode):
    """How far to search around a point, for one transport mode.

    Tolerates the old list shape, taking the widest: `trips.transit` held a JSON
    array before the form became one question. Ticking several always meant
    "take the widest", so that is what a legacy list resolves to.
    """
    if isinstance(mode, (list, tuple, set)):
        return max((reach_km(m) for m in mode), default=DEFAULT_REACH_KM)
    return REACH_KM.get(mode, DEFAULT_REACH_KM)


def within_reach(venue, anchor, reach):
    """Whether `venue` is inside a search radius around `anchor`.

    True when either coordinate is missing, on purpose, and only safe because
    this ranks a nearby search: four curated venues have no coordinates yet,
    including both Granville Island markets, and leaving them out of a lunch
    list costs more than including one that turns out to be a little far.

    Not the planner's rule. There, "no coordinates" cannot mean "always
    allowed" -- see within_budget.
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
