"""RAG (retrieval-augmented generation) pipeline for the website chatbot.

Chunks data/knowledge_base.md, embeds each chunk, and stores them in a
persistent ChromaDB collection. The chatbot retrieves only the top few chunks
relevant to a question, instead of the whole file.

Embedding runs on **ONNX Runtime**, not PyTorch. Same model and therefore the
same vectors (measured: cosine similarity 1.000000 against sentence-transformers
on this knowledge base), but 286MB of memory instead of 580MB and a 1.1s cold
start instead of 5.0s. That is the difference between fitting a 512MB instance
and being killed before the app finishes booting, and chromadb ships the ONNX
model already, so it drops a dependency rather than adding one.
"""

import hashlib
import json
import os
import re
import resource
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import chromadb
from chromadb.errors import NotFoundError
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KNOWLEDGE_BASE_PATH = DATA_DIR / "knowledge_base.md"
CHROMA_DIR = DATA_DIR / "chroma"
RAG_CONFIG_PATH = DATA_DIR / "rag_config.json"

COLLECTION_NAME = "knowledge_base"
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_CHUNK_SIZE = 128
TOP_K = 3
MIN_SIMILARITY = 0.25

# Where the 86MB ONNX model is kept. chromadb's own default is
# $HOME/.cache/chroma, and a deployment preserves only the project directory
# from a build into the running service, so the model the build downloaded was
# not there when a request needed it. Measured: an absent model is re-downloaded
# (79MB, then extracted) inside the request that asked the question, which
# gunicorn kills at 120s -- a knowledge-base question returned an empty 502
# after 127.9s while the same route without retrieval answered in 4.1s. With the
# model already on disk the same step takes 0.18s.
#
# Under data/ so it travels with the index that is already there, and set as a
# class attribute because the constructor takes only preferred_providers. Not by
# setting HOME, which would move pip's cache with it and would apply only in the
# deployment -- the local/deployed divergence is what hid this in the first
# place, and here both now read the same path.
MODEL_DIR = DATA_DIR / "onnx_models"

if hasattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH"):
    ONNXMiniLM_L6_V2.DOWNLOAD_PATH = str(MODEL_DIR / EMBEDDING_MODEL_NAME)
else:
    # Said out loud, not raised. Without the override the model lands in $HOME
    # and the deployed knowledge base is broken exactly as it was before, which
    # is survivable; raising here would stop the app booting at all and take
    # every working page down with it.
    print("chromadb's ONNXMiniLM_L6_V2 has no DOWNLOAD_PATH to override, so the "
          "embedding model will be cached outside the project directory and "
          "will not survive a deploy.")

_embedder = None
_client = None
_status = {"state": "not_started", "chunk_size": DEFAULT_CHUNK_SIZE, "error": None}
_status_lock = threading.Lock()
_index_lock = threading.Lock()


# Where the stage trace is kept, and how much of it. A file rather than memory
# because the failure this exists for ends in the worker being killed, and an
# in-process ring buffer dies with it. The container outlives the worker, so the
# file is still there to be read afterwards -- which is the whole point: it says
# how far a killed request got.
TRACE_PATH = DATA_DIR / "rag_trace.log"
TRACE_LINES = 40


def _rss_mb():
    """This process's resident memory. The number an out-of-memory kill acts on.

    ru_maxrss is the peak rather than the current figure, which is the one that
    matters: ONNX allocates hard during session init and gives most of it back,
    so a reading taken afterwards misses the spike entirely.
    """
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux kilobytes. Deployment is Linux; the branch is
    # so a local measurement is comparable rather than a thousand times out.
    return peak / (1024 * 1024) if sys.platform == "darwin" else peak / 1024


def _trace(line):
    """Append one line, keeping only the last TRACE_LINES.

    Never raises. A trace that costs a parent their reply would be worse than
    no trace, and this runs on the request path.
    """
    try:
        TRACE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(TRACE_PATH, "a") as f:
            f.write(line + "\n")
        kept = TRACE_PATH.read_text().splitlines()[-TRACE_LINES:]
        TRACE_PATH.write_text("\n".join(kept) + "\n")
    except OSError:
        pass


