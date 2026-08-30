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
