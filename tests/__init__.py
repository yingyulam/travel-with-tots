"""What the whole suite needs set before any test module is imported.

Here rather than in each test file so it holds however the suite is invoked.
Both settings default the safe way, so forgetting them anywhere else leaves the
real behaviour in place rather than removing it.
"""

import os

# SQLite, never a live database. Most tests point `db.DB_PATH` at a temp file,
# which is enough on its own. Some do not: they call a query function and read
# whatever database is configured, which was harmless while that could only be a
# local file. With Supabase selected on /settings it is not -- those become
# network reads against the real project, so the suite gets slow and starts
# depending on somebody else's uptime.
os.environ["DB_BACKEND"] = "local"

# No rate limiting. Every test file runs in one process, so the limiter's
# buckets are shared across the entire run: nineteen posts to the planning
# routes in three seconds looks like one caller with a script, and the later
# tests were answered 429 by a limit meant for somebody else.
# tests/test_rate_limit.py turns it back on for the tests that are about it.
os.environ["RATE_LIMITS"] = "off"


# No real AI calls, ever, whatever a test forgets to mock. The block is on the
# host rather than on the key, because the key cannot be kept out of the
# environment: src/agents.py calls load_dotenv() at import and puts it straight
# back, and supabase_sync re-reads .env with override=True.
#
# A ConnectionError is what every AI path already handles -- plan_trip,
# replan_trip, the tools and the workflows all catch RequestException and fall
# back to their unadjusted draft -- so this changes no behaviour the suite
# asserts. Three tests already simulate the same outcome by hand.
#
# Added after a route was changed to call plan_days() while its tests still
# mocked plan_trip(): the mocks quietly stopped matching, eight tests started
# talking to OpenRouter for real, and the only symptom was the suite taking 85
# seconds instead of 9. A bill is a bad way to find out a mock has gone stale.
import requests

_real_post = requests.post


def _no_paid_calls(url, *args, **kwargs):
    if "openrouter.ai" in str(url):
        raise requests.exceptions.ConnectionError(
            "Blocked: a test tried to call OpenRouter for real. Mock the agent "
            "it goes through, or the component that calls it.")
    return _real_post(url, *args, **kwargs)


requests.post = _no_paid_calls


# The same block again, for the other way this app reaches OpenRouter. The
# chatbot and the plan/replan adjusters go through requests.post above, but
# src/agent.py's LangGraph agent uses ChatOpenAI, which is the openai SDK and
# never touches requests at all. Measured with only the block above installed:
# a ChatOpenAI call went straight past it and came back with a real 401 from
# OpenRouter, so half the app was uncovered. The key is in the environment
# during every run, because src/agents.py calls load_dotenv() at import, so an
# unmocked agent path is a billable call with a slow suite as its only symptom.
#
# Both httpx and httpx2 are patched because the SDK uses httpx2 -- a separate
# distribution with its own Client class, so patching httpx alone changes
# nothing the agent will ever call. That is exactly how this was missed the
# first time. Each is optional: whichever is installed gets blocked.
#
# Client.send is the one method every request passes through, sync and async
# alike, whatever the caller above it looks like.
_BLOCKED_HOSTS = ("openrouter.ai", "api.openai.com")

# The deliberate way out, for a live check somebody actually meant to run:
#   ALLOW_LIVE_AI=1 python3 -m unittest discover -s tests -t .
# Opt in by name rather than by deleting the block, so it is one visible
# variable on one command instead of an edit that outlives its reason.
ALLOW_LIVE_AI = os.environ.get(
    "ALLOW_LIVE_AI", "").strip().lower() in ("1", "true", "yes")


def _block(module):
    """Refuse paid hosts on one httpx-shaped module. Returns False if absent."""
    try:
        client_module = __import__(module)
    except ImportError:
        return False
    real_send = client_module.Client.send
    real_async_send = client_module.AsyncClient.send

    def refuse(url):
        raise client_module.ConnectError(
            f"Blocked: a test tried to call {url.host} for real. Mock the agent "
            "it goes through, or the component that calls it. Set "
            "ALLOW_LIVE_AI=1 to allow it on purpose.")

    def send(self, request, **kwargs):
        if any(host in request.url.host for host in _BLOCKED_HOSTS):
            refuse(request.url)
        return real_send(self, request, **kwargs)

    async def async_send(self, request, **kwargs):
        if any(host in request.url.host for host in _BLOCKED_HOSTS):
            refuse(request.url)
        return await real_async_send(self, request, **kwargs)

    client_module.Client.send = send
    client_module.AsyncClient.send = async_send
    return True


if ALLOW_LIVE_AI:
    requests.post = _real_post
else:
    BLOCKED_TRANSPORTS = tuple(m for m in ("httpx", "httpx2") if _block(m))


# The suite gets its own database, with venues in it.
#
# Startup used to seed data/venues.json into data/app.db, and ~39 tests quietly
# leaned on that: they post to /plan or /trip with no DB_PATH redirect, so they
# read whichever database the developer happened to have. They passed because a
# boot had guaranteed 28 venues were in it. Retiring the startup seeder made
# them fail on a fresh clone while still passing here, which is the same
# machine-dependent trap as discovering the suite the wrong way.
#
# So the fixture moves here. Nothing in the suite reads data/app.db any more,
# the pool is explicit rather than whatever the seed file happens to say today,
# and a test that wants its own database still redirects DB_PATH as before.
#
# Note _DEFAULT_DB_PATH is deliberately *not* touched: db._supabase_dsn reads a
# non-default DB_PATH as "a test redirected this, stay local", which is a second
# guard behind the DB_BACKEND pin above. test_pg_dialect's backend-selection
# tests lift both by hand, because switching backends is what they are for.
import sqlite3
import tempfile

from src.store import db as _db
from src.store import schema as _schema

_db.DB_PATH = tempfile.mkdtemp(prefix="twt-tests-") + "/suite.db"

# Wide enough for a real day: hours on every row because a venue without them
# is not schedulable, coordinates because the travel limit filters on them, a
# can_eat for the lunch block, and a mix of settings and nap-friendly types
# (see data_loader.NAP_FRIENDLY_TYPES).
SUITE_VENUES = (
    ("Suite Museum",  "museum",  "indoor",  0, 49.2860, -123.1120),
    ("Suite Science", "museum",  "indoor",  1, 49.2735, -123.1035),
    ("Suite Park",    "park",    "outdoor", 0, 49.2790, -123.1170),
    ("Suite Garden",  "garden",  "outdoor", 0, 49.2700, -123.1250),
    ("Suite Beach",   "beach",   "outdoor", 0, 49.2865, -123.1430),
    ("Suite Seawall", "seawall", "outdoor", 0, 49.2800, -123.1300),
    ("Suite Mall",    "mall",    "indoor",  1, 49.2820, -123.1180),
    ("Suite Market",  "market",  "both",    1, 49.2715, -123.1085),
)

_conn = sqlite3.connect(_db.DB_PATH)
_conn.row_factory = sqlite3.Row
_schema.create_schema(_conn)
with _conn:
    for _rank, _v in enumerate(SUITE_VENUES):
        _conn.execute(
            "INSERT INTO venues (name, source, city, neighbourhood, type, "
            "setting, can_eat, open_time, close_time, lat, lng, seed_rank) "
            "VALUES (?, 'curated', 'Vancouver', 'Downtown', ?, ?, ?, "
            "'09:00', '18:00', ?, ?, ?)", (*_v, _rank))
_conn.close()