def read_trace():
    """The last few stage lines, for /rag/status to hand back.

    Deliberately carries no paths: that route is public, and stage names,
    timings and a memory figure say nothing a caller could use.
    """
    try:
        return TRACE_PATH.read_text().splitlines()[-TRACE_LINES:]
    except OSError:
        return []


def _log(stage, since=None):
    """One line of the retrieval path, timed, flushed, and kept on disk.

    Flushed because the worker is killed at 120s and anything still in a stdout
    buffer dies with it. Written to a file for the same reason and one more: the
    deployed logs have to be fetched by hand, and two attempts at that failed,
    so the instance reports its own trace instead.
    """
    elapsed = "" if since is None else f" +{time.monotonic() - since:.2f}s"
    line = (f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {stage}{elapsed} "
            f"rss={_rss_mb():.0f}MB thread={threading.current_thread().name}")
    print(f"[rag] {line}", flush=True)
    _trace(line)
    return time.monotonic()


def _get_embedder():
    """The ONNX embedder, warmed so its tokenizer exists.

    It builds the model and tokenizer on first call rather than in __init__, so
    a caller reaching for `.tokenizer` straight away would otherwise find None.
    One embedding of an empty string is the public way to force that.
    """
    global _embedder
    if _embedder is None:
        # The expensive branch, and only the first caller in a process takes it.
        # model_on_disk is the whole question: False means this is about to
        # download 79MB, and on a deployment that is what the 120s went on.
        # No path in the line: read_trace is served on a public route.
        started = _log(f"embedder: building, model_on_disk={model_cached()}")
        embedder = ONNXMiniLM_L6_V2()
        _log("embedder: constructed", started)
        embedder([""])
        _log("embedder: warmed", started)
        _embedder = embedder
    return _embedder


# How many texts to embed at once. Measured, not guessed: the embedder's own
# default is 32, and ONNX Runtime's allocator sizes its arena to the batch and
# keeps it. Embedding this knowledge base in one batch of 32 left the process
# 224MB heavier and never gave it back; in batches of 8 it stays flat. That is
# the difference between fitting a 512MB instance and not.
EMBED_BATCH = 8


def _embed(texts):
    """Embeddings for `texts`, as plain lists Chroma can store.

    `.tolist()` rather than `list()`: the embedder returns numpy arrays, and
    iterating one yields numpy scalars, which Chroma refuses. tolist() converts
    all the way down to Python floats.

    Batched for memory, not speed. The vectors are unaffected: the tokenizer
    pads every input to a fixed 256 tokens rather than to the longest in the
    batch, so no text can see another.
    """
    embedder = _get_embedder()
    out = []
    for start in range(0, len(texts), EMBED_BATCH):
        out.extend(v.tolist() for v in embedder(texts[start:start + EMBED_BATCH]))
    return out


def _token_count(text):
    """How many tokens `text` is, ignoring padding.

    The tokenizer pads every input to 256, so its ids are always 256 long; the
    attention mask is what says which of those are real. It also truncates
    there, so a sentence longer than 256 tokens counts as 256. That cannot
    change chunking, whose only question is whether a sentence exceeds
    chunk_size, and 256 already exceeds every allowed value.
    """
    return sum(_get_embedder().tokenizer.encode(text).attention_mask)


