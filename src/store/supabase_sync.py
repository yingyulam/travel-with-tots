"""Copy the local SQLite database into Supabase.

One direction only, local to remote. It exists so a Supabase project can be
brought up to match what is already here, without hand-copying 992 rows.

Two things it deliberately does not do:

**It does not create the tables.** Supabase's Python client speaks PostgREST,
which is a REST interface over tables that already exist; there is no DDL in it
at all. `postgres_ddl()` writes the statements out instead, for a person to run
once in the Supabase SQL editor. That is a real limit of the client, not a
shortcut.

**It does not serve pages.** Reading and writing through Supabase is
`postgres.py`'s job, over a direct Postgres connection, because PostgREST
takes no SQL and `db.py` is a thousand lines of it. This module owns the copy and
the switch; that one owns the dialect.

The client is a parameter rather than a module-level singleton so the whole of
this can be tested against a fake.
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import connection, schema
from .backend import SyncError, credentials

# Parents before children, venues before the rows that reference them. A copy
# into a database with foreign keys fails on order, and Supabase's generated
# schema will have them if the DDL below is used unchanged.
TABLES = ("parents", "children", "venues", "trips",
          "venue_hours", "venue_reports", "venue_hours_checks")

# Rows per request. Small enough that one oversized row cannot fail a whole
# table, large enough that 688 reports take two calls rather than 688.
CHUNK = 500

# SQLite has five storage classes; Postgres wants a real type. `id` columns stay
# plain bigint rather than becoming bigserial on purpose: the values are
# meaningful here, because venue_reports.venue_id and trips.parent_id point at
# them, so a copy that let Postgres assign new ids would break every reference.
_TYPES = {"INTEGER": "bigint", "REAL": "double precision", "TEXT": "text",
          "BLOB": "bytea", "NUMERIC": "numeric", "": "text"}

# SQLite writes 'YYYY-MM-DD HH:MM:SS' in UTC and the columns are TEXT, so the
# Postgres default has to produce the same string rather than a timestamptz.
_NOW = "to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')"







def get_client():
    """A Supabase client, or a SyncError explaining what is missing."""
    url, key = credentials()
    try:
        from supabase import create_client
    except ImportError:
        raise SyncError("The supabase package is not installed. "
                        "pip install -r requirements.txt") from None
    return create_client(url, key)


def _columns(conn, table):
    """[(name, type, notnull, default, is_pk)] for one table, in order."""
    return [(r["name"], (r["type"] or "").upper(), bool(r["notnull"]),
             r["dflt_value"], bool(r["pk"]))
            for r in conn.execute(f"PRAGMA table_info({table})")]


def _column_ddl(name, sql_type, notnull, default):
    """One column's Postgres definition."""
    parts = [f"    {name} {_TYPES.get(sql_type, 'text')}"]
    if default is not None:
        # The only default SQLite carries that is not a literal.
        parts.append(f"DEFAULT {_NOW}" if "datetime" in str(default).lower()
                     else f"DEFAULT {default}")
    if notnull:
        parts.append("NOT NULL")
    return " ".join(parts)


def postgres_ddl(tables=TABLES):
    """CREATE TABLE statements for Supabase's SQL editor.

    Generated from the live SQLite schema, so a column added to `schema.SCHEMA`
    cannot be forgotten here. `IF NOT EXISTS` throughout, so running it twice
    is safe.

    Foreign keys are omitted: they would enforce the copy order this module
    already follows, and a half-finished copy worth retrying beats one that
    fails on a missing parent row. See postgres_runtime_ddl.
    """
    with closing(connection.connect_sqlite()) as conn:
        out = []
        for table in tables:
            columns = _columns(conn, table)
            if not columns:
                continue
            lines = [_column_ddl(name, kind, notnull, default)
                     for name, kind, notnull, default, _pk in columns]
            keys = [name for name, _k, _n, _d, pk in columns if pk]
            if keys:
                lines.append(f"    PRIMARY KEY ({', '.join(keys)})")
            out.append(f"CREATE TABLE IF NOT EXISTS {table} (\n"
                       + ",\n".join(lines) + "\n);")
        return "\n\n".join(out)


