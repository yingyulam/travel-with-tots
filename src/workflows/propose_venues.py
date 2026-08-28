"""Propose a small batch of new venues for a person to check.

The growth loop this app is built around: the agent searches the web and writes
candidates to `data/venue_candidates.csv`, a person reviews a batch at
/venues/review, and approving one is what puts it in the venues table. The agent
never writes to that table. Nothing here decides a venue is good enough to plan
a family's day around; a person does, by clicking.

Small batches on purpose. Verification capacity is one person's attention, so
proposing a hundred venues does not grow the database faster, it just grows a
backlog. `batch_size` is that capacity, and the run stops when it is filled.

Nothing schedules this. A timer firing while nobody has time to review only
grows the same backlog, so the run is invoked when there is capacity to absorb
it, which is also why "scheduled" here means "repeatable", not "automatic".
"""

import re
import time
from itertools import zip_longest
from pathlib import Path

from .. import candidates, db, nominatim, osm
from ..agents import call_openrouter, parse_json_reply
from ..data_loader import (CITIES, NEIGHBOURHOODS, SETTINGS, SHELTERED,
                           VENUE_TYPES)
from ..geo import in_metro_vancouver
from ..components.extract_form import _FILLER, _words
from ..components.search_web import WebSearchError, search_web

PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "propose_venues.txt"
_TEMPLATE = None

# A realistic sitting for one reviewer. The first run is invoked with more.
DEFAULT_BATCH_SIZE = 10

# Pinned rather than agents.DEFAULT_MODEL for the reason extract_form documents
# at length: the free auto-router advertises structured outputs but picks a
# different model per request, and honoured the schema about half the time.
CURATOR_MODEL = "openai/gpt-4o-mini"

CITY = "Vancouver"

# Where the search is pointed: at what nothing else covers. Vancouver Open Data
# publishes the City's parks and community centres, so the agent's only job is
# the places the City does not own -- museums, aquariums, private attractions --
# and the indoor ones most of all, because that is the gap a wet day exposes.
#
# Three queries were dropped when this was retargeted. Two searched for
# restaurants, which left the venue table entirely; one searched for community
# centres, which the importer already brings in authoritatively, so proposing
# them was effort spent against a worse source.
STANDING_QUERIES = (
    "indoor activities Vancouver toddlers rainy day",
    "indoor play spaces Vancouver under 5",
    "best places to take a toddler in Vancouver",
    "children's museums and science centres Vancouver",
    "Vancouver aquarium and animal attractions for toddlers",
    "farms and petting zoos near Vancouver with young children",
    "Vancouver pools with a toddler or leisure pool",
)

# How many venues a single search result set can realistically support. Tavily
# returns 5 results per query, so this bounds one LLM call's output and keeps a
# batch spread across several queries rather than lifted from one listicle.
MAX_PER_QUERY = 6

# type and neighbourhood are closed lists, in the schema and not only in the
# prompt. Describing them in prose did not hold: a live run produced type
# "activity" four times, plus "cafe" and "restaurant" for venues the prompt
# tells it to skip outright, and neighbourhoods "Central Vancouver" and "East
# Vancouver" that are not places the City names. Every one reached the review
# page as "(not a known value)" for a person to fix by hand, one at a time.
#
# `type` is the one that had to stop: it drives data_loader.is_nap_friendly, so
# an unrecognised value does not fail, it silently means "never nap-friendly".
#
# Built from the data_loader constants rather than retyped, so the schema, the
# prompt and the review page's dropdowns cannot drift apart. null stays
# allowed, because "the results did not say" is still the honest answer.
def _one_of(values):
    return {"type": ["string", "null"], "enum": [*values, None]}


