"""Log a place we don't have yet, so an admin can verify it into the database.

The chain a parent walks: pin or name a location, name the place, say what it
offers, describe it, submit. Three components, and nothing else sequences them:
`components/geocode.py` states it "never decides what to do with that place",
and `db.add_venue` does not geocode. So `store()` below is the sequencing, and it
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

from .. import db, interactions
from ..components.geocode import GeocodeError, geocode
from ..intent import matches_only

# Read from db, not defined here. The Log a Place page, the dashboard's edit
# form, this workflow and the chat agent's tool all offer the same list, and
# production must not import a vocabulary out of the demo layer.
AMENITY_OPTIONS = db.AMENITY_OPTIONS

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


def store(parent_id, values, place=None):
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
    # add_or_update_submission rather than add_venue: logging the same place
    # twice is the parent correcting it, not a second place, so their earlier
    # submission is replaced instead of the review queue growing a duplicate.
    record["id"] = db.add_or_update_submission(
        record["name"],
        parent_id=parent_id,
        type=record["venue_type"],
        neighbourhood=record["neighbourhood"],
        city=record["city"],
        lat=record["lat"],
        lng=record["lng"],
        notes=record["notes"],
        address=record["address"])
    # The amenities go in as reports by this parent, not as columns on the row.
    # They were standing in the building; that is exactly the author a claim
    # wants, and storing it as a column recorded it as a claim by nobody. One
    # real submission ended up with reported_by=None and the note "Hand-typed
    # into the seed file; never verified" about boxes a parent had ticked.
    #
    # Every option is passed, not only the ticked ones, so unticking is a real
    # report of absence rather than silence.
    db.record_amenities(
        record["id"], {key: bool(values.get(key)) for key, _ in AMENITY_OPTIONS},
        reported_by=parent_id, note="Reported when logging the place.")
    return record


# ---------------------------------------------------------------------------
# The chat conversation. `store` above is the same function it always was, and
# both entry points end there: the Log a Place form posts to it, and this walks
# a parent through the same fields a message at a time.

STAGE_NAME = "name"
STAGE_AMENITIES = "amenities"
STAGE_NOTES = "notes"
STAGE_CONFIRMING = "confirming"

NAME_QUESTION = ("Happy to log it. What's the place called? Add the area after "
                 "a comma if the name alone wouldn't find it, like \"Nourish "
                 "Kitchen, Gastown\".")
AMENITY_QUESTION = interactions.AMENITY_QUESTION
NOTES_QUESTION = "Anything else worth knowing about it?"

DONE_CHOICE = "✓ Done"
NONE_CHOICE = "None of these"
NOTHING_CHOICE = "No, that's everything"
CONFIRM_CHOICE = "📌 Looks right"

# Nothing to add. Shared shape with the planning chat, which reads its own
# declines the same way.
_NOTHING = ("no", "nope", "nothing", "none", "nothing else", "no thanks",
            "that's everything", "thats everything", "that's all", "thats all",
            "that's it", "thats it", "all good", "none of these", "done")

_YES = ("yes", "yep", "yeah", "ok", "okay", "sure", "correct", "looks right",
        "log it", "go ahead", "do it", "perfect", "great")


def read_amenities(message: str) -> list:
    """Every amenity the message mentions, not the first.

    A place is several things at once: a mall with a family room usually has a
    nursing room too. So this collects, where find_nearby_place.read_need
    picks. The four labels are the vocabulary, which is also what the chips
    send, so a typed answer and a tapped one are read identically.
    """
    said = message.lower()
    return [key for key, label in AMENITY_OPTIONS if label.lower() in said]


def split_name(message: str) -> tuple:
    """A typed answer into (name, area), on the last comma.

    "Nourish Kitchen, Gastown" is how people write this, and resolve_place
    joins the two back with a comma anyway. No comma means the whole thing is
    the name, which geocodes just as well.
    """
    name, _, area = message.strip().rpartition(",")
    return (name.strip(), area.strip()) if name else (area.strip(), "")


def _ask(stage: str, values: dict, reply: str, **extra) -> dict:
    return {"reply": reply, "state": {"stage": stage, "values": values}, **extra}


def _summarise(values: dict) -> str:
    """Everything collected, so nothing is submitted that was not seen."""
    lines = [f"- name: {values['name']}"]
    if values.get("neighbourhood"):
        lines.append(f"- area: {values['neighbourhood']}")
    picked = [label for key, label in AMENITY_OPTIONS if values.get(key)]
    lines.append(f"- offers: {', '.join(picked) if picked else '(none said)'}")
    if values.get("notes"):
        lines.append(f"- notes: {values['notes']}")
    return ("Here's what I have:\n\n" + "\n".join(lines)
            + "\n\nLog it, or open the form to check the map pin first.")


def _confirm(values: dict) -> dict:
    return {"reply": _summarise(values),
            "state": {"stage": STAGE_CONFIRMING, "values": values},
            "choices": [CONFIRM_CHOICE]}


def run(message: str, state: dict | None = None,
        context: dict | None = None) -> dict:
    """One turn of the logging conversation.

    Collects what the Log a Place form collects, then hands the values to that
    page rather than writing them. The chat has no parent to attach a
    submission to, and no way to drop a map pin; a form post has both.

    `context` is part of the contract and unused here: where the parent is
    standing is not where the place is.
    """
    stage = (state or {}).get("stage")
    values = dict((state or {}).get("values") or {})

    if stage is None:
        # Deliberately does not read the opening message, unlike the planning
        # chat. That one can, because an extractor decides what is a
        # destination and what is noise. Here the reader is split_name, a comma
        # split, which cannot tell a place name from a sentence about wanting
        # to log one: "I want to log a place" would be stored as a venue called
        # "I want to log a place". One extra question is cheaper than a junk row.
        return _ask(STAGE_NAME, values, NAME_QUESTION)

    if stage == STAGE_NAME:
        name, area = split_name(message)
        if not name:
            return _ask(STAGE_NAME, values, NAME_QUESTION)
        values["name"] = name
        if area:
            values["neighbourhood"] = area
        return _ask(STAGE_AMENITIES, values, AMENITY_QUESTION,
                    choices=[label for _, label in AMENITY_OPTIONS],
                    choose_many=True)

    if stage == STAGE_AMENITIES:
        if not matches_only(message, _NOTHING):
            for key in read_amenities(message):
                values[key] = True
        return _ask(STAGE_NOTES, values, NOTES_QUESTION,
                    choices=[NOTHING_CHOICE])

    if stage == STAGE_NOTES:
        if not matches_only(message, _NOTHING):
            values["notes"] = message.strip()
        return _confirm(values)

    if matches_only(message, _YES):
        return {
            "reply": ("Ready. Log it straight away, or open the form to check "
                      "the map pin first."),
            "state": None,
            "place_form": values,
        }
    # Anything else at the confirmation is a correction to the notes, which is
    # the only free-text field left to correct.
    values["notes"] = message.strip()
    return _confirm(values)


WORKFLOW = {
    "name": "Log a place we don't have",
    "emoji": "📌",
    "trigger": "message",
    # This workflow's own watch page. The real page a parent uses is
    # /log-place, which the nav links to and which this hands off to.
    "page": "devpages.log_place_from_chat_page",
    "description": (
        "A parent tells the chat about somewhere good that isn't in the venue "
        "table, naming it and saying what it offers, and the assistant hands "
        "the filled submission to the Log a Place page. It is geocoded so the "
        "submission is complete enough to check, then held out of every search "
        "until an admin verifies it."
    ),
    "steps": [
        {"component": "User in-trip input", "built": True},
        {"component": "Google Map handoff", "built": True},
        {"component": "Venues DB", "built": True},
    ],
}
