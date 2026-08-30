"""The test suite always runs against SQLite, never a live database.

Most tests point `db.DB_PATH` at a temp file, which is enough on its own. Some
do not: they call a query function and read whatever database is configured,
which was harmless while that could only be a local file. With Supabase
selected on /settings it is not -- those become network reads against the real
project, so the suite gets slow and starts depending on somebody else's uptime.

Set here rather than in each test file so it holds however the suite is
invoked, and before any test module is imported.
"""

import os

os.environ["DB_BACKEND"] = "local"
