"""Venues an agent proposed, and what a human decided about each one.

Two jobs in one file, `data/venue_candidates.csv`:

1. The review artifact: what the agent found, with the evidence and the URL it
   came from, so a human can judge it.
2. The agent's memory. `known_names()` covers rejected rows as well as approved
   ones, so a place you turned down is never proposed again.

The agent only ever writes here, never to the venues table. A row becomes a
venue when a human approves it, and that is the whole of the gate.

CSV rather than JSON because this is a flat table someone may want to sort,
diff or read outside the app. Tracked in git, unlike data/app.db, so it is also
the durable record of which venues were verified. See
scripts/replay_candidates.py.
"""

import csv
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .db import CANDIDATE_FEATURE_COLUMNS, REPORTABLE_FIELDS

CANDIDATES_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "venue_candidates.csv"
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

# Fields the proposer fills even though review owns them. Hours never come
# from a search snippet: they come from OpenStreetMap, or failing that the
# venue's own page, read by a model and grounded against the times printed.
#
# `hours_week` holds a whole week in the notation osm.per_day_hours reads, e.g.
# "Mo-Th 10:00-16:00; Fr-Su 08:30-16:00". Blank when the week is uniform, since
# the plain pair says it, or when neither source produced a usable timetable.
#
# `hours_source` is where the times came from, in words, so the review page can
# say "read from maplewoodfarm.bc.ca" rather than presenting them as fact.
PREFILLED_COLUMNS = ("open_time", "close_time", "hours_week")

# The amenity ticks a reviewer makes, plus can_eat. An amenity nobody checked
# is a claim rather than a fact, so the agent leaves all six blank. Built from
# CANDIDATE_FEATURE_COLUMNS so the review form and the planner's filters cannot
# drift.
#
# All six live here though only can_eat is a column on `venues`: this file is
# the reviewer's working copy between "save edits" and "approve", and on
# approval the other five become venue_reports authored by the reviewer. See
# web/venues._approve_candidate.
REVIEWED_COLUMNS = (PREFILLED_COLUMNS
                    + tuple(sorted(set(REPORTABLE_FIELDS)
                                   | CANDIDATE_FEATURE_COLUMNS)))

COLUMNS = (("id", "status") + PROPOSED_COLUMNS + REVIEWED_COLUMNS
           + ("proposed_at", "decided_at", "decided_by"))

# Fields review may change. Everything the agent proposed is correctable except
# coordinates and evidence.
#
# Coordinates come from a place lookup, and a hand-typed one is worse than
# none: a wrong coordinate silently mis-ranks distance, while a missing one
# falls back to neighbourhood matching.
#
# official_url, hours_note and hours_source are evidence, not judgments. A
# reviewer who thinks the citation is wrong rejects the row rather than
# repointing it; one who disagrees with the times changes the times.
#
# external_id is identity, which makes a re-proposal recognisable rather than a
# second row. There is no candidate-level `source`: an approved candidate is
# always "curated", because a human clicked.
EDITABLE = tuple(c for c in PROPOSED_COLUMNS
                 if c not in ("lat", "lng", "source_url", "evidence",
                              "official_url", "hours_note", "hours_source",
                              "external_id")) + REVIEWED_COLUMNS


def normalize_name(name) -> str:
    """A venue name reduced to a comparison key.

    Spacing and punctuation carry no meaning for identity, so "VanDusen
    Botanical Garden" and "Van Dusen Botanical Garden" fold together. American
    spellings fold into ours for the same reason: "Community Center" matches
    "Community Centre".
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

    Includes rejected names, which is what stops the agent proposing a place
    somebody has already turned down.
    """
    return {normalize_name(row.get("name"))
            for row in _read_all() if normalize_name(row.get("name"))}


def add(proposals) -> int:
    """Append proposals as pending, skipping names already on file.

    Returns how many were new. Deduplicates within the batch as well as against
    the file, since two search queries can surface the same venue.
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
    renamed form input fails loudly instead of dropping every edit silently.
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


# What a lookup may rewrite, as opposed to what a reviewer may. EDITABLE
# excludes evidence because rewriting a citation would break the one thing that
# makes a row checkable, and a fresh lookup produces exactly that evidence.
# Coordinates likewise: a geocoder may correct itself, a person typing one
# cannot.
LOOKED_UP = ("official_url", "hours_note", "hours_source", "external_id",
             "lat", "lng", "address", "open_time", "close_time", "hours_week")


def refresh_evidence(candidate_id, **fields) -> None:
    """Write what a fresh lookup found for one candidate.

    Separate from `update` because the permissions differ: a reviewer may not
    rewrite a citation and a lookup may. Both raise on an unknown field.
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
