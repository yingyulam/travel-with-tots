"""Back up the live Supabase project into a local SQLite file.

    python3 scripts/pull_from_supabase.py

Supabase is the only copy of production data: the app clones local rows up and
nothing brings them back down, so a row written on the deployed site exists
once. This is the missing direction.

Writes a timestamped file under data/backups/ and never touches data/app.db.
The two have diverged in both directions, so overwriting the database you
develop against would destroy local-only rows in order to fix a backup problem.
To develop against production data, copy the result over app.db deliberately:

    cp data/backups/supabase-<stamp>.db data/app.db

The result is a database the app can open, not a dump only Postgres can read,
because the schema comes from schema.create_schema. Nothing here needs pg_dump.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.store import supabase_sync


def main():
    try:
        dest, summary = supabase_sync.pull()
    except supabase_sync.SyncError as e:
        print(f"Could not back up Supabase: {e}")
        return 1
    for table, count in summary.items():
        if not table.startswith("_"):
            print(f"  {table:20} {count:>6} rows")
    size_kb = dest.stat().st_size / 1024
    print(f"\n{summary['_total']} rows -> {dest} ({size_kb:.0f}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
