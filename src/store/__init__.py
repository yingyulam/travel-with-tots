"""Everything that persists, and the only place SQL is written.

    connection.py      which database is serving, and opening it
    db.py              every query, in SQLite's dialect
    schema.py          the tables
    postgres.py        the same SQL, translated for Postgres
    backend.py         which database is selected, and its credentials
    supabase_sync.py   generating Supabase's DDL, and cloning rows either way
    candidates.py      proposed venues awaiting review (CSV)
    results.py         thumbs up/down ratings (JSON)

The last two hold no SQL and belong here because this is where durable state
lives. backend.py imports nothing else in the package and connection.py imports
only backend.py and postgres.py, which is what keeps the graph acyclic.
"""
