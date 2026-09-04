"""Bootstrap a fresh database with the 28 hand-curated venues. Re-runnable.

    python3 scripts/seed_venues.py            # dry run, prints what it would insert
    python3 scripts/seed_venues.py --write

Run this once on a new database, **before** scripts/import_open_data.py: the
importer upgrades a seeded park in place rather than duplicating it (see
db.upsert_imported_venue), and it can only do that if the curated row is already
there. That is the order startup used to guarantee.

`data/venues.json` is the record of a set somebody typed in to get the app off
the ground. It is not a second source of truth. The venues table is, and
everything new reaches it through review: municipal import, an agent's proposal,
or a parent's submission.

**Insert-only, and that is the whole point.** This used to run on every startup
and upsert, so that an edit to the file reached a populated database. That also
meant a restart silently reverted an admin's correction, which is why hours and
coordinates had already been made fill-only after it happened to the Vancouver
Aquarium's opening time. Skipping any venue already present retires the problem
rather than narrowing it: this script cannot revert a decision, even run by
accident, so admin edits and verification survive unconditionally.

To change a curated venue now, use /venues/review, which carries a citation and
an author.
"""

import json
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.store import db, schema

VENUES_SEED = db._DATA_DIR / "venues.json"

# The columns the seed file owns. No source, parent_id or provenance: this
# writes a row nobody has checked, so it must not claim otherwise. seed_rank is
# the file's own order, which is the curator's ranking -- the planner takes the
# first venue that fits a slot, so without it the fallback ORDER BY name would
# quietly demote whatever the curator put first.
SEED_FIELDS = ("type", "setting", "neighbourhood", "can_eat",
               "open_time", "close_time", "seed_rank")


def seed(conn):
    """Insert the seed-file venues this database does not already have.

    Returns (inserted, skipped). Matching is scoped to curated rows: comparing
    against every row let a parent's submission of an existing name suppress
    the curated entry entirely.
    """
    venues = json.loads(VENUES_SEED.read_text(encoding="utf-8"))
    columns = ", ".join(("name", "source", "city") + SEED_FIELDS + ("lat", "lng"))
    placeholders = ", ".join("?" for _ in range(len(SEED_FIELDS) + 5))
    inserted = skipped = 0
    with conn:  # one transaction for the whole batch
        for rank, v in enumerate(venues):
            if conn.execute(
                    "SELECT 1 FROM venues WHERE name = ? AND source = 'curated'",
                    (v["name"],)).fetchone():
                skipped += 1
                continue
            conn.execute(
                f"INSERT INTO venues ({columns}) VALUES ({placeholders})",
                (v["name"], "curated", "Vancouver",
                 v["type"], v["setting"], v["neighbourhood"], int(v["can_eat"]),
                 v["open"], v["close"], rank, v.get("lat"), v.get("lng")))
            inserted += 1
    return inserted, skipped


def main():
    write = "--write" in sys.argv
    schema.init_db()
    with closing(db.connect()) as conn:
        if not write:
            # The dry run asks the same question the write does, so the count
            # it prints is the count you will get.
            venues = json.loads(VENUES_SEED.read_text(encoding="utf-8"))
            present = {r["name"] for r in conn.execute(
                "SELECT name FROM venues WHERE source = 'curated'")}
            missing = [v["name"] for v in venues if v["name"] not in present]
            print(f"would insert {len(missing)}, skip {len(venues) - len(missing)}")
            for name in missing:
                print(f"  + {name}")
            print("\ndry run. pass --write to apply.")
            return 0
        inserted, skipped = seed(conn)
    print(f"inserted {inserted}, skipped {skipped} already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
