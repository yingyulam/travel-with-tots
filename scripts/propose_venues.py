"""Propose a batch of new venues for review. Re-runnable.

    python3 scripts/propose_venues.py                # a default batch
    python3 scripts/propose_venues.py --batch 30     # a bigger one

Writes candidates to data/venue_candidates.csv and nothing else. Review them at
/venues/review; approving one there is what puts it in the venues table.

On the command line rather than only in the browser because a batch of thirty
costs thirty place lookups and several model calls, which is longer than a web
request should live.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import candidates
from src.workflows.propose_venues import DEFAULT_BATCH_SIZE, ProposalError, propose


def main():
    batch = DEFAULT_BATCH_SIZE
    if "--batch" in sys.argv:
        batch = int(sys.argv[sys.argv.index("--batch") + 1])

    print(f"proposing up to {batch} venues, skipping anything already known")
    try:
        result = propose(batch_size=batch)
    except ProposalError as e:
        print(f"\nfailed: {e}")
        return 1

    print(f"\nproposed {result['proposed']} new, skipped {result['skipped']}")
    print(f"model {result['model']}, {result['response_time']}s")
    print("\nqueries:")
    for query in result["queries"]:
        print(f"  {query}")

    pending = candidates.load(candidates.PENDING)
    if pending:
        print(f"\n{len(pending)} pending review:")
        for row in pending:
            where = row["neighbourhood"] or row["city"] or "location unknown"
            print(f"  {row['name'][:44]:46} {row['category'] or '?':9} {where}")
    print("\nreview them at /venues/review")
    return 0


if __name__ == "__main__":
    sys.exit(main())
