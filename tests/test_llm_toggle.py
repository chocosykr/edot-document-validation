"""Tests for the local/frontier LLM switch (USE_LOCAL_LLM_ONLY)."""

import os
import unittest
from unittest.mock import patch

import utils.llm_client as llm_client
from utils.llm_client import get_llm_config


class TestLlmToggle(unittest.TestCase):
    def test_unset_defaults_to_local(self):
        with patch.dict(os.environ, {}, clear=True):
            cfg = get_llm_config()
        self.assertFalse(cfg["is_frontier"])
        self.assertTrue(cfg["use_local_only"])
        self.assertEqual(cfg["url"], llm_client.LLM_URL)

    def test_true_is_local(self):
        with patch.dict(os.environ, {"USE_LOCAL_LLM_ONLY": "true"}, clear=True):
            cfg = get_llm_config()
        self.assertFalse(cfg["is_frontier"])
        self.assertEqual(cfg["url"], llm_client.LLM_URL)
        self.assertEqual(cfg["model"], llm_client.LLM_MODEL)

    def test_false_uses_same_gateway_with_frontier_model(self):
        """Frontier is the local gateway (LLM_URL/LLM_API_KEY) + a different model."""
        with patch.dict(
            os.environ,
            {
                "USE_LOCAL_LLM_ONLY": "false",
                "LLM_URL": "https://ai.edot-solutions.com/v1/chat/completions",
                "LLM_API_KEY": "gateway-key",
            },
            clear=True,
        ):
            cfg = get_llm_config()
        self.assertTrue(cfg["is_frontier"])
        self.assertEqual(
            cfg["url"], "https://ai.edot-solutions.com/v1/chat/completions"
        )
        self.assertEqual(cfg["model"], llm_client.FRONTIER_LLM_MODEL)
        self.assertEqual(cfg["api_key"], "gateway-key")

    def test_false_honours_explicit_frontier_overrides(self):
        with patch.dict(
            os.environ,
            {
                "USE_LOCAL_LLM_ONLY": "false",
                "FRONTIER_LLM_URL": "https://frontier.example/v1/chat/completions",
                "FRONTIER_LLM_MODEL": "my-frontier-model",
                "FRONTIER_LLM_API_KEY": "fk",
            },
            clear=True,
        ):
            cfg = get_llm_config()
        self.assertTrue(cfg["is_frontier"])
        self.assertEqual(cfg["url"], "https://frontier.example/v1/chat/completions")
        self.assertEqual(cfg["model"], "my-frontier-model")
        self.assertEqual(cfg["api_key"], "fk")

    def test_false_without_any_key_still_targets_gateway(self):
        with patch.dict(os.environ, {"USE_LOCAL_LLM_ONLY": "false"}, clear=True):
            cfg = get_llm_config()
        # Requested frontier with only the gateway configured: same URL, no key.
        self.assertEqual(cfg["url"], llm_client.LLM_URL)
        self.assertEqual(cfg["model"], llm_client.FRONTIER_LLM_MODEL)

    def test_frontier_model_default_is_gateway_gemini(self):
        self.assertEqual(llm_client.FRONTIER_LLM_MODEL, "gemini/gemini-2.5-flash")

    def test_other_falsey_values_select_frontier(self):
        for value in ("0", "no", "off", "FALSE"):
            with self.subTest(value=value):
                with patch.dict(
                    os.environ,
                    {"USE_LOCAL_LLM_ONLY": value, "LLM_API_KEY": "lk"},
                    clear=True,
                ):
                    cfg = get_llm_config()
                self.assertTrue(cfg["is_frontier"])


if __name__ == "__main__":
    unittest.main()
