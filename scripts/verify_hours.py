"""Check our stored opening hours against OpenStreetMap. Re-runnable.

    python3 scripts/verify_hours.py            # dry run, prints a report
    python3 scripts/verify_hours.py --write    # also flags findings for review

Why this exists: hours are typed in once when a venue is approved and nothing
ever writes them again, so without a step like this a venue's hours are frozen
at whatever was entered the day it went in. That matters because the planner now
trusts them completely, refusing to schedule a venue it cannot verify.

It does not change anything itself. A finding goes to the review queue and a
person decides, which is the same shape as every other tier here: a tool
proposes, a human approves.

Same-day closures are out of scope on purpose. Only a live call catches a
private event or a burst pipe, and every stop already carries a Google Maps
link for exactly that.

Contains information from OpenStreetMap, available under the Open Database
License.
"""

import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db, osm
from src.data_loader import HOURS_ARE_A_CONVENTION

SOURCE = "osm"

# Venues whose hours are a convention rather than a posted fact keep
# dawn-to-dusk hours that OSM rarely states and nobody disputes, and they are
# most of the table. Checking them would bury the findings that matter in noise.
#
# Shared with data_loader rather than listed again here. The private copy also
# included `garden`, which meant a ticketed botanical garden's hours were never
# checked -- and VanDusen's 10:00-16:00 survived only by luck.
SKIP_TYPES = HOURS_ARE_A_CONVENTION


def _venues():
    with closing(db.connect()) as conn:
        return conn.execute(
            "SELECT id, name, type, open_time, close_time FROM venues "
            "WHERE source IN ('curated', 'municipal_open_data') "
            "ORDER BY seed_rank IS NULL, seed_rank, name").fetchall()


def main():
    write = "--write" in sys.argv
    rows = [r for r in _venues() if (r["type"] or "") not in SKIP_TYPES]
    if not rows:
        print("no venues to check")
        return 0

    print(f"checking {len(rows)} venues against OpenStreetMap "
          f"(skipping {', '.join(SKIP_TYPES)})")
    try:
        osm_hours = osm.opening_hours_for([r["name"] for r in rows])
    except osm.OverpassError as e:
        print(f"\nfailed: {e}")
        print("Overpass rate-limits hard. Wait a few minutes and re-run.")
        return 1

    buckets = {"differs": [], "more_detail": [], "agrees": [], "unverifiable": []}
    for row in rows:
        said = osm_hours.get(row["name"], "")
        finding = osm.compare(row["open_time"], row["close_time"], said)
        buckets[finding].append((row, said))

    print(f"\n  agrees        {len(buckets['agrees']):>3}")
    print(f"  differs       {len(buckets['differs']):>3}")
    print(f"  more detail   {len(buckets['more_detail']):>3}")
    print(f"  unverifiable  {len(buckets['unverifiable']):>3}"
          f"   (OSM has no readable hours)")

    for finding, label in (("differs", "OSM disagrees with our hours"),
                           ("more_detail", "OSM knows more than one pair can hold")):
        if not buckets[finding]:
            continue
        print(f"\n{label}:")
        for row, said in buckets[finding]:
            ours = f"{row['open_time'] or '?'}-{row['close_time'] or '?'}"
            print(f"  {row['name'][:32]:34} ours {ours:12} OSM {said[:40]}")

    flagged = buckets["differs"] + buckets["more_detail"]
    if not write:
        print(f"\ndry run, nothing written. Re-run with --write to send "
              f"{len(flagged)} finding(s) to /venues/review.")
        return 0

    for row, said in flagged:
        finding = "differs" if (row, said) in buckets["differs"] else "more_detail"
        db.record_hours_check(row["id"], SOURCE, said, finding,
                              row["open_time"], row["close_time"])
    print(f"\nflagged {len(flagged)} for review at /venues/review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
