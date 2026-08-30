"""Venues an agent proposed, and what a human decided about each one.

Two jobs in one file, `data/venue_candidates.csv`:

1. The review artifact. What the agent found, with the evidence and the URL it
   came from, so a human can judge it.
2. The agent's memory. `known_names()` covers rejected rows as well as approved
   ones, so a place you turned down is never proposed again. Without that the
   agent re-proposes the same venues every run and a reviewer's limited capacity
   goes on re-rejecting them, which is the difference between a loop that
   converges and one that spins.

The agent only ever writes here, never to the venues table. A row becomes a
venue when a human approves it, and that is the whole of the gate.

CSV rather than JSON, against the grain of the rest of `data/`, because this is
a flat table someone may want to sort, diff or read outside the app. It is
tracked in git, unlike `data/app.db`, so it is also the durable record of which
venues were verified: see scripts/replay_candidates.py.
"""

import csv
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import CANDIDATE_FEATURE_COLUMNS, REPORTABLE_FIELDS

CANDIDATES_PATH = Path(__file__).resolve().parent.parent / "data" / "venue_candidates.csv"
_lock = threading.Lock()

PENDING = "pending"
APPROVED = "approved"
REJECTED = "rejected"
# Approving inserts the venue and stamps the candidate in one action, so there
# is no "approved but not yet imported" state to get stuck in.
STATUSES = (PENDING, APPROVED, REJECTED)

# What the agent writes: what it found, and where it found it.
PROPOSED_COLUMNS = ("name", "type", "setting", "neighbourhood", "city",
                    "address", "lat", "lng", "source_url", "evidence",
                    "official_url", "hours_note", "hours_source", "external_id")

# Fields the proposer fills even though review owns them. Hours still never
# come from a search snippet -- the proposal prompt forbids that, because a
# listicle does not establish when a museum opens. They come from two outside
# sources a person can check: OpenStreetMap, and failing that the venue's own
# page, read by a model and grounded against the times actually printed on it.
#
# `hours_week` holds a whole week in the notation osm.per_day_hours reads, e.g.
# "Mo-Th 10:00-16:00; Fr-Su 08:30-16:00". One column rather than fourteen, in
# the same syntax OSM would have given, so one parser serves both sources and a
# reviewer reads one notation. Blank when the week is uniform, since the plain
# pair says it, or when neither source produced a usable timetable.
#
# `hours_source` is where the times came from, in words, so the review page can
# say "read from maplewoodfarm.bc.ca" rather than presenting them as fact. It
# is evidence, not a judgment, which is why it is outside EDITABLE.
PREFILLED_COLUMNS = ("open_time", "close_time", "hours_week")

# What only review writes. An amenity nobody checked is a claim rather than a
# fact, so the agent leaves every one of these blank. Built from
# CANDIDATE_FEATURE_COLUMNS rather than typed out, so the review form and the
# planner's filters cannot drift.
# There were 12 more columns here, hours by season and day type, and not one
# was ever filled. They are gone with the venue_hours table: the model could
# not express a museum closed on Mondays anyway, and hours_note can.
# The amenity ticks a reviewer makes, plus can_eat. All six live here even
# though five of them are no longer columns on `venues`: this file is the
# reviewer's working copy, held between "save edits" and "approve", and on
# approval the five become venue_reports authored by the reviewer while can_eat
# goes to its column. See app._approve_candidate.
REVIEWED_COLUMNS = (PREFILLED_COLUMNS
                    + tuple(sorted(set(REPORTABLE_FIELDS)
                                   | CANDIDATE_FEATURE_COLUMNS)))

COLUMNS = (("id", "status") + PROPOSED_COLUMNS + REVIEWED_COLUMNS
           + ("proposed_at", "decided_at", "decided_by"))

# Fields review may change. Everything the agent proposed is correctable, since
# a wrong neighbourhood is exactly what a human is there to fix, except the
# coordinates and the evidence: coordinates come from a place lookup and a
# hand-typed one is worse than none (a wrong coordinate silently mis-ranks
# distance, a missing one falls back to neighbourhood matching), and rewriting
# a citation would break the one thing making the row checkable.
#
# official_url, hours_note and hours_source are evidence too, not judgments. The reviewer
# reads them to decide, and the hours they decide on go in open_time/close_time
# where the whole app already looks. A reviewer who thinks the official site is
# wrong should reject the row rather than quietly repoint its citation.
#
# external_id is identity, and identity is nobody's judgment: it is what makes
# a re-proposal of the same place recognisable instead of a second row. There
# is deliberately no candidate-level `source`. A venue's source says which
# pipeline vouched for it, and for an approved candidate that is always
# "curated" because a human clicked; a settable one would invite writing
# "municipal_open_data" onto a reviewed row and moving it between queues.
# hours_source is excluded for the same reason as official_url: it says where
# the times came from, and a reviewer who disagrees changes the times rather
# than rewriting the provenance. It also has to be in PROPOSED_COLUMNS, because
# `add` copies only those and the prefilled ones -- it sat outside both and was
# silently dropped on write, so a week read from a venue's own page arrived
# with no record of where it came from.
EDITABLE = tuple(c for c in PROPOSED_COLUMNS
                 if c not in ("lat", "lng", "source_url", "evidence",
                              "official_url", "hours_note", "hours_source",
                              "external_id")) + REVIEWED_COLUMNS


