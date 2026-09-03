"""Turn Vancouver Open Data records into venue rows.

Split from src/opendata.py on purpose: that module knows how to talk to the
City, this one knows what a venue is. So every mapping decision below can be
tested against one captured response body, with no network and no database.

The three-tier rule this sits inside: the City publishes what the City owns, so
parks and community centres need no review. Museums, aquariums and private
attractions are not in any municipal dataset and never will be, which is why
src/candidates.py and its review queue are not made redundant by this file.
"""

from .store import db
from .clients import opendata
from .data_loader import NEIGHBOURHOODS

# The City writes one local area with a hyphen where our enum has a space. The
# only disagreement in 22 areas, measured against both datasets, so an alias
# map beats loosening the enum: a second mismatch should fail loudly rather
# than quietly write a neighbourhood nothing else in the app uses.
NEIGHBOURHOOD_ALIASES = {"Arbutus-Ridge": "Arbutus Ridge"}

# Where the City and the curator name the same park differently. Without this
# the import inserts a second copy of each and the curator's row -- the one
# with seed_rank and the hours somebody chose -- is the one the planner stops
# reaching. The entry is renamed to the curated spelling so the exact-name
# match in db.upsert_imported_venue hits; name is not an import field, so the
# stored name is the curator's either way.
#
# Two rows, both checked by hand, and deliberately not a fuzzy rule. The 11
# seeded parks were measured against all 218 City records: 5 match outright,
# these 2 are the same place under another name, 3 are not City parks at all
# (Lynn Canyon is North Vancouver's, UBC Botanical Garden is UBC's, Second
# Beach has no record of its own), and Stanley Park Seawall is a part of
# Stanley Park rather than the same thing, so the City's Stanley Park lands as
# its own venue. A normalized or prefix match would have to accept "English Bay
# Beach" as "English Bay Beach Park" and would then also accept a park as its
# own extension, which is a wrong merge nobody would notice.
CURATED_ALIASES = {
    "John Hendry (Trout Lake) Park": "Trout Lake (John Hendry Park)",
    "English Bay Beach Park": "English Bay Beach",
}

# A park has no door, and the parks dataset publishes no hours because there
# are none to publish. Rather than leave 218 parks unschedulable, they get the
# same pair the curator chose for 9 of the 11 parks already in the table:
# usable daylight, stopping well before a toddler's bedtime. This is the one
# value in this file that is a judgment rather than a field from the City, so
# it is named here and reported by the import script as an assumption.
PARK_HOURS = ("06:00", "22:00")

# Community centres get no default. They are buildings with staff and real
# varying hours, and the City publishes the address and the coordinates but not
# the hours, so any pair invented here would be a guess about whether a family
# can get in. They import without hours, stay out of the planner, and appear in
# db.get_venues_missing_hours until somebody reads the centre's page.
CENTRE_HOURS = (None, None)

# Namespaced so an external_id can never collide with another source's.
ID_PREFIX = "vanopendata"

# What these rows are labelled with. Already in VERIFIED_SOURCES, so an
# imported venue is plannable the moment it lands -- which is the three-tier
# rule in one line: trust comes from the source here, not from a reviewer.
SOURCE = "municipal_open_data"
assert SOURCE in db.VERIFIED_SOURCES

INSERTED, UPGRADED, UNCHANGED = "inserted", "upgraded", "unchanged"


def _neighbourhood(value):
    """A City local area as our enum spells it, or None.

    None rather than the raw string when it is unrecognised: neighbourhood is
    how get_candidate_venues keeps a day's stops near each other, so a value
    outside the enum silently forms a cluster of one.
    """
    value = (value or "").strip()
    value = NEIGHBOURHOOD_ALIASES.get(value, value)
    return value if value in NEIGHBOURHOODS else None


def _slug(text):
    return "-".join((text or "").lower().split())


def park_entry(record):
    """One `parks` record as arguments for db.upsert_imported_venue.

    `googlemapdest` rather than a centroid: it is the point the City itself
    would send you to, which for a large park is an entrance rather than the
    middle of the trees.
    """
    point = record.get("googlemapdest") or {}
    address = " ".join(str(part) for part in
                       (record.get("streetnumber"), record.get("streetname"))
                       if part)
    opens, closes = PARK_HOURS
    name = record["name"]
    return {
        "external_id": f"{ID_PREFIX}:{opendata.PARKS}/{record['parkid']}",
        "name": CURATED_ALIASES.get(name, name),
        "source_url": opendata.record_url(opendata.PARKS, "parkid",
                                          record["parkid"]),
        # A park is open air by definition; the City publishes nothing that
        # would make one anything else.
        "fields": {"type": "park", "setting": "outdoor",
                   "neighbourhood": _neighbourhood(record.get("neighbourhoodname")),
                   "city": "Vancouver",
                   "address": address or None,
                   "lat": point.get("lat"), "lng": point.get("lon"),
                   "open_time": opens, "close_time": closes,
                   "can_eat": 0},
        # The City's own answer for this park, kept separate from `fields`
        # because it becomes a report rather than a column. See seed_washroom.
        "washroom": {"Y": True, "N": False}.get(record.get("washrooms")),
    }


