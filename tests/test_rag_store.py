"""Where the vectors live, and why they stopped living in ChromaDB.

Measured on the deployed instance, twice each: get_or_create_collection and
then get_collection both returned in 0.00s at startup on MainThread and never
returned on a gunicorn worker thread. Every knowledge-base question rode to
gunicorn's 120s timeout at ~165MB, without reaching the embedding model at all.
It never reproduced locally under the same gunicorn and the same thread count.

28 chunks is 28 x 384 floats, about 43KB. A database engine, its SQLite file
and its threading model were carrying no weight a list does not, so the store
is a JSON file read once per process. chromadb stays: the ONNX embedder is its.

Retrieval is unchanged and that is asserted rather than assumed: cosine over
the vectors gives the same number Chroma returned as 1 - distance.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from src import rag


class NothingAsksChromaForAnIndexTest(unittest.TestCase):
    def test_the_client_is_gone(self):
        # The two calls that hung, and the client that made them.
        for name in ("_get_client", "_get_collection"):
            self.assertFalse(hasattr(rag, name), f"{name} is back")

    def test_the_embedder_still_comes_from_chromadb(self):
        # Only the vector store left. Removing the package would take the
        # embedder with it and change the vectors.
        import inspect
        self.assertIn("chromadb", inspect.getsource(rag).split("import numpy")[0])


class ScoresMatchChromaTest(unittest.TestCase):
    """Cosine here must equal 1 - distance there, or MIN_SIMILARITY moves."""

    def test_identical_vectors_score_one(self):
        got = rag._similarities([1.0, 0.0], [[1.0, 0.0]])
        self.assertAlmostEqual(float(got[0]), 1.0, places=6)

    def test_orthogonal_vectors_score_zero(self):
        got = rag._similarities([1.0, 0.0], [[0.0, 1.0]])
        self.assertAlmostEqual(float(got[0]), 0.0, places=6)

    def test_length_does_not_change_the_score(self):
        # Cosine, not dot product: a longer chunk vector must not outrank a
        # closer one just for being longer.
        got = rag._similarities([1.0, 0.0], [[5.0, 0.0]])
        self.assertAlmostEqual(float(got[0]), 1.0, places=6)

    def test_a_zero_vector_scores_nothing_rather_than_dividing_by_zero(self):
        got = rag._similarities([1.0, 0.0], [[0.0, 0.0]])
        self.assertEqual(float(got[0]), 0.0)


class RetrieveTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = pathlib.Path(self._tmp.name) / "index.json"
        path.write_text(json.dumps([
            {"text": "about the site", "section": "About",
             "embedding": [1.0, 0.0, 0.0]},
            {"text": "about naps", "section": "Naps",
             "embedding": [0.0, 1.0, 0.0]},
        ]))
        for name, value in (("INDEX_PATH", path), ("_index", None)):
            patcher = mock.patch.object(rag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(rag, "get_status",
                                    return_value={"state": "ready"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _retrieve(self, vector, **kwargs):
        with mock.patch.object(rag, "_embed", return_value=[vector]):
            return rag.retrieve("q", **kwargs)

    def test_the_nearest_chunk_comes_back_first(self):
        got = self._retrieve([1.0, 0.0, 0.0])
        self.assertEqual(got[0]["section"], "About")

    def test_the_better_match_is_ranked_above_the_weaker_one(self):
        # Both clear the threshold here, deliberately. With only one survivor
        # the order cannot be wrong, which let a reversed sort pass.
        got = self._retrieve([0.9, 0.44, 0.0])
        self.assertEqual([s["section"] for s in got], ["About", "Naps"])
        self.assertGreater(got[0]["score"], got[1]["score"])

    def test_citation_numbers_follow_the_ranking(self):
        got = self._retrieve([0.9, 0.44, 0.0])
        self.assertEqual([s["index"] for s in got], [1, 2])

    def test_a_chunk_below_the_threshold_is_dropped(self):
        # The anti-hallucination guard: an off-topic question returns nothing
        # rather than the least-bad chunk.
        self.assertEqual(self._retrieve([0.0, 0.0, 1.0]), [])

    def test_it_returns_at_most_top_k(self):
        self.assertEqual(len(self._retrieve([1.0, 1.0, 0.0], top_k=1)), 1)

    def test_an_empty_index_answers_nothing_rather_than_raising(self):
        with mock.patch.object(rag, "_index", []):
            self.assertEqual(self._retrieve([1.0, 0.0, 0.0]), [])

    def test_a_missing_index_file_reads_as_empty(self):
        with mock.patch.object(rag, "INDEX_PATH",
                               pathlib.Path("/nonexistent/index.json")), \
             mock.patch.object(rag, "_index", None):
            self.assertEqual(rag._load_index(), [])

    def test_every_source_carries_what_the_widget_renders(self):
        for source in self._retrieve([1.0, 0.0, 0.0]):
            for key in ("index", "text", "score", "section"):
                self.assertIn(key, source)


class BuildWritesTheIndexTest(unittest.TestCase):
    def test_it_stores_text_section_and_vector(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = pathlib.Path(tmp.name) / "index.json"
        with mock.patch.object(rag, "INDEX_PATH", path), \
             mock.patch.object(rag, "RAG_CONFIG_PATH",
                               pathlib.Path(tmp.name) / "config.json"), \
             mock.patch.object(rag, "_index", None), \
             mock.patch.object(rag, "chunk_markdown",
                               return_value=[{"text": "t", "section": "S"}]), \
             mock.patch.object(rag, "_embed", return_value=[[0.1, 0.2]]):
            rag.build_index(128)
        stored = json.loads(path.read_text())
        self.assertEqual(stored, [{"text": "t", "section": "S",
                                   "embedding": [0.1, 0.2]}])
        self.assertEqual(rag.get_status()["state"], "ready")


if __name__ == "__main__":
    unittest.main()
