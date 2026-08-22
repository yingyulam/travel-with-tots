"""Log a place we don't have yet, so an admin can verify it into the database.

The chain a parent walks: pin or name a location, name the place, say what it
offers, describe it, submit. Three components, and nothing else sequences them:
`components/geocode.py` states it "never decides what to do with that place",
and `db.add_venue` does not geocode. So `run()` below is the sequencing, and it
lives here rather than in `app.py`, whose own docstring says the real work
belongs in `src/`.

Naming the place is the step that needs the geocoder. A name alone is not
verifiable, and without coordinates a venue can never be distance-ranked, so
`resolve_place` turns "Nourish Kitchen" plus an area into a real address. A
geocoder that is unreachable or unconfigured does not cost the parent their
submission: it stores without coordinates instead.

What this deliberately does *not* do is make the place findable. It is stored as
`source="user_submitted"`, and `db.VERIFIED_SOURCES` covers only "curated" and
"municipal_open_data", so it appears on the parent's own dashboard and in no
search. Promoting it is a human decision, and the admin page for making that
decision does not exist yet: submissions accumulate until it does.
"""

from .. import db
from ..components.geocode import GeocodeError, geocode

# The amenities a parent can vouch for, as (field name, label). Shared by the
# log-a-place page and the dashboard's edit form so the two cannot offer
# different lists. The names match db.add_venue's parameters.
AMENITY_OPTIONS = [
    ("kid_friendly", "Kid-friendly"),
    ("has_family_room", "Family room"),
    ("has_nursing_room", "Nursing room"),
    ("stroller_accessible", "Stroller / step-free"),
]

# "We couldn't work out where this is", in the shape a resolved place has.
UNRESOLVED_PLACE = {"city": "", "neighbourhood": "", "formatted_address": "",
                    "lat": None, "lng": None}


def resolve_place(name, area=""):
    """Coordinates and a confirmable address for a place named by a parent.

    A failure here must not cost them the submission, so an unreachable or
    unconfigured geocoder degrades to the unresolved shape and the caller
    stores what it has.
    """
    query = f"{name}, {area}" if area else name
    try:
        return geocode(query)
    except (GeocodeError, KeyError) as e:
        print(f"Logged-place lookup skipped, storing without coordinates: {e}")
        return dict(UNRESOLVED_PLACE)


def _pinned_place(values):
    """The place the parent pinned, if they pinned one.

    A dropped pin beats geocoding the name, and not as an optimisation: a
    playground or a park building has no address to look up, so its coordinates
    are the only thing that locates it. Returns None when no usable pin came
    through, so the caller falls back to resolving the name.
    """
    try:
        lat = float(values["lat"])
        lng = float(values["lng"])
    except (KeyError, TypeError, ValueError):
        return None
    return {
        "city": (values.get("city") or "").strip(),
        "neighbourhood": (values.get("neighbourhood") or "").strip(),
        "formatted_address": (values.get("address") or "").strip(),
        "lat": lat,
        "lng": lng,
    }


def run(parent_id, values, place=None):
    """Store one parent-submitted place and return the row as stored.

    `values` is form-like: name, venue_type, neighbourhood, notes, any amenity
    keys, and optionally a pinned lat/lng. `place` overrides all of that when a
    caller has already resolved a location.

    Always `source="user_submitted"`. That is what keeps it out of every search
    until an admin verifies it, and it is why nothing here takes `source` as an
    argument.
    """
    name = (values.get("name") or "").strip()
    if not name:
        raise ValueError("a place needs a name")
    area = (values.get("area") or values.get("neighbourhood") or "").strip()
    if place is None:
        place = _pinned_place(values) or resolve_place(name, area)

    record = {
        "name": name,
        "venue_type": (values.get("venue_type") or "").strip() or None,
        # The parent's own area wins over the geocoder's: they know which
        # neighbourhood they mean better than an address lookup does.
        "neighbourhood": area or place["neighbourhood"] or None,
        "city": place["city"] or None,
        "lat": place["lat"],
        "lng": place["lng"],
        "address": place["formatted_address"] or None,
        "notes": (values.get("notes") or "").strip() or None,
        "amenities": [key for key, _ in AMENITY_OPTIONS if values.get(key)],
    }
    record["id"] = db.add_venue(
        record["name"],
        source="user_submitted",
        parent_id=parent_id,
        venue_type=record["venue_type"],
        neighbourhood=record["neighbourhood"],
        city=record["city"],
        lat=record["lat"],
        lng=record["lng"],
        notes=record["notes"],
        address=record["address"],
        **{key: bool(values.get(key)) for key, _ in AMENITY_OPTIONS})
    return record


WORKFLOW = {
    "name": "Log a place we don't have",
    "emoji": "📌",
    "trigger": "event",
    # Endpoint name for the real page, not a separate admin copy: a test
    # surface that exercises what a parent uses cannot drift from it.
    "page": "log_place_page",
    "description": (
        "A parent finds somewhere good that isn't in the venue table, pins it "
        "on a map, names it, and says what it offers. It is geocoded so the "
        "submission is complete enough to check, then held out of every search "
        "until an admin verifies it."
    ),
    "steps": [
        {"component": "User in-trip input", "built": True},
        {"component": "Google Map handoff", "built": True},
        {"component": "Venues DB", "built": True},
    ],
}
