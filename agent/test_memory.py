"""Regression tests for what the providers actually put on the wire.

Every provider here is single-message, so the message list `build_messages`
assembles has to be flattened by hand. When that flattening read `messages[-1]`
instead of the whole list, the daemon faithfully saved every turn to
`threads/<id>.json`, read it back on the next turn, and then threw it away one
line later -- so Kara could not answer a question she had asked herself.

These assert on the text that leaves the provider, because that is the only
place the loss was observable: the history was present in the state file, in the
RPC payload and in the audit trail the whole time.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.providers.media import flatten_messages  # noqa: E402


class FlattenMessages(unittest.TestCase):
    def test_keeps_history_across_turns(self):
        """The regression: turn 2 must still contain turn 1."""
        msgs = [
            {"role": "system", "content": "You are Kara."},
            {"role": "user", "content": "What is 17 * 3?"},
            {"role": "assistant", "content": "17 * 3 = 51."},
            {"role": "user", "content": "What was that again?"},
        ]
        text = flatten_messages(msgs)
        # Every earlier turn survives...
        self.assertIn("What is 17 * 3?", text)
        self.assertIn("17 * 3 = 51.", text)
        self.assertIn("You are Kara.", text)
        # ...and the live instruction is last, unlabelled, so it still reads as
        # the thing to do now rather than as quoted history.
        self.assertTrue(text.rstrip().endswith("What was that again?"))
        self.assertNotIn("[user]\nWhat was that again?", text)

    def test_roles_are_labelled(self):
        text = flatten_messages([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
        ])
        self.assertIn("[user]\nfirst", text)
        self.assertIn("[assistant]\nsecond", text)

    def test_never_drops_the_final_instruction(self):
        text = flatten_messages([{"role": "user", "content": "just this"}])
        self.assertEqual(text, "just this")

    def test_empty_and_malformed_input(self):
        self.assertEqual(flatten_messages(None), "")
        self.assertEqual(flatten_messages([]), "")
        self.assertEqual(flatten_messages([{"role": "user", "content": "  "}]), "")
        # Non-string content must not raise mid-turn.
        self.assertEqual(flatten_messages([{"role": "user", "content": 7}]), "")
        self.assertEqual(flatten_messages(["not a dict"]), "")


if __name__ == "__main__":
    unittest.main()