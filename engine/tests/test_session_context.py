"""Session context on the MCP wire (_meta / _apo)."""
from __future__ import annotations

import unittest

from apo_engine.session_context import (
    META_CONVERSATION_ID,
    bind_request_session,
    extract_session_fields,
    request_conversation_id,
    strip_session_payload,
)


class SessionContextTest(unittest.TestCase):
    def test_extract_from_apo_args(self):
        cid, gid = extract_session_fields(
            arguments={"path": "a.md", "_apo": {"conversation_id": "c1", "generation_id": "g1"}}
        )
        self.assertEqual(cid, "c1")
        self.assertEqual(gid, "g1")
        cleaned = strip_session_payload(
            {"path": "a.md", "_apo": {"conversation_id": "c1"}}
        )
        self.assertEqual(cleaned, {"path": "a.md"})

    def test_extract_from_meta(self):
        cid, _ = extract_session_fields(
            meta={META_CONVERSATION_ID: "wire-cid"},
            arguments={"vault": "meta"},
        )
        self.assertEqual(cid, "wire-cid")

    def test_request_contextvar_per_call(self):
        with bind_request_session(conversation_id="sess-a"):
            self.assertEqual(request_conversation_id(), "sess-a")
        # outside bind: may fall back to env/file — just ensure reset
        with bind_request_session(conversation_id="sess-b"):
            self.assertEqual(request_conversation_id(), "sess-b")


if __name__ == "__main__":
    unittest.main()
