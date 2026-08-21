"""Fill in lat/lng on data/venues.json from open data. One-time, re-runnable.

Deliberately conservative: a wrong coordinate is worse than a missing one,
because it silently mis-ranks distance results instead of falling back to
neighbourhood matching. So each venue is looked up in the source that is
actually authoritative for its kind, matched on a near-exact name, and left
null (and reported) whenever the answer is uncertain.

    python3 scripts/geocode_venues.py            # dry run, prints a report
    python3 scripts/geocode_venues.py --write    # also updates venues.json

Sources, none of which need an API key:
  parks             city parks and beaches, coordinates in `googlemapdest`
  business-licences restaurants and cafes, coordinates in `geo_point_2d`
  Nominatim         landmarks, museums, malls -- what free-text geocoding is
                    genuinely good at, and the only source covering venues
                    outside the City of Vancouver (North Vancouver)
"""

import json
import re
import sys
import time
from pathlib import Path

import requests

VENUES_FILE = Path(__file__).resolve().parent.parent / "data" / "venues.json"
OPENDATA = "https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "travel-with-tots/1.0 (one-time venue geocoding)"
# Nominatim's usage policy asks for at most one request per second.
NOMINATIM_DELAY_SECONDS = 1.1
# Metro Vancouver, generously drawn: a last-resort guard so no source can slip
# in a coordinate on the wrong continent. (south, north, west, east)
METRO_VANCOUVER_BOUNDS = (48.9, 49.6, -123.5, -122.5)

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def _normalize(text):
    """Lowercase, drop punctuation and parentheticals, collapse whitespace, so
    "Sophie's Cosmic Cafe" and "Sophies Cosmic Cafe" compare equal."""
    text = re.sub(r"\s*\(.*?\)", " ", text or "")
    text = re.sub(r"[^a-z0-9 ]", "", text.lower())
    return " ".join(text.split())


def _brand_terms(name):
    """Progressively shorter search terms for a venue name, longest first.

    A chain venue's name often ends in its location ("White Spot English
    Bay", "Cactus Club Cafe English Bay") while the licence carries the brand
    alone, so searching the full name finds nothing. Searching broadly is
    safe here because _names_match still has to accept the result and the
    caller still requires the neighbourhood to agree."""
    tokens = _normalize(name).split()
    seen = []
    for size in range(len(tokens), 0, -1):
        term = " ".join(tokens[:size])
        if term not in seen:
            seen.append(term)
    return seen


def _names_match(a, b):
    """True when two normalized names refer to the same place: equal, or one
    is a whole-token prefix of the other ("white spot" vs "white spot
    restaurant"). A single shared token only counts when it is distinctive
    enough to stand alone, which is what stops "Vancouver" from matching
    "Vancouver Maritime Museum"."""
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((a, b), key=len)
    if not longer.startswith(shorter + " "):
        return False
    return len(shorter.split()) >= 2 or len(shorter) >= 6


def _opendata(dataset, where, limit=20, order_by=None):
    params = {"limit": limit, "where": where}
    if order_by:
        params["order_by"] = order_by
    response = session.get(f"{OPENDATA}/{dataset}/records", params=params, timeout=25)
    response.raise_for_status()
    return response.json().get("results", [])


def _park_aliases(name):
    """A park may be listed under either half of a name like "Trout Lake
    (John Hendry Park)", so try the parenthetical alias too."""
    aliases = [_normalize(name)]
    alias = re.search(r"\((.*?)\)", name)
    if alias:
        aliases.append(_normalize(alias.group(1)))
    return aliases


def from_parks(venue):
    """City parks dataset: authoritative for parks and beaches."""
    for target in _park_aliases(venue["name"]):
        for record in _opendata("parks", f'search(name,"{target}")'):
            if not _names_match(_normalize(record.get("name")), target):
                continue
            point = record.get("googlemapdest") or {}
            if point.get("lat") is not None:
                return (point["lat"], point["lon"],
                        record.get("neighbourhoodname"), "parks")
    return None


def from_licences(venue):
    """Business licences: the only open source covering independent
    restaurants. Requires the licence's own `localarea` to agree with the
    venue's neighbourhood, which is what picks the right branch of a chain.
    Without that agreement it refuses rather than guessing a branch."""
    for term in _brand_terms(venue["name"]):
        records = _opendata(
            "business-licences",
            f'search(businesstradename,"{term}") AND status="Issued"',
            limit=50, order_by="-folderyear")
        in_area = [
            r for r in records
            if _names_match(_normalize(r.get("businesstradename")), term)
            and r.get("localarea") == venue["neighbourhood"]
            and (r.get("geo_point_2d") or {}).get("lat") is not None
        ]
        if in_area:
            point = in_area[0]["geo_point_2d"]
            return point["lat"], point["lon"], in_area[0].get("localarea"), "licences"
    return None


