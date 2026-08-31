"""Embedding over the API instead of loading a model into this process.

Running all-MiniLM-L6-v2 here cost 208MB resident to search 12KB of text, and
loading it never finished inside a request on a shared CPU -- so the worker was
killed and the model never cached, and the knowledge-base chat was the one path
that never worked. The vectors come from `text-embedding-3-small` now.

Every test here stubs `requests.post`. The suite is offline and must stay that
way: an embedding call is the one thing in this module that would reach the
network, so nothing may leave it unstubbed.
"""

import os
import unittest
from unittest import mock

from src import rag


def _reply(vectors, shuffle=False):
    """An embeddings response, in the shape the API returns."""
    data = [{"index": i, "embedding": v} for i, v in enumerate(vectors)]
    if shuffle:
        data.reverse()
    return mock.Mock(json=lambda: {"data": data}, raise_for_status=lambda: None)


class _Stubbed(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)


class TheEmbeddingCallTest(_Stubbed):
    def test_it_asks_the_right_endpoint_for_the_right_model(self):
        with mock.patch.object(rag.requests, "post",
                               return_value=_reply([[0.1, 0.2]])) as posted:
            out = rag._embed(["hello"])
        url = posted.call_args.args[0]
        body = posted.call_args.kwargs["json"]
        self.assertEqual(url, rag.EMBEDDINGS_URL)
        self.assertEqual(body["model"], rag.EMBEDDING_MODEL_NAME)
        self.assertEqual(body["input"], ["hello"])
        self.assertEqual(out, [[0.1, 0.2]])

    def test_the_key_travels_as_a_bearer_token_and_not_in_the_body(self):
        with mock.patch.object(rag.requests, "post",
                               return_value=_reply([[0.1]])) as posted:
            rag._embed(["hello"])
        headers = posted.call_args.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-key")
        self.assertNotIn("test-key", str(posted.call_args.kwargs["json"]))

    def test_it_sends_a_timeout(self):
        # An unbounded call is how the chat used to hold a worker until gunicorn
        # killed it, and the caller then learned nothing.
        with mock.patch.object(rag.requests, "post",
                               return_value=_reply([[0.1]])) as posted:
            rag._embed(["hello"])
        self.assertEqual(posted.call_args.kwargs["timeout"],
                         rag.REQUEST_TIMEOUT_SECONDS)


class BatchingTest(_Stubbed):
    def test_large_inputs_are_split_across_requests(self):
        texts = [f"chunk {i}" for i in range(45)]
        with mock.patch.object(rag, "EMBED_BATCH", 20), \
             mock.patch.object(rag.requests, "post") as posted:
            posted.side_effect = lambda *a, **k: _reply(
                [[float(i)] for i in range(len(k["json"]["input"]))])
            out = rag._embed(texts)
        self.assertEqual(posted.call_count, 3)          # 20 + 20 + 5
        self.assertEqual(len(out), 45)

    def test_the_whole_knowledge_base_fits_one_request(self):
        # 28 chunks today. Over HTTP the pressure is towards fewer, larger
        # calls -- the opposite of the old batch of 8, which existed because
        # ONNX sized its allocator to the batch.
        self.assertGreaterEqual(rag.EMBED_BATCH, 28)


class OrderTest(_Stubbed):
    def test_vectors_are_returned_in_input_order(self):
        # The vectors are zipped back against the chunks that produced them, so
        # a reordered reply would attach every citation to the wrong text: wrong
        # answers, and no error anywhere to notice.
        with mock.patch.object(rag.requests, "post",
                               return_value=_reply([[1.0], [2.0], [3.0]],
                                                   shuffle=True)):
            out = rag._embed(["a", "b", "c"])
        self.assertEqual(out, [[1.0], [2.0], [3.0]])


