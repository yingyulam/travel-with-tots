"""Read venues out of the database, shaped for the planners.

The venues table is the source of truth; data/venues.json is only its seed
(see db._seed_venues). This module is the boundary between the two: it turns
database rows into the plain dicts the rest of the app expects, so nothing
above it knows which database is underneath.
"""

from datetime import date
from urllib.parse import quote_plus

from . import db
from .dates import day_type_for

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
VENUE_KEYS = ("id", "name", "type", "setting", "neighbourhood", "hours_note",
              "can_eat", "lat", "lng")

# The venue keys that are yes/no. SQLite has no boolean type and hands these
# back as 0/1, so they are coerced: every venue dict the app has ever built has
# carried real booleans, including the ones already saved into trips.plan_json.
BOOL_KEYS = ("can_eat",)

# The kinds of place this app plans days around. A closed list rather than free
# text because `type` is not a label: is_nap_friendly reads it, so a typo
# silently changes which venues can hold a nap.
VENUE_TYPES = ("park", "garden", "beach", "seawall", "playground",
               "mall", "market", "museum", "aquarium", "attraction",
               "community centre", "library", "pool", "farm")

# Where a visit is spent. Shelter, and nothing else -- not nap suitability, not
# calm, not admission, not whether the hours are real. Two readers only: the
# "it's raining" replan path, and a weather forecast if one is ever wired in.
# If anything else starts reading this field, it is being overloaded the way
# `type` was, and the new reader needs its own answer.
#
# The test that decides a value, which an admin can apply without judgement:
#   A. if it rained all day, would you still go, and would the visit work?
#   B. in good weather, is the visit mostly in the open air?
# A only -> indoor.  B only -> outdoor.  both -> "both".
#
# "both" means either mode is a real visit on its own, NOT that some part of
# the venue has a roof. Capilano has a gift shop and a cafe and is still
# plainly outdoor: nobody goes there in the rain to stand in the shop.
SETTINGS = ("indoor", "outdoor", "both")

# Venues with no door for anyone to lock, so their hours are a convention
# rather than a posted fact. importers.PARK_HOURS already assumes exactly this
# when it writes 06:00-22:00 for all 218 City parks; this names the assumption
# once so the rest of the app can share it instead of re-deciding it.
#
# What it is for: a statutory holiday. A default pair is a statement about
# ordinary days, so for a venue with a door it says nothing about Christmas --
# but a seawall is open on Christmas in exactly the sense it is open on a
# Tuesday. Without this the whole database was unschedulable on all 11 BC
# statutory holidays, which produced an empty plan on Canada Day.
#
# `garden` is deliberately absent. All four of ours -- VanDusen, UBC Botanical,
# Sun Yat-Sen, Bloedel -- are gated and ticketed with real posted hours, and a
# botanical garden shuts on Christmas Day like any other paid attraction. So is
# Playland, which is `attraction` and therefore already excluded.
#
# This is `type` driving a hard behaviour, which the model otherwise avoids.
# Accepted because it is the same concept scripts/verify_hours.py already
# encoded privately as SKIP_TYPES (so this is a consolidation, not a new
# dependency), and because it fails in the safe direction: the worst case is
# telling a parent a park is open, which it is.
HOURS_ARE_A_CONVENTION = ("park", "beach", "seawall")

# The settings acceptable when a slot wants shelter, and when it wants open
# air. Two tiers rather than three, deliberately: ranking "both" below an exact
# match measurably drops Grouse Mountain below all 222 imported parks, throwing
# away the curator's seed_rank for a weaker heuristic. Two tiers also confine
# the field's only ambiguity to where it cannot matter -- indoor and "both"
# share a tier, so misjudging one for the other changes nothing, while the
# indoor/outdoor call that does change plans is never the ambiguous one.
SHELTERED = ("indoor", "both")
OPEN_AIR = ("outdoor", "both")


