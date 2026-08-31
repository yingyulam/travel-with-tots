"""Whether startup is allowed to build the search index.

Building used to mean loading a 208MB model on a 512MB instance: the attempt was
killed, the replacement worker tried again, and one missing index became a
restart loop where every request landed on a worker about to die. So a
deployment built the index during the deploy and set RAG_AUTOBUILD=off.

Embedding happens over the API now, so a build is one request and about a
second. Startup building is back on by default and the deploy step is gone. The
switch stays, because "do not build here" is still a reasonable thing to say --
and because a missing index must still degrade honestly rather than pretend.
"""

import os
import pathlib
import tempfile
import unittest
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
                ("CHROMA_DIR", pathlib.Path(self._tmp.name) / "chroma"),
                ("RAG_CONFIG_PATH", pathlib.Path(self._tmp.name) / "config.json")):
            patcher = mock.patch.object(rag, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(rag, "_client", None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_it_refuses_to_build_and_says_why(self):
        with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": "off"}), \
             mock.patch.object(rag, "rebuild_index") as built:
            rag.init_index_async()
        built.assert_not_called()
        status = rag.get_status()
        self.assertEqual(status["state"], "error")
        # Names the switch that caused it, so the state is explicable from the
        # message rather than needing somebody to know this file exists.
        self.assertIn("RAG_AUTOBUILD", status["error"])

    def test_it_builds_when_allowed(self):
        with mock.patch.dict(os.environ, {"RAG_AUTOBUILD": ""}), \
             mock.patch.object(rag, "rebuild_index") as built:
            rag.init_index_async()
        built.assert_called_once()

    def test_the_chatbot_reports_the_state_rather_than_failing(self):
        # A missing index must not become an HTML error page: the widget parses
        # every reply, and the route already answers 503 JSON for this.
        import app as app_module
        with mock.patch.object(rag, "get_status",
                               return_value={"state": "error", "error": "no index",
                                             "chunk_size": 128}):
            app_module.app.config["TESTING"] = True
            reply = app_module.app.test_client().post(
                "/chatbot", json={"message": "hello"})
        self.assertEqual(reply.status_code, 503)
        self.assertIn("error", reply.get_json())


if __name__ == "__main__":
    unittest.main()
