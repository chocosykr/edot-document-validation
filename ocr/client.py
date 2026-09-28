import hashlib
import json
import logging
import os
import tempfile
import time

import requests

from config import OCR_API_URL

logger = logging.getLogger(__name__)

# The LAN OCR service intermittently answers HTTP 200 with pages=[] for a
# perfectly readable page (observed live, September 2026: the same PNG of
# the ANUP CDC booklet's identity page returned 1 page, then 0 pages, then
# 1 page on consecutive attempts). Silent-empty responses are therefore
# retried instead of trusted. Attempts per page are env-overridable.
_PAGE_RETRY_BACKOFF_SECONDS = 2

# OCR cache: raw OCR responses are pure functions of the FILE BYTES, so they
# are cached on disk keyed by the file's SHA-256. Extraction (the LLM step)
# is NOT cached — it may be re-run, e.g. after prompt changes. Cache lives in
# .ocr_cache/ (gitignored) and never goes near git; entries are JSON of the
# exact response extract_document would return.
_CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".ocr_cache"
)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_get(file_hash: str):
    path = os.path.join(_CACHE_DIR, file_hash + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            logger.info("OCR cache hit for %s", file_hash[:12])
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Corrupt OCR cache entry %s (%s); re-running OCR", file_hash[:12], e)
        try:
            os.remove(path)
        except OSError:
            pass
        return None


def _cache_put(file_hash: str, payload: dict) -> None:
    try:
        os.makedirs(_CACHE_DIR, exist_ok=True)
        tmp = os.path.join(_CACHE_DIR, "." + file_hash[:16] + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(_CACHE_DIR, file_hash + ".json"))
    except OSError as e:
        logger.warning("OCR cache write failed (%s); continuing uncached", e)


def extract_document(file_path: str) -> dict:
    """
    Send a document to the OCR API and return its JSON response.
    If file_path is a PDF, split it into page images and process page by page
    to prevent timeouts or size limits on large multi-page PDFs.

    Results are cached by the source file's SHA-256 in .ocr_cache/ — a
    re-run of the same document (retries, folder re-tests, structural
    re-probes) skips the OCR round-trip entirely. The extraction step that
    consumes this response is deliberately NOT cached.
    """
    file_hash = _sha256_file(file_path)
    cached = _cache_get(file_hash)
    if cached is not None:
        return cached

    if not file_path.lower().endswith(".pdf"):
        with open(file_path, "rb") as file:
            response = requests.post(
                OCR_API_URL,
                files={"file": file},
                timeout=120
            )
        response.raise_for_status()
        result = response.json()
        _cache_put(file_hash, result)
        return result

    # Handle PDF page by page
    try:
        from pdf2image import convert_from_path
        images = convert_from_path(file_path)
    except Exception as e:
        logger.warning(f"Could not convert PDF using pdf2image ({e}), falling back to direct upload")
        with open(file_path, "rb") as file:
            response = requests.post(
                OCR_API_URL,
                files={"file": file},
                timeout=120
            )
        response.raise_for_status()
        result = response.json()
        _cache_put(file_hash, result)
        return result

    if not images:
        raise ValueError(f"No pages extracted from PDF: {file_path}")

    logger.info(f"Processing PDF page-by-page ({len(images)} pages) for OCR: {file_path}")
    combined_pages = []
    first_page_meta = {}

    empty_pages: list[int] = []

    for i, img in enumerate(images):
        page_attempts = max(1, int(os.environ.get("OCR_PAGE_ATTEMPTS", "3")))
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            img.save(tmp_path, "PNG")
            for attempt in range(1, page_attempts + 1):
                with open(tmp_path, "rb") as f:
                    filename = f"page_{i+1}.png"
                    res = requests.post(
                        OCR_API_URL,
                        files={"file": (filename, f, "image/png")},
                        timeout=120
                    )
                    res.raise_for_status()
                    data = res.json()
                if data.get("pages"):
                    break
                if attempt < page_attempts:
                    logger.warning(
                        "Page %d OCR returned 0 pages (attempt %d/%d); retrying",
                        i + 1, attempt, page_attempts,
                    )
                    time.sleep(_PAGE_RETRY_BACKOFF_SECONDS * attempt)

            if not first_page_meta:
                first_page_meta = {
                    "mode": data.get("mode", "structured"),
                    "image_quality": data.get("image_quality", {}),
                }
            for page in data.get("pages", []):
                page["page_number"] = i + 1
                if "data" in page and isinstance(page["data"], dict):
                    page["data"]["page_number"] = i + 1
                combined_pages.append(page)

            if not data.get("pages"):
                empty_pages.append(i + 1)
                logger.error(
                    "Page %d/%d OCR returned 0 pages after %d attempts; "
                    "continuing without it",
                    i + 1, len(images), page_attempts,
                )
            else:
                logger.info(f"  Page {i+1}/{len(images)} OCR completed")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    result = {
        "filename": os.path.basename(file_path),
        "mode": first_page_meta.get("mode", "structured"),
        "page_count": len(combined_pages),
        "image_quality": first_page_meta.get("image_quality", {}),
        "pages": combined_pages,
    }

    if empty_pages:
        # A run that silently lost pages must NOT be cached: the cache is
        # keyed by the FILE hash, so a poisoned entry would replay the
        # missing pages on every future run of the same document. Skip the
        # cache write; the next run re-OCRs and gets a fresh chance at the
        # flaky pages.
        logger.warning(
            "Skipping OCR cache write for %s: pages %s came back empty "
            "(transient service issue); the next run will re-OCR them",
            os.path.basename(file_path), empty_pages,
        )
        return result

    _cache_put(file_hash, result)
    return result
