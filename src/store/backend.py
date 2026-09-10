"""Which database is selected, and the credentials for reaching it.

The runtime half of the Supabase story, split from supabase_sync.py because the
lifetimes differ: `active_source` is read on every connect(), while cloning
and generating DDL happen by hand a handful of times.

Imports nothing else in the package, which is what lets connection.py depend
on it at module level.
"""

import json
import os
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"

# Loaded here, at import, and not only inside _setting. _setting overrides
# os.environ, so the first call to it used to change what DB_BACKEND said
# mid-process: anything reading the variable before that saw one answer and
# after it another. db.py imports this module at module level, so doing it here
# means every reader agrees from the start.
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=True)

# Which backend the app is set to use. A file rather than an env var so the
# /settings dropdown can change it without a restart, and beside the other
# generated state in data/.
SOURCE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "data_source.json"
LOCAL, SUPABASE = "local", "supabase"
SOURCES = (LOCAL, SUPABASE)


class SyncError(Exception):
    """Raised when Supabase is not configured or a copy fails."""


def active_source():
    """The selected backend, defaulting to local.

    Read on every call rather than cached, so the app never serves from a
    database the admin has already switched away from.
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


def _setting(name):
    """One value from .env, re-read on every call.

    `load_dotenv` fills os.environ once at import, so a value pasted into .env
    while the server runs would not be seen until a restart. Swapping a key is
    exactly what an admin does here.

    `override=True` so the new value wins over the stale one in os.environ. A
    real environment variable still wins when there is no .env entry, which is
    the deployment case.
    """
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH, override=True)
    return os.environ.get(name, "").strip()


def db_url():
    """The Postgres connection string for the Supabase project, or ''.

    Separate from SUPABASE_URL and SUPABASE_API_KEY, which are the REST
    credentials the clone uses. This one is what postgres.py connects with,
    from Connect -> ORMs in the Supabase dashboard.
    """
    return _setting("SUPABASE_DB_URL")


def credentials():
    """(url, key) from .env, or raise SyncError."""
    url, key = _setting("SUPABASE_URL"), _setting("SUPABASE_API_KEY")
    if not url or not key:
        raise SyncError("Set SUPABASE_URL and SUPABASE_API_KEY in .env first.")
    return url, key
