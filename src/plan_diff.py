"""What would change, in a parent's words, if they accepted a replan.

A replan is a proposal now, not something that happens to the day. Showing the
new timeline beside the old one is not enough on its own: the two differ in a
handful of places and the eye has to find them, at the exact moment somebody is
standing outside a shut aquarium with a toddler. So the difference is computed
and said outright.

Deliberately here rather than in the page. It is the kind of logic that gets a
pairing wrong at an edge -- a venue kept but retimed, a swap that looks like a
drop and an add -- and this is the only place in the project where such logic
can be tested without a browser.
"""

from .itinerary import display_to_min

# What a stop with no venue is called. Lunch is the common one: it is a block
# with a handoff rather than a place, and it can still move or disappear, which
# is a change the parent needs told.
KIND_LABELS = {"meal": "lunch", "leave": "setting off", "nap": "the nap stop"}


def _label(stop):
    venue = stop.get("venue")
    if venue and venue.get("name"):
        return venue["name"]
    return KIND_LABELS.get(stop.get("kind"), stop.get("kind") or "a stop")


def _minutes(stop):
    """A stop's time in minutes, or None when it cannot be read.

    None rather than zero: an unreadable time sorts last instead of pretending
    to be midnight, and a change is still reported rather than being dropped
    for having a malformed clock.
    """
    try:
        return display_to_min(stop.get("time") or "")
    except (ValueError, TypeError):
        return None


def _at(stop):
    return stop.get("time") or "an unknown time"


def describe_changes(before, after):
    """The differences between two versions of a day, in time order.

    Each entry is ``{"kind", "text"}``. ``kind`` is one of "swapped",
    "dropped", "added" or "moved", so the page can style them; ``text`` is the
    sentence a parent reads. An empty list means the two days are the same,
    which is a real outcome -- the adjuster often agrees with the draft -- and
    one the page has to be able to say.

    Stops are matched by what they are: a venue by name, a venue-less block by
    its kind. Not by position, because a dropped stop shifts every stop after
    it and would report the whole afternoon as changed.
    """
    before_by = {_label(s): s for s in before or []}
    after_by = {_label(s): s for s in after or []}

    gone = [n for n in before_by if n not in after_by]
    fresh = [n for n in after_by if n not in before_by]

    # A replacement is one change, not two. Pairing on the time slot is what
    # makes "Science World replaces the aquarium at 12:45" possible; without
    # it the parent reads a drop and an add and has to work out they are the
    # same decision.
    changes = []
    for name in list(gone):
        slot = _minutes(before_by[name])
        match = next((n for n in fresh
                      if slot is not None and _minutes(after_by[n]) == slot), None)
        if match is None:
            continue
        gone.remove(name)
        fresh.remove(match)
        changes.append({"kind": "swapped", "at": slot,
                        "text": f"{match} replaces {name} at {_at(after_by[match])}"})

    for name in gone:
        changes.append({"kind": "dropped", "at": _minutes(before_by[name]),
                        "text": f"{name} is dropped from {_at(before_by[name])}"})

    for name in fresh:
        changes.append({"kind": "added", "at": _minutes(after_by[name]),
                        "text": f"{name} is added at {_at(after_by[name])}"})

    for name, stop in after_by.items():
        was = before_by.get(name)
        if was is None or was.get("time") == stop.get("time"):
            continue
        changes.append({"kind": "moved", "at": _minutes(stop),
                        "text": f"{name} moves from {_at(was)} to {_at(stop)}"})

    # In the order the day happens, so the list reads as a day rather than as a
    # report grouped by our own categories. An unreadable time goes last.
    changes.sort(key=lambda c: (c["at"] is None, c["at"] or 0))
    return [{"kind": c["kind"], "text": c["text"]} for c in changes]


def summarise(changes):
    """One line above the list, for a parent deciding whether to read it."""
    if not changes:
        return "Nothing would change: this is already the best we can do."
    if len(changes) == 1:
        return "One change to the rest of your day:"
    return f"{len(changes)} changes to the rest of your day:"
