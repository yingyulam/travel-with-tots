"""How the first admin account comes into being.

It used to be seeded with the literal password "admin1234", written in
src/db.py. That is a credential everyone who can read the repository also has,
and it opens /settings, which can change the data source and rewrite the
chatbot's prompt. The same argument SECRET_KEY already carries in this
codebase: a default that works is one an attacker also has.
"""

import os
import tempfile
import unittest
from contextlib import closing
from unittest import mock

from werkzeug.security import check_password_hash

import src.db as db


class _FreshDatabase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = mock.patch.object(db, "DB_PATH",
                                    os.path.join(self._tmp.name, "app.db"))
        patcher.start()
        self.addCleanup(patcher.stop)
        with closing(db.connect_sqlite()) as conn:
            db.create_schema(conn)

    def _seed(self, **env):
        with mock.patch.dict(os.environ, env, clear=False):
            for key in ("ADMIN_EMAIL", "ADMIN_PASSWORD"):
                if key not in env:
                    os.environ.pop(key, None)
            with closing(db.connect_sqlite()) as conn:
                db._seed_admin(conn)


class SeedingTest(_FreshDatabase):
    def test_no_password_means_no_admin(self):
        # The important one. Rather than fall back to something published, a
        # database with no ADMIN_PASSWORD simply has nobody who can reach
        # /settings, and says so on startup.
        self._seed()
        self.assertEqual(db.admins_with_password(db.RETIRED_PASSWORD), [])
        with closing(db.connect_sqlite()) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM parents WHERE is_admin = 1").fetchone()[0]
        self.assertEqual(count, 0)

    def test_the_old_published_password_is_never_created(self):
        self._seed(ADMIN_EMAIL="me@example.com", ADMIN_PASSWORD="a-long-password")
        parent = db.get_parent_by_email("me@example.com")
        self.assertTrue(parent["is_admin"])
        self.assertFalse(
            check_password_hash(parent["password_hash"], db.RETIRED_PASSWORD))
        self.assertTrue(
            check_password_hash(parent["password_hash"], "a-long-password"))

    def test_an_email_is_stored_lowercased(self):
        # get_parent_by_email lowercases what it is given, so an admin seeded
        # with capitals could never be found again.
        self._seed(ADMIN_EMAIL="Me@Example.COM", ADMIN_PASSWORD="a-long-password")
        self.assertIsNotNone(db.get_parent_by_email("me@example.com"))

    def test_an_existing_admin_is_left_alone(self):
        # Setting the variable must not reset a password somebody chose, which
        # is what would happen on every restart otherwise.
        self._seed(ADMIN_EMAIL="me@example.com", ADMIN_PASSWORD="the-first-one")
        self._seed(ADMIN_EMAIL="me@example.com", ADMIN_PASSWORD="a-different-one")
        parent = db.get_parent_by_email("me@example.com")
        self.assertTrue(check_password_hash(parent["password_hash"], "the-first-one"))


class PromotionTest(_FreshDatabase):
    """The route to prefer: sign up in the app, then be promoted.

    Nothing on this path handles a password, which is the point. A script that
    sets one is a script that has one, and the signup form already validates
    what a parent chose.
    """

    def test_promoting_leaves_the_password_alone(self):
        db.add_parent("me@example.com", db.generate_password_hash("my-own-password"))
        db.make_admin("me@example.com")
        parent = db.get_parent_by_email("me@example.com")
        self.assertTrue(parent["is_admin"])
        self.assertTrue(
            check_password_hash(parent["password_hash"], "my-own-password"))

    def test_promoting_an_account_that_does_not_exist_says_so(self):
        # Rather than creating one, which would put an account with no chosen
        # password back in the database.
        self.assertIsNone(db.make_admin("nobody@example.com"))

    def test_a_promotion_can_be_undone(self):
        db.add_parent("me@example.com", db.generate_password_hash("x"))
        db.make_admin("me@example.com")
        db.revoke_admin("me@example.com")
        self.assertFalse(db.get_parent_by_email("me@example.com")["is_admin"])

    def test_admins_can_be_listed(self):
        db.add_parent("a@example.com", db.generate_password_hash("x"))
        db.add_parent("b@example.com", db.generate_password_hash("x"))
        db.make_admin("b@example.com")
        self.assertEqual([row["email"] for row in db.list_admins()],
                         ["b@example.com"])

    def test_an_address_is_matched_however_it_is_typed(self):
        db.add_parent("me@example.com", db.generate_password_hash("x"))
        db.make_admin("  Me@Example.COM  ")
        self.assertTrue(db.get_parent_by_email("me@example.com")["is_admin"])