def from_nominatim(venue):
    """Free-text geocoding, last resort. Good at landmarks, museums, and
    malls, and the only source here that reaches beyond City of Vancouver
    boundaries (Grouse Mountain, Capilano, Lynn Canyon).

    Tries the neighbourhood-qualified query first since it pins a chain's
    branch, then the name alone -- but a name-only hit is only accepted when
    the result is actually in British Columbia, so a bare name can't silently
    resolve to a same-named place in another province."""
    name = re.sub(r"\s*\(.*?\)", "", venue["name"]).strip()
    queries = (f"{name}, {venue['neighbourhood']}, Vancouver, BC, Canada",
               f"{name}, Vancouver, BC, Canada")
    for index, query in enumerate(queries):
        response = session.get(NOMINATIM, params={
            "q": query, "format": "json", "limit": 1, "addressdetails": 1},
            timeout=20)
        response.raise_for_status()
        time.sleep(NOMINATIM_DELAY_SECONDS)
        hits = response.json()
        if not hits:
            continue
        hit = hits[0]
        address = hit.get("address", {})
        if index and address.get("state") != "British Columbia":
            continue
        area = (address.get("neighbourhood") or address.get("suburb")
                or address.get("city_district") or "")
        return float(hit["lat"]), float(hit["lon"]), area, "nominatim"
    return None


def resolve(venue):
    """Try the source that is authoritative for this kind of venue first, then
    fall back to free-text geocoding. Returns (lat, lng, area, source) or None.

    Food venues additionally have to land in their expected neighbourhood.
    Restaurants are where the chains are, and a chain resolved to the wrong
    branch is a confidently wrong coordinate several kilometres out -- exactly
    what would mis-rank a "what's near me" result. Landmarks have no branches,
    so they are allowed a finer-grained area name (Financial District inside
    Downtown) without being refused."""
    is_food = venue["category"] == "food"
    strategies = ([from_licences, from_nominatim] if is_food
                  else [from_parks, from_nominatim])
    for strategy in strategies:
        try:
            found = strategy(venue)
        except requests.exceptions.RequestException as e:
            # Never print the exception itself: for a keyed API it would carry
            # the key. None of these are keyed, but the habit is the rule.
            print(f"  ! {venue['name']}: {strategy.__name__} request failed "
                  f"({type(e).__name__})")
            found = None
        if not found:
            continue
        lat, lng, area = found[0], found[1], found[2]
        south, north, west, east = METRO_VANCOUVER_BOUNDS
        if not (south <= lat <= north and west <= lng <= east):
            print(f"  ! {venue['name']}: {found[3]} returned {lat},{lng}, "
                  "outside Metro Vancouver -- ignored")
            continue
        if is_food and area and _normalize(area) != _normalize(venue["neighbourhood"]):
            continue
        return found
    return None


def main():
    write = "--write" in sys.argv
    venues = json.loads(VENUES_FILE.read_text(encoding="utf-8"))

    resolved, disagreed, missing = [], [], []
    for venue in venues:
        found = resolve(venue)
        if not found:
            venue.setdefault("lat", None)
            venue.setdefault("lng", None)
            missing.append(venue["name"])
            continue
        lat, lng, area, source = found
        venue["lat"], venue["lng"] = lat, lng
        resolved.append((venue["name"], source, lat, lng))
        if area and _normalize(area) != _normalize(venue["neighbourhood"]):
            disagreed.append((venue["name"], venue["neighbourhood"], area, source))

    print(f"\nresolved {len(resolved)}/{len(venues)}")
    by_source = {}
    for _, source, _, _ in resolved:
        by_source[source] = by_source.get(source, 0) + 1
    for source, count in sorted(by_source.items()):
        print(f"  {source:10} {count}")

    if disagreed:
        print(f"\nneighbourhood disagreements ({len(disagreed)}) -- coordinate kept, "
              "worth an eyeball since venues.json's neighbourhood is hand-curated:")
        for name, want, got, source in disagreed:
            print(f"  {name[:34]:36} curated={want:26} {source}={got}")

    if missing:
        print(f"\nstill without coordinates ({len(missing)}) -- add by hand if wanted:")
        for name in missing:
            print(f"  {name}")

    if write:
        VENUES_FILE.write_text(json.dumps(venues, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {VENUES_FILE}")
    else:
        print("\ndry run, nothing written. Re-run with --write to save.")


if __name__ == "__main__":
    main()
