"""Tests for transient LLM retry and the frontier scrub opt-out."""

import io
import os
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import utils.llm_client as llm
from utils.log_scrubber import set_active_profile


def _ok_response(content: str):
    resp = MagicMock()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: False
    resp.read.return_value = (
        '{"choices":[{"message":{"content":' + __import__("json").dumps(content) + "}}]}"
    ).encode("utf-8")
    return resp


def _http_error(code: int):
    return urllib.error.HTTPError(
        "http://x", code, "err", None, io.BytesIO(b"body")
    )


class TestTransientRetry(unittest.TestCase):
    def test_transient_5xx_is_retried_then_succeeds(self):
        with patch.object(llm.urllib.request, "urlopen",
                          side_effect=[_http_error(503), _http_error(503),
                                       _ok_response('{"ok": true}')]) as urlopen, \
             patch.object(llm.time, "sleep"):
            out = llm._post_chat("http://x", {}, {"model": "m", "messages": []})
        self.assertEqual(out, '{"ok": true}')
        self.assertEqual(urlopen.call_count, 3)

    def test_non_transient_4xx_is_not_retried(self):
        with patch.object(llm.urllib.request, "urlopen",
                          side_effect=[_http_error(404)]) as urlopen, \
             patch.object(llm.time, "sleep"):
            out = llm._post_chat("http://x", {}, {"model": "m", "messages": []})
        self.assertIsNone(out)
        self.assertEqual(urlopen.call_count, 1)

    def test_exhausted_retries_returns_none(self):
        with patch.dict(os.environ, {"LLM_MAX_ATTEMPTS": "3"}, clear=False), \
             patch.object(llm.urllib.request, "urlopen",
                          side_effect=[_http_error(503)] * 5) as urlopen, \
             patch.object(llm.time, "sleep"):
            out = llm._post_chat("http://x", {}, {"model": "m", "messages": []})
        self.assertIsNone(out)
        self.assertEqual(urlopen.call_count, 3)


class TestFrontierScrub(unittest.TestCase):
    def setUp(self):
        set_active_profile({"document_number": "ABC123"})
        self._captured = {}

        def fake_post(url, headers, payload, timeout=60):
            self._captured["payload"] = payload
            return '{"ok": true}'

        self._fake_post = fake_post

    def tearDown(self):
        set_active_profile(None)

    def _run(self, env):
        with patch.dict(os.environ, env, clear=True), \
             patch.object(llm, "_post_chat", side_effect=self._fake_post):
            llm.generate_json("sys", "the document number is ABC123")

    def test_frontier_scrubs_by_default(self):
        self._run({"USE_LOCAL_LLM_ONLY": "false", "LLM_API_KEY": "lk"})
        content = self._captured["payload"]["messages"][1]["content"]
        self.assertNotIn("ABC123", content)
        self.assertIn("‹", content)

    def test_frontier_scrub_can_be_disabled(self):
        self._run({
            "USE_LOCAL_LLM_ONLY": "false", "LLM_API_KEY": "lk",
            "DVS_SCRUB_FRONTIER": "false",
        })
        content = self._captured["payload"]["messages"][1]["content"]
        self.assertIn("ABC123", content)

    def test_local_mode_never_scrubs(self):
        self._run({"USE_LOCAL_LLM_ONLY": "true", "LLM_API_KEY": "lk"})
        content = self._captured["payload"]["messages"][1]["content"]
        self.assertIn("ABC123", content)


if __name__ == "__main__":
    unittest.main()
