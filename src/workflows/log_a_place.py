"""Log a place we don't have yet, so an admin can verify it into the database.

The chain a parent walks: set a location (share it or type an address), name the
place, say what it offers, and submit. `app.py`'s `_log_place` stores it and
`templates/log_a_place.html` shows exactly what was stored.

Naming the place is the step that needs the geocoder. A name on its own is not
verifiable, and without coordinates a venue can never be distance-ranked, so
`_resolve_place` turns "Nourish Kitchen" plus an area into a real address the
parent confirms. A geocoder that is unreachable or unconfigured does not cost
them the submission: it stores without coordinates instead.

What this deliberately does *not* do is make the place findable. It is stored as
`source="user_submitted"`, and `db.VERIFIED_SOURCES` covers only "curated" and
"municipal_open_data", so it appears on the parent's own dashboard and in no
search. Promoting it is a human decision, and the admin page for making that
decision does not exist yet: submissions accumulate until it does.

Replaces an earlier "Find a nearby place" card, which turned out not to be a
workflow at all. Its whole chain (curated venues, else a web search) already
lives inside `src/components/find_nearby.py`, so declaring it here would have
described sequencing that no code performs.
"""

WORKFLOW = {
    "name": "Log a place we don't have",
    "emoji": "📌",
    "trigger": "event",
    # Endpoint name for its test page, so /workflows can offer a "Try it" link.
    "page": "log_a_place_page",
    "description": (
        "A parent finds somewhere good that isn't in the venue table, sets a "
        "location, names it, and says what it offers. It is geocoded so the "
        "submission is complete enough to check, then held out of every search "
        "until an admin verifies it."
    ),
    "steps": [
        {"component": "User in-trip input", "built": True},
        {"component": "Google Map handoff", "built": True},
        {"component": "Venues DB", "built": True},
    ],
}