VENUE_PROPERTIES = {
    "name": {"type": "string"},
    "type": _one_of(VENUE_TYPES),
    # Whether a visit is spent under cover. Asked of the model because a search
    # result usually does establish it -- "indoor play space", "waterfront
    # park" -- unlike hours or amenities, which it never does. Enum-constrained
    # and null-allowed like the rest, and a reviewer confirms it either way.
    "setting": _one_of(SETTINGS),
    "neighbourhood": _one_of(NEIGHBOURHOODS),
    "evidence": {"type": ["string", "null"]},
}

# Strict mode requires every key present and listed in `required`, so "the
# results did not say" is an explicit null rather than an omitted key. Same
# shape as extract_form.EXTRACTED_FORM_RESPONSE_FORMAT.
PROPOSAL_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "venue_proposals",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "venues": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": VENUE_PROPERTIES,
                        "required": list(VENUE_PROPERTIES),
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["venues"],
            "additionalProperties": False,
        },
    },
}

# A name that is only the kind of thing it is. A live run proposed "Library",
# which no reviewer can act on: there is no way to tell which library was meant.
GENERIC_NAMES = frozenset((
    "library", "park", "museum", "cafe", "coffee shop", "restaurant", "diner",
    "playground", "pool", "mall", "shopping centre", "shopping center",
    "garden", "beach", "aquarium", "zoo", "farm", "market", "play space",
    "playspace", "community centre", "community center", "rec centre",
    "recreation centre", "art gallery", "science centre", "science center"))

# Search results about the wrong Vancouver. There are two, 500km apart, and a
# query for "indoor play spaces Vancouver" reaches both: a live run returned a
# Portland listicle ("8-indoor-playgrounds-portland") and took Dizzy Castle and
# Play Street Museum from it, both in Washington state.
#
# The coordinate guard cannot catch these. It only fires on a *located*
# candidate, and a venue in Washington is one Nominatim looking inside Metro
# Vancouver will not find at all -- so it arrived with no coordinates and
# sailed past the bounds check. The article is the thing to reject, not the
# venue: a Portland guide is not evidence for a Vancouver outing whatever it
# names.
#
# Matched against the URL and the title only, never the snippet. A snippet can
# mention Portland in passing; a URL or headline that does is what the article
# is about. "washington" is deliberately absent: Vancouver BC has a Washington
# Street, and the two spellings below already cover the real case.
WRONG_REGION = re.compile(
    r"portland|oregon|\bpdx\b|vancouver[,\s-]+wa\b|vancouver[,\s-]+washington",
    re.IGNORECASE)

# Things a search result names that are not a place you can take a child.
NOT_A_VENUE = re.compile(
    r"\b(best|top \d+|guide|things to do|itinerary|blog|review|tips|ideas|"
    r"ultimate|list of|where to|how to)\b", re.IGNORECASE)


# Domains where anyone can publish anything about a place. Not a blocklist:
# a Facebook post is often how a small venue announces itself, and dropping the
# candidate would lose a real find. It is a reason to go looking for the
# venue's own site, and a thing to say out loud on the review page so a
# reviewer weighs the citation instead of assuming somebody vetted it.
LOW_TRUST_DOMAINS = frozenset((
    "facebook.com", "instagram.com", "twitter.com", "x.com", "threads.net",
    "reddit.com", "pinterest.com", "tiktok.com", "yelp.com", "tripadvisor.com",
    "tripadvisor.ca", "wheree.com", "foursquare.com"))

# What a site has to share with the venue's name to count as its own. A
# measured threshold: over the pending queue, one shared word picked out
# roundhouse.ca, maplewoodfarm.bc.ca, granvilleisland.com, playscapecafe.com
# and earnesticecream.com with nothing wrong accepted, and correctly declined
# on the three whose top result was a listicle. Requiring more loses
# roundhouse.ca; requiring none accepts vancouvermom.ca.
NAME_WORDS_IN_DOMAIN = 1


class ProposalError(Exception):
    """The proposal run could not complete."""


def reload_propose_venues_prompt() -> None:
    """Force the next propose() call to re-read the prompt from disk."""
    global _TEMPLATE
    _TEMPLATE = None