def suits_weather(venue, wet):
    """Whether this venue is a reasonable choice for a wet (or dry) slot.

    Weather only ever pushes towards shelter: rain makes indoors better, dry
    weather does not make outdoors obligatory. So a dry slot accepts anything,
    which is also what an unknown forecast does -- one code path, and the
    reason a forecast can be added later without changing how a day with no
    forecast is planned.
    """
    if not wet:
        return True
    return (venue.get("setting") or "") in SHELTERED


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


def interest_options():
    """The kinds of place a parent can ask for, in VENUE_TYPES order.

    This is the whole of what replaced the theme system, and it is deliberately
    the type list itself rather than a grouping over it. Groups were tried on
    paper and added nothing: because an interest only ever *sorts*, asking for
    "museum" still reaches the aquarium a few places down, so the extra
    vocabulary would only have been another thing to keep in sync with
    VENUE_TYPES -- which is exactly how the themes rotted, with 10 of 14 types
    ending up in no theme at all.

    Only types with venues behind them, because a choice that returns nothing
    is worse than not offering it.
    """
    in_use = db.get_venue_types_in_use()
    return [t for t in VENUE_TYPES if t in in_use]


def maps_url(name, city=SUPPORTED_CITIES[0]):
    """Build a Google Maps search link from a venue name and city."""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(name + ', ' + city)}"


def _as_venue(row, reported=None, day_type="weekday"):
    """One database row as the venue dict the planners expect.

    `open`/`close` rather than the columns' own open_time/close_time: the
    planner and the trip page have used those names since the data lived in
    JSON, and venue dicts already saved into trips.plan_json carry them too.
    """
    venue = {key: row[key] for key in VENUE_KEYS}
    for key in BOOL_KEYS:
        venue[key] = bool(venue[key])
    # Amenities come only from venue_reports, so a field nobody has reported on
    # is **absent** from this dict rather than False. That is the distinction
    # the whole reports table exists for, and it was not true until the venues
    # columns went: they were the base layer here, and being NOT NULL DEFAULT 0
    # they made every venue assert the absence of every unexamined amenity.
    #
    # Read them with .get(). An absent key is not the same as "no".
    venue.update(reported or {})
    venue["nap_friendly"] = is_nap_friendly(venue)
    venue.update(_hours_for(row, day_type))
    venue["maps_url"] = maps_url(row["name"])
    return venue


UNKNOWN_HOURS = {"open": None, "close": None}


def _hours_for(row, day_type):
    """A venue's open/close for a kind of day, and where they came from.

    Three outcomes:

    - **no pair at all**: unknown. Not knowing is a reason to leave a place
      out, never to include it.
    - **an ordinary day**: the default pair. That pair is what the venue says
      its hours are, and an ordinary day is what it means.
    - **a holiday**: it depends on whether there is a door. For a venue with
      one, a default pair says nothing about Christmas -- most attractions keep
      different hours or shut altogether, so carrying the pair over would be
      inventing an answer. For a park, a beach or a seawall there is nothing to
      lock, and it is open on Christmas in the same sense it is open on a
      Tuesday. See HOURS_ARE_A_CONVENTION.

    That distinction is the whole fix. Refusing every venue on a holiday made
    the app useless on 11 days a year: Canada Day produced a plan with zero
    stops, while 222 imported parks sat there open.

    There is deliberately no season or weekday dimension. A `venue_hours` table
    keyed on (season, day_type) existed for exactly that and never held a single
    row, and it could not express what the real data turned out to contain --
    a museum closed on Mondays from September, a mountain with its own Christmas
    Eve hours. What a single pair cannot hold now goes in `hours_note`, in words
    a parent reads.
    """
    if not row["open_time"] or not row["close_time"]:
        return {**UNKNOWN_HOURS, "hours_source": "missing"}
    if day_type == "holiday" and (row["type"] or "") not in HOURS_ARE_A_CONVENTION:
        return {**UNKNOWN_HOURS, "hours_source": "holiday_unknown"}
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
    # One query for the whole set, not one per venue.
    reported = db.reported_flags(ids)
    day_type = day_type_for(on_date or date.today())
    return [_as_venue(row, reported.get(row["id"]), day_type) for row in rows]
