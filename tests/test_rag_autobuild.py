"""Whether startup is allowed to build the search index.

Building costs roughly 580MB where serving costs 190MB. On a 512MB instance the
attempt is killed by the host, and because it happens at startup the replacement
worker attempts it again: one missing index becomes a restart loop, and every
request lands on a worker that is about to die. A chat turn then gets an HTML 502
from the proxy rather than an answer, which is how this was first noticed.

So a deployment builds the index during the deploy and forbids it at startup.
A missing index has to degrade honestly instead of taking the app down with it.
"""

import os
import pathlib
import tempfile
import unittest
from src.web import chat as web_chat
from unittest import mock

from src import rag


class AutobuildSwitchTest(unittest.TestCase):
    def test_it_is_allowed_by_default(self):
        # Local development has no memory cap worth worrying about, and an
        # index that builds itself is one less thing to know about.
        with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": ""}):
            self.assertTrue(rag.autobuild_allowed())

    def test_off_forbids_it(self):
        with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": "off"}):
            self.assertFalse(rag.autobuild_allowed())

    def test_anything_unrecognised_still_allows_it(self):
        # Fails towards working. Getting the value wrong should not silently
        # disable the chatbot on somebody's laptop.
        for value in ("on", "yes", "1", "true", "nonsense"):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": value}):
                    self.assertTrue(rag.autobuild_allowed())


class StartupWithNoIndexTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        for name, value in (
                ("INDEX_PATH", pathlib.Path(self._tmp.name) / "index.json"),
                ("RAG_CONFIG_PATH", pathlib.Path(self._tmp.name) / "config.json")):
            patcher = mock.patch.object(rag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # The vectors are a cached module global now, where they used to be a
        # Chroma client. Cleared so a test starts with nothing loaded.
        patcher = mock.patch.object(rag, "_index", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_refuses_to_build_and_says_why(self):
        with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": "off"}), \
             mock.patch.object(rag, "rebuild_index") as built:
            rag.init_index_async()
        built.assert_not_called()
        status = rag.get_status()
        self.assertEqual(status["state"], "error")
        self.assertIn("deploy step", status["error"])

    def test_it_builds_when_allowed(self):
        with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": ""}), \
             mock.patch.object(rag, "rebuild_index") as built:
            rag.init_index_async()
        built.assert_called_once()

    def _chat_with_status(self, state):
        """One chat turn with the index in `state`, the agent stubbed out.

        Stubbed because the point is which turns the *gate* lets through, and a
        real one would reach OpenRouter.
        """
        import app as app_module
        app_module.app.config["TESTING"] = True
        with mock.patch.object(rag, "get_status",
                               return_value={"state": state, "error": None,
                                             "chunk_size": 128}), \
             mock.patch.object(web_chat, "handle_message",
                               return_value={"reply": "ok", "workflow": None}):
            return app_module.app.test_client().post(
                "/chatbot", json={"message": "hello"})

    def test_a_build_in_progress_asks_the_parent_to_wait(self):
        # The only state worth refusing on, and only because it lasts seconds.
        reply = self._chat_with_status("indexing")
        self.assertEqual(reply.status_code, 503)
        self.assertIn("error", reply.get_json())

    def test_a_broken_index_costs_the_faq_and_nothing_else(self):
        # The whole bubble used to 503 on this. Workflows and the three tools
        # that never retrieve were taken down by a fault in the one that does,
        # so a parent replanning a day was refused because the FAQ was broken.
        # Retrieval degrades by itself: rag.retrieve returns nothing unless the
        # index is ready, and the prompt then says so.
        reply = self._chat_with_status("error")
        self.assertEqual(reply.status_code, 200)

    def test_an_index_that_never_started_does_not_block_a_turn_either(self):
        reply = self._chat_with_status("not_started")
        self.assertEqual(reply.status_code, 200)


class ModelCacheLocationTest(unittest.TestCase):
    """Where the 86MB embedding model is kept, which decided whether the
    deployed knowledge base worked at all.

    chromadb's default is $HOME/.cache/chroma. A deployment hands only the
    project directory from a build to the running service, so the model the
    build downloaded was not there when a request needed it, and the first
    knowledge-base question re-downloaded 79MB inside the request. gunicorn
    kills a worker at 120s: measured, that question returned an empty 502 after
    127.9s while the same route without retrieval answered in 4.1s.
    """

    def test_the_model_lives_under_the_project_data_directory(self):
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        self.assertTrue(
            pathlib.Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH).is_relative_to(rag.DATA_DIR),
            "the embedding model must be cached inside the project directory, "
            "or a deploy cannot hand it to the running service")

    def test_it_is_not_left_in_the_home_cache(self):
        # The specific default that broke the deployment. Named explicitly so
        # reverting the override fails here rather than 120 seconds into a
        # request on the deployed instance.
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        home_cache = pathlib.Path.home() / ".cache" / "chroma"
        self.assertFalse(
            pathlib.Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH).is_relative_to(home_cache))

    def test_an_absent_model_is_reported_as_absent(self):
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        with mock.patch.object(ONNXMiniLM_L6_V2, "DOWNLOAD_PATH",
                               "/nonexistent-model-dir"):
            self.assertFalse(rag.model_cached())

    def test_the_status_route_says_whether_the_model_is_here(self):
        # Without this the deployed instance could not be asked the one question
        # that mattered, and four fixes were guessed at from outside instead.
        import app as app_module
        app_module.app.config["TESTING"] = True
        body = app_module.app.test_client().get("/rag/status").get_json()
        self.assertIn("model_cached", body)

    def test_the_status_route_does_not_leak_the_path(self):
        # It is public and unauthenticated, so a boolean is the whole answer.
        import app as app_module
        app_module.app.config["TESTING"] = True
        body = app_module.app.test_client().get("/rag/status").get_json()
        self.assertNotIn(str(rag.MODEL_DIR), str(body))


if __name__ == "__main__":
    unittest.main()
