"""Read venues out of the database, shaped for the planners.

The venues table is the source of truth; data/venues.json is only its seed
(see db._seed_venues). This module is the boundary between the two: it turns
database rows into the plain dicts the rest of the app expects, so nothing
above it knows which database is underneath.
"""

from urllib.parse import quote_plus

from . import db

# Feature keys we know about, with display labels, in presentation order.
FEATURE_LABELS = {
    "kid_friendly": "Kid-friendly",
    "has_family_room": "Family room",
    "has_nursing_room": "Nursing room",
    "stroller_accessible": "Stroller / step-free",
}


# Every venue in the table is in Vancouver, so this is the whole of what the app
# can plan. Named here rather than repeated as a literal, so anything that
# offers the parent a choice of city offers what the data can actually support.
SUPPORTED_CITIES = ("Vancouver",)

# The venue dict the planners consume. Listed explicitly rather than taking
# whole rows, so a new column on the venues table cannot silently end up in a
# saved trip's plan_json or in the JSON sent to the browser.
VENUE_KEYS = ("name", "type", "neighbourhood",
              "kid_friendly", "has_family_room", "has_nursing_room",
              "stroller_accessible", "nap_friendly", "can_eat", "lat", "lng")

# The venue keys that are yes/no. SQLite has no boolean type and hands these
# back as 0/1, so they are coerced: every venue dict the app has ever built has
# carried real booleans, including the ones already saved into trips.plan_json.
BOOL_KEYS = ("kid_friendly", "has_family_room", "has_nursing_room",
             "stroller_accessible", "nap_friendly", "can_eat")

# Venues with no seed_rank sort after every seeded one. Python's sort is stable
# and get_venues_in_city returns ORDER BY name, so those stay alphabetical.
UNRANKED = 10 ** 6


def maps_url(name, city=SUPPORTED_CITIES[0]):
    """Build a Google Maps search link from a venue name and city."""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(name + ', ' + city)}"


def _as_venue(row):
    """One database row as the venue dict the planners expect.

    `open`/`close` rather than the columns' own open_time/close_time: the
    planner and the trip page have used those names since the data lived in
    JSON, and venue dicts already saved into trips.plan_json carry them too.
    """
    venue = {key: row[key] for key in VENUE_KEYS}
    for key in BOOL_KEYS:
        venue[key] = bool(venue[key])
    venue["open"] = row["open_time"]
    venue["close"] = row["close_time"]
    venue["maps_url"] = maps_url(row["name"])
    return venue


def get_venues(city=""):
    """Every verified venue in `city`, as plain dicts with a maps_url attached.

    Read per call rather than cached, so a venue added to the table shows up in
    the next plan without a restart. `city` is a substring match, and "" means
    every city that has venues.

    Seeded venues come back in the curator's order, not alphabetically: the
    planner picks the first venue that fits a slot, so the order of
    data/venues.json is a ranking of what to offer first.
    """
    rows = db.get_venues_in_city(city)
    rows.sort(key=lambda row: UNRANKED if row["seed_rank"] is None
              else row["seed_rank"])
    return [_as_venue(row) for row in rows]
