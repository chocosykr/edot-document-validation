"""Tests for ocr.client: empty-page retry and no-poisoned-cache behavior.

The LAN OCR service intermittently answers HTTP 200 with pages=[] for a
readable page (live-observed September 2026). The client must retry such
responses and must never cache a run that silently lost pages, because the
cache is keyed by the file hash and would replay the loss forever.
"""

import json
import os
import sys
import types
import unittest
from unittest import mock


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _pages_payload(n_pages):
    return {
        "filename": "page_1.png",
        "mode": "structured",
        "page_count": n_pages,
        "image_quality": {},
        "pages": [
            {
                "page_number": 1,
                "data": {"page_number": 1, "sections": [{"title": "t", "fields": []}]},
            }
            for _ in range(n_pages)
        ],
    }


class OcrClientEmptyPageTests(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        import ocr.client as client

        self.client = client
        self.cache_dir = os.path.join(self.tmp.name, "cache")
        client._CACHE_DIR = self.cache_dir

        # Fake pdf2image: one 1x1 PIL page, no poppler needed.
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("Pillow not available")
        self._pil_image = Image.new("RGB", (2, 2))

        fake_pdf2image = types.ModuleType("pdf2image")
        fake_pdf2image.convert_from_path = lambda *a, **k: [self._pil_image]
        self._saved = sys.modules.get("pdf2image")
        sys.modules["pdf2image"] = fake_pdf2image
        self.addCleanup(self._restore_pdf2image)

        self.pdf_path = os.path.join(self.tmp.name, "doc.pdf")
        with open(self.pdf_path, "wb") as f:
            f.write(b"%PDF-1.4 fake")

    def _restore_pdf2image(self):
        if self._saved is None:
            sys.modules.pop("pdf2image", None)
        else:
            sys.modules["pdf2image"] = self._saved

    def test_empty_page_is_retried_and_recovered(self):
        """A silent-empty response is retried; the good retry is used and cached."""
        responses = [_FakeResponse(_pages_payload(0)), _FakeResponse(_pages_payload(1))]
        with mock.patch.object(
            self.client.requests, "post", side_effect=lambda *a, **k: responses.pop(0)
        ):
            result = self.client.extract_document(self.pdf_path)

        self.assertEqual(len(result["pages"]), 1)
        self.assertEqual(result["page_count"], 1)
        # Good run must be cached for future runs of the same file.
        import hashlib

        file_hash = hashlib.sha256(open(self.pdf_path, "rb").read()).hexdigest()
        self.assertTrue(os.path.exists(os.path.join(self.cache_dir, file_hash + ".json")))

    def test_persistently_empty_run_is_not_cached(self):
        """All attempts empty -> result returned honestly, cache NOT poisoned."""
        calls = []

        def _always_empty(*a, **k):
            calls.append(1)
            return _FakeResponse(_pages_payload(0))

        with mock.patch.object(self.client.requests, "post", side_effect=_always_empty):
            result = self.client.extract_document(self.pdf_path)

        self.assertEqual(len(calls), 3)  # OCR_PAGE_ATTEMPTS default
        self.assertEqual(result["pages"], [])
        cached = os.listdir(self.cache_dir) if os.path.exists(self.cache_dir) else []
        self.assertEqual(cached, [])  # nothing cached


if __name__ == "__main__":
    unittest.main()
