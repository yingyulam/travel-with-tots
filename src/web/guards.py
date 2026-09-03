"""Who is asking, and how often they may ask.

Shared by every blueprint, so it holds nothing about any one of them. Callers
reach these module-qualified (`guards.current_parent()`) rather than importing
the names: a test that patches `current_parent` then has one target for the
whole app instead of one per blueprint.
"""

import os
from functools import wraps

from flask import (flash, jsonify, make_response, redirect, request, session,
                   url_for)

from src.web import ratelimit
from src.db import get_parent

# Whether X-Forwarded-For can be believed. True only behind a proxy that sets
# it; off a proxy it is a header the caller wrote, and trusting it would let one
# attacker present as an unlimited number of addresses. render.yaml turns it on.
TRUST_PROXY = os.environ.get(
    "TRUST_PROXY", "").strip().lower() in ("1", "true", "yes")

# What one caller may do per minute on the routes that cost money. Generous
# against real use -- a parent plans a day a few times, not sixty -- and low
# enough that a script cannot spend a month's API budget before anyone notices.
CHAT_LIMIT, CHAT_WINDOW = 20, 60
PLAN_LIMIT, PLAN_WINDOW = 12, 60
LOOKUP_LIMIT, LOOKUP_WINDOW = 40, 60
# Tighter, because guessing repeatedly is the whole attack on this one.
LOGIN_LIMIT, LOGIN_WINDOW = 8, 300


def current_parent():
    """The logged-in parent's row, or None if no one is logged in."""
    parent_id = session.get("parent_id")
    return get_parent(parent_id) if parent_id else None


def login_required(view):
    """Redirect anonymous visitors to the login page instead of the view."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if current_parent() is None:
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    """Redirect logged-in non-admins away from admin-only pages. Stack under
    @login_required, which already handles anonymous visitors."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_parent()["is_admin"]:
            flash("You don't have access to that page.")
            return redirect(url_for("account.dashboard"))
        return view(*args, **kwargs)
    return wrapped


def rate_limited(limit, window):
    """Cap how often one caller may reach this view.

    Keyed on the parent when there is one and the address otherwise, so a
    household behind one address is not throttled by a stranger, and a logged-in
    parent is not punished for sharing an office with one.

    Answers 429 with Retry-After. JSON for the JSON routes, so the widget can
    say something rather than failing on a parse error; for a form POST, the
    flash-and-redirect the rest of the app already uses, so the parent lands
    back on the page they submitted with the reason on it.
    """
    def decorate(view):
        bucket = ratelimit.RateLimit(limit, window)

        @wraps(view)
        def wrapped(*args, **kwargs):
            if not _rate_limits_on():
                return view(*args, **kwargs)
            parent = current_parent()
            key = f"parent:{parent['id']}" if parent else f"ip:{_caller_address()}"
            try:
                bucket.check(key)
            except ratelimit.TooMany as e:
                message = (f"That's a lot of requests. Please wait "
                           f"{e.retry_after} seconds and try again.")
                if request.is_json:
                    response = make_response(jsonify({"error": message}), 429)
                else:
                    flash(message)
                    response = make_response(
                        redirect(request.referrer or url_for("home")))
                response.headers["Retry-After"] = str(e.retry_after)
                return response
            return view(*args, **kwargs)
        return wrapped
    return decorate


def _rate_limits_on():
    """Whether to enforce the limits. Read per request, not at import.

    Off in the test suite, which `tests/__init__.py` sets, for the same reason
    it pins the database: every test file runs in one process, so the buckets
    are shared across the whole run. Nineteen posts to the planning routes in
    three seconds is one caller as far as a limiter is concerned, so the later
    tests were answered 429 by a limit meant for a stranger with a script.

    Default on, and off only when something says "off" out loud, so forgetting
    to set it leaves the limits in place rather than removing them.
    """
    return os.environ.get("RATE_LIMITS", "").strip().lower() != "off"


def _caller_address():
    """The client's address, trusting proxy headers only when told to.

    X-Forwarded-For is set by whoever spoke to us, so off a proxy it is simply
    a header the caller chose and trusting it would let one attacker look like
    thousands. Render sits in front of this and sets it, so TRUST_PROXY says
    when to believe it. `access_route[-1]` rather than `[0]`: the last entry is
    the one our own proxy added, and the earlier ones are still the caller's
    to invent.
    """
    if TRUST_PROXY and request.access_route:
        return request.access_route[-1]
    return request.remote_addr or "unknown"