def _foreign_keys(conn, table):
    """[(column, parent_table, parent_column, on_delete)] for one table."""
    return [(r["from"], r["table"], r["to"], r["on_delete"] or "NO ACTION")
            for r in conn.execute(f"PRAGMA foreign_key_list({table})")]


def _identity_ddl(table):
    """Give `table.id` a sequence, and set it past the highest cloned id.

    postgres_ddl() emits `id bigint` with no default so the copy can carry
    SQLite's ids and every venue_id and parent_id still points at the same row.
    Serving needs the opposite: every INSERT in db.py omits `id`, so without a
    sequence the first registration fails on a not-null violation.

    Guarded because ADD GENERATED errors on a column that already has it. The
    setval runs unconditionally, which is what makes this safe to re-run after
    another clone.
    """
    return f"""DO $$
DECLARE next_id bigint;
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_attribute
                   WHERE attrelid = '{table}'::regclass
                     AND attname = 'id' AND attidentity <> '') THEN
        ALTER TABLE {table} ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY;
    END IF;
    SELECT COALESCE(MAX(id), 0) + 1 INTO next_id FROM {table};
    PERFORM setval(pg_get_serial_sequence('{table}', 'id'), next_id, false);
END $$;"""


def _foreign_key_ddl(table, column, parent, parent_column, on_delete):
    """One foreign key, skipped if it is already there.

    Omitted from postgres_ddl() so a copy that failed halfway could be retried
    without order mattering. Serving needs them: delete_venue relies on
    ON DELETE CASCADE to take a venue's reports and hours with it, and without
    the constraint those rows are simply orphaned.
    """
    name = f"{table}_{column}_fkey"
    return f"""DO $$
BEGIN
    ALTER TABLE {table} ADD CONSTRAINT {name}
        FOREIGN KEY ({column}) REFERENCES {parent}({parent_column})
        ON DELETE {on_delete};
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;"""


def postgres_runtime_ddl(tables=TABLES):
    """The rest of the schema, needed only once Supabase serves the app.

    Three things postgres_ddl() leaves out because a clone does not need them,
    and every one of which the app does: id sequences, foreign keys, and the
    unique indexes from `schema.INDEXES` that stop a duplicate venue. Run after the
    first clone, and safe to run again.

    Generated from the live SQLite schema, like the CREATE TABLEs, so a new
    reference or a new index cannot be forgotten here.
    """
    with closing(connection.connect_sqlite()) as conn:
        out = []
        for table in tables:
            columns = _columns(conn, table)
            if any(name == "id" and pk for name, _k, _n, _d, pk in columns):
                out.append(_identity_ddl(table))
        for table in tables:
            for column, parent, parent_column, on_delete in _foreign_keys(conn, table):
                if parent in tables:
                    out.append(_foreign_key_ddl(table, column, parent,
                                                parent_column, on_delete))
    return "\n\n".join(out) + "\n\n" + schema.INDEXES.strip()


def primary_key(table):
    """The column names that identify a row, for skipping duplicates."""
    with closing(connection.connect_sqlite()) as conn:
        return [name for name, _k, _n, _d, pk in _columns(conn, table) if pk]


def local_rows(table):
    """Every row of one local table, as plain dicts ready to send."""
    with closing(connection.connect_sqlite()) as conn:
        return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]


# What PostgREST says when the table has not been created. Matched on the text
# as well as the code, because the code is not in every client version and this
# is the one failure a first-time user will actually hit.
def _is_missing_table(error) -> bool:
    text = str(error)
    return "PGRST205" in text or "Could not find the table" in text


# Postgres' "insufficient privilege". With row-level security on, which is what
# the SQL editor recommends and what keeps a publishable key safe, an insert
# through a publishable key is refused: that key is meant for browsers, and its
# safety *is* RLS. A clone is a server-side admin job and wants a secret key,
# which bypasses RLS.
def _is_rls_refusal(error) -> bool:
    text = str(error)
    return "42501" in text or "row-level security" in text


def _remote_count(client, table):
    """How many rows Supabase holds, for the before-and-after summary."""
    try:
        answer = client.table(table).select("*", count="exact").limit(1).execute()
    except Exception as e:                                      # noqa: BLE001
        if _is_missing_table(e):
            raise SyncError(
                f"Supabase has no '{table}' table yet. Run the CREATE TABLE "
                "statements on this page in the Supabase SQL editor first, "
                "then clone again.") from None
        raise
    return getattr(answer, "count", None) or 0


