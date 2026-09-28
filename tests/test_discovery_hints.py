"""Regression tests: document-carried discovery hints must seed the crawl.

The Indian SID prints only "www.dgshipping.gov.in" — a bare domain without a
scheme. _hint_urls used to accept scheme-full URLs only, silently dropping the
document's own seed and killing search-free discovery for that run (observed
live 2026-09-28: SID generation fell back to an unrelated India source and the
generator's evidence gate correctly refused to mint a method).
"""

import os
import unittest

# discovery.agent validates API keys at import time; defaults keep the test
# hermetic in environments without a populated .env.
os.environ.setdefault("LLM_API_KEY", "test-key")
os.environ.setdefault("TAVILY_API_KEY", "test-key")

from discovery.agent import _hint_urls


class HintUrlTests(unittest.TestCase):
    def test_bare_www_domain_is_upgraded_to_https(self):
        self.assertEqual(
            _hint_urls({"source_discovery_hints": ["www.dgshipping.gov.in"]}),
            ["https://www.dgshipping.gov.in"],
        )

    def test_bare_domain_with_path_is_kept(self):
        self.assertEqual(
            _hint_urls({"source_discovery_hints": ["dgshipping.gov.in/seafarer"]}),
            ["https://dgshipping.gov.in/seafarer"],
        )

    def test_scheme_full_url_still_passes_through(self):
        self.assertEqual(
            _hint_urls({"source_discovery_hints": ["https://x.gov/verify"]}),
            ["https://x.gov/verify"],
        )

    def test_http_url_passes_through(self):
        self.assertEqual(
            _hint_urls({"source_discovery_hints": ["http://220.156.189.33/esamudraUI/"]}),
            ["http://220.156.189.33/esamudraUI/"],
        )

    def test_url_embedded_in_prose_is_extracted(self):
        self.assertEqual(
            _hint_urls({"source_discovery_hints": ["verify at https://x.gov/lookup now"]}),
            ["https://x.gov/lookup"],
        )

    def test_prose_mentioning_a_domain_is_ignored(self):
        self.assertEqual(
            _hint_urls({"source_discovery_hints": ["check the dgshipping website"]}),
            [],
        )

    def test_missing_or_empty_hints(self):
        self.assertEqual(_hint_urls({}), [])
        self.assertEqual(_hint_urls({"source_discovery_hints": [None, ""]}), [])

    def test_single_labels_and_non_domains_are_rejected(self):
        self.assertEqual(
            _hint_urls({"source_discovery_hints": ["localhost", "not a domain", "12345"]}),
            [],
        )


if __name__ == "__main__":
    unittest.main()