class FailureTest(_Stubbed):
    def test_a_transport_failure_becomes_an_embedding_error(self):
        with mock.patch.object(rag.requests, "post",
                               side_effect=rag.requests.exceptions.Timeout("slow")):
            with self.assertRaises(rag.EmbeddingError):
                rag._embed(["hello"])

    def test_an_unusable_body_becomes_an_embedding_error(self):
        broken = mock.Mock(json=lambda: {"nope": 1}, raise_for_status=lambda: None)
        with mock.patch.object(rag.requests, "post", return_value=broken):
            with self.assertRaises(rag.EmbeddingError):
                rag._embed(["hello"])

    def test_a_short_reply_is_refused_rather_than_zipped(self):
        # Two texts, one vector. Silently accepting it would pair the second
        # chunk with nothing, or shift every later one.
        with mock.patch.object(rag.requests, "post", return_value=_reply([[0.1]])):
            with self.assertRaises(rag.EmbeddingError) as caught:
                rag._embed(["one", "two"])
        self.assertIn("got 1", str(caught.exception))

    def test_a_missing_key_says_so_rather_than_calling(self):
        with mock.patch.dict(os.environ, {"OPENROUTER_API_KEY": ""}), \
             mock.patch.object(rag.requests, "post") as posted:
            with self.assertRaises(rag.EmbeddingError) as caught:
                rag._embed(["hello"])
        posted.assert_not_called()
        self.assertIn("OPENROUTER_API_KEY", str(caught.exception))

    def test_the_error_is_one_the_faq_tool_already_catches(self):
        # answer_faq_tool catches TOOL_ERRORS so a parent is told the knowledge
        # base is unavailable rather than getting a 500. If EmbeddingError ever
        # stops being covered, that becomes a 500 with nothing said.
        from src.agent import TOOL_ERRORS
        with mock.patch.object(rag.requests, "post",
                               side_effect=rag.requests.exceptions.Timeout("slow")):
            try:
                rag._embed(["hello"])
            except TOOL_ERRORS:
                return
            except rag.EmbeddingError:
                self.fail("EmbeddingError is not in TOOL_ERRORS")


class TokenCountTest(unittest.TestCase):
    """Counts now come from the encoding the embedding model actually uses."""

    def test_it_counts_real_tokens(self):
        self.assertEqual(rag._token_count(""), 0)
        self.assertGreater(rag._token_count("a sentence of several words"), 3)

    def test_it_grows_with_the_text(self):
        short = rag._token_count("a quiet park")
        long = rag._token_count("a quiet park " * 20)
        self.assertGreater(long, short * 10)

    def test_it_is_not_capped_at_256(self):
        # The old count came from a tokenizer that padded and truncated at 256,
        # so anything longer read as exactly 256.
        self.assertGreater(rag._token_count("word " * 400), 256)

    def test_it_is_nearer_the_truth_than_a_character_estimate(self):
        # Measured on this knowledge base: len(text)//4 was off by up to 75%
        # and moved chunk boundaries, which is why tiktoken earns its 46MB.
        text = "Travel with Tots plans realistic, low-stress single-day outings."
        self.assertNotEqual(rag._token_count(text), len(text) // 4)


class NothingLoadsAModelTest(unittest.TestCase):
    def test_the_local_embedder_is_gone(self):
        # The whole point: no 208MB model in the request path.
        self.assertFalse(hasattr(rag, "_get_embedder"))
        with open(rag.__file__) as f:
            self.assertNotIn("ONNXMiniLM", f.read())


class RetryWhenTheIndexFailedTest(_Stubbed):
    """A build is one API call now, so a blip at boot must not be permanent."""

    def test_retrieve_rebuilds_once_when_the_index_errored(self):
        with mock.patch.object(rag, "get_status",
                               return_value={"state": "error", "error": "blip",
                                             "chunk_size": 128}), \
             mock.patch.object(rag, "rebuild_index") as rebuilt:
            rag.retrieve("anything")
        rebuilt.assert_called_once()

    def test_it_does_not_rebuild_while_a_build_is_running(self):
        # "indexing" means one is already in flight; starting another would
        # embed the same chunks twice.
        with mock.patch.object(rag, "get_status",
                               return_value={"state": "indexing", "error": None,
                                             "chunk_size": 128}), \
             mock.patch.object(rag, "rebuild_index") as rebuilt:
            rag.retrieve("anything")
        rebuilt.assert_not_called()

    def test_it_does_not_rebuild_when_autobuild_is_off(self):
        with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": "off"}), \
             mock.patch.object(rag, "get_status",
                               return_value={"state": "error", "error": "blip",
                                             "chunk_size": 128}), \
             mock.patch.object(rag, "rebuild_index") as rebuilt:
            rag.retrieve("anything")
        rebuilt.assert_not_called()


if __name__ == "__main__":
    unittest.main()