def _messages(results, known) -> list:
    global _TEMPLATE
    if _TEMPLATE is None:
        _TEMPLATE = PROMPT_PATH.read_text(encoding="utf-8")
    formatted = "\n\n".join(
        f"[{i}] {r['title']}\n{r['url']}\n{r['snippet']}"
        for i, r in enumerate(results, 1))
    prompt = (_TEMPLATE
              .replace("{results}", formatted)
              .replace("{known}", ", ".join(sorted(known)) or "(none yet)")
              .replace("{types}", ", ".join(VENUE_TYPES))
              .replace("{settings}", ", ".join(SETTINGS))
              .replace("{neighbourhoods}", ", ".join(NEIGHBOURHOODS)))
    return [{"role": "system", "content": prompt}]


def gap_queries(limit=3) -> list:
    """Searches aimed at the neighbourhoods with nowhere to go indoors.

    The gap used to be geographic, and this counted venues per neighbourhood.
    With 222 imported parks that is no longer where the shortage is: every City
    area has somewhere outdoors, and what a family cannot find is somewhere
    under cover. So the measure is categorical now -- areas with outdoor venues
    and nothing indoor first, then areas with nothing at all.

    Two faults in the old version, beyond the wrong measure. It iterated only
    the neighbourhoods that already appeared in the data, so an area with
    **zero** venues never entered the counts and the biggest gaps were the ones
    it could not see. And it never read `setting`, so the indoor shortage the
    module's own comment cited was left entirely to the hardcoded queries.

    Still read from the data, so the targeting follows the table as it grows
    rather than going stale the first time a gap is filled.
    """
    indoor, outdoor = {}, {}
    for row in db.get_venues_in_city(CITY):
        area = (row["neighbourhood"] or "").strip()
        if not area:
            continue
        counter = indoor if (row["setting"] or "") in SHELTERED else outdoor
        counter[area] = counter.get(area, 0) + 1

    # Every area the app knows, not only those already represented, so a
    # neighbourhood with nothing in it is a candidate rather than invisible.
    # Sorted by how little shelter it has, then by how much else is there:
    # somewhere with ten parks and no roof is a better search than somewhere
    # with nothing at all, because we know families already go there.
    ranked = sorted(NEIGHBOURHOODS,
                    key=lambda area: (indoor.get(area, 0), -outdoor.get(area, 0)))
    return [f"indoor activities for toddlers in {area} {CITY}"
            for area in ranked[:limit]]


def in_region(result) -> bool:
    """Whether a search result is about the Vancouver we plan for."""
    return not WRONG_REGION.search(
        f"{result.get('url', '')} {result.get('title', '')}")


def _grounded(venue, said) -> dict:
    """Drop anything the search results cannot support.

    The name is the load-bearing check: a name sharing no word with the fetched
    text was invented, and an invented venue is the one failure that makes a
    reviewer stop trusting the whole batch. Same primitive extract_form uses on
    a parent's own words, for the same reason.
    """
    name = (venue.get("name") or "").strip()
    if not name:
        return {}
    # The city's own name is not evidence. "Vancouver Public Library" shares
    # "vancouver" with an article about Vancouver, Washington restaurants, and a
    # live run accepted exactly that: a real venue carrying a citation that says
    # nothing about it. What has to overlap is the distinctive part of the name.
    mine = _words(name) - _FILLER - _words(CITY)
    if not mine or not (mine & said):
        return {}
    if NOT_A_VENUE.search(name):
        return {}
    if name.strip().casefold() in GENERIC_NAMES:
        return {}
    kept = {"name": name}
    for field in ("type", "setting", "neighbourhood", "evidence"):
        value = (venue.get(field) or "").strip()
        kept[field] = value
    # A neighbourhood the results never mention is a guess from the name.
    if kept["neighbourhood"] and not (_words(kept["neighbourhood"]) & said):
        kept["neighbourhood"] = ""
    # Belt and braces behind the schema's enums. extract_form documents at
    # length that a model can ignore a structured-output schema, and a value
    # outside the list is worse than a blank here: blank asks the reviewer a
    # question, wrong tells them an answer. Blanked rather than dropped, since
    # the venue itself may still be a good find.
    for field, allowed in (("type", VENUE_TYPES), ("setting", SETTINGS),
                           ("neighbourhood", NEIGHBOURHOODS)):
        kept[field] = in_enum(kept[field], allowed)
    # A name identical to its own type says nothing: "Museum", type museum.
    if kept["type"] and candidates.normalize_name(name) == candidates.normalize_name(kept["type"]):
        return {}
    return kept


