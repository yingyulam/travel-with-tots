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
import re
import threading
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

_embedder = None
_client = None
_status = {"state": "not_started", "chunk_size": DEFAULT_CHUNK_SIZE, "error": None}
_status_lock = threading.Lock()
_index_lock = threading.Lock()


def _get_embedder():
    """The ONNX embedder, warmed so its tokenizer exists.

    It builds the model and tokenizer on first call rather than in __init__, so
    a caller reaching for `.tokenizer` straight away would otherwise find None.
    One embedding of an empty string is the public way to force that.
    """
    global _embedder
    if _embedder is None:
        embedder = ONNXMiniLM_L6_V2()
        embedder([""])
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
    rebuild_index()


def retrieve(query, top_k=TOP_K):
    """Top `top_k` chunks most similar to `query`, filtered by
    MIN_SIMILARITY. `index` here is the citation number for THIS response
    (1..top_k), not the chunk's absolute position in the knowledge base."""
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
