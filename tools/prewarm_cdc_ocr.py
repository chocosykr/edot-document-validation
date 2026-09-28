"""Resumable OCR pre-warm for the ANUP CDC booklet (chunked across runs).

The LAN OCR service is currently slow and flaky (HTTP 200 + pages=[] on random
pages); a full 13-page OCR with retries exceeds the 10-minute command cap. This
script persists per-page successes under /tmp/cdc_pages/ and can be re-run until
all pages are captured; once complete it assembles the combined result and
writes it into .ocr_cache/ under the file's SHA-256, exactly as ocr.client
would have — so the next `python main.py` run starts from a complete, honest
OCR result instead of re-rolling the flaky service.

Usage:  source .venv/bin/activate && timeout 500 python tools/prewarm_cdc_ocr.py
"""

import hashlib
import json
import os
import sys
import tempfile
import time

import requests
from pdf2image import convert_from_path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import OCR_API_URL  # noqa: E402

PDF = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "InputFiles", "ANUP CDC ALL PAGES.pdf")
PAGE_DIR = "/tmp/cdc_pages"
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         ".ocr_cache")
ATTEMPTS = int(os.environ.get("OCR_PAGE_ATTEMPTS", "3"))
BUDGET_SECONDS = float(os.environ.get("PREWARM_BUDGET", "440"))
BACKOFF = 2


def main():
    started = time.time()
    os.makedirs(PAGE_DIR, exist_ok=True)
    with open(PDF, "rb") as f:
        file_hash = hashlib.sha256(f.read()).hexdigest()

    images = convert_from_path(PDF)
    total = len(images)
    print(f"booklet pages: {total}, budget: {BUDGET_SECONDS:.0f}s")

    for i, img in enumerate(images, start=1):
        page_file = os.path.join(PAGE_DIR, f"page_{i}.json")
        if os.path.exists(page_file):
            print(f"page {i:2d}: already captured")
            continue
        if time.time() - started > BUDGET_SECONDS:
            print("budget exhausted — re-run to continue")
            _summary(total)
            return

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            img.save(tmp_path, "PNG")
            data = None
            for attempt in range(1, ATTEMPTS + 1):
                with open(tmp_path, "rb") as f:
                    res = requests.post(
                        OCR_API_URL,
                        files={"file": (f"page_{i}.png", f, "image/png")},
                        timeout=120,
                    )
                res.raise_for_status()
                data = res.json()
                if data.get("pages"):
                    break
                print(f"page {i:2d}: empty (attempt {attempt}/{ATTEMPTS})")
                if attempt < ATTEMPTS:
                    time.sleep(BACKOFF * attempt)
            if data and data.get("pages"):
                with open(page_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False)
                print(f"page {i:2d}: CAPTURED ({len(data['pages'])} entries)")
            else:
                print(f"page {i:2d}: still empty after {ATTEMPTS} attempts")
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)

    _summary(total)
    if _all_captured(total):
        _assemble(file_hash, total)
    else:
        print("INCOMPLETE — re-run this script to capture remaining pages")


def _all_captured(total: int) -> bool:
    return all(
        os.path.exists(os.path.join(PAGE_DIR, f"page_{i}.json"))
        for i in range(1, total + 1)
    )


def _summary(total: int) -> None:
    missing = [
        i for i in range(1, total + 1)
        if not os.path.exists(os.path.join(PAGE_DIR, f"page_{i}.json"))
    ]
    print(f"captured {total - len(missing)}/{total}" + (f", missing: {missing}" if missing else ""))


def _assemble(file_hash: str, total: int) -> None:
    combined_pages = []
    first_page_meta = {}
    for i in range(1, total + 1):
        with open(os.path.join(PAGE_DIR, f"page_{i}.json"), encoding="utf-8") as f:
            data = json.load(f)
        if not first_page_meta:
            first_page_meta = {
                "mode": data.get("mode", "structured"),
                "image_quality": data.get("image_quality", {}),
            }
        for page in data.get("pages", []):
            page["page_number"] = i
            if "data" in page and isinstance(page["data"], dict):
                page["data"]["page_number"] = i
            combined_pages.append(page)

    result = {
        "filename": os.path.basename(PDF),
        "mode": first_page_meta.get("mode", "structured"),
        "page_count": len(combined_pages),
        "image_quality": first_page_meta.get("image_quality", {}),
        "pages": combined_pages,
    }
    os.makedirs(CACHE_DIR, exist_ok=True)
    out = os.path.join(CACHE_DIR, file_hash + ".json")
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False)
    os.replace(tmp, out)
    print(f"DONE — assembled {len(combined_pages)} pages -> {out}")


if __name__ == "__main__":
    main()
