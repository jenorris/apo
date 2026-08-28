"""config.env_bool: one settled truthy/falsy word set for every boolean env var.

Three call sites (APO_WATCH_EVENTS, APO_RERANK, APO_QUERY_EXPAND) used to each
hand-roll their own word list and drifted — APO_WATCH_EVENTS didn't accept
"off" and didn't strip whitespace, so `APO_WATCH_EVENTS=off` (a form that
works for APO_RERANK) silently no-opped instead of disabling anything.
"""

from __future__ import annotations

import os
import unittest

from apo_engine.config import ENV_FALSY, ENV_TRUTHY, env_bool


class EnvBoolTest(unittest.TestCase):
    KEY = "APO_TEST_ENV_BOOL_FLAG"

    def setUp(self):
        self._prev = os.environ.pop(self.KEY, None)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(self.KEY, None)
        else:
            os.environ[self.KEY] = self._prev

    def test_default_when_unset(self):
        self.assertTrue(env_bool(self.KEY, True))
        self.assertFalse(env_bool(self.KEY, False))

    def test_every_truthy_word_accepted(self):
        for word in ENV_TRUTHY:
            os.environ[self.KEY] = word
            self.assertTrue(env_bool(self.KEY, False), msg=word)

    def test_every_falsy_word_accepted_including_off(self):
        for word in ENV_FALSY:
            os.environ[self.KEY] = word
            self.assertFalse(env_bool(self.KEY, True), msg=word)

    def test_case_and_whitespace_insensitive(self):
        os.environ[self.KEY] = "  OFF  "
        self.assertFalse(env_bool(self.KEY, True))
        os.environ[self.KEY] = "  On  "
        self.assertTrue(env_bool(self.KEY, False))

    def test_unrecognized_value_falls_back_to_default(self):
        os.environ[self.KEY] = "maybe"
        self.assertTrue(env_bool(self.KEY, True))
        self.assertFalse(env_bool(self.KEY, False))


if __name__ == "__main__":
    unittest.main()