class DeletingASeededAccountTest(_FreshDatabase):
    """What to do with the demo and admin logins a clone brought along.

    Their passwords are published, and nobody uses the accounts, so deleting
    beats choosing new passwords for them.
    """

    def test_it_removes_the_account(self):
        db.add_parent("demo@travelwithtots.app", db.generate_password_hash("demo1234"))
        db.delete_parent("demo@travelwithtots.app")
        self.assertIsNone(db.get_parent_by_email("demo@travelwithtots.app"))

    def test_their_children_go_too(self):
        parent = db.add_parent("demo@travelwithtots.app",
                               db.generate_password_hash("demo1234"))
        db.add_child(parent, "Sam", "2024-01-01")
        db.delete_parent("demo@travelwithtots.app")
        self.assertEqual(db.get_children(parent), [])

    def test_deleting_something_absent_says_so(self):
        self.assertIsNone(db.delete_parent("nobody@example.com"))


class SetAdminPasswordTest(_FreshDatabase):
    """The path that fixes a database seeded before any of this changed.

    _seed_admin cannot do it: it skips as soon as an admin exists, which is
    exactly the case that needs fixing.
    """

    def test_it_creates_an_admin_that_is_not_there(self):
        _id, action = db.set_admin_password("new@example.com", "a-long-password")
        self.assertEqual(action, "created")
        self.assertTrue(db.get_parent_by_email("new@example.com")["is_admin"])

    def test_it_replaces_a_known_password(self):
        db.add_parent("old@example.com",
                      db.generate_password_hash(db.RETIRED_PASSWORD))
        _id, action = db.set_admin_password("old@example.com", "a-long-password")
        self.assertEqual(action, "updated")
        parent = db.get_parent_by_email("old@example.com")
        self.assertFalse(
            check_password_hash(parent["password_hash"], db.RETIRED_PASSWORD))
        self.assertTrue(parent["is_admin"])

    def test_it_promotes_an_ordinary_parent(self):
        # How a real person's own account becomes the admin, rather than
        # everyone sharing one called "Admin".
        db.add_parent("parent@example.com", db.generate_password_hash("whatever"))
        db.set_admin_password("parent@example.com", "a-long-password")
        self.assertTrue(db.get_parent_by_email("parent@example.com")["is_admin"])


class KnownPasswordReportTest(_FreshDatabase):
    """What tells you a published credential is still live."""

    def test_it_names_an_admin_still_using_the_old_password(self):
        db.add_parent("stale@example.com",
                      db.generate_password_hash(db.RETIRED_PASSWORD))
        db.set_admin_password("stale@example.com", db.RETIRED_PASSWORD)
        found = [row["email"] for row in db.admins_with_password(db.RETIRED_PASSWORD)]
        self.assertEqual(found, ["stale@example.com"])

    def test_a_changed_password_is_not_reported(self):
        db.set_admin_password("fine@example.com", "a-long-password")
        self.assertEqual(db.admins_with_password(db.RETIRED_PASSWORD), [])

    def test_a_non_admin_is_not_reported(self):
        # It answers "who can reach /settings with a password everybody knows",
        # so an ordinary parent is not the question.
        db.add_parent("ordinary@example.com",
                      db.generate_password_hash(db.RETIRED_PASSWORD))
        self.assertEqual(db.admins_with_password(db.RETIRED_PASSWORD), [])

    def test_a_test_password_is_caught_as_well_as_the_seeded_one(self):
        # The one that was actually missed. A test run against the real
        # database left an admin with the password "pw", and the clone carried
        # it to Supabase; checking only the seeded default reported all clear.
        db.set_admin_password("leftover@example.com", "pw")
        self.assertEqual(db.admins_with_password(db.RETIRED_PASSWORD), [])
        self.assertEqual(db.admins_with_weak_password(),
                         {"leftover@example.com": "pw"})

    def test_a_real_password_is_not_reported(self):
        db.set_admin_password("fine@example.com", "a-long-chosen-password")
        self.assertEqual(db.admins_with_weak_password(), {})

    def test_the_seeded_defaults_are_both_covered(self):
        for password in (db.RETIRED_PASSWORD, "demo1234"):
            with self.subTest(password=password):
                self.assertIn(password, db.WEAK_PASSWORDS)


if __name__ == "__main__":
    unittest.main()
