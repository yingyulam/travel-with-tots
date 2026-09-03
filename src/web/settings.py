"""Admin settings: which database, what the chatbot knows, its prompt.

The knowledge base and the prompt are edited here rather than only on disk, so
a change to what the chatbot says does not need a deploy. Both writes go
through rag, which re-chunks and re-embeds.
"""

from contextlib import closing

from flask import (Blueprint, flash, jsonify, redirect, render_template,
                   request, url_for)

from src.store import db, postgres, supabase_sync
from src.ai import rag
from src.ai.agents import WEBSITE_CHATBOT_PROMPT_PATH, reload_website_chatbot_prompt
from src.form_helpers import clamp_int
from src.web.guards import admin_required, login_required

bp = Blueprint("settings", __name__)

@bp.route("/settings")
@login_required
@admin_required
def settings():
    """Edit the chatbot's knowledge base and system prompt."""
    knowledge_base = rag.KNOWLEDGE_BASE_PATH.read_text()
    with open(WEBSITE_CHATBOT_PROMPT_PATH) as f:
        prompt = f.read()
    return render_template(
        "settings.html", knowledge_base=knowledge_base, prompt=prompt,
        data_source=supabase_sync.active_source(),
        data_sources=supabase_sync.SOURCES,
        # What is actually serving, which is not always what the dropdown says:
        # the dropdown lives in a file, and a host with an ephemeral disk loses
        # it on every deploy while DB_BACKEND keeps pinning the real backend.
        effective_source=db.effective_backend(),
        pinned_backend=db.backend_pinned_by_env(),
        supabase_configured=_supabase_configured(),
        supabase_db_url_set=bool(supabase_sync.db_url()),
        backend_error=db.LAST_BACKEND_ERROR,
        supabase_ddl=supabase_sync.postgres_ddl(),
        supabase_runtime_ddl=supabase_sync.postgres_runtime_ddl())


def _supabase_configured():
    """Whether both Supabase credentials are set, without revealing either."""
    try:
        supabase_sync.credentials()
    except supabase_sync.SyncError:
        return False
    return True


def _supabase_unreachable():
    """Why a connection to Supabase failed, or None if one can be opened.

    Run before the dropdown is allowed to switch, so an unusable connection
    string is a sentence on this page rather than a warning banner on every
    other one.
    """
    url = supabase_sync.db_url()
    if not url:
        return "SUPABASE_DB_URL is not set in .env."
    try:
        with closing(postgres.connect(url)) as conn:
            conn.execute("SELECT 1").fetchone()
    except Exception as e:                                      # noqa: BLE001
        # Broad on purpose: a bad host, a bad password, a missing driver and a
        # firewall all mean the same thing here, and all of them should reach
        # the admin as a sentence.
        return f"{type(e).__name__}: {postgres.first_line(e)}"
    return None


@bp.route("/settings/knowledge-base", methods=["POST"])
@login_required
@admin_required
def save_knowledge_base():
    """Save the chatbot's knowledge base and re-chunk/re-embed it in the background."""
    content = request.form.get("content", "").replace("\r\n", "\n")
    rag.KNOWLEDGE_BASE_PATH.write_text(content)
    rag.rebuild_index(rag.get_chunk_size())
    flash("Knowledge base saved. Re-indexing in the background.")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/data-source", methods=["POST"])
@login_required
@admin_required
def save_data_source():
    """Record which backend the app should use."""
    chosen = request.form.get("source", "")
    if chosen == supabase_sync.SUPABASE:
        problem = _supabase_unreachable()
        if problem:
            flash(f"Staying on the local database: Supabase could not be "
                  f"reached. {problem}")
            return redirect(url_for("settings.settings"))
    supabase_sync.set_active_source(chosen)
    if supabase_sync.active_source() == supabase_sync.SUPABASE:
        flash("Data source set to Supabase. Every page now reads and writes "
              "there. Rows written here will not appear in the local database.")
    else:
        flash("Data source set to the local database.")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/clone-to-supabase", methods=["POST"])
@login_required
@admin_required
def clone_to_supabase():
    """Copy every local row into Supabase, skipping what is already there."""
    try:
        summary = supabase_sync.clone()
    except supabase_sync.SyncError as e:
        flash(str(e))
        return redirect(url_for("settings.settings"))
    except Exception as e:                                      # noqa: BLE001
        # Deliberately broad: this talks to somebody else's database over the
        # network, and every failure mode there should reach the admin as a
        # sentence rather than a 500 on the settings page.
        print(f"Clone to Supabase failed: {type(e).__name__}: {e}")
        flash(f"Clone failed: {type(e).__name__}. The details are in the "
              "server log.")
        return redirect(url_for("settings.settings"))

    total = summary.pop("_total", 0)
    parts = [f"{name} {counts['copied']}/{counts['local']}"
             for name, counts in summary.items() if counts["local"]]
    flash(f"Copied {total} row{'s' if total != 1 else ''} to Supabase. "
          + ", ".join(parts) + ".")
    return redirect(url_for("settings.settings"))


@bp.route("/settings/prompt", methods=["POST"])
@login_required
@admin_required
def save_prompt():
    """Save the chatbot's system prompt."""
    content = request.form.get("content", "").replace("\r\n", "\n")
    with open(WEBSITE_CHATBOT_PROMPT_PATH, "w") as f:
        f.write(content)
    reload_website_chatbot_prompt()
    flash("Chatbot prompt saved.")
    return redirect(url_for("settings.settings"))


@bp.route("/rag/status")
def rag_status():
    """Poll-able indexing status, used by the chatbot widget and Chunks page.

    `model_cached` is here because a green index and an answerable question are
    not the same thing: with the embedding model missing from this instance,
    every knowledge-base question is killed downloading one while the status
    still reads "ready". A boolean rather than the path, since this route is
    public.
    """
    return jsonify({**rag.get_status(), "model_cached": rag.model_cached(),
                    # Temporary diagnostic scaffolding. The deployed knowledge
                    # base fails inside a request the worker does not survive,
                    # and the platform logs have to be fetched by hand, which
                    # twice did not happen. The trace carries stage names,
                    # timings and a memory figure, and no paths, so it is safe
                    # on a public route. Remove it once the cause is settled.
                    "trace": rag.read_trace()})


@bp.route("/chunks")
@login_required
@admin_required
def chunks():
    """List every chunk the knowledge base was split into."""
    return render_template(
        "chunks.html", chunks=rag.list_chunks(), chunk_size=rag.get_chunk_size())


@bp.route("/chunks/rerun", methods=["POST"])
@login_required
@admin_required
def chunks_rerun():
    """Re-chunk and re-embed the knowledge base with a different chunk size."""
    data = request.get_json(silent=True) or {}
    chunk_size = clamp_int(data.get("chunk_size"), 20, 2000, rag.DEFAULT_CHUNK_SIZE)
    rag.rebuild_index(chunk_size)
    return jsonify({"status": "started"})
