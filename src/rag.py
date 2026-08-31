"""RAG (retrieval-augmented generation) pipeline for the website chatbot.

Chunks data/knowledge_base.md, embeds each chunk, and stores them in a
persistent ChromaDB collection. The chatbot retrieves only the top few chunks
relevant to a question, instead of the whole file.

**Embedding happens over the API, not in this process**, and the reason is
proportion. Running all-MiniLM-L6-v2 locally cost 208MB resident for an 86MB
model, to search 12KB of text in 28 chunks -- and loading it is CPU-heavy graph
work that never finished inside a request on a shared-CPU host, so the worker was
killed at 120s and the model was never cached. The knowledge-base chat was
unusable while every other path worked, because only this one retrieves.

What was measured before changing it (Step 0 of the plan), on this knowledge base
against the previous model:

* the relevant/off-topic score gap **widened**, 0.201 to 0.229, so
  MIN_SIMILARITY needed no retuning;
* top chunks were the same or better -- "what is Travel with Tots" now returns
  the About section rather than an FAQ aside;
* a query costs one round trip, 0.37-0.51s, against a chat turn of several
  seconds;
* resident memory falls from 424MB to about 296MB.

Everything else is unchanged: the chunking, ChromaDB, cosine similarity, the
citation numbering, the configurable chunk size.
"""

import hashlib
import json
import os
import re
import threading
from pathlib import Path

import chromadb
import requests
import tiktoken
from chromadb.errors import NotFoundError

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KNOWLEDGE_BASE_PATH = DATA_DIR / "knowledge_base.md"
CHROMA_DIR = DATA_DIR / "chroma"
RAG_CONFIG_PATH = DATA_DIR / "rag_config.json"

COLLECTION_NAME = "knowledge_base"

# OpenRouter serves an OpenAI-shaped embeddings endpoint. It is not in their
# model list and is not documented alongside the chat models, so it was verified
# against the live API with this project's own key before being relied on.
EMBEDDINGS_URL = "https://openrouter.ai/api/v1/embeddings"
EMBEDDING_MODEL_NAME = "openai/text-embedding-3-small"

# The encoding text-embedding-3-small actually uses, so a chunk's token count is
# now measured against the model doing the embedding. The previous count came
# from a different model's tokenizer and was capped at 256.
TOKEN_ENCODING = "cl100k_base"

DEFAULT_CHUNK_SIZE = 128
TOP_K = 3

# Kept at 0.25 because it was measured rather than assumed: on this knowledge
# base the least-similar relevant match scores 0.391 and the most-similar
# off-topic one 0.162, so the threshold sits in a 0.229-wide gap. Re-measure
# before changing the embedding model -- this number belongs to a model, not to
# the app.
MIN_SIMILARITY = 0.25

# Long enough that a chunk this size never times out, short enough that a stalled
# call cannot hold a worker the way an unbounded one did.
REQUEST_TIMEOUT_SECONDS = 30

_encoding = None
_client = None
_status = {"state": "not_started", "chunk_size": DEFAULT_CHUNK_SIZE, "error": None}
_status_lock = threading.Lock()
_index_lock = threading.Lock()


class EmbeddingError(Exception):
    """Raised when the embeddings API cannot be reached or answers unusably."""


# How many texts to send per request. The old value of 8 existed because ONNX
# Runtime sized its allocator arena to the batch and kept it; over HTTP the
# pressure is the opposite way round, since each request is a round trip. The
# whole knowledge base is 28 chunks, so this sends it in one.
EMBED_BATCH = 64


def _embed(texts):
    """Embeddings for `texts`, in order, as plain lists Chroma can store.

    Raises EmbeddingError rather than returning something half-formed. Callers
    already handle it: `build_index` records the failure in its status, and
    `agent.answer_faq_tool` catches RequestException, so a parent is told the
    knowledge base is unavailable instead of getting a 500.

    Order matters and is asserted, not assumed: the vectors are zipped back
    against the chunks that produced them, so a reordered response would attach
    every citation to the wrong text -- wrong answers, no error.
    """
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise EmbeddingError("OPENROUTER_API_KEY is not set")

    out = []
    for start in range(0, len(texts), EMBED_BATCH):
        batch = texts[start:start + EMBED_BATCH]
        try:
            response = requests.post(
                EMBEDDINGS_URL,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": EMBEDDING_MODEL_NAME, "input": batch},
                timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            data = response.json()["data"]
        except requests.exceptions.RequestException as e:
            raise EmbeddingError(f"{type(e).__name__}: {e}") from None
        except (ValueError, KeyError) as e:
            raise EmbeddingError(f"unusable reply: {type(e).__name__}: {e}") from None
        if len(data) != len(batch):
            raise EmbeddingError(
                f"asked for {len(batch)} embeddings, got {len(data)}")
        # The API returns an index per item; sort on it rather than trusting the
        # order it happened to arrive in.
        out.extend(item["embedding"] for item in
                   sorted(data, key=lambda d: d.get("index", 0)))
    return out


def _get_encoding():
    """The tokenizer, loaded once. Costs about 46MB resident, measured."""
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding(TOKEN_ENCODING)
    return _encoding


def _token_count(text):
    """How many tokens `text` is, for the model that embeds it.

    Real tokens now, from the encoding text-embedding-3-small uses. The previous
    count came from a different model's tokenizer and was capped at 256.

    Not approximated by character count, and that was measured rather than
    assumed: `len(text) // 4` was off by up to 75% on the sentences in this
    knowledge base, 11 of 76 by more than 20%, and it moved chunk boundaries
    (29 chunks against 28). Chunking is the one thing this feeds, so being
    wrong here reshapes what gets embedded.
    """
    return len(_get_encoding().encode(text))


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
    try:
        text = KNOWLEDGE_BASE_PATH.read_text()
        chunks = chunk_markdown(text, chunk_size)

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
        _set_status(state="ready", chunk_size=chunk_size, error=None)
    except Exception as e:
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

    On by default, and it can be now. It was forced off in the deployment
    because building meant loading a 208MB model on a 512MB instance: the
    attempt was killed, the replacement worker tried again, and one missing
    index became a restart loop. Over the API a build is one request and about a
    second, so there is nothing left to protect against.

    The variable stays as an override, because "do not build here" is still a
    reasonable thing to be able to say.
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
            "No search index, and RAG_AUTOBUILD=off so one was not built here."))
        print("No search index. Unset RAG_AUTOBUILD to build it at startup.")
        return
    rebuild_index()


def retrieve(query, top_k=TOP_K):
    """Top `top_k` chunks most similar to `query`, filtered by
    MIN_SIMILARITY. `index` here is the citation number for THIS response
    (1..top_k), not the chunk's absolute position in the knowledge base."""
    if get_status()["state"] == "error" and autobuild_allowed():
        # One retry, here rather than at startup. Building the index is now a
        # single API call, so a network blip during boot used to leave the
        # chatbot answering "unavailable" for the life of the container while
        # everything else worked. Only from "error": "indexing" is a build
        # already running, and starting a second would embed the same chunks
        # twice.
        print("No search index; rebuilding on demand.")
        rebuild_index()

    if get_status()["state"] != "ready":
        return []
    collection = _get_collection()
    if collection.count() == 0:
        return []

    query_embedding = _embed([query])
    results = collection.query(
        query_embeddings=query_embedding, n_results=min(top_k, collection.count()))

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
