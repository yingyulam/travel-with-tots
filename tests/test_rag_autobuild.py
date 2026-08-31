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

    def test_a_hung_build_is_given_up_on_rather_than_indexing_forever(self):
        # "indexing" was a state with no way out: a blocked build held the
        # lock, so nothing could retry, the status never moved, and the widget
        # showed "Preparing knowledge base" indefinitely. A deployment sat like
        # that while three separate theories about the cause were wrong.
        import time
        with mock.patch.object(rag, "BUILD_DEADLINE_SECONDS", 0.2), \
             mock.patch.object(rag, "chunk_markdown",
                               side_effect=lambda *a, **k: time.sleep(5)):
            rag.rebuild_index()
            time.sleep(0.6)
            status = rag.get_status()
        self.assertEqual(status["state"], "error")
        # Names the stage, so the log says where rather than only that.
        self.assertIn("chunking", status["error"])

    def test_a_finished_build_is_not_given_up_on(self):
        import time
        with mock.patch.object(rag, "BUILD_DEADLINE_SECONDS", 0.2), \
             mock.patch.object(rag, "build_index") as built:
            rag.rebuild_index()
            time.sleep(0.6)
        built.assert_called_once()
        self.assertNotEqual(rag.get_status()["state"], "error")

    def test_each_stage_is_printed_and_recorded(self):
        # The whole point: on a host the log is the only window in, and this
        # module used to write nothing to it.
        rag._stage("doing a thing")
        self.assertEqual(rag.get_status()["stage"], "doing a thing")

    def _post(self, state):
        import app as app_module
        app_module.app.config["TESTING"] = True
        with mock.patch.object(rag, "get_status",
                               return_value={"state": state, "error": "x",
                                             "chunk_size": 128}), \
             mock.patch.object(app_module, "handle_message",
                               return_value={"reply": "ok"}) as handled:
            reply = app_module.app.test_client().post(
                "/chatbot", json={"message": "hello"})
        return reply, handled

    def test_a_build_in_progress_is_answered_as_json_not_an_error_page(self):
        # The widget parses every reply, so this has to stay JSON. "Try again
        # shortly" is also true during the few seconds a first build takes.
        reply, handled = self._post("indexing")
        self.assertEqual(reply.status_code, 503)
        self.assertIn("error", reply.get_json())
        handled.assert_not_called()

    def test_a_failed_index_does_not_take_the_rest_of_the_chat_down(self):
        # This used to refuse the whole turn, which turned a retrieval problem
        # into a broken chat: a deployment whose build stalled answered 503 to
        # "plan a trip", which needs no index and had been working. Retrieval
        # degrades on its own now -- retrieve returns nothing and the FAQ tool
        # says so -- so the turn goes through.
        reply, handled = self._post("error")
        self.assertEqual(reply.status_code, 200)
        handled.assert_called_once()


if __name__ == "__main__":
    unittest.main()
