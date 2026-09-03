"""Cloning the local database into Supabase.

The client is injected rather than built inside `clone`, which is what lets all
of this be tested without credentials and without touching a real project. The
fake below records what it was asked to do; the assertions are about the copy
being ordered, chunked, idempotent and honest about its counts.

What is deliberately not tested here is PostgREST itself. The one thing that
needed a live check was whether the client could be built and what a missing
table looks like, and that was run once against the real project: it connects,
and an absent table raises PGRST205, which is turned into a sentence naming the
table and pointing at the SQL on the settings page.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import os
import pathlib
import tempfile
import unittest
from contextlib import closing
from unittest import mock

import src.store.db as db
from src.store import schema
from src.store import supabase_sync as sync


class FakeTable:
    def __init__(self, store, name):
        self.store, self.name = store, name
        self._count = False

    def select(self, *_args, **kwargs):
        self._count = kwargs.get("count") == "exact"
        return self

    def limit(self, _n):
        return self

    def upsert(self, rows, on_conflict=None, ignore_duplicates=False):
        self._pending = (rows, on_conflict, ignore_duplicates)
        return self

    def execute(self):
        if self._count:
            return mock.Mock(count=len(self.store.rows.get(self.name, {})))
        rows, on_conflict, ignore = self._pending
        keys = (on_conflict or "").split(",")
        table = self.store.rows.setdefault(self.name, {})
        self.store.calls.append((self.name, len(rows), ignore))
        for row in rows:
            key = tuple(row[k] for k in keys)
            if ignore and key in table:
                continue
            table[key] = row
        return mock.Mock(data=rows)


class FakeSupabase:
    """Enough of the client to exercise clone(): counting and upsert."""

    def __init__(self, rows=None):
        self.rows = rows or {}
        self.calls = []

    def table(self, name):
        return FakeTable(self, name)


class _SyncTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                    os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect()) as conn:
            schema.create_schema(conn)
        self.parent = db.add_parent("p@example.com", "hash", name="P")
        self.child = db.add_child(self.parent, "Sam", "2024-01-01")
        self.venue = db.add_venue("A Park", source="curated", city="Vancouver",
                                  venue_type="park")


class TheGeneratedSchemaTest(_SyncTest):
    """The statements a person pastes into Supabase."""

    def test_it_covers_every_table_the_clone_copies(self):
        ddl = sync.postgres_ddl()
        for table in sync.TABLES:
            with self.subTest(table=table):
                self.assertIn(f"CREATE TABLE IF NOT EXISTS {table} (", ddl)

    def test_it_is_safe_to_run_twice(self):
        # Every statement guarded, so pasting it again after adding a table
        # does not error on the ones already there.
        ddl = sync.postgres_ddl()
        self.assertEqual(ddl.count("CREATE TABLE IF NOT EXISTS"),
                         ddl.count("CREATE TABLE"))

    def test_sqlite_types_become_postgres_types(self):
        ddl = sync.postgres_ddl(["venues"])
        self.assertIn("id bigint", ddl)
        self.assertIn("lat double precision", ddl)
        self.assertIn("name text NOT NULL", ddl)

    def test_ids_are_not_generated_by_postgres(self):
        # venue_reports.venue_id points at venues.id, so a copy that let
        # Postgres assign fresh ids would break every reference in the data.
        self.assertNotIn("serial", sync.postgres_ddl().lower())

    def test_sqlites_datetime_default_becomes_the_same_string(self):
        # The column is text and SQLite writes 'YYYY-MM-DD HH:MM:SS' in UTC, so
        # a timestamptz default would change the format of every new row.
        ddl = sync.postgres_ddl(["parents"])
        self.assertIn("to_char(now() AT TIME ZONE 'utc'", ddl)
        self.assertNotIn("datetime('now')", ddl)

    def test_a_composite_primary_key_survives(self):
        ddl = sync.postgres_ddl(["venue_hours"])
        self.assertIn("PRIMARY KEY (venue_id, weekday)", ddl)

    def test_it_is_generated_from_the_live_schema(self):
        # Written by hand it would drift the first time a column was added.
        with closing(db.connect()) as conn:
            conn.execute("ALTER TABLE venues ADD COLUMN a_new_column TEXT")
            conn.commit()
        self.assertIn("a_new_column text", sync.postgres_ddl(["venues"]))


class TheCopyTest(_SyncTest):
    def test_every_local_row_is_sent(self):
        client = FakeSupabase()
        summary = sync.clone(client)
        self.assertEqual(summary["parents"]["local"], 1)
        self.assertEqual(summary["venues"]["local"], 1)
        self.assertEqual(summary["_total"], 3)      # parent, child, venue

    def test_parents_are_copied_before_children(self):
        # A copy into a schema with foreign keys fails on order.
        db.add_report(self.venue, "has_washroom", True, reported_by=self.parent)
        client = FakeSupabase()
        sync.clone(client)
        order = [name for name, _n, _i in client.calls]
        self.assertLess(order.index("parents"), order.index("children"))
        self.assertLess(order.index("venues"), order.index("venue_reports"))

    def test_running_it_twice_copies_nothing_the_second_time(self):
        client = FakeSupabase()
        sync.clone(client)
        again = sync.clone(client)
        self.assertEqual(again["_total"], 0)
        self.assertEqual(again["parents"]["skipped"], 1)

    def test_duplicates_are_left_to_postgres(self):
        # Server-side, so the skip cannot race another run, and so a table of
        # 688 rows costs one request rather than 688 existence checks.
        client = FakeSupabase()
        sync.clone(client)
        self.assertTrue(all(ignore for _t, _n, ignore in client.calls))

    def test_an_empty_table_is_reported_rather_than_skipped_silently(self):
        summary = sync.clone(FakeSupabase())
        self.assertEqual(summary["venue_hours"],
                         {"local": 0, "copied": 0, "skipped": 0})

    def test_large_tables_are_chunked(self):
        for i in range(sync.CHUNK + 20):
            db.add_report(self.venue, "has_washroom", True, reported_by=self.parent,
                          note=f"n{i}")
        client = FakeSupabase()
        sync.clone(client, tables=["venue_reports"])
        sizes = [n for name, n, _i in client.calls if name == "venue_reports"]
        self.assertEqual(len(sizes), 2)
        self.assertLessEqual(max(sizes), sync.CHUNK)

    def test_the_summary_counts_what_arrived_not_what_was_sent(self):
        # Asked of Supabase before and after, because the skip happens there:
        # trusting the request would report rows that were quietly ignored.
        client = FakeSupabase()
        sync.clone(client)
        client.rows["parents"] = {}          # something else emptied it
        second = sync.clone(client)
        self.assertEqual(second["parents"]["copied"], 1)


class WhenSupabaseIsNotReadyTest(_SyncTest):
    def test_a_missing_table_says_which_one_and_what_to_do(self):
        # Verified against the real project: PostgREST answers PGRST205 for a
        # table that has not been created, which is what a first run hits.
        class NoTables(FakeSupabase):
            def table(self, name):
                raise RuntimeError(
                    "{'message': \"Could not find the table 'public.%s' in the "
                    "schema cache\", 'code': 'PGRST205'}" % name)

        with self.assertRaises(sync.SyncError) as caught:
            sync.clone(NoTables())
        self.assertIn("parents", str(caught.exception))
        self.assertIn("SQL editor", str(caught.exception))

    def test_an_rls_refusal_says_which_key_to_use(self):
        # Hit for real: RLS on plus a publishable key refuses every insert with
        # 42501. The publishable key is the browser one and RLS is exactly what
        # restricts it, so the fix is the secret key, not a policy change.
        class Locked(FakeSupabase):
            def table(self, name):
                table = super().table(name)
                original = table.execute

                def execute():
                    if table._count:
                        return original()
                    raise RuntimeError(
                        "{'message': 'new row violates row-level security "
                        "policy for table \"%s\"', 'code': '42501'}" % name)
                table.execute = execute
                return table

        with self.assertRaises(sync.SyncError) as caught:
            sync.clone(Locked())
        message = str(caught.exception)
        self.assertIn("parents", message)
        self.assertIn("secret key", message)
        self.assertIn("never leaves the server", message)

    def test_missing_credentials_are_a_sentence_not_a_stack_trace(self):
        # _ENV_PATH is pointed away from the real .env as well as the
        # environment being emptied: credentials() re-reads that file on every
        # call, so the developer's own keys would otherwise satisfy this.
        missing = pathlib.Path(self._tmp.name) / "no.env"
        with mock.patch.object(sync, "_ENV_PATH", missing), \
             mock.patch.dict(os.environ, {"SUPABASE_URL": "",
                                          "SUPABASE_API_KEY": ""}):
            with self.assertRaises(sync.SyncError) as caught:
                sync.credentials()
        self.assertIn(".env", str(caught.exception))

    def test_a_key_swapped_while_running_is_picked_up(self):
        # load_dotenv fills os.environ once at import, so without re-reading,
        # a key pasted into .env would need a restart. Swapping the key is
        # exactly what the RLS error above tells an admin to do.
        env = pathlib.Path(self._tmp.name) / ".env"
        env.write_text("SUPABASE_URL=https://one.example\n"
                       "SUPABASE_API_KEY=sb_publishable_first\n")
        with mock.patch.object(sync, "_ENV_PATH", env):
            self.assertEqual(sync.credentials()[1], "sb_publishable_first")
            env.write_text("SUPABASE_URL=https://one.example\n"
                           "SUPABASE_API_KEY=sb_secret_second\n")
            self.assertEqual(sync.credentials()[1], "sb_secret_second")

    def test_an_unrelated_failure_is_not_disguised_as_a_missing_table(self):
        class Broken(FakeSupabase):
            def table(self, name):
                raise RuntimeError("connection reset")

        with self.assertRaises(RuntimeError):
            sync.clone(Broken())


class TheSelectedSourceTest(_SyncTest):
    def setUp(self):
        super().setUp()
        # DB_PATH is patched to a temp string above, so the setting file is
        # pointed at the same directory rather than the real data/.
        self._source = pathlib.Path(self._tmp.name) / "data_source.json"
        patcher = mock.patch.object(sync, "SOURCE_PATH", self._source)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_defaults_to_local(self):
        self.assertEqual(sync.active_source(), sync.LOCAL)

    def test_a_choice_survives(self):
        sync.set_active_source(sync.SUPABASE)
        self.assertEqual(sync.active_source(), sync.SUPABASE)

    def test_an_unknown_value_falls_back_to_local(self):
        # The switch decides which database serves pages. Anything unrecognised
        # has to mean the one that is known to work.
        sync.set_active_source("mysql")
        self.assertEqual(sync.active_source(), sync.LOCAL)

    def test_an_unreadable_file_falls_back_to_local(self):
        self._source.write_text("not json")
        self.assertEqual(sync.active_source(), sync.LOCAL)


if __name__ == "__main__":
    unittest.main()


class FakeReadTable:
    """Enough of the client to exercise pull(): ordered, ranged reads."""

    def __init__(self, rows):
        self.rows, self._lo, self._hi = rows, 0, None

    def select(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def range(self, lo, hi):
        self._lo, self._hi = lo, hi
        return self

    def execute(self):
        return mock.Mock(data=self.rows[self._lo:self._hi + 1])


class FakeReadClient:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return FakeReadTable(self.tables.get(name, []))


class PullBringsProductionBackTest(_SyncTest):
    """The direction clone() does not go.

    Supabase is the only copy of production data: a row written on the deployed
    site is nowhere else, because nothing read it downward until this. So the
    thing worth testing is that the file it writes is a database the app can
    actually open, not merely that rows arrived.
    """

    def _client(self):
        return FakeReadClient({
            "parents": [{"id": 14, "email": "only-in-prod@example.com",
                         "password_hash": "x", "name": "Prod",
                         "is_admin": 1, "created_at": "2026-09-01"}],
            "venues": [{"id": 900, "name": "Remote Park", "city": "Vancouver",
                        "type": "park", "source": "curated"}],
        })

    def _pull(self, **kwargs):
        dest = os.path.join(self._tmp.name, "backup.db")
        return sync.pull(dest=dest, client=self._client(), **kwargs)

    def test_it_writes_the_rows_supabase_holds(self):
        _dest, summary = self._pull()
        self.assertEqual(summary["parents"], 1)
        self.assertEqual(summary["venues"], 1)
        self.assertEqual(summary["_total"], 2)

    def test_the_result_is_a_database_the_app_can_open(self):
        # The point of building it with schema.create_schema rather than
        # dumping SQL: the app's own query functions must work against it.
        dest, _summary = self._pull()
        with mock.patch.object(db, "DB_PATH", str(dest)):
            parent = db.get_parent(14)
        self.assertEqual(parent["email"], "only-in-prod@example.com")
        self.assertTrue(parent["is_admin"])

    def test_the_seeded_venues_do_not_survive(self):
        # create_schema seeds venues.json, and those rows would sit alongside
        # production's, making the backup a mix of two databases.
        dest, summary = self._pull()
        with mock.patch.object(db, "DB_PATH", str(dest)):
            names = [v["name"] for v in db.get_venues_in_city("")]
        self.assertEqual(summary["venues"], 1)
        self.assertEqual(names, ["Remote Park"])

    def test_it_does_not_touch_the_database_you_develop_against(self):
        # The two have diverged in both directions, so overwriting app.db to
        # fix a backup problem would destroy local-only rows.
        local_id = db.add_parent("local-only@example.com", "h", name="Local")
        self._pull()
        self.assertIsNotNone(db.get_parent(local_id))

    def test_it_refuses_to_overwrite_an_existing_backup(self):
        dest, _ = self._pull()
        with self.assertRaises(sync.SyncError):
            sync.pull(dest=dest, client=self._client())

    def test_the_default_name_is_timestamped(self):
        # One overwritten file only protects you from the last mistake.
        self.assertIn("supabase-", sync.BACKUPS_DIR.name + "supabase-")
        self.assertTrue(str(sync.BACKUPS_DIR).endswith(os.path.join("data", "backups")))

    def test_a_table_longer_than_a_page_arrives_whole(self):
        many = [{"id": i, "name": f"V{i}", "city": "Vancouver",
                 "type": "park", "source": "curated"}
                for i in range(sync.CHUNK + 7)]
        dest = os.path.join(self._tmp.name, "big.db")
        _dest, summary = sync.pull(dest=dest,
                                   client=FakeReadClient({"venues": many}))
        self.assertEqual(summary["venues"], sync.CHUNK + 7)
