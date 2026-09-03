"""Travel with Tots -- Flask entry point.

Creates the app, configures it, registers one blueprint per subject, and holds
the handful of things that are the app's rather than any one subject's: the
error handler, the security headers, the two context processors every template
reads, and the landing page.

Every feature route lives in src/web/, and the logic those routes call lives in
src/components, src/workflows and the domain modules. See src/web/__init__.py.
"""

import os
import traceback

from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from src.ai import rag
from src.ai.agents import ALLOWED_CHAT_MODELS, DEFAULT_MODEL
from src.dates import compute_age
from src.db import get_children
from src.schema import init_db
from src.web import (account, auth, chat, devpages, guards, places, planning,
                     settings, trip, venues)

app = Flask(__name__)

# Signs the session cookie, which holds only session["parent_id"]. A known key
# therefore means anyone can mint a cookie naming any parent, admin included,
# with no password: authentication bypass rather than mere tampering. It used
# to fall back to a literal committed to this repo, so following the documented
# setup (cp .env.example .env, which never mentioned SECRET_KEY) shipped that
# known key. No fallback now, and no default worth having: one that works is
# one an attacker also has.
try:
    app.secret_key = os.environ["SECRET_KEY"]
except KeyError:
    raise RuntimeError(
        "SECRET_KEY is not set. It signs session cookies, so there is no safe "
        "default. Generate one and add it to .env:\n"
        "  python3 -c \"import secrets; print('SECRET_KEY=' + secrets.token_hex(32))\" >> .env"
    ) from None

# Over HTTPS the session cookie should never be sent in clear, and a deployment
# is HTTPS-only while local development is not. Off by default so `flask run` on
# http://localhost still logs you in; render.yaml turns it on.
#
# SameSite=Lax is also what stands in for CSRF tokens: a browser will not send
# this cookie on a cross-site POST, so a form on somebody else's page cannot act
# as a logged-in parent. That is why logout is a POST rather than a GET -- Lax
# *does* send the cookie on a cross-site top-level GET, so a link or an <img>
# could otherwise log a parent out.
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
    # Every route reads a body somebody else sent. Unset, Flask will buffer a
    # body of any size, which on a 512MB instance is a one-request memory kill.
    # Larger than any real submit here: the biggest is the review page posting
    # a page of hours for a batch of candidates.
    MAX_CONTENT_LENGTH=256 * 1024,
)


# Each blueprint owns one subject's routes. Registered here rather than
# discovered, so the set is explicit and a broken import is loud.
app.register_blueprint(account.bp)
app.register_blueprint(auth.bp)
app.register_blueprint(chat.bp)
app.register_blueprint(devpages.bp)
app.register_blueprint(places.bp)
app.register_blueprint(planning.bp)
app.register_blueprint(settings.bp)
app.register_blueprint(trip.bp)
app.register_blueprint(venues.bp)

# Create the SQLite tables (data/app.db) on startup if they don't exist yet.
# A no-op when the data source is Supabase: the tables are already there.
init_db()

# Chunk + embed the knowledge base in the background; the chatbot widget
# polls /rag/status and shows a progress animation until this finishes.
rag.init_index_async()


@app.errorhandler(Exception)
def _json_endpoints_answer_json(error):
    """Keep a JSON endpoint answering JSON, even when it fails.

    The chat widget, the planner and find-nearby all parse every reply. A Flask
    HTML error page therefore surfaced as the browser's own parse message --
    Safari says "The string did not match the expected pattern" -- which told a
    parent nothing and told the log nothing either. The routes catch the errors
    they expect; this is for the ones nobody predicted, which are precisely the
    ones worth seeing.

    HTML requests keep Flask's normal behaviour, so an ordinary page still gets
    an ordinary error page.
    """
    unexpected = not isinstance(error, HTTPException)
    if unexpected:
        traceback.print_exc()
    if not request.is_json:
        return error if isinstance(error, HTTPException) else ("Server error", 500)
    if unexpected:
        return jsonify({"error": "Something went wrong on the server."}), 500
    return jsonify({"error": error.description}), error.code


@app.after_request
def _security_headers(response):
    """Headers a browser needs in order to defend the page.

    No Content-Security-Policy yet: the templates carry inline handlers and
    styles, so a useful policy would need 'unsafe-inline', which is a policy
    that mostly is not one. Worth doing properly rather than for show.
    """
    # This app has no reason to be framed, and framing is how a click on an
    # invisible overlay becomes a click on "Delete trip".
    response.headers.setdefault("X-Frame-Options", "DENY")
    # Stop a browser guessing a content type: a stored venue note sniffed as
    # HTML would run as HTML.
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # A trip page's URL says where a family is going. Do not hand it to every
    # site they click through to.
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.context_processor
def inject_current_parent():
    """Make the logged-in parent (and their children, with computed age)
    available to every template, so the masthead auth-status link and the
    child pickers work without threading them through each render_template
    call."""
    parent = guards.current_parent()
    children = []
    if parent:
        for child in get_children(parent["id"]):
            years, months = compute_age(child["date_of_birth"])
            children.append({
                "id": child["id"],
                "name": child["name"],
                "date_of_birth": child["date_of_birth"],
                "age_years": years,
                "age_months": months,
            })
    return {"current_parent": parent, "current_parent_children": children}


# What the chat widget's dropdown offers. Free first, then the default, so the
# order reads cheapest-first and the checked one is the one that answers.
CHAT_MODEL_LABELS = {
    "nvidia/nemotron-3-super-120b-a12b:free": "Nemotron 3 Super (free)",
    "openai/gpt-4o-mini": "GPT-4o mini (paid)",
}


@app.context_processor
def inject_chat_models():
    """The models the widget may offer, from the server's own allowed set.

    The dropdown used to be a hand-written list in the template, and it drifted:
    it still defaulted to `openrouter/free` after the server default had moved,
    so every page load selected a model nobody had chosen. Rendering it from
    ALLOWED_CHAT_MODELS means adding or removing one is a single edit.
    """
    offered = [m for m in CHAT_MODEL_LABELS if m in ALLOWED_CHAT_MODELS]
    return {"chat_models": offered,
            "chat_model_labels": CHAT_MODEL_LABELS,
            "default_chat_model": DEFAULT_MODEL}


@app.route("/")
def home():
    """Marketing landing page."""
    return render_template("index.html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8016, debug=True)