def domain(url) -> str:
    """The host of a URL, without "www.". "" if it is not a web address.

    Public because the review page shows it beside every citation: what makes a
    source worth trusting is mostly which site it is, and a link labelled
    "Source" hides exactly that.
    """
    match = re.match(r"https?://([^/?#]+)", (url or "").strip(), re.IGNORECASE)
    return re.sub(r"^www\.", "", match.group(1).lower()) if match else ""


def is_low_trust(url) -> bool:
    """Whether a citation is somewhere anyone can publish anything."""
    host = domain(url)
    return any(host == d or host.endswith("." + d) for d in LOW_TRUST_DOMAINS)


def _looks_official(url, name) -> bool:
    """Whether a URL's domain is plausibly this venue's own.

    Read off the domain, not the page: a domain is the one part of a result a
    venue has to own. "granvilleisland.com" for Granville Island is its site;
    "vancouvermom.ca" writing about Little Nest is not, however good the
    article. Deterministic on purpose -- there is nothing here for a model to
    judge that a string comparison cannot.
    """
    host = domain(url)
    if not host or is_low_trust(url):
        return False
    squashed = re.sub(r"[^a-z0-9]", "", host)
    wanted = _words(name) - _FILLER - _words(CITY)
    return sum(1 for word in wanted if word in squashed) >= NAME_WORDS_IN_DOMAIN


def official_site(name, from_osm=None) -> str:
    """The venue's own website, or "".

    OSM first, because it costs nothing: the hours lookup has already fetched
    it, and osm.venue_facts matches on the whole name, so a website it returns
    belongs to the place we asked about. Otherwise one search, which is a
    Tavily credit per candidate and the reason this is not attempted for
    anything already answered.

    Returns the site's root rather than whichever page ranked, since what a
    reviewer wants is somewhere to look the venue up, and the deep link Tavily
    returns often carries tracking parameters.
    """
    if from_osm and _looks_official(from_osm, name):
        return _root(from_osm)
    try:
        results = search_web(f"{name} {CITY} official website")
    except (WebSearchError, KeyError) as e:
        print(f"Official site lookup skipped for {name}: {e}")
        return ""
    for result in results:
        if _looks_official(result["url"], name):
            return _root(result["url"])
    return ""


def _root(url) -> str:
    host = domain(url)
    return f"https://{host}/" if host else ""


def _hours_from_osm(names) -> dict:
    """{name: {"open_time", "close_time", "hours_note", "website"}} from OSM.

    One batched Overpass query for the whole batch, on the page timeout rather
    than the script one: osm.py says not to call it from a page, and this is
    the exception it allows for, because it is one request per run and a
    failure costs the batch its prefilled hours rather than the run.

    Hours are prefilled only where OSM says one plain pair (osm.single_pair).
    Anything richer -- a Monday closure, a lunch break, seasonal bands -- is
    left for the reviewer, because a single open/close cannot hold it and
    picking half of it would be inventing an answer.

    `hours_note` carries the raw string and the OSM entry's own name whether or
    not the pair was prefilled. That is what makes a prefill checkable: the
    reviewer sees what it was read from and which entry answered, so a plausible
    wrong match is caught before approval rather than after.
    """
    try:
        facts = osm.venue_facts(names, timeout=osm.PAGE_TIMEOUT_SECONDS)
    except osm.OverpassError as e:
        print(f"OSM lookup skipped for this batch: {e}")
        return {}
    out = {}
    for name, fact in facts.items():
        hours = fact.get("opening_hours")
        found = {"website": fact.get("website", "")}
        if hours:
            opens, closes = osm.single_pair(hours)
            found["open_time"] = opens or ""
            found["close_time"] = closes or ""
            found["hours_note"] = f"OpenStreetMap, \u201c{fact['osm_name']}\u201d: {hours}"
        out[name] = found
    return out


