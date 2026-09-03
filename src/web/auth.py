"""Signing up, logging in, logging out."""

import os

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

from src.db import add_parent, get_parent_by_email
from src.web.guards import LOGIN_LIMIT, LOGIN_WINDOW, rate_limited

bp = Blueprint("auth", __name__)

# The shortest password worth calling one. Not a character-class rule: those
# push people towards "Passw0rd!" and a long ordinary phrase is stronger. This
# is the floor, and the login limiter is what makes guessing expensive.
MIN_PASSWORD_LENGTH = 10

# Compared against when the email matches no account, purely so that path costs
# the same as a real check. Hashed from random bytes at import, so no password
# can match it.
_ABSENT_ACCOUNT_HASH = generate_password_hash(os.urandom(16).hex())

@bp.route("/signup", methods=["GET", "POST"])
@rate_limited(LOGIN_LIMIT, LOGIN_WINDOW)
def signup():
    """Create a parent account. Children are added afterward from the dashboard."""
    if request.method == "POST":
        form = request.form
        required = ("parent_name", "email", "password", "confirm_password")
        if any(not form.get(field, "").strip() for field in required):
            flash("Please fill in every field.")
            return render_template("signup.html", form=form)
        if len(form["password"]) < MIN_PASSWORD_LENGTH:
            flash(f"Please use a password of at least {MIN_PASSWORD_LENGTH} "
                  "characters.")
            return render_template("signup.html", form=form)
        if form["password"] != form["confirm_password"]:
            flash("Passwords do not match.")
            return render_template("signup.html", form=form)
        email = form["email"].strip().lower()
        if get_parent_by_email(email) is not None:
            flash("An account with this email already exists.")
            return render_template("signup.html", form=form)

        parent_id = add_parent(
            email, generate_password_hash(form["password"]),
            name=form["parent_name"].strip())
        session["parent_id"] = parent_id
        return redirect(url_for("account.dashboard"))

    return render_template("signup.html", form={})


@bp.route("/login", methods=["GET", "POST"])
@rate_limited(LOGIN_LIMIT, LOGIN_WINDOW)
def login():
    """Log an existing parent in.

    The hash is checked even when no such account exists, against a throwaway
    one. Skipping it made a wrong email answer measurably faster than a wrong
    password, which is enough to sort real addresses from invented ones.
    """
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        parent = get_parent_by_email(email)
        stored = parent["password_hash"] if parent else _ABSENT_ACCOUNT_HASH
        if not check_password_hash(stored, password) or parent is None:
            flash("Incorrect email or password.")
            return render_template("login.html", email=email)
        session["parent_id"] = parent["id"]
        return redirect(url_for("account.dashboard"))

    return render_template("login.html", email="")


@bp.route("/logout", methods=["POST"])
def logout():
    """Log the current parent out.

    POST rather than GET, which is what makes SameSite=Lax cover it: Lax still
    sends the cookie on a cross-site top-level GET, so as a GET this was a link
    or an <img> on any page that logged a parent out.
    """
    session.clear()
    return redirect(url_for("home"))
