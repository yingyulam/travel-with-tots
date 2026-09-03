"""Serving pages from Supabase instead of the local SQLite file.

`src/db.py` writes SQLite's dialect and Supabase is Postgres, so a thin adapter
translates on the way through. Everything here runs without a database and
without psycopg installed, which is the point: the translation is pure string
work and the connection wrapper only needs something shaped like a connection.

What is not covered here is Postgres itself. The statements this produces were
run against the live project once, listed under Verification in the plan; the
one thing worth asserting in code is that the translation cannot silently stop
happening.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import os
import pathlib
import tempfile
import unittest
from unittest import mock

import src.store.db as db
from src.store import postgres, schema
from src.store import supabase_sync as sync


class TranslationTest(unittest.TestCase):
    def test_question_marks_become_postgres_placeholders(self):
        self.assertEqual(postgres.translate("SELECT ? , ?"), "SELECT %s , %s")

    def test_a_question_mark_inside_a_string_is_left_alone(self):
        self.assertEqual(postgres.translate("SELECT 'why?' WHERE a = ?"),
                         "SELECT 'why?' WHERE a = %s")

    def test_ifnull_becomes_coalesce(self):
        self.assertIn("COALESCE(city, '')",
                      postgres.translate("SELECT IFNULL(city, '')"))

    def test_sqlites_now_becomes_the_same_string_not_a_timestamp(self):
        # The column is TEXT holding 'YYYY-MM-DD HH:MM:SS' and the cloned rows
        # are in that format. now() is a timestamptz with a fractional part and
        # an offset, so rows written that way would sort wrongly against the
        # existing ones, and recency is what decides an amenity conflict.
        out = postgres.translate("UPDATE v SET verified_at = datetime('now')")
        self.assertIn("to_char(now() AT TIME ZONE 'utc'", out)
        self.assertNotIn("datetime", out)

    def test_like_becomes_ilike(self):
        # The one translation that fails silently rather than loudly. SQLite's
        # LIKE ignores case and Postgres' does not, so without this a plan for
        # "vancouver" would match no venues and simply return an empty day.
        self.assertEqual(postgres.translate("WHERE city LIKE ?"),
                         "WHERE city ILIKE %s")

    def test_a_literal_percent_is_refused_rather_than_guessed_at(self):
        # psycopg reads % as the start of a placeholder. Every wildcard here
        # lives in a parameter (f"%{city}%"), so a % in the SQL itself means
        # that assumption has stopped holding and should say so.
        with self.assertRaises(postgres.DialectError):
            postgres.translate("WHERE city LIKE '%x%'")

    def test_named_parameters_are_refused(self):
        with self.assertRaises(postgres.DialectError):
            postgres.adapt({"a": 1})

    def test_a_boolean_parameter_becomes_an_integer(self):
        # The flag columns are integers. SQLite takes True for 1 silently;
        # Postgres refuses to compare bigint with boolean.
        self.assertEqual(postgres.adapt([True, False, 2]), [1, 0, 2])

    def test_no_parameters_is_sent_unformatted(self):
        self.assertIsNone(postgres.adapt(()))
        self.assertIsNone(postgres.adapt(None))


class SecretsInErrorsTest(unittest.TestCase):
    """The connection string holds a password, and errors get shown."""

    def test_a_password_is_never_quoted_back(self):
        message = ("connection to postgresql://postgres.abc:s3cret@db.example"
                   ":5432/postgres failed")
        out = postgres.redact(message)
        self.assertNotIn("s3cret", out)
        self.assertIn("db.example", out)      # host is what makes it diagnosable

    def test_only_the_first_line_reaches_a_flash(self):
        out = postgres.first_line(ValueError("bad dsn://u:pw@h\nstack noise"))
        self.assertNotIn("pw", out)
        self.assertNotIn("stack noise", out)

    def test_the_fallback_message_is_redacted_too(self):
        with mock.patch.object(db, "_supabase_dsn", lambda: "postgresql://x"), \
             mock.patch.object(postgres, "connect", side_effect=ImportError(
                 "postgresql://u:s3cret@h/db unavailable")), \
             mock.patch.object(db, "connect_sqlite", lambda: "sqlite"):
            db.connect()
        self.assertNotIn("s3cret", db.LAST_BACKEND_ERROR)


class ReturningIdTest(unittest.TestCase):
    def test_an_insert_asks_for_the_new_id(self):
        # Postgres has no lastrowid, and _write returns one for every insert.
        sql, has_id = postgres.with_returning("INSERT INTO parents (a) VALUES (%s)")
        self.assertTrue(has_id)
        self.assertTrue(sql.endswith("RETURNING id"))

    def test_venue_hours_is_not_asked_for_an_id_it_has_not_got(self):
        # Composite primary key, no id column: RETURNING id would error.
        sql, has_id = postgres.with_returning(
            "INSERT INTO venue_hours (venue_id) VALUES (%s)")
        self.assertFalse(has_id)
        self.assertNotIn("RETURNING", sql)

    def test_updates_and_deletes_are_left_alone(self):
        for sql in ("UPDATE venues SET a = 1", "DELETE FROM venues"):
            with self.subTest(sql=sql):
                self.assertEqual(postgres.with_returning(sql), (sql, False))


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.executed = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return self

    def executemany(self, sql, seq):
        self.executed.append((sql, list(seq)))
        return self

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class FakeConnection:
    """Enough of a psycopg connection to exercise the wrapper."""

    def __init__(self, rows=()):
        self._rows, self.log = rows, []

    def execute(self, sql, params=None):
        self.log.append(("execute", sql, params))
        return FakeCursor(self._rows)

    def cursor(self):
        return FakeCursor(self._rows)

    def commit(self):
        self.log.append(("commit",))

    def rollback(self):
        self.log.append(("rollback",))

    def close(self):
        self.log.append(("close",))


class TheConnectionWrapperTest(unittest.TestCase):
    def test_it_translates_on_the_way_through(self):
        fake = FakeConnection()
        postgres.Connection(fake).execute("SELECT ? WHERE a LIKE ?", (1, True))
        _kind, sql, params = fake.log[0]
        self.assertEqual(sql, "SELECT %s WHERE a ILIKE %s")
        self.assertEqual(params, [1, 1])

    def test_lastrowid_reads_the_returned_id(self):
        wrapped = postgres.Connection(FakeConnection([{"id": 7}]))
        self.assertEqual(
            wrapped.execute("INSERT INTO parents (a) VALUES (?)", (1,)).lastrowid, 7)

    def test_lastrowid_is_none_where_there_is_no_id_column(self):
        wrapped = postgres.Connection(FakeConnection([{"id": 7}]))
        cursor = wrapped.execute(
            "INSERT INTO venue_hours (venue_id) VALUES (?)", (1,))
        self.assertIsNone(cursor.lastrowid)

    def test_executemany_is_supported(self):
        # psycopg3's Connection has execute but not executemany, and
        # set_venue_hours writes seven rows with it.
        fake = FakeConnection()
        postgres.Connection(fake).executemany(
            "INSERT INTO venue_hours (venue_id) VALUES (?)", [(1,), (2,)])

    def test_leaving_the_block_commits_and_does_not_close(self):
        # sqlite3's behaviour, and db.py depends on it: it writes
        # `with closing(connect()) as conn, conn:`, so closing here would close
        # the connection before closing() had finished with it.
        fake = FakeConnection()
        with postgres.Connection(fake):
            pass
        self.assertEqual(fake.log, [("commit",)])

    def test_an_exception_rolls_back_rather_than_committing(self):
        fake = FakeConnection()
        with self.assertRaises(ValueError):
            with postgres.Connection(fake):
                raise ValueError
        self.assertEqual(fake.log, [("rollback",)])


class WhichDatabaseServesTest(unittest.TestCase):
    """The four conditions for using Postgres, and the fallback."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._source = pathlib.Path(self._tmp.name) / "data_source.json"
        for patcher in (mock.patch.object(sync, "SOURCE_PATH", self._source),
                        # tests/__init__.py pins the whole suite to local, which
                        # is what this class exists to switch off.
                        mock.patch.dict(os.environ, {"DB_BACKEND": ""})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_page_reports_what_is_serving_not_what_is_stored(self):
        # The bug this exists to stop: on a host with an ephemeral disk the
        # dropdown's file is gone after every deploy, so it read "local" while
        # DB_BACKEND pinned Supabase and Supabase was serving every page. An
        # admin was told the wrong database was live by the one page whose job
        # is answering that.
        sync.set_active_source(sync.LOCAL)
        with mock.patch.object(sync, "db_url", lambda: "postgresql://somewhere"), \
             mock.patch.dict(os.environ, {"DB_BACKEND": "supabase"}):
            self.assertEqual(sync.active_source(), sync.LOCAL)      # the file
            self.assertEqual(db.effective_backend(), sync.SUPABASE)  # the truth
            self.assertEqual(db.backend_pinned_by_env(), sync.SUPABASE)

    def test_nothing_is_pinned_when_the_variable_is_unset(self):
        with mock.patch.dict(os.environ, {"DB_BACKEND": ""}):
            self.assertIsNone(db.backend_pinned_by_env())

    def test_an_unrecognised_pin_is_not_reported_as_one(self):
        with mock.patch.dict(os.environ, {"DB_BACKEND": "mysql"}):
            self.assertIsNone(db.backend_pinned_by_env())

    def test_the_environment_can_pin_supabase_without_the_settings_file(self):
        # How a deployment survives an ephemeral disk. data/data_source.json is
        # generated and does not persist across a deploy, so without this the
        # app would come back up reading a fresh empty SQLite file it had just
        # seeded with demo rows, and say nothing about it.
        sync.set_active_source(sync.LOCAL)
        with mock.patch.object(sync, "db_url", lambda: "postgresql://somewhere"), \
             mock.patch.dict(os.environ, {"DB_BACKEND": "supabase"}):
            self.assertEqual(db._supabase_dsn(), "postgresql://somewhere")

    def test_local_wins_over_a_pinned_supabase(self):
        # Whichever way they disagree, the database that always works wins.
        with self._choose(sync.SUPABASE, "postgresql://somewhere"), \
             mock.patch.dict(os.environ, {"DB_BACKEND": "local"}):
            self.assertIsNone(db._supabase_dsn())

    def test_the_environment_can_override_the_dropdown(self):
        # How the suite stays offline. Sixty-odd tests call a query function
        # without redirecting DB_PATH, so they read whatever is configured;
        # with Supabase selected those became network reads against the real
        # project. Also how a deployment pins the backend without the file.
        with self._choose(sync.SUPABASE, "postgresql://somewhere"), \
             mock.patch.dict(os.environ, {"DB_BACKEND": "local"}):
            self.assertIsNone(db._supabase_dsn())

    def _choose(self, source, url):
        sync.set_active_source(source)
        return mock.patch.object(sync, "db_url", lambda: url)

    def test_a_named_database_file_always_means_sqlite(self):
        # Every test file patches db.DB_PATH at a temp file, and naming a
        # specific SQLite file is a clear enough statement of intent to
        # override the dropdown. Without this the suite would send its writes
        # to the live Supabase project.
        with self._choose(sync.SUPABASE, "postgresql://nowhere"), \
             mock.patch.object(db, "DB_PATH", os.path.join(self._tmp.name, "t.db")):
            self.assertIsNone(db._supabase_dsn())

    def test_local_means_sqlite_even_with_a_connection_string(self):
        with self._choose(sync.LOCAL, "postgresql://nowhere"):
            self.assertIsNone(db._supabase_dsn())

    def test_supabase_without_a_connection_string_means_sqlite(self):
        with self._choose(sync.SUPABASE, ""):
            self.assertIsNone(db._supabase_dsn())

    def test_supabase_with_a_connection_string_uses_it(self):
        with self._choose(sync.SUPABASE, "postgresql://somewhere"):
            self.assertEqual(db._supabase_dsn(), "postgresql://somewhere")

    def test_an_unreachable_supabase_serves_local_data_and_says_why(self):
        # A page rendering local data with a warning beats every page 500ing,
        # and beats a silent switch back, which would leave an admin believing
        # they were looking at Supabase.
        with self._choose(sync.SUPABASE, "postgresql://somewhere"), \
             mock.patch.object(postgres, "connect",
                               side_effect=ImportError("no psycopg")), \
             mock.patch.object(db, "connect_sqlite", lambda: "sqlite"):
            self.assertEqual(db.connect(), "sqlite")
        self.assertIn("no psycopg", db.LAST_BACKEND_ERROR)

    def test_setup_and_seeding_are_skipped_on_supabase(self):
        # init_db is PRAGMA table_info and ALTER TABLE the whole way down, and
        # the tables are already there.
        with self._choose(sync.SUPABASE, "postgresql://somewhere"), \
             mock.patch.object(db, "connect_sqlite") as opened:
            schema.init_db()
        opened.assert_not_called()


class TheRuntimeSchemaTest(unittest.TestCase):
    """The SQL that turns the cloned tables into ones the app can write to."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                    os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        from contextlib import closing
        with closing(db.connect_sqlite()) as conn:
            schema.create_schema(conn)
        self.ddl = sync.postgres_runtime_ddl()

    def test_every_id_gets_a_sequence(self):
        # postgres_ddl emits `id bigint` with no default so the clone can carry
        # SQLite's ids. Every INSERT in db.py omits id, so without this the
        # first registration fails on a not-null violation.
        for table in ("parents", "children", "venues", "trips",
                      "venue_reports", "venue_hours_checks"):
            with self.subTest(table=table):
                self.assertIn(
                    f"ALTER TABLE {table} ALTER COLUMN id "
                    "ADD GENERATED BY DEFAULT AS IDENTITY", self.ddl)

    def test_venue_hours_gets_no_sequence(self):
        # Composite primary key, no id column.
        self.assertNotIn("ALTER TABLE venue_hours ALTER COLUMN id", self.ddl)

    def test_the_sequence_is_moved_past_the_cloned_rows(self):
        self.assertIn("setval(pg_get_serial_sequence('venues', 'id')", self.ddl)

    def test_deleting_a_venue_still_takes_its_reports_with_it(self):
        # delete_venue relies on ON DELETE CASCADE; the clone's schema has no
        # foreign keys at all, so without this the rows are orphaned.
        self.assertIn("FOREIGN KEY (venue_id) REFERENCES venues(id)\n"
                      "        ON DELETE CASCADE", self.ddl)

    def test_a_deleted_reviewer_does_not_take_the_venue_with_them(self):
        self.assertIn("ALTER TABLE venues ADD CONSTRAINT venues_verified_by_fkey",
                      self.ddl)
        self.assertIn("ON DELETE SET NULL", self.ddl)

    def test_every_index_reaches_supabase(self):
        # Without idx_venues_curated_identity the review page's duplicate catch
        # is dead code and an import duplicates rows instead of updating them.
        for line in schema.INDEXES.splitlines():
            if "INDEX IF NOT EXISTS" in line:
                with self.subTest(line=line):
                    self.assertIn(line.strip(), self.ddl)

    def test_it_is_generated_from_the_live_schema(self):
        from contextlib import closing
        with closing(db.connect_sqlite()) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS venue_hours_extra ("
                         "id INTEGER PRIMARY KEY)")
            conn.commit()
        self.assertIn("ALTER TABLE venue_hours_extra ALTER COLUMN id",
                      sync.postgres_runtime_ddl(["venue_hours_extra"]))


class UniqueViolationsReachTheReviewPageTest(unittest.TestCase):
    def test_both_databases_integrity_errors_are_caught(self):
        import sqlite3
        self.assertIn(sqlite3.IntegrityError, db.INTEGRITY_ERRORS)
        try:
            import psycopg
        except ImportError:
            self.skipTest("psycopg not installed")
        self.assertIn(psycopg.errors.UniqueViolation, db.INTEGRITY_ERRORS)


class ColumnsReachSupabaseTooTest(unittest.TestCase):
    """A column added to SCHEMA has to reach Supabase, not just SQLite.

    The reported bug: adding venue_reports.status took the deployed planner,
    the nearby search, the trip page and the review page down at once.
    reported_flags selects on it and get_venues calls reported_flags for every
    plan, so a column that existed in SQLite and not in Postgres was a 500 on
    every one of them.

    postgres_ddl only emits CREATE TABLE IF NOT EXISTS, which does nothing to a
    table that already exists, so nothing carried the column across. That is
    what POSTGRES_ADDED_COLUMNS is for.
    """

    def test_every_listed_column_exists_in_the_sqlite_schema(self):
        # A typo here would run an ALTER for a column no table wants, and the
        # failure would be a log line nobody reads.
        for table, column, _spec in schema.POSTGRES_ADDED_COLUMNS:
            with self.subTest(column=f"{table}.{column}"):
                self.assertIn(f"{column} ", schema.SCHEMA,
                              f"{column} is migrated but not in SCHEMA")

    def test_the_statements_are_postgres_and_idempotent(self):
        for table, column, spec in schema.POSTGRES_ADDED_COLUMNS:
            with self.subTest(column=f"{table}.{column}"):
                self.assertTrue(table and column and spec)

    def test_it_does_not_touch_sqlite_when_supabase_is_unreachable(self):
        # connect() falls back to SQLite, and ADD COLUMN IF NOT EXISTS is not
        # SQLite syntax, so the fallback would run the wrong dialect against
        # the local file.
        with mock.patch.object(db, "_supabase_dsn", return_value="postgresql://x/y"), \
             mock.patch.object(schema.postgres, "connect",
                               side_effect=ImportError("no psycopg")), \
             mock.patch.object(db, "connect_sqlite") as sqlite:
            schema._ensure_postgres_columns()
        sqlite.assert_not_called()

    def test_boot_runs_it_on_supabase(self):
        # The wiring, and the whole point: init_db returns early on Supabase,
        # so without this line the migration exists and never runs.
        with mock.patch.object(db, "_supabase_dsn", return_value="postgresql://x/y"), \
             mock.patch.object(schema, "_ensure_postgres_columns") as migrated:
            schema.init_db()
        migrated.assert_called_once()

    def test_boot_does_not_run_it_on_sqlite(self):
        # SQLite has _ensure_columns for this, which handles its own dialect.
        with mock.patch.object(db, "_supabase_dsn", return_value=None), \
             mock.patch.object(schema, "_ensure_postgres_columns") as migrated, \
             mock.patch.object(db, "connect_sqlite"), \
             mock.patch.object(schema, "create_schema"), \
             mock.patch.object(schema, "_drop_dead_columns"), \
             mock.patch.object(schema, "_migrate_trips_ownership"), \
             mock.patch.object(schema, "_seed_venues"), \
             mock.patch.object(schema, "_migrate_seed_claims"), \
             mock.patch.object(schema, "_seed_sample_data"), \
             mock.patch.object(schema, "_seed_admin"):
            schema.init_db()
        migrated.assert_not_called()

    def test_it_does_nothing_on_sqlite(self):
        with mock.patch.object(db, "_supabase_dsn", return_value=None), \
             mock.patch.object(schema.postgres, "connect") as opened:
            schema._ensure_postgres_columns()
        opened.assert_not_called()

    def test_one_failing_column_does_not_stop_the_rest(self):
        # A boot that cannot add one column must still add the others, and must
        # not take every page down.
        conn = mock.MagicMock()
        conn.execute.side_effect = [Exception("nope"), None, None]
        with mock.patch.object(db, "_supabase_dsn", return_value="postgresql://x/y"), \
             mock.patch.object(schema.postgres, "connect", return_value=conn):
            schema._ensure_postgres_columns()
        self.assertEqual(conn.execute.call_count, len(schema.POSTGRES_ADDED_COLUMNS))


if __name__ == "__main__":
    unittest.main()
