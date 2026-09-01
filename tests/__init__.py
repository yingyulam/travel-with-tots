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
