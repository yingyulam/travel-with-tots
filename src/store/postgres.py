"""Run this app's SQL against Postgres instead of SQLite.

`src/db.py` is the only module that writes SQL, and it writes SQLite's dialect.
Almost all of it is already valid Postgres: there is no GROUP BY, no CASE and no
window function anywhere, and `ORDER BY x IS NULL` means the same thing in both.
What differs is small enough to translate, which is why this is 150 lines rather
than a rewrite of 40 query functions.

Deliberately knows nothing about the rest of the app: no credentials, no
settings, no `db` import. It is handed a connection string and returns something
shaped like a `sqlite3.Connection`, which is what keeps `db.py` free of any
Postgres branching.
"""

import re


class DialectError(Exception):
    """Raised for SQL this module cannot faithfully translate."""


# SQLite writes 'YYYY-MM-DD HH:MM:SS' in UTC and the timestamp columns are TEXT,
# so `now()` is wrong twice over: it is a timestamptz, and it carries a
# fractional part and an offset. Rows written that way would sort against the
# existing ones incorrectly, which matters because recency is what decides an
# amenity conflict. Same expression supabase_sync uses for the column defaults.
NOW = "to_char(now() AT TIME ZONE 'utc', 'YYYY-MM-DD HH24:MI:SS')"

_RULES = (
    (re.compile(r"datetime\(\s*'now'\s*\)", re.I), NOW),
    (re.compile(r"\bIFNULL\s*\(", re.I), "COALESCE("),
    # SQLite's LIKE ignores case; Postgres' does not. Every city match here is
    # `city LIKE '%...%'`, so without this a lowercase "vancouver" would return
    # no venues at all and the failure would be a silently empty day.
    (re.compile(r"\bLIKE\b", re.I), "ILIKE"),
)

# venue_hours has a composite primary key and no `id` column, so it is the one
# INSERT that must not ask for one back.
_NO_ID = frozenset({"venue_hours"})
_INSERT = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z_0-9]*)", re.I)


def placeholders(sql):
    """`?` to `%s`, leaving single-quoted literals alone.

    A literal `%` is refused rather than escaped. psycopg reads `%` as the start
    of a placeholder, and every wildcard in this codebase lives in a parameter
    (`f"%{city}%"`), so a `%` in the SQL itself means an assumption here has
    stopped holding and should say so.
    """
    out, quoted = [], False
    for char in sql:
        if char == "'":
            quoted = not quoted
        elif char == "%":
            raise DialectError(
                "SQL with a literal '%' cannot be translated: keep LIKE "
                "wildcards in the parameters.")
        elif char == "?" and not quoted:
            out.append("%s")
            continue
        out.append(char)
    return "".join(out)


def translate(sql):
    """One SQLite statement as Postgres."""
    for pattern, replacement in _RULES:
        sql = pattern.sub(replacement, sql)
    return placeholders(sql)


def with_returning(sql):
    """(statement, has_id): append RETURNING id to an INSERT that has one.

    Postgres has no `lastrowid`, and `_write` returns one for every insert.
    """
    match = _INSERT.match(sql)
    if not match or match.group(1).lower() in _NO_ID:
        return sql, False
    return sql.rstrip().rstrip(";") + " RETURNING id", True


def adapt(params):
    """Query parameters as Postgres will accept them.

    `bool` becomes `int` because the flag columns are integers: SQLite takes
    True for 1 silently, Postgres refuses `bigint = boolean`. An empty sequence
    becomes None so a statement with no placeholders is sent unformatted.
    """
    if params is None:
        return None
    if not isinstance(params, (list, tuple)):
        raise DialectError("named parameters are not supported; use ?")
    return [int(v) if isinstance(v, bool) else v for v in params] or None


class Cursor:
    """The part of a cursor `db.py` uses: iteration, fetch, lastrowid."""

    def __init__(self, cursor, has_id):
        self._cursor, self._has_id = cursor, has_id

    def __iter__(self):
        return iter(self._cursor)

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    @property
    def lastrowid(self):
        """The new row's id, read from RETURNING. None where there is no id."""
        if not self._has_id:
            return None
        row = self._cursor.fetchone()
        return row and row["id"]


class Connection:
    """A `sqlite3.Connection` shape over psycopg, translating as it goes.

    `__exit__` commits or rolls back and does **not** close, which is sqlite3's
    behaviour and the reason this is not psycopg's own context manager: `db.py`
    writes `with closing(connect()) as conn, conn:`, so closing here would close
    the connection twice and, worse, before `closing` had finished with it.
    """

    def __init__(self, connection):
        self._connection = connection

    def execute(self, sql, params=()):
        statement, has_id = with_returning(translate(sql))
        return Cursor(self._connection.execute(statement, adapt(params)), has_id)

    def executemany(self, sql, seq_of_params):
        cursor = self._connection.cursor()
        cursor.executemany(translate(sql), [adapt(p) for p in seq_of_params])
        return Cursor(cursor, False)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.rollback() if exc_type else self.commit()
        return False


# postgresql://user:password@host:5432/db -- the password is in the middle of
# the connection string, so anything that quotes a failing DSN back at a person
# quotes the password with it.
_PASSWORD = re.compile(r"(://[^:/@\s]+:)[^@/\s]+(@)")


def redact(text):
    """A message with any connection-string password replaced.

    Applied to every error that reaches a page or a log. psycopg usually reports
    host and user without the password, but a malformed connection string can
    come back quoted whole, and /settings is not the place to find that out.
    """
    return _PASSWORD.sub(r"\1***\2", str(text))


def first_line(exc):
    """One redacted line describing a failure, for a flash or a banner."""
    return redact(str(exc).strip().splitlines()[0] if str(exc).strip()
                  else type(exc).__name__)


def connect(dsn):
    """Open a Postgres connection returning rows as dicts.

    `dict_row` rather than psycopg's default tuples: a dict is a superset of
    `sqlite3.Row`, so the 12 functions that hand rows straight to callers keep
    working and gain `.get()`.

    `prepare_threshold=None` disables server-side prepared statements. Harmless
    on a direct connection, and required on Supabase's transaction pooler, where
    a prepared statement can be reused on a different backend session.
    """
    import psycopg
    from psycopg.rows import dict_row
    return Connection(psycopg.connect(dsn, row_factory=dict_row,
                                      prepare_threshold=None))


def _errors():
    try:
        import psycopg
    except ImportError:
        return None
    return psycopg.errors


def integrity_errors():
    """Postgres' unique-violation class, or () when psycopg is absent.

    Joined with sqlite3.IntegrityError in `db.INTEGRITY_ERRORS`, so the review
    page's duplicate-venue catch works whichever database is serving.
    """
    errors = _errors()
    return (errors.UniqueViolation,) if errors else ()


def unreachable_errors():
    """The failures that mean "cannot reach Supabase", for falling back."""
    errors = _errors()
    return (errors.OperationalError,) if errors else ()
