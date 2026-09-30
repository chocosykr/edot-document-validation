import argparse
import json
import logging
import os
import subprocess
import sys

logging.basicConfig(
    level=logging.INFO,
    format='%(levelname)s:%(name)s:%(message)s',
)

from ocr.client import extract_document
from ocr.extractor import extract_and_redact, split_credentials
from engine.validation_engine import ValidationEngine
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
    parser.add_argument(
        "--folder",
        default=None,
        help="Person folder containing several documents belonging to one "
             "person. Each document is verified independently; missing "
             "required identity inputs may be resolved from the person's "
             "other documents in the folder.",
    )
    return parser


def validate_one_document(
    document_path: str,
    credential_provider=None,
    label: str = None,
):
    """Run the full pipeline for ONE document (used by both modes)."""
    if label:
        print(f"\n{'=' * 60}\nDOCUMENT: {label}\n{'=' * 60}")

    print(f"\n[1/4] Running OCR...")
    ocr_result = extract_document(document_path)

    print("[2/4] Extracting and redacting...")
    extracted = extract_and_redact(ocr_result)

    # Split IN MEMORY: the redacted profile (tokens, safe for prompts/discovery/
    # reports) and the real lookup credentials (raw values, engine-only).
    # Credentials are never logged or persisted.
    redacted_profile, credentials = split_credentials(extracted)

    from utils.log_scrubber import set_active_profile
    set_active_profile(credentials)

    if credential_provider is None:
        if credentials.get("document_number"):
            print("       Lookup credentials captured in memory (never persisted).")
        else:
            print(
                "       WARNING: no real document number found in the raw extraction —\n"
                "       live lookups will be refused for methods that require one."
            )

        def credential_provider():
            return dict(credentials) if credentials else None

    print("[3/4] Running validation engine...")
    engine = ValidationEngine(
        credential_provider=credential_provider,
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
    decision_dict = decision.model_dump()
    print(json.dumps(decision_dict, indent=2, ensure_ascii=False))

    return decision, decision_dict, credentials


def run_folder_mode(folder_path: str):
    """
    Folder mode: a person folder containing several documents belonging to
    ONE person (their SID, COC, CDC, passport scan, ...).

    Each document is verified INDEPENDENTLY — a folder of N documents means N
    verification runs, never one merged run. While verifying a document, the
    engine's credential provider may resolve missing required identity keys
    from the person's OTHER documents in the folder (cross-document lookup
    context — the document being verified is always the subject).

    Extraction results are persisted per document under person_folders/
    (gitignored) as reusable fixtures; re-runs skip OCR until the source
    file changes.
    """
    from fixtures.person_folder import load_subject_document

    if not os.path.isdir(folder_path):
        print(f"Not a folder: {folder_path}")
        return

    person = os.path.basename(os.path.normpath(folder_path))
    documents = sorted(
        os.path.join(folder_path, name)
        for name in os.listdir(folder_path)
        if name.lower().endswith((".pdf", ".png", ".jpg", ".jpeg"))
        and not name.startswith(".")
    )
    if not documents:
        print(f"No documents found in {folder_path}")
        return

    print(f"Person folder: {folder_path} ({len(documents)} document(s))")
    print("Each document will be verified independently; missing required")
    print("identity inputs may be resolved from this person's other documents.")

    results = {}
    for doc in documents:
        name = os.path.basename(doc)
        # One unextractable document must not abort the folder: report it
        # honestly in the summary and continue with the rest.
        try:
            subject_fixture, provider = load_subject_document(doc, person)
        except Exception as e:
            print(f"\n[!] Could not extract {name}: {e}")
            results[name] = {
                "decision_status": "VALIDATION_UNAVAILABLE",
                "failure_reason": f"Document extraction failed: {e}",
            }
            continue

        try:
            decision, decision_dict, _ = validate_one_document(
                doc,
                credential_provider=provider,
                label=name,
            )
        except Exception as e:
            print(f"\n[!] Validation crashed for {name}: {e}")
            results[name] = {
                "decision_status": "TECHNICAL_FAILURE",
                "failure_reason": f"Pipeline error: {e}",
            }
            continue
        results[name] = decision_dict

    print(f"\n{'=' * 60}\nFOLDER SUMMARY — {person}\n{'=' * 60}")
    for name, d in results.items():
        print(f"  {name}: {d.get('decision_status')} / {d.get('document_result')}")


def main():
    args = build_arg_parser().parse_args()

    if args.folder:
        run_folder_mode(args.folder)
        return

    document_path = args.document or select_document()

    if not document_path:
        print("No document selected.")
        return

    print(f"Selected: {document_path}")

    decision, decision_dict, credentials = validate_one_document(document_path)

    from engine.validation_engine import ExecutionDecisionStatus
    if decision.decision_status in (ExecutionDecisionStatus.TECHNICAL_FAILURE, ExecutionDecisionStatus.VALIDATION_UNAVAILABLE):
        import copy
        scrubbed_decision = copy.deepcopy(decision_dict)
        if credentials:
            from utils.log_scrubber import scrub_pii
            scrubbed_str = scrub_pii(json.dumps(scrubbed_decision), credentials)
            scrubbed_decision = json.loads(scrubbed_str)
        
        with open("agent_debug_log.json", "w") as f:
            json.dump(scrubbed_decision, f, indent=2)
        print("\n[!] Technical Failure occurred. Scrubbed log written to agent_debug_log.json for external agent debugging.")
        
        print("\n[?] Do you want to activate the Agentic Fallback Loop to automatically fix this issue?")
        from utils.llm_client import get_llm_config
        fallback_model = get_llm_config().get("model", "AI_Local")
        print(f"    (This will run the fallback agent on the configured model '{fallback_model}' "
              "and may consume credits.)")
        try:
            user_input = input("Proceed? (y/n): ").strip().lower()
        except EOFError:
            # Non-interactive / background run (stdin closed): default to no.
            print("[!] No input available; skipping the fallback loop.")
            user_input = ""
        if user_input in ['y', 'yes']:
            print("[!] Spinning up Agentic Fallback Loop...")
            subprocess.run([sys.executable, "agent_fallback.py"])
        else:
            print("[!] Agentic Fallback Loop aborted.")


if __name__ == "__main__":
    main()
