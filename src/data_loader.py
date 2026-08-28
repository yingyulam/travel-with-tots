"""Read venues out of the database, shaped for the planners.

The venues table is the source of truth; data/venues.json is only its seed
(see db._seed_venues). This module is the boundary between the two: it turns
database rows into the plain dicts the rest of the app expects, so nothing
above it knows which database is underneath.
"""

from datetime import date
from urllib.parse import quote_plus

from . import db
from .dates import day_type_for, season_for

# Feature keys we know about, with display labels, in presentation order.
FEATURE_LABELS = {
    "has_washroom": "Washroom",
    "has_family_room": "Family room",
    "has_nursing_room": "Nursing room",
    "stroller_accessible": "Stroller / step-free",
    "has_highchair": "Highchair",
    "can_eat": "Food on site",
}


# Every venue in the table is in Vancouver, so this is the whole of what the app
# can plan. Named here rather than repeated as a literal, so anything that
# offers the parent a choice of city offers what the data can actually support.
SUPPORTED_CITIES = ("Vancouver",)

# The venue dict the planners consume. Listed explicitly rather than taking
# whole rows, so a new column on the venues table cannot silently end up in a
# saved trip's plan_json or in the JSON sent to the browser.
# `id` is the one database-internal column here, and it earns its place: a
# parent reporting a change table has to be able to name which venue, and a name
# is not a stable identity. The rest stay out, so a new column cannot silently
# end up in a saved trip's plan_json or in the JSON sent to the browser.
VENUE_KEYS = ("id", "name", "type", "neighbourhood",
              "has_washroom", "has_family_room", "has_nursing_room",
              "stroller_accessible", "has_highchair", "can_eat", "lat", "lng")

# The venue keys that are yes/no. SQLite has no boolean type and hands these
# back as 0/1, so they are coerced: every venue dict the app has ever built has
# carried real booleans, including the ones already saved into trips.plan_json.
BOOL_KEYS = ("has_washroom", "has_family_room", "has_nursing_room",
             "stroller_accessible", "has_highchair", "can_eat")

# The kinds of place this app plans days around. A closed list rather than free
# text because `type` is not a label: is_nap_friendly reads it, so a typo
# silently changes which venues can hold a nap.
VENUE_TYPES = ("park", "garden", "beach", "seawall", "playground",
               "mall", "market", "museum", "aquarium", "attraction",
               "community centre", "library", "pool", "farm")

# Vancouver's 22 local areas as the City publishes them, plus the informal areas
# people actually use for a venue's location and the neighbouring
# municipalities. A closed list so a reviewer picks rather than types, which is
# what stops "Central Vancouver" and "Downtown Vancouver" becoming two places.
NEIGHBOURHOODS = (
    "Arbutus Ridge", "Downtown", "Dunbar-Southlands", "Fairview",
    "Grandview-Woodland", "Hastings-Sunrise", "Kensington-Cedar Cottage",
    "Kerrisdale", "Killarney", "Kitsilano", "Marpole", "Mount Pleasant",
    "Oakridge", "Renfrew-Collingwood", "Riley Park", "Shaughnessy",
    "South Cambie", "Strathcona", "Sunset", "Victoria-Fraserview",
    "West End", "West Point Grey",
    # Informal, but how a parent and every listing names them.
    "Chinatown", "False Creek", "Gastown", "Granville Island", "Point Grey",
    "Stanley Park", "UBC", "Yaletown",
    # Outside the City, still a day out from it.
    "Burnaby", "North Vancouver", "Richmond", "West Vancouver")

CITIES = ("Vancouver", "North Vancouver", "West Vancouver", "Burnaby",
          "Richmond")

# Somewhere a stroller nap works: open space to keep walking, or a mall to walk
# indoors when it rains. Derived from the kind of place rather than stored,
# because it is not a judgment anyone should have to make twice, and because
# the stored version was a coin-flip: of the venues once marked nap-friendly,
# all but one were a park or a mall.
NAP_FRIENDLY_TYPES = ("park", "garden", "beach", "seawall", "mall")