def clone(client=None, tables=TABLES):
    """Copy every local row into Supabase, skipping ones already there.

    Returns {table: {"local", "copied", "skipped"}} plus a "_total". Counted by
    asking Supabase how many rows it holds before and after rather than by
    trusting the reply, because the skip is done server-side: `upsert` with
    `ignore_duplicates` lets Postgres decide what is already present, which is
    both atomic and immune to a race with another run.

    Idempotent by construction. Running it twice copies nothing the second time.
    """
    client = client or get_client()
    summary, total = {}, 0
    for table in tables:
        rows = local_rows(table)
        keys = primary_key(table)
        if not rows:
            summary[table] = {"local": 0, "copied": 0, "skipped": 0}
            continue
        if not keys:
            raise SyncError(f"{table} has no primary key, so duplicates "
                            "could not be skipped.")
        before = _remote_count(client, table)
        for start in range(0, len(rows), CHUNK):
            try:
                client.table(table).upsert(
                    rows[start:start + CHUNK],
                    on_conflict=",".join(keys),
                    ignore_duplicates=True).execute()
            except Exception as e:                              # noqa: BLE001
                if _is_rls_refusal(e):
                    raise SyncError(
                        f"Supabase refused the write to '{table}': row-level "
                        "security is on and SUPABASE_API_KEY is a publishable "
                        "key, which browsers use and RLS is meant to restrict. "
                        "Put the project's secret key (sb_secret_..., or "
                        "service_role) in .env instead. It bypasses RLS and "
                        "never leaves the server.") from None
                raise
        copied = max(_remote_count(client, table) - before, 0)
        summary[table] = {"local": len(rows), "copied": copied,
                          "skipped": len(rows) - copied}
        total += copied
    summary["_total"] = total
    return summary


# Where `pull` writes. Timestamped rather than one overwritten file: a backup
# you replace on every run only ever protects you from the last mistake, and
# these are a few hundred KB each.
BACKUPS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "backups"


def remote_rows(table, client=None):
    """Every row of one Supabase table, paged so a large one still arrives.

    PostgREST caps a response, so this asks by range until a page comes back
    short. Ordered by primary key, because an unordered paged read can repeat
    or skip a row between requests.
    """
    client = client or get_client()
    keys = primary_key(table) or ["id"]
    rows, start = [], 0
    while True:
        query = client.table(table).select("*")
        for key in keys:
            query = query.order(key)
        page = query.range(start, start + CHUNK - 1).execute().data or []
        rows.extend(page)
        if len(page) < CHUNK:
            return rows
        start += CHUNK


def pull(dest=None, client=None, tables=TABLES):
    """Copy every Supabase row into a fresh SQLite file. Returns (path, summary).

    The direction `clone` does not go. Nothing else reads Supabase downward,
    so without this the live project is the only copy of production data.

    Writes a new file rather than touching data/app.db, which can hold rows
    Supabase does not. Copy the result over it deliberately if that is what you
    want.

    Built with schema.create_schema, so the result is a database the app can
    open, not a dump only Postgres can read.
    """
    from . import schema
    client = client or get_client()
    dest = Path(dest) if dest else (
        BACKUPS_DIR / f"supabase-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        raise SyncError(f"{dest} already exists; pick another name.")

    summary = {}
    with closing(sqlite3.connect(dest)) as conn:
        conn.row_factory = sqlite3.Row
        schema.create_schema(conn)
        for table in tables:
            # Seeded rows first: create_schema seeds venues, and those rows
            # would collide with the real ones arriving next.
            with conn:
                conn.execute(f"DELETE FROM {table}")
        for table in tables:
            rows = remote_rows(table, client)
            summary[table] = len(rows)
            if not rows:
                continue
            columns = [name for name, *_rest in _columns(conn, table)]
            keep = [c for c in columns if c in rows[0]]
            placeholders = ", ".join("?" for _ in keep)
            statement = (f"INSERT INTO {table} ({', '.join(keep)}) "
                         f"VALUES ({placeholders})")
            with conn:
                conn.executemany(
                    statement, [[row.get(c) for c in keep] for row in rows])
    summary["_total"] = sum(v for k, v in summary.items() if not k.startswith("_"))
    return dest, summary
