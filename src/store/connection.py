"""Which database is serving, and how to open it.

`connect()` opens whichever backend is selected and falls back to SQLite when
Supabase is unreachable, recording why in LAST_BACKEND_ERROR.
`connect_sqlite()` is for the SQLite-only work: PRAGMA, executescript, and the
read side of the clone.

Every module that runs SQL depends on this one, and it depends on nothing in
the package except data_source.py and postgres.py. Redirect DB_PATH here, not on a
module that imported it, or the redirect reaches only that module's copy of the
name.
"""

import os
import sqlite3
from pathlib import Path

from . import data_source, postgres


_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DB_PATH = _DATA_DIR / "app.db"

# What DB_PATH is when nobody has redirected it. Naming a specific SQLite file
# overrides the data-source dropdown, so a test or script that redirects it can
# never reach the live project.
_DEFAULT_DB_PATH = DB_PATH

# Why the last attempt to reach Supabase failed, or None. Read by /settings so a
# fallback to local is visible rather than silent.
LAST_BACKEND_ERROR = None


# What a unique-index violation looks like on either backend. The review page
# catches it per row so one clashing candidate cannot unwind a batch of
# decisions.
INTEGRITY_ERRORS = (sqlite3.IntegrityError,) + postgres.integrity_errors()

def connect_sqlite():
    """Open the local SQLite file, whichever data source is selected.

    Named rather than dispatched so PRAGMA, executescript and the clone's read
    side can never be handed a Postgres connection.
    """
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def serves_supabase():
    """Whether Supabase serves this request, rather than the local SQLite file.

    Four conditions, all required; any one missing means SQLite. The fallback
    runs in that direction on purpose, so the worst outcome of a mistake here
    is reading a local file rather than somebody else's live project.

    DB_BACKEND overrides the dropdown in both directions: "local" keeps the
    test suite off the live project, "supabase" pins a deployment whose disk
    does not survive a restart.
    """
    pinned = os.environ.get("DB_BACKEND", "").strip().lower()

    if pinned == data_source.LOCAL:
        return False                       # the environment forbids it

    if Path(DB_PATH) != _DEFAULT_DB_PATH:
        return False                       # a test or script redirected the file

    asked_by_env = pinned == data_source.SUPABASE
    asked_by_dropdown = data_source.active_source() == data_source.SUPABASE
    if not (asked_by_env or asked_by_dropdown):
        return False                       # nobody asked for Supabase

    return bool(data_source.db_url())      # and there has to be somewhere to connect


def _supabase_dsn():
    """The Postgres connection string when Supabase serves, else None."""
    return data_source.db_url() if serves_supabase() else None


def effective_backend():
    """Which database is actually serving: "supabase" or "local".

    Differs from data_source.active_source(), which reads only the dropdown's
    file, whenever DB_BACKEND is set.
    """
    return data_source.SUPABASE if serves_supabase() else data_source.LOCAL


def backend_pinned_by_env():
    """The backend DB_BACKEND forces, or None when it is not set.

    What lets /settings say the dropdown has no effect, rather than showing a
    control that silently does nothing.
    """
    pinned = os.environ.get("DB_BACKEND", "").strip().lower()
    return pinned if pinned in data_source.SOURCES else None


def connect():
    """A connection to whichever database is selected.

    Falls back to SQLite when Supabase cannot be reached, recording why in
    LAST_BACKEND_ERROR for /settings to display.
    """
    global LAST_BACKEND_ERROR
    dsn = _supabase_dsn()
    if dsn is None:
        return connect_sqlite()
    try:
        conn = postgres.connect(dsn)
    except (ImportError, *postgres.unreachable_errors()) as e:
        LAST_BACKEND_ERROR = postgres.first_line(e)
        return connect_sqlite()
    LAST_BACKEND_ERROR = None
    return conn