def in_enum(value, allowed) -> str:
    """`value` if the app knows it, "" otherwise.

    One helper rather than a check at each source, because the enum has to hold
    wherever a value enters and it very nearly did not: the schema constrains
    what the model may say, but _locate writes a neighbourhood and a city from
    the place lookup *after* grounding, and a geocoder does not know our list.
    "Central Vancouver" reached the review queue that way, past a fix aimed
    only at the model.
    """
    value = (value or "").strip()
    return value if value in allowed else ""


def _locate(name, area=None) -> dict:
    """Address and coordinates for a proposed venue, or blanks.

    Nominatim rather than Google Places. Places terms allow keeping a place id
    but restrict retaining returned content, and everything this function
    writes lands in data/venue_candidates.csv, which is tracked in git -- so a
    public repo redistributed Google's addresses and coordinates. Nominatim is
    ODbL: storable and showable with attribution, the same reason src/osm.py
    was chosen for hours.

    Three return shapes, and callers depend on all three: `{}` keeps the
    candidate without coordinates, `{"out_of_area": True}` drops it, anything
    else is the location. A lookup failure costs the candidate its
    coordinates, never the candidate.
    """
    try:
        found = nominatim.locate(name, area)
    except nominatim.NominatimError as e:
        print(f"Location lookup skipped for {name}: {e}")
        return {}
    if not found:
        return {}
    lat, lng = found["lat"], found["lng"]
    if not in_metro_vancouver(lat, lng):
        print(f"Rejecting {name}: resolved to {lat},{lng}, outside Metro Vancouver")
        return {"out_of_area": True}
    # The geocoder's own area names are its own, not ours: it answers "Olympic
    # Village" and "Financial District" where our list says neither. Held to
    # the same enum as the model's answer, so an unrecognised area arrives
    # blank for a reviewer to pick rather than pre-filled with a value nothing
    # in the app can use.
    return {"address": found["address"], "lat": lat, "lng": lng,
            "city": CITY,
            "neighbourhood": in_enum(found["area"], NEIGHBOURHOODS),
            "external_id": found["external_id"]}


