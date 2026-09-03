"""Vancouver Open Data, the one source that needs no review.

The City is more reliable about its own parks than any reviewer we could put
in front of them, so these records go straight into the venues table. That is
the whole reason this module exists separately from src/candidates.py: those
rows wait for a human, these do not.

No API key, no rate limit worth pacing for, openly licensed. The datasets used
here, with the field names verified against live responses:

    parks               218 rows. `parkid`, `name`, `neighbourhoodname`,
                        `googlemapdest{lat,lon}`, `washrooms` (Y/N), `hectare`
    community-centres    27 rows. `name`, `address`, `geo_local_area`,
                        `geo_point_2d{lat,lon}`, `urllink`
    public-washrooms    147 rows. `park_name`, `location`, `summer_hours`,
                        `winter_hours` -- read as an attribute of a venue,
                        never as a venue. A public toilet is not an outing.
"""

import requests

CATALOG = "https://opendata.vancouver.ca/api/explore/v2.1/catalog/datasets"
# The Explore v2.1 API caps a page at 100 records, so anything over that is
# paged rather than asked for in one go.
PAGE_SIZE = 100
# A guard, not a limit: the largest dataset here is 218 rows. Without it a
# changed API contract could spin this loop forever.
MAX_PAGES = 20
USER_AGENT = "travel-with-tots/1.0 (venue import from Vancouver Open Data)"

PARKS = "parks"
COMMUNITY_CENTRES = "community-centres"
PUBLIC_WASHROOMS = "public-washrooms"

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


def records(dataset, where=None, order_by=None):
    """Every record in `dataset`, paging until the rows run out.

    Returns the raw dicts. Mapping them onto venue columns is
    src/importers.py's job, so this module stays a client and nothing more.
    """
    out = []
    for page in range(MAX_PAGES):
        params = {"limit": PAGE_SIZE, "offset": page * PAGE_SIZE}
        if where:
            params["where"] = where
        if order_by:
            params["order_by"] = order_by
        response = session.get(f"{CATALOG}/{dataset}/records",
                               params=params, timeout=30)
        response.raise_for_status()
        results = response.json().get("results", [])
        out.extend(results)
        if len(results) < PAGE_SIZE:
            return out
    raise RuntimeError(f"{dataset}: more than {MAX_PAGES} pages, refusing to loop")


def record_url(dataset, field, value):
    """A link to the one record we read a venue out of.

    The citation is the dataset record rather than the venue's own website,
    because this is the provenance of what we actually stored. A reviewer
    following it sees the same row the importer saw, which is the only kind of
    citation worth having. (community-centres does publish a `urllink` to a
    City page per centre, and it is worth reading, but it is not where the
    coordinates and the neighbourhood came from.)
    """
    return f"https://opendata.vancouver.ca/explore/dataset/{dataset}/table/?refine.{field}={value}"


def parks():
    """City parks and beaches. Authoritative, and 218 of them."""
    return records(PARKS)


def community_centres():
    """The City's 27 community centres. `urllink` is a real page per centre,
    the best citation any source in this project offers."""
    return records(COMMUNITY_CENTRES)


def washrooms():
    """Public washrooms, used only to set has_washroom on a venue.

    The dataset publishes `summer_hours` and `winter_hours` separately, which
    is the City telling us these close seasonally. So a Y/N derived from it is
    not true year-round, and a parent who was there last week is the better
    source -- which is exactly why it lands as a report rather than as a column
    nobody can correct.
    """
    return records(PUBLIC_WASHROOMS)
