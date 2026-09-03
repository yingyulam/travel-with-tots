import tests  # noqa: F401  -- applies the suite-wide safety settings
              # (local DB, no rate limits, no paid AI calls) however the
              # suite is invoked. See tests/__init__.py.
import unittest

from src.ai.agents import parse_json_reply


class CleanRepliesTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(parse_json_reply('{"a": 1}'), {"a": 1})

    def test_surrounding_whitespace(self):
        self.assertEqual(parse_json_reply('\n  {"a": 1}\n '), {"a": 1})

    def test_a_fenced_block(self):
        self.assertEqual(parse_json_reply('```json\n{"a": 1}\n```'), {"a": 1})

    def test_a_fence_without_a_language(self):
        self.assertEqual(parse_json_reply('```\n{"a": 1}\n```'), {"a": 1})


class WrappedRepliesTest(unittest.TestCase):
    """The shapes that were failing. Each one is content a model really can
    return under a strict schema, and each used to raise."""

    def test_prose_before_a_fence(self):
        # The old parser only looked for a fence at position 0, so a single
        # lead-in line was enough to lose the whole form.
        reply = 'Here is the form:\n```json\n{"a": 1}\n```'
        self.assertEqual(parse_json_reply(reply), {"a": 1})

    def test_prose_either_side_of_a_bare_object(self):
        reply = 'Sure! {"a": 1} Let me know if you want changes.'
        self.assertEqual(parse_json_reply(reply), {"a": 1})

    def test_a_reasoning_block_before_the_answer(self):
        reply = '<think>They said 2 years old, so age_years is 2.</think>\n{"a": 1}'
        self.assertEqual(parse_json_reply(reply), {"a": 1})

    def test_a_reasoning_block_and_a_fence(self):
        reply = '<think>working it out</think>\n```json\n{"a": 1}\n```'
        self.assertEqual(parse_json_reply(reply), {"a": 1})

    def test_a_nested_object_keeps_its_braces(self):
        # Taking the outermost braces has to survive nesting, or a form with
        # a naps array would come back truncated.
        reply = 'Result: {"naps": [{"start": "13:00"}], "n": 1} done'
        self.assertEqual(parse_json_reply(reply),
                         {"naps": [{"start": "13:00"}], "n": 1})


class FailureMessageTest(unittest.TestCase):
    """The reason has to survive into the message. An empty reply needs a
    different fix from unparseable content, and telling them apart used to
    cost a slow live reproduction."""

    def test_an_empty_reply_says_so(self):
        for empty in ("", "   ", "\n"):
            with self.subTest(reply=repr(empty)):
                with self.assertRaises(ValueError) as caught:
                    parse_json_reply(empty)
                self.assertIn("empty reply", str(caught.exception))

    def test_a_missing_reply_says_so(self):
        # content comes back as null when a model produces nothing at all.
        with self.assertRaises(ValueError) as caught:
            parse_json_reply(None)
        self.assertIn("empty reply", str(caught.exception))

    def test_reasoning_with_no_answer_says_so(self):
        with self.assertRaises(ValueError) as caught:
            parse_json_reply("<think>still thinking about it</think>")
        self.assertIn("only reasoning", str(caught.exception))

    def test_prose_with_no_object_quotes_what_came_back(self):
        with self.assertRaises(ValueError) as caught:
            parse_json_reply("I cannot help with that request.")
        message = str(caught.exception)
        self.assertIn("no JSON object", message)
        self.assertIn("cannot help", message)

    def test_broken_json_quotes_what_came_back(self):
        with self.assertRaises(ValueError) as caught:
            parse_json_reply('{"a": 1,,,}')
        message = str(caught.exception)
        self.assertIn("unparseable JSON", message)
        self.assertIn('"a"', message)

    def test_the_quoted_reply_is_capped(self):
        # A reasoning model can return thousands of characters; the message
        # goes into an exception a route may surface, so it stays short.
        with self.assertRaises(ValueError) as caught:
            parse_json_reply("x" * 5000)
        self.assertLess(len(str(caught.exception)), 300)


if __name__ == "__main__":
    unittest.main()