def centre_entry(record):
    """One `community-centres` record as arguments for db.upsert_imported_venue."""
    point = record.get("geo_point_2d") or {}
    opens, closes = CENTRE_HOURS
    return {
        "external_id": f"{ID_PREFIX}:{opendata.COMMUNITY_CENTRES}/{_slug(record['name'])}",
        "name": f"{record['name']} Community Centre",
        "source_url": opendata.record_url(opendata.COMMUNITY_CENTRES, "name",
                                          record["name"]),
        # A visit to a community centre is the building: the gym, the pool,
        # the drop-in room. Several have a playground outside, but that is not
        # what the visit is, so "indoor" rather than "both".
        "fields": {"type": "community centre", "setting": "indoor",
                   "neighbourhood": _neighbourhood(record.get("geo_local_area")),
                   "city": "Vancouver",
                   "address": record.get("address") or None,
                   "lat": point.get("lat"), "lng": point.get("lon"),
                   "open_time": opens, "close_time": closes,
                   "can_eat": 0},
        # The dataset names 134 washroom locations after a park or a centre, so
        # the join below answers this for centres too.
        "washroom": None,
    }


def washroom_places(washroom_records):
    """The place names the public-washrooms dataset has a facility for.

    `park_name` is an exact join key against both datasets' `name`, which is
    better than any radius: 100 of the 134 named rows match a park outright and
    most of the rest name a community centre. Geometry was the obvious approach
    and the wrong one, because a park's coordinate is a single point and
    Stanley Park is 400 hectares.
    """
    return {(record.get("park_name") or "").strip()
            for record in washroom_records if record.get("park_name")}


def resolved_washroom(entry, washroom_names):
    """What has_washroom will end up saying about this venue, or None if
    neither dataset mentions it.

    Shared with the dry run, so the counts it prints are the counts the write
    produces. Without this the preview reported only the parks dataset's own
    Y/N and understated washrooms by the 9 parks the two datasets disagree on.
    """
    plain_name = entry["name"].removesuffix(" Community Centre")
    if plain_name in washroom_names:
        return True
    return entry["washroom"]


def seed_washroom(venue_id, entry, washroom_names):
    """Record what the City says about a washroom at this venue, as a report.

    A column would be the wrong home twice over: the dataset publishes separate
    summer and winter hours, which is the City admitting these close
    seasonally, and a parent who was there last week has to be able to
    disagree. As a report with no author it is visibly the weakest kind of
    claim, and one real report supersedes it (db.reported_flags).

    Both answers are written, including "no washroom here". That is the whole
    point of the reports table: "the City says there is none" and "nobody has
    said" stop being the same value.

    Two claims, resolved by insert order. The parks dataset's own Y/N goes in
    first and the washrooms dataset's point second, so where the City
    contradicts itself -- 9 parks flagged N that have a facility named after
    them -- the more specific record wins.
    """
    if entry["washroom"] is not None:
        db.add_report(venue_id, "has_washroom", entry["washroom"], None,
                      "City parks dataset, not verified on the ground.")
    if entry["name"].removesuffix(" Community Centre") in washroom_names:
        db.add_report(venue_id, "has_washroom", True, None,
                      "City public-washrooms dataset lists one here.")
    return resolved_washroom(entry, washroom_names)


def classify(entry, existing_rows):
    """What upsert_imported_venue will do with this entry, without writing.

    Mirrors its two-step match so a dry run reports the real answer rather than
    a guess, which is what makes "would this duplicate the 11 seeded parks?"
    answerable before touching the database.
    """
    for row in existing_rows:
        if row["external_id"] == entry["external_id"]:
            return UNCHANGED
    for row in existing_rows:
        if row["name"] == entry["name"] and row["source"] == "curated":
            return UPGRADED
    return INSERTED


def store(entry, washroom_names, source=SOURCE):
    """Write one entry and its washroom report. Returns (action, washroom)."""
    venue_id, action = db.upsert_imported_venue(
        entry["external_id"], entry["name"],
        source=source, source_url=entry["source_url"], **entry["fields"])
    return action, seed_washroom(venue_id, entry, washroom_names)