def normalize_name(name) -> str:
    """A venue name reduced to a comparison key.

    Spacing and punctuation carry no meaning for identity: "VanDusen Botanical
    Garden" and "Van Dusen Botanical Garden" are one place, and a proposal
    differing only that way is a duplicate a reviewer should not have to catch.

    American spellings fold into ours for the same reason, and it is not
    hypothetical: the agent proposed "Roundhouse Community Center" while the
    City publishes "Roundhouse Community Centre", so the duplicate check missed
    it and approving would have added a second copy of a venue we already had.
    """
    folded = _SPELLING.sub("re", (name or "").lower())
    return re.sub(r"[^a-z0-9]", "", folded)


# -er where we write -re. Only the two words this database actually contains,
# rather than a general rule: folding every American spelling would eventually
# merge two places that really are different.
_SPELLING = re.compile(r"(?<=cent)er\b|(?<=theat)er\b")


def _read_all() -> list[dict]:
    """Every candidate, oldest first. Missing or unreadable file reads as empty
    rather than raising, so a first run needs no setup step."""
    if not CANDIDATES_PATH.exists():
        return []
    try:
        with open(CANDIDATES_PATH, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (csv.Error, OSError):
        return []
    for row in rows:
        # A blank status reads as pending, so a truncated or hand-edited file
        # degrades to "needs review" rather than to a wrong decision.
        if not (row.get("status") or "").strip():
            row["status"] = PENDING
    return rows


def _write_all(rows) -> None:
    """Rewrite the whole file. Callers hold _lock."""
    CANDIDATES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CANDIDATES_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in COLUMNS})


def load(status=None) -> list[dict]:
    """Candidates, optionally only those with `status`."""
    rows = _read_all()
    if status is None:
        return rows
    return [row for row in rows if row["status"] == status]


def known_names() -> set:
    """Every name ever proposed, whatever was decided about it.

    Includes rejected names on purpose: that is what stops the agent proposing
    a place you have already turned down.
    """
    return {normalize_name(row.get("name"))
            for row in _read_all() if normalize_name(row.get("name"))}


def add(proposals) -> int:
    """Append proposals as pending, skipping names already on file. Returns how
    many were actually new.

    Deduplicates within the batch as well as against the file, since two search
    queries can easily surface the same venue.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _lock:
        rows = _read_all()
        seen = {normalize_name(row.get("name")) for row in rows}
        # Identity as well as name. Two searches can surface one venue under
        # two spellings that normalize_name does not fold ("The Beaty Museum"
        # / "Beaty Biodiversity Museum"), and the geocoder resolves both to
        # the same OSM node.
        located = {row.get("external_id") for row in rows if row.get("external_id")}
        added = 0
        for proposal in proposals:
            name = (proposal.get("name") or "").strip()
            key = normalize_name(name)
            external_id = (proposal.get("external_id") or "").strip()
            if not key or key in seen:
                continue
            if external_id and external_id in located:
                continue
            seen.add(key)
            if external_id:
                located.add(external_id)
            row = {column: "" for column in COLUMNS}
            row.update({column: proposal.get(column, "") or ""
                        for column in PROPOSED_COLUMNS + PREFILLED_COLUMNS})
            row.update({"id": uuid.uuid4().hex, "status": PENDING,
                        "name": name, "proposed_at": now})
            rows.append(row)
            added += 1
        if added:
            _write_all(rows)
    return added


def update(candidate_id, **fields) -> None:
    """Apply review's edits to one candidate.

    Unknown or non-editable field names raise rather than being ignored, so a
    renamed form input fails loudly instead of silently dropping every edit a
    reviewer made.
    """
    unknown = set(fields) - set(EDITABLE)
    if unknown:
        raise ValueError(f"not editable: {', '.join(sorted(unknown))}")
    if not fields:
        return
    with _lock:
        rows = _read_all()
        for row in rows:
            if row.get("id") == candidate_id:
                row.update({key: "" if value is None else value
                            for key, value in fields.items()})
                _write_all(rows)
                return


# What a lookup may rewrite, as opposed to what a reviewer may. EDITABLE is
# the reviewer's permission and deliberately excludes evidence: rewriting a
# citation would break the one thing that makes a row checkable. Looking a
# venue up again is the other half of that rule, because the evidence is
# exactly what a fresh lookup produces.
#
# Coordinates are here and not in EDITABLE for the same reason as ever: a
# geocoder may correct itself, a person typing one cannot.
LOOKED_UP = ("official_url", "hours_note", "hours_source", "external_id",
             "lat", "lng", "address", "open_time", "close_time", "hours_week")


def refresh_evidence(candidate_id, **fields) -> None:
    """Write what a fresh lookup found for one candidate.

    Separate from `update` because the permissions differ: a reviewer may not
    rewrite a citation, and a lookup may. Both raise on an unknown field rather
    than dropping it silently.
    """
    unknown = set(fields) - set(LOOKED_UP)
    if unknown:
        raise ValueError(f"not a looked-up field: {', '.join(sorted(unknown))}")
    if not fields:
        return
    with _lock:
        rows = _read_all()
        for row in rows:
            if row.get("id") == candidate_id:
                row.update({key: "" if value is None else value
                            for key, value in fields.items()})
                _write_all(rows)
                return


def set_status(candidate_id, status, decided_by=None) -> None:
    """Record a decision. Raises on an unknown status rather than writing it."""
    if status not in STATUSES:
        raise ValueError(f"unknown status: {status}")
    with _lock:
        rows = _read_all()
        for row in rows:
            if row.get("id") == candidate_id:
                row["status"] = status
                row["decided_at"] = datetime.now(timezone.utc).isoformat()
                row["decided_by"] = "" if decided_by is None else str(decided_by)
                _write_all(rows)
                return


def counts() -> dict:
    """How many candidates sit in each status, for the review page's summary."""
    rows = _read_all()
    return {status: sum(1 for row in rows if row["status"] == status)
            for status in STATUSES}
