"""
Seed-credential onboarding (interactive, once per registry/document-type)
==========================================================================

Accepts a real, confirmed-valid record for a specific registry + document
type from a consenting person, ONCE per registry — not per document, not per
run — and stores it encrypted via registry.seed_store.SeedStore.

The seed's only purposes:
  - derive success/rejection discriminators (Component 2 probe), and
  - support manual re-probes (Component 6).

It is never used as a per-document lookup value; per-document execution
inputs still come from each document's own raw extraction.

Usage:
    python -m engine.onboard_seed --country India --document-type INDOS
    python -m engine.onboard_seed --show
    python -m engine.onboard_seed --delete --country India --document-type INDOS
"""

import argparse
import getpass
import json
import sys

from registry.seed_store import (
    SeedStore,
    SeedStoreError,
    seed_scope_key,
)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Onboard a seed credential (one real, confirmed-valid record) "
            "for a registry + document type. Stored encrypted; the plaintext "
            "exists in memory only during onboarding."
        )
    )
    parser.add_argument("--country", help="Issuing country of the registry (e.g. India).")
    parser.add_argument("--document-type", help="Document type (e.g. INDOS).")
    parser.add_argument(
        "--store",
        default=None,
        help="Override the seed store file path (default: ./seed_credentials.enc).",
    )
    parser.add_argument(
        "--key-file",
        default=None,
        help="Override the encryption key file path (default: alongside the store).",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Delete the seed for the given scope instead of onboarding.",
    )
    parser.add_argument(
        "--show",
        action="store_true",
        help="List stored scopes only (no seed values are ever displayed).",
    )
    return parser


def _prompt_for_seed() -> tuple:
    """
    Interactively collect the seed credential. getpass hides input from
    shoulder-surfing; the values are returned in memory only.
    """
    print(
        "Enter the seed credential — a REAL, CONFIRMED-VALID record for this\n"
        "registry and document type. This record will be used to derive the\n"
        "success/rejection markers via a one-time probe, and re-probes only on\n"
        "explicit request. It is never used as a stand-in for other documents.\n"
    )
    document_number = input("  Document number: ").strip()
    date_of_birth = getpass.getpass("  Date of birth  : ").strip()

    if not document_number:
        print("\nERROR: a document number is required — a seed without one cannot\n"
              "produce a success marker (the diff needs a real lookup).", file=sys.stderr)
        sys.exit(2)

    return document_number, date_of_birth


def _prompt_for_workflow_params() -> dict:
    """Optionally collect workflow-fixed values that remain constant for this registry."""
    print(
        "\nOptional workflow-fixed values (press Enter to skip each):"
    )
    values: dict = {}
    for key in ("searchType", "processId"):
        value = input(f"  {key}: ").strip()
        if value:
            values[key] = value
    return values


def _confirm_consent(scope: str) -> bool:
    """
    Explicit, typed consent gate. The person supplying the seed must be
    entitled to use this record for probe purposes.
    """
    print(
        f"\nScope: {scope}\n"
        "By continuing you confirm that:\n"
        "  1. This record is real and currently VALID on the live registry.\n"
        "  2. You are authorised to use it for one-time probe verification.\n"
        "  3. You understand the values are stored ENCRYPTED at rest and that\n"
        "     plaintext exists in memory only during probes.\n"
    )
    answer = input('Type "yes" to store the seed: ').strip().lower()
    return answer == "yes"


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    try:
        store = SeedStore(store_path=args.store, key_path=args.key_file)
    except SeedStoreError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.show:
        try:
            scopes = sorted(store._read_all().keys())
        except Exception as e:
            print(f"ERROR: cannot read store: {e}", file=sys.stderr)
            return 1
        if not scopes:
            print("No seed credentials stored.")
        else:
            print("Stored seed scopes (values are never displayed):")
            for s in scopes:
                print(f"  - {s}")
        return 0

    if not args.country or not args.document_type:
        print(
            "ERROR: --country and --document-type are required "
            "(one seed per registry/document-type scope).",
            file=sys.stderr,
        )
        return 2

    try:
        scope = seed_scope_key(args.country, args.document_type)
    except SeedStoreError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    if args.delete:
        if store.delete_seed(args.country, args.document_type):
            print(f"Seed deleted for scope {scope}.")
        else:
            print(f"No seed existed for scope {scope}.")
        return 0

    if store.has_seed(args.country, args.document_type):
        print(
            f"A seed already exists for {scope}.\n"
            "Delete it first (--delete) if it needs replacing — seeds are not\n"
            "overwritten silently."
        )
        return 1

    document_number, date_of_birth = _prompt_for_seed()
    workflow_params = _prompt_for_workflow_params()

    if not _confirm_consent(scope):
        print("Aborted — nothing was stored.")
        return 1

    try:
        store.store_seed(
            country=args.country,
            document_type=args.document_type,
            document_number=document_number,
            date_of_birth=date_of_birth,
            workflow_params=workflow_params,
        )
    except SeedStoreError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(
        f"\nSeed stored (encrypted) for {scope}.\n"
        "Methods for this scope can now be upgraded from REJECTED-only via:\n"
        "    python -m engine.upgrade_method <method_id>\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
