"""The suite's own guard rails, tested, because nothing else would notice them
failing.

Every one of these protects against a silent failure rather than a loud one: a
stale mock that reaches OpenRouter shows up as a slow suite and a bill, and a
missing safety import shows up as two dozen convincing failures in the planning
tests. Both have happened.
"""

import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import ast
import os
import pathlib
import unittest

import requests


class PaidCallsAreBlockedTest(unittest.TestCase):
    """Both ways this app reaches OpenRouter, not just the one we found first.

    src/agents.py goes through requests.post; src/agent.py's LangGraph agent
    goes through the openai SDK, which uses httpx2 and never touches requests.
    Only the first was covered until a ChatOpenAI call was measured going
    straight past the block and returning a real 401.
    """

    def test_the_requests_path_is_refused(self):
        with self.assertRaises(requests.exceptions.ConnectionError) as caught:
            requests.post("https://openrouter.ai/api/v1/chat/completions")
        self.assertIn("Blocked", str(caught.exception))

    def test_the_openai_sdk_path_is_refused(self):
        for name in tests.BLOCKED_TRANSPORTS:
            with self.subTest(transport=name):
                module = __import__(name)
                client = module.Client()
                with self.assertRaises(module.ConnectError) as caught:
                    client.post("https://openrouter.ai/api/v1/chat/completions")
                self.assertIn("Blocked", str(caught.exception))

    def test_both_transports_are_covered(self):
        # httpx2 is the one that matters: it is what the openai SDK actually
        # uses, and patching only httpx changes nothing the agent will call.
        self.assertIn("httpx2", tests.BLOCKED_TRANSPORTS)

    def test_an_ordinary_host_is_left_alone(self):
        # The block names hosts, so stubbing an unrelated request still works.
        # 127.0.0.1:1 refuses rather than answering, which is the point: the
        # error is the socket's, not ours.
        module = __import__(tests.BLOCKED_TRANSPORTS[0])
        with self.assertRaises(module.ConnectError) as caught:
            module.Client(timeout=2).get("http://127.0.0.1:1/")
        self.assertNotIn("Blocked", str(caught.exception))

    def test_letting_them_through_is_opt_in_and_off(self):
        self.assertFalse(tests.ALLOW_LIVE_AI)
        self.assertEqual(os.environ.get("ALLOW_LIVE_AI", ""), "")


class EveryTestFileLoadsTheSafetyNetTest(unittest.TestCase):
    """`python3 -m unittest discover tests` does not import this package, so
    tests/__init__.py would not run and every setting in it would be silently
    absent: rate limits on, database unpinned, paid calls unblocked. That
    produced 26 failures on a slow run and 46 on a fast one, all of them
    looking like real regressions in the planner.

    Each test file importing the package is what makes the settings apply
    however the suite is invoked. This is the check that keeps a new file from
    quietly leaving it out.
    """

    def test_no_test_file_is_missing_the_import(self):
        missing = []
        for path in sorted(pathlib.Path("tests").glob("test_*.py")):
            tree = ast.parse(path.read_text())
            imports_package = any(
                isinstance(node, ast.Import)
                and any(a.name == "tests" for a in node.names)
                for node in tree.body)
            if not imports_package:
                missing.append(path.name)
        self.assertEqual(missing, [], "these files must `import tests`")

    def test_it_comes_before_any_project_import(self):
        # The settings must be in place before app or src is imported, or the
        # first file to load them wins and the rest inherit whatever it saw.
        late = []
        for path in sorted(pathlib.Path("tests").glob("test_*.py")):
            for node in ast.parse(path.read_text()).body:
                names = []
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                if "tests" in names:
                    break
                if any(n.split(".")[0] in ("app", "src") for n in names):
                    late.append(path.name)
                    break
        self.assertEqual(late, [], "`import tests` must come first")


if __name__ == "__main__":
    unittest.main()
