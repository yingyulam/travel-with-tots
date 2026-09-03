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
`src/postgres.py`'s job, over a direct Postgres connection, because PostgREST
takes no SQL and `src/db.py` is 1451 lines of it. This module owns the copy and
the switch; that one owns the dialect.

The client is a parameter rather than a module-level singleton so the whole of
this can be tested against a fake.
"""

import json
import os
from contextlib import closing
from pathlib import Path

from dotenv import load_dotenv

from . import db, schema

_ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

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


# Which backend the app is set to use. A file rather than an env var so the
# dropdown can change it without a restart, and beside the other generated
# state in data/.
#
# Wrapped in Path() because this runs at import, and `db.connect()` imports this
# module lazily -- so it can now run at any moment, including while a test has
# `db.DB_PATH` patched to a plain string. It used to work only because something
# else had already imported this module first, which is not a guarantee.
SOURCE_PATH = Path(db.DB_PATH).parent / "data_source.json"
LOCAL, SUPABASE = "local", "supabase"
SOURCES = (LOCAL, SUPABASE)


def active_source():
    """The selected backend, defaulting to local.

    Read on every call rather than cached: this is the one switch a future
    Supabase read path would hook into, and a stale value would mean the app
    served from a database the admin had already switched away from.
    """
    try:
        chosen = json.loads(SOURCE_PATH.read_text()).get("source")
    except (OSError, ValueError, AttributeError):
        return LOCAL
    return chosen if chosen in SOURCES else LOCAL


def set_active_source(source):
    """Record which backend is selected. Unknown values fall back to local."""
    SOURCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SOURCE_PATH.write_text(json.dumps(
        {"source": source if source in SOURCES else LOCAL}))


class SyncError(Exception):
    """Raised when Supabase is not configured or a copy fails."""


def _setting(name):
    """One value from .env, re-read on every call.

    The re-read is the point rather than a detail: `load_dotenv` fills
    os.environ once at import, so a value pasted into .env while the server is
    running would not be seen until a restart. Swapping the key is exactly what
    an admin does here, having been told by the previous error to swap it, and
    being told to restart as well would be a poor answer.

    `override=True` so the new value wins over the stale one already in
    os.environ. A real environment variable set outside .env still wins when
    there is no .env entry, which is the deployment case.
    """
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=True)
    return os.environ.get(name, "").strip()


def db_url():
    """The Postgres connection string for the Supabase project, or ''.

    Separate from SUPABASE_URL and SUPABASE_API_KEY, which are the REST
    credentials the clone uses. This one is what `src/postgres.py` connects
    with, from Connect -> ORMs in the Supabase dashboard.
    """
    return _setting("SUPABASE_DB_URL")


def credentials():
    """(url, key) from .env, or raise."""
    url, key = _setting("SUPABASE_URL"), _setting("SUPABASE_API_KEY")
    if not url or not key:
        raise SyncError("Set SUPABASE_URL and SUPABASE_API_KEY in .env first.")
    return url, key


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

    Generated from the live SQLite schema rather than written out by hand, so a
    column added to `schema.SCHEMA` cannot be forgotten here. `IF NOT EXISTS`
    throughout, so running it twice is safe.

    Foreign keys are deliberately omitted. They would enforce the copy order
    this module already follows, and a half-finished copy that a reviewer wants
    to retry is more useful than one that fails on a missing parent row.
    """
    with closing(db.connect_sqlite()) as conn:
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

    postgres_ddl() emits `id bigint` with no default on purpose, so the copy can
    carry SQLite's ids and every venue_id and parent_id still points at the same
    row. Serving needs the opposite: every INSERT in db.py omits `id`, so
    without a sequence the first registration fails on a not-null violation.

    Wrapped in a guard rather than run bare because ADD GENERATED errors on a
    column that already has it. The setval runs unconditionally, which is what
    makes this the right thing to re-run after another clone.
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
    with closing(db.connect_sqlite()) as conn:
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
    with closing(db.connect_sqlite()) as conn:
        return [name for name, _k, _n, _d, pk in _columns(conn, table) if pk]


def local_rows(table):
    """Every row of one local table, as plain dicts ready to send."""
    with closing(db.connect_sqlite()) as conn:
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