def model_cached():
    """Whether the embedding model is already on this instance's disk.

    The one fact that could not be seen from outside. A deployment hands only
    the project directory from a build to the running service, so "the index is
    ready" and "a question can be answered" are different claims: with the model
    absent, every knowledge-base question spends its first 79MB downloading one
    and is killed at 120s, while the index itself is perfect. Read off
    chromadb's own attributes rather than rebuilt from ours, so it reports where
    the model is really looked for even if the override above did not take.
    """
    path = getattr(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH", None)
    if path is None:
        return False
    folder = getattr(ONNXMiniLM_L6_V2, "EXTRACTED_FOLDER_NAME", "onnx")
    return (Path(path) / folder / "model.onnx").exists()


def _get_client():
    global _client
    if _client is None:
        CHROMA_DIR.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    return _client


def _get_collection():
    """Always fetched fresh (not cached) -- a rebuild deletes and recreates
    the collection, so a cached handle would go stale."""
    return _get_client().get_or_create_collection(
        COLLECTION_NAME, metadata={"hnsw:space": "cosine"})


def _set_status(**kwargs):
    with _status_lock:
        _status.update(kwargs)


def get_status():
    with _status_lock:
        return dict(_status)


def get_chunk_size():
    if RAG_CONFIG_PATH.exists():
        try:
            return json.loads(RAG_CONFIG_PATH.read_text()).get("chunk_size", DEFAULT_CHUNK_SIZE)
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_CHUNK_SIZE


def _split_sentences(paragraph):
    return [s for s in re.split(r"(?<=[.!?])\s+", paragraph.strip()) if s]


def chunk_markdown(text, chunk_size=DEFAULT_CHUNK_SIZE):
    """Split markdown into chunks of about `chunk_size` tokens each. Never
    crosses a `## ` section boundary and never splits a sentence -- a lone
    sentence longer than `chunk_size` is kept whole rather than cut."""
    sections = re.split(r"(?m)^## ", text)[1:]  # drop the title/preamble before the first "## " heading
    chunks = []
    for section in sections:
        if not section.strip():
            continue
        heading, _, body = section.partition("\n")
        heading = heading.strip()
        sentences = []
        for paragraph in body.split("\n\n"):
            sentences.extend(_split_sentences(paragraph))

        current, current_tokens = [], 0
        for sentence in sentences:
            sentence_tokens = _token_count(sentence)
            if current and current_tokens + sentence_tokens > chunk_size:
                chunks.append({"text": " ".join(current), "section": heading})
                current, current_tokens = [], 0
            current.append(sentence)
            current_tokens += sentence_tokens
        if current:
            chunks.append({"text": " ".join(current), "section": heading})
    return chunks


def build_index(chunk_size=None):
    """Chunk, embed, and store the knowledge base. Blocking -- call
    rebuild_index() from request handlers so it runs in the background."""
    chunk_size = chunk_size or DEFAULT_CHUNK_SIZE
    _set_status(state="indexing", chunk_size=chunk_size, error=None)
    started = _log("build: start")
    try:
        text = KNOWLEDGE_BASE_PATH.read_text()
        # Chunking needs the tokenizer, so this line is where a build first
        # touches the model. The last deployed hang was inside here and looked
        # from outside like a build that never finished.
        chunks = chunk_markdown(text, chunk_size)
        _log(f"build: chunked into {len(chunks)}", started)

        client = _get_client()
        try:
            client.delete_collection(COLLECTION_NAME)
        except (ValueError, NotFoundError):
            # Nothing to delete, which is every first run: an empty data
            # directory locally, and *every* deploy on a host with an ephemeral
            # disk. Chroma raises NotFoundError rather than ValueError, so
            # catching only ValueError left the index unbuilt and the chatbot
            # with no knowledge base at all.
            pass
        collection = client.get_or_create_collection(
            COLLECTION_NAME, metadata={"hnsw:space": "cosine"})

        if chunks:
            embeddings = _embed([c["text"] for c in chunks])
            _log("build: embedded", started)
            collection.add(
                ids=[f"chunk-{i}" for i in range(len(chunks))],
                embeddings=embeddings,
                documents=[c["text"] for c in chunks],
                metadatas=[{"section": c["section"]} for c in chunks],
            )

        RAG_CONFIG_PATH.write_text(json.dumps({
            "chunk_size": chunk_size,
            "kb_hash": hashlib.sha256(text.encode()).hexdigest(),
        }))
        _log("build: ready", started)
        _set_status(state="ready", chunk_size=chunk_size, error=None)
    except Exception as e:
        # The bare except is why a failed build reads as a status rather than a
        # traceback, so the reason has to be printed or it is lost entirely.
        _log(f"build: FAILED {type(e).__name__}: {e}", started)
        _set_status(state="error", error=str(e))


def rebuild_index(chunk_size=None):
    """Rebuild the index in a background thread. No-op if a rebuild is
    already running -- no queueing needed for this scope."""
    if not _index_lock.acquire(blocking=False):
        return

    def _run():
        try:
            build_index(chunk_size)
        finally:
            _index_lock.release()

    threading.Thread(target=_run, daemon=True).start()


def autobuild_allowed():
    """Whether startup may build the index when there isn't one.

    Off in a deployment, and that is the point rather than a convenience.
    Building costs roughly 580MB where serving costs 190MB, so on a 512MB
    instance the attempt is killed by the host -- and because it runs at
    startup, the replacement worker attempts it again. One missing index turns
    into a restart loop, and every request lands on a worker that is about to
    die: the chat turn gets an HTML 502 from the proxy rather than an answer.

    With it off, a missing index degrades honestly instead. The chatbot reports
    that the knowledge base is not ready and every other page keeps working.
    The index is meant to be built during the deploy (see render.yaml), where
    the memory cap does not apply.
    """
    return os.environ.get("RAG_AUTOBUILD", "").strip().lower() != "off"


def init_index_async():
    """Called once at app startup. Skips re-embedding if the on-disk config
    already matches the current knowledge base file's content."""
    if RAG_CONFIG_PATH.exists():
        try:
            config = json.loads(RAG_CONFIG_PATH.read_text())
            text = KNOWLEDGE_BASE_PATH.read_text()
            current_hash = hashlib.sha256(text.encode()).hexdigest()
            if config.get("kb_hash") == current_hash:
                collection = _get_collection()
                if collection.count() > 0 or not text.strip():
                    # Which branch this took answers whether the index came from
                    # the deploy or from this process, and therefore whether the
                    # model was ever downloaded here.
                    _log(f"startup: reusing the index from disk "
                         f"({collection.count()} chunks), "
                         f"model_on_disk={model_cached()}")
                    _set_status(
                        state="ready",
                        chunk_size=config.get("chunk_size", DEFAULT_CHUNK_SIZE),
                        error=None,
                    )
                    return
        except (json.JSONDecodeError, OSError):
            pass
    if not autobuild_allowed():
        _set_status(state="error", error=(
            "No search index, and RAG_AUTOBUILD=off so one was not built here. "
            "It belongs in the deploy step: the build ran but produced nothing, "
            "most likely because it ran out of memory."))
        print("No search index. Build it during deploy, or unset RAG_AUTOBUILD "
              "to build it at startup.")
        return
    # Reached only when autobuild is allowed, which on a deployment means
    # RAG_AUTOBUILD never arrived: the index is then this process's work, not the
    # deploy's, and the 79MB download happens here rather than at build time.
    _log("startup: no reusable index, building one in this process")
    rebuild_index()


def retrieve(query, top_k=TOP_K):
    """Top `top_k` chunks most similar to `query`, filtered by
    MIN_SIMILARITY. `index` here is the citation number for THIS response
    (1..top_k), not the chunk's absolute position in the knowledge base."""
    if get_status()["state"] != "ready":
        return []
    started = _log("retrieve: start")
    # Split finer than reads well, deliberately. On the deployed instance the
    # trace stopped dead between "start" and "collection opened", so the three
    # calls that used to sit inside that gap are each named now: which one it
    # is decides whether this is Chroma's client, its SQLite, or the thread the
    # request runs on.
    client = _get_client()
    _log(f"retrieve: client ready (new={client is not None})", started)
    collection = _get_collection()
    _log("retrieve: collection handle", started)
    count = collection.count()
    _log(f"retrieve: counted {count}", started)
    if count == 0:
        _log("retrieve: empty collection, nothing to search", started)
        return []
    _log("retrieve: collection opened", started)

    query_embedding = _embed([query])
    _log("retrieve: query embedded", started)
    results = collection.query(
        query_embeddings=query_embedding, n_results=min(top_k, collection.count()))
    _log("retrieve: search done", started)

    sources = []
    for i, (doc, meta, dist) in enumerate(zip(
        results["documents"][0], results["metadatas"][0], results["distances"][0]
    )):
        score = 1 - dist
        if score < MIN_SIMILARITY:
            continue
        sources.append({
            "index": i + 1,
            "text": doc,
            "score": score,
            "section": meta.get("section", ""),
        })
    return sources


def list_chunks():
    """Every current chunk, in knowledge-base order, for the Chunks page."""
    collection = _get_collection()
    if collection.count() == 0:
        return []
    data = collection.get()

    chunks = []
    for chunk_id, doc, meta in zip(data["ids"], data["documents"], data["metadatas"]):
        chunks.append({
            "index": int(chunk_id.split("-")[1]) + 1,
            "section": meta.get("section", ""),
            "token_count": _token_count(doc),
            "text": doc,
        })
    chunks.sort(key=lambda c: c["index"])
    return chunks