def _queries(batch_size) -> list:
    """Search queries for one run, gap-driven first then the standing set.

    Rotated by how many candidates already exist, so consecutive runs do not
    re-issue the same queries and re-read the same articles.
    """
    standing = list(STANDING_QUERIES)
    offset = len(candidates.load()) % len(standing)
    rotated = standing[offset:] + standing[:offset]
    # Interleaved rather than concatenated: grounding drops a good share of what
    # each query yields, so a run needs several, and taking a prefix of
    # gap-then-standing would let the gap queries crowd the standing set out
    # entirely for any realistic batch size.
    pool = [q for pair in zip_longest(gap_queries(), rotated) for q in pair if q]
    needed = max(4, -(-batch_size // 3))
    return pool[:needed]


def propose(batch_size=DEFAULT_BATCH_SIZE, model=CURATOR_MODEL) -> dict:
    """Search, extract, locate, and write up to `batch_size` new candidates.

    Returns what happened, so a run is legible without reading the file:
    {"proposed", "skipped", "queries", "model", "response_time"}.
    """
    known = candidates.known_names() | {
        candidates.normalize_name(row["name"])
        for row in db.get_venues_in_city("")}
    started = time.time()
    queries, proposals, skipped = [], [], 0

    for query in _queries(batch_size):
        if len(proposals) >= batch_size:
            break
        try:
            found_results = search_web(query)
        except (WebSearchError, KeyError) as e:
            raise ProposalError(f"web search failed: {e}") from None
        results = [r for r in found_results if in_region(r)]
        for dropped in (r for r in found_results if not in_region(r)):
            print(f"Ignoring {dropped['url']}: about the other Vancouver")
        if not results:
            continue
        queries.append(query)
        said = _words(" ".join(f"{r['title']} {r['snippet']}" for r in results))
        by_url = {r["title"]: r["url"] for r in results}

        reply, _usage, _elapsed = call_openrouter(
            _messages(results, known), model, PROPOSAL_RESPONSE_FORMAT)
        try:
            found = parse_json_reply(reply).get("venues") or []
        except ValueError as e:
            print(f"Proposal reply unusable for {query!r}: {e}")
            continue

        for venue in found[:MAX_PER_QUERY]:
            if len(proposals) >= batch_size:
                break
            kept = _grounded(venue, said)
            if not kept:
                skipped += 1
                continue
            if candidates.normalize_name(kept["name"]) in known:
                skipped += 1
                continue
            known.add(candidates.normalize_name(kept["name"]))
            # The citation is whichever result the name came from, falling back
            # to the first: every candidate must carry a URL a person can open.
            kept["source_url"] = next(
                (url for title, url in by_url.items()
                 if _words(kept["name"]) & _words(title)),
                results[0]["url"])
            located = _locate(kept["name"], kept.get("neighbourhood"))
            if located.pop("out_of_area", False):
                skipped += 1
                continue
            # Only what the lookup actually answered. A blank here used to
            # overwrite a neighbourhood the model had grounded in the search
            # results, so a good value was replaced by "" whenever the
            # geocoder's area name was not one of ours.
            kept.update({k: v for k, v in located.items() if v not in ("", None)})
            proposals.append(kept)

    enrich(proposals)
    added = candidates.add(proposals)
    return {"proposed": added, "skipped": skipped, "queries": queries,
            "model": model, "response_time": round(time.time() - started, 3)}


def enrich(proposals) -> None:
    """Fill in hours and the official website, in place.

    Runs once over the finished batch rather than per venue inside the loop,
    because Overpass rate-limits hard enough that one query per candidate
    earned a 429 within about thirty requests.

    Neither field is something a search result establishes, which is why the
    prompt forbids the model from reporting hours at all. They come from
    outside sources a person can check instead: OSM for the hours, and the
    venue's own domain for the site. Nothing here decides anything -- it is
    the same batch, with the two fields a reviewer would otherwise have looked
    up by hand already looked up, and the evidence attached.
    """
    if not proposals:
        return
    facts = _hours_from_osm([p["name"] for p in proposals])
    for proposal in proposals:
        found = facts.get(proposal["name"], {})
        for field in ("open_time", "close_time", "hours_note"):
            if found.get(field):
                proposal[field] = found[field]
        proposal["official_url"] = official_site(
            proposal["name"], found.get("website"))


WORKFLOW = {
    "name": "Propose new venues",
    "emoji": "🌱",
    "trigger": "scheduled",
    "page": "propose_venues_page",
    "description": (
        "The agent searches the web for real places a parent could take a "
        "toddler, checks each one against what the search actually said, and "
        "writes a small batch of candidates for a person to review. Approving "
        "one at /venues/review is what puts it in the database: the agent never "
        "writes a venue itself. Rejections are remembered, so a place you turn "
        "down is never proposed again."),
    "steps": [
        {"component": "Web Search", "built": True},
        {"component": "Place Search", "built": True},
        {"component": "Venue candidates", "built": True},
        {"component": "Human review", "built": True},
    ],
}
