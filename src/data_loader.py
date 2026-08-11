"""Load the hardcoded venue data from disk.

Kept deliberately thin so the JSON file can later be swapped for a real
database or an external API without touching the rest of the app.
"""

import json
from pathlib import Path
from urllib.parse import quote_plus

# data/venues.json lives one directory up from src/.
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "venues.json"

# Feature keys we know about, with display labels, in presentation order.
FEATURE_LABELS = {
    "kid_friendly": "Kid-friendly",
    "has_family_room": "Family room",
    "has_nursing_room": "Nursing room",
    "stroller_accessible": "Stroller / step-free",
}
FEATURE_KEYS = tuple(FEATURE_LABELS)


def maps_url(name, city="Vancouver"):
    """Build a Google Maps search link from a venue name and city."""
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(name + ', ' + city)}"


def load_venues():
    """Return the list of venues, each with a generated maps_url attached."""
    with open(DATA_FILE, encoding="utf-8") as f:
        venues = json.load(f)
    for venue in venues:
        venue["maps_url"] = maps_url(venue["name"])
    return venues
