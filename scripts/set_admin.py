"""Manage admin accounts on whichever database is selected.

The normal route, and the one to prefer:

    1. Sign up through the app with your own email and password
    2. python3 scripts/set_admin.py promote you@example.com

That never handles a password. You choose it in the signup form, which already
enforces a length, and nothing here ever sees it -- a script that sets a
password is a script that has one.

Everything else:

    python3 scripts/set_admin.py list
    python3 scripts/set_admin.py revoke  someone@example.com
    python3 scripts/set_admin.py delete  demo@travelwithtots.app
    python3 scripts/set_admin.py password you@example.com   # locked out only

`password` prompts without echoing and is the fallback for a deployment nobody
can log into. It is last for a reason.

Acts on the backend the /settings dropdown selects, so this is how a live
Supabase deployment is fixed. DB_BACKEND overrides that:

    DB_BACKEND=supabase python3 scripts/set_admin.py list
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.store import db  # noqa: E402

MIN_LENGTH = 10


def _warn_about_known_passwords():
    """Say plainly whether any admin can be logged into by guessing.

    Printed after every command, because the answer changes as accounts are
    fixed and it is the one thing worth knowing before deploying. It covers the
    seeded defaults and the throwaway values tests use: a test that once ran
    against the real database left an admin with the password "pw", and the
    clone carried it to Supabase, where checking only the seeded default would
    have reported everything fine.
    """
    weak = db.admins_with_weak_password()
    if weak:
        print("\n  WARNING: these admin accounts have guessable passwords:")
        for email, password in weak.items():
            print(f"    {email}  ({password!r})")
        print("  Delete them, or give them a password of their own.")
    else:
        print("\n  No admin has a guessable password.")


def _list():
    admins = db.list_admins()
    if not admins:
        print("No admin accounts. Sign up in the app, then promote yourself.")
    for row in admins:
        print(f"  {row['email']}")
    return 0


def _promote(email):
    if db.make_admin(email) is None:
        print(f"No account for {email}. Sign up in the app first, then run "
              "this again.")
        return 1
    print(f"{email} is now an admin.")
    return 0


def _revoke(email):
    if db.revoke_admin(email) is None:
        print(f"No account for {email}.")
        return 1
    print(f"{email} is no longer an admin.")
    return 0


def _delete(email):
    if db.delete_parent(email) is None:
        print(f"No account for {email}.")
        return 1
    print(f"Deleted {email}, and their children and trips with them.")
    return 0


def _password(email):
    password = getpass.getpass(f"New password for {email}: ")
    if len(password) < MIN_LENGTH:
        print(f"Too short: use at least {MIN_LENGTH} characters.")
        return 1
    if password != getpass.getpass("Again: "):
        print("Those do not match.")
        return 1
    _id, action = db.set_admin_password(email, password)
    print(f"{action} {email} as an admin.")
    return 0


COMMANDS = {"promote": _promote, "revoke": _revoke, "delete": _delete,
            "password": _password}


def main(argv):
    if not argv or argv[0] not in ("list", *COMMANDS):
        print(__doc__)
        return 1
    if argv[0] == "list":
        code = _list()
    elif len(argv) != 2:
        print(__doc__)
        return 1
    else:
        code = COMMANDS[argv[0]](argv[1])
    _warn_about_known_passwords()
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