def is_nap_friendly(venue):
    """Whether a parent could push a sleeping child around here for 45 minutes
    without needing to engage with the place or pay to get in.

    Takes a venue dict or a sqlite3.Row: the AI adjuster checks candidates
    straight off the database, and Row has no .get().
    """
    try:
        venue_type = venue["type"]
    except (KeyError, IndexError):
        return False
    return (venue_type or "").strip().lower() in NAP_FRIENDLY_TYPES

# Venues with no seed_rank sort after every seeded one. Python's sort is stable
# and get_venues_in_city returns ORDER BY name, so those stay alphabetical.
UNRANKED = 10 ** 6


def maps_url(name, city=SUPPORTED_CITIES[0]):
    """Build a Google Maps search link from a venue name and city."""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(name + ', ' + city)}"


def _as_venue(row, reported=None, hours=None, slot=None):
    """One database row as the venue dict the planners expect.

    `open`/`close` rather than the columns' own open_time/close_time: the
    planner and the trip page have used those names since the data lived in
    JSON, and venue dicts already saved into trips.plan_json carry them too.
    """
    venue = {key: row[key] for key in VENUE_KEYS}
    for key in BOOL_KEYS:
        venue[key] = bool(venue[key])
    # An amenity is whatever the newest report says, not what the column says.
    # The columns are still written (by the review queue, by an import seed) but
    # nothing reads them for this: a claim needs an author and a date.
    venue.update(reported or {})
    venue["nap_friendly"] = is_nap_friendly(venue)
    venue.update(_hours_for_slot(row, hours, slot))
    venue["maps_url"] = maps_url(row["name"])
    return venue


def _hours_for_slot(row, hours, slot):
    """A venue's open/close for one (season, day_type), and where they came from.

    Three outcomes, and the third is the point:

    - a row for this exact slot: use it, it was entered for this kind of day.
    - no slot, an ordinary day: use the venue's default pair. That pair is what
      the venue says its hours are, and an ordinary day is what it means.
    - no slot, a **holiday**: unknown. A default pair is a statement about
      ordinary days, and most attractions keep different hours on a holiday or
      shut altogether, so carrying the weekday pair over would be inventing an
      answer. Unknown hours mean the venue cannot be scheduled, not that it is
      open all day.
    """
    if hours and slot in hours:
        opens, closes = hours[slot]
        return {"open": opens, "close": closes, "hours_source": "slot"}
    season, day_type = slot
    if day_type == "holiday":
        return {"open": None, "close": None, "hours_source": "holiday_unknown"}
    if not row["open_time"] or not row["close_time"]:
        return {"open": None, "close": None, "hours_source": "missing"}
    return {"open": row["open_time"], "close": row["close_time"],
            "hours_source": "default"}


def get_venues(city="", on_date=None):
    """Every verified venue in `city`, as plain dicts with a maps_url attached.

    Read per call rather than cached, so a venue added to the table shows up in
    the next plan without a restart. `city` is a substring match, and "" means
    every city that has venues.

    Seeded venues come back in the curator's order, not alphabetically: the
    planner picks the first venue that fits a slot, so the order of
    data/venues.json is a ranking of what to offer first.

    `on_date` is the day being planned, defaulting to today. A venue's `open`
    and `close` are resolved for that date, so a museum that shuts at four in
    the winter is not offered a five o'clock slot in January. Every caller stays
    date-unaware: itinerary.venue_open_for reads the same two keys it always did.

    Amenities come from venue_reports, so a field nobody has reported on is
    absent rather than False. That is the difference between "nobody has said"
    and "somebody looked and there was none", and it is why the planner no
    longer filters on them.
    """
    rows = db.get_venues_in_city(city)
    rows.sort(key=lambda row: UNRANKED if row["seed_rank"] is None
              else row["seed_rank"])
    ids = [row["id"] for row in rows]
    # One query each for the whole set, not one per venue.
    reported = db.reported_flags(ids)
    hours = db.venue_hours_by_slot(ids)
    on_date = on_date or date.today()
    slot = (season_for(on_date), day_type_for(on_date))
    return [_as_venue(row, reported.get(row["id"]), hours.get(row["id"]), slot)
            for row in rows]
