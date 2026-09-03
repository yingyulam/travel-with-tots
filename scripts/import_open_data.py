"""Import City of Vancouver open data into the venues table. Re-runnable.

    python3 scripts/import_open_data.py                  # dry run, prints a report
    python3 scripts/import_open_data.py --write
    python3 scripts/import_open_data.py --source parks --write

No review step, and that is the point: the City is more reliable about its own
parks than any reviewer, so putting a human in front of "Trafalgar Park exists
at these coordinates" is review as theatre. The review queue stays for what no
municipal dataset covers -- museums, aquariums, private attractions.

The dry run is not a preview of a preview: it runs the same two-step match the
write does, so the counts it prints are the counts you will get.
"""

import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from src import db, importers, opendata
from src import schema

SOURCES = {
    "parks": (opendata.parks, importers.park_entry),
    "community-centres": (opendata.community_centres, importers.centre_entry),
}


def _existing():
    """Every venue the matcher could match against, name and external_id only."""
    with closing(db.connect()) as conn:
        return conn.execute(
            "SELECT id, name, source, external_id FROM venues").fetchall()


def _report(label, actions, washrooms, unplaced, hourless, upgraded):
    print(f"\n{label}")
    for action in (importers.INSERTED, importers.UPGRADED, importers.UNCHANGED):
        print(f"  {action:10} {actions.count(action)}")
    yes = sum(1 for w in washrooms if w is True)
    no = sum(1 for w in washrooms if w is False)
    print(f"  washroom reported: {yes} yes, {no} no, "
          f"{len(washrooms) - yes - no} unknown")
    if upgraded:
        print(f"  upgraded in place, seed_rank kept: {', '.join(sorted(upgraded))}")
    if hourless:
        print(f"  no hours, so not schedulable until somebody fills them in: "
              f"{hourless}")
    if unplaced:
        print(f"  neighbourhood not in our enum, left null ({len(unplaced)}): "
              f"{', '.join(sorted(unplaced)[:6])}")


def run(name, write, washroom_names):
    fetch, to_entry = SOURCES[name]
    entries = [to_entry(record) for record in fetch()]
    existing = _existing()

    actions, washrooms, upgraded = [], [], []
    for entry in entries:
        if write:
            action, washroom = importers.store(entry, washroom_names)
        else:
            action = importers.classify(entry, existing)
            washroom = importers.resolved_washroom(entry, washroom_names)
        actions.append(action)
        washrooms.append(washroom)
        if action == importers.UPGRADED:
            upgraded.append(entry["name"])

    unplaced = {e["fields"]["neighbourhood"] or "(blank)" for e in entries
                if e["fields"]["neighbourhood"] is None}
    hourless = sum(1 for e in entries if not e["fields"]["open_time"])
    _report(f"{name}: {len(entries)} records", actions, washrooms,
            unplaced, hourless, upgraded)
    return actions


def main():
    write = "--write" in sys.argv
    wanted = list(SOURCES)
    if "--source" in sys.argv:
        wanted = [sys.argv[sys.argv.index("--source") + 1]]
        if wanted[0] not in SOURCES:
            sys.exit(f"unknown source: {wanted[0]}. "
                     f"one of {', '.join(SOURCES)}")

    schema.init_db()
    try:
        washroom_names = importers.washroom_places(opendata.washrooms())
    except requests.exceptions.RequestException as e:
        sys.exit(f"public-washrooms unreachable ({type(e).__name__}), "
                 "stopping: importing without it would report every venue as "
                 "having no washroom.")
    print(f"public-washrooms: {len(washroom_names)} named places")

    for name in wanted:
        try:
            run(name, write, washroom_names)
        except requests.exceptions.RequestException as e:
            print(f"\n{name}: request failed ({type(e).__name__}), skipped")

    print(f"\nvenues missing hours: {len(db.get_venues_missing_hours())} "
          "(listed on /venues/review)")
    print(f"\nPark hours are an assumption, not City data: "
          f"{'-'.join(importers.PARK_HOURS)} for every park. "
          "See importers.PARK_HOURS.")
    if not write:
        print("\ndry run, nothing written. Re-run with --write to import.")


if __name__ == "__main__":
    main()
