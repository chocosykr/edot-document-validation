import argparse
import json
import logging
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s',
)

from ocr.client import extract_document
from ocr.extractor import extract_and_redact, split_credentials
from engine.validation_engine import ValidationEngine
from registry.seed_store import SeedStore
from report import generate_markdown_report


def select_document():
    result = subprocess.run(
        [
            "zenity",
            "--file-selection",
            "--title=Select document",
            "--file-filter=Documents | *.pdf *.png *.jpg *.jpeg",
            "--file-filter=All files | *"
        ],
        capture_output=True,
        text=True
    )

    if result.returncode != 0:
        return None

    return result.stdout.strip()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Seafarer document validation system (DVS)."
    )
    parser.add_argument(
        "document",
        nargs="?",
        default=None,
        help="Path to the document to validate (PDF/PNG/JPG). "
             "Omit to pick the file with a graphical chooser.",
    )
    return parser


def main():
    args = build_arg_parser().parse_args()

    document_path = args.document or select_document()

    if not document_path:
        print("No document selected.")
        return

    print(f"Selected: {document_path}")

    print("\n[1/4] Running OCR...")
    ocr_result = extract_document(document_path)

    print("[2/4] Extracting and redacting...")
    extracted = extract_and_redact(ocr_result)

    # Split IN MEMORY: the redacted profile (tokens, safe for prompts/discovery/
    # reports) and the real lookup credentials (raw values, engine-only).
    # Credentials are never logged or persisted.
    redacted_profile, credentials = split_credentials(extracted)
    if credentials.get("document_number"):
        print("       Lookup credentials captured in memory (never persisted).")
    else:
        print(
            "       WARNING: no real document number found in the raw extraction —\n"
            "       live lookups will be refused for methods that require one."
        )

    def credential_provider():
        return dict(credentials) if credentials else None

    # Encrypted seed-credential store: one real, confirmed-valid record per
    # registry/document-type, onboarded once via `python -m engine.onboard_seed`.
    # The SEED (not this document's values) feeds the discriminator probe;
    # this document's raw values feed its own execution only.
    seed_store = None
    try:
        seed_store = SeedStore()
    except Exception as e:
        print(
            f"       WARNING: encrypted seed store unavailable ({e}) —\n"
            "       generated methods will be REJECTED-only until a seed is onboarded."
        )

    print("[3/4] Running validation engine...")
    engine = ValidationEngine(
        credential_provider=credential_provider,
        seed_store=seed_store,
    )
    decision = engine.validate(redacted_profile)

    print("\n[4/4] Generating report...")
    report_path = generate_markdown_report(
        {
            "result": decision.model_dump(),
            "searches_performed": [],
            "analyzed_results": []
        },
        document_path
    )
    print(f"Report saved to: {report_path}")

    print("\n--- REDACTED PROFILE ---")
    print(json.dumps(redacted_profile, indent=2, ensure_ascii=False))

    print("\n--- VALIDATION DECISION ---")
    print(json.dumps(decision.model_dump(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
