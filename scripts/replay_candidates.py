"""Put approved venues back into the database. One-time per rebuild, re-runnable.

    python3 scripts/replay_candidates.py            # dry run, prints a report
    python3 scripts/replay_candidates.py --write    # also inserts

Not how venues get approved: that happens in the review queue, where a person
clicks. This exists because data/app.db is gitignored while
data/venue_candidates.csv is tracked, so on a fresh clone the venues you verified
exist as decisions but not as rows. Replaying them is what makes the CSV a real
record rather than a comforting one.

Idempotent: a venue already in the table by name and city is left alone, the same
rule schema._seed_venues matches on.
"""

import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.store import candidates, db


def _existing():
    with closing(db.connect()) as conn:
        return {((row["name"] or "").strip().casefold(),
                 (row["city"] or "").strip().casefold())
                for row in conn.execute("SELECT name, city FROM venues")}


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main():
    write = "--write" in sys.argv
    approved = candidates.load(candidates.APPROVED)
    if not approved:
        print("no approved candidates on file, nothing to replay")
        return 0

    existing = _existing()
    to_insert, already, unusable = [], [], []
    for row in approved:
        key = ((row["name"] or "").strip().casefold(),
               (row["city"] or "").strip().casefold())
        if key in existing:
            already.append(row["name"])
        elif not row.get("category"):
            # The review queue refuses to approve without one, so this only
            # happens to a hand-edited file. Reported rather than guessed.
            unusable.append(row["name"])
        else:
            to_insert.append(row)

    print(f"approved on file: {len(approved)}")
    print(f"  already in the database: {len(already)}")
    print(f"  to insert:               {len(to_insert)}")
    if unusable:
        print(f"  skipped, no category:    {len(unusable)}")
        for name in unusable:
            print(f"      {name}")

    if not write:
        print(f"\ndry run, nothing written. Re-run with --write to insert "
              f"{len(to_insert)}.")
        return 0

    for row in to_insert:
        venue_id = db.add_venue(
            row["name"],
            source="curated",
            venue_type=row.get("type") or None,
            setting=row.get("setting") or None,
            neighbourhood=row.get("neighbourhood") or None,
            city=row.get("city") or None,
            address=row.get("address") or None,
            open_time=row.get("open_time") or None,
            close_time=row.get("close_time") or None,
            lat=_as_float(row.get("lat")),
            lng=_as_float(row.get("lng")),
            source_url=row.get("source_url") or None,
            verified_at=row.get("decided_at") or None,
            verified_by=int(row["decided_by"]) if (row.get("decided_by") or "").isdigit() else None,
            can_eat=row.get("can_eat") in ("1", 1, True))
        # The amenities the reviewer had ticked, restored as reports rather than
        # columns, so a rebuild does not silently drop every amenity claim.
        db.record_amenities(
            venue_id,
            {f: row.get(f) in ("1", 1, True) for f in db.REPORTABLE_FIELDS
             if row.get(f) not in (None, "")},
            reported_by=int(row["decided_by"]) if (row.get("decided_by") or "").isdigit() else None,
            note="Restored from venue_candidates.csv after a rebuild.")
        print(f"  inserted {row['name']}")
    print(f"\ninserted {len(to_insert)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
