"""
Re-probe entry point (Component 6 — manual, on-demand)
=======================================================

Re-runs the discriminator discovery probe against an existing method and
rewrites its cached discriminators in place. The ONLY path that may re-hit
the live registry with the seed credential:

  - Manual trigger only (this CLI). Nothing automatic ever re-probes a live
    government server — no background jobs, no per-health-check probes.
  - Rate-limited: a probe is refused while a cooldown from a previous probe
    is still running (default 1 hour), because a live registry must not be
    hit repeatedly. `--force` overrides for a deliberate re-probe.

Effects on the method:
  - Confirmed markers replace the cached ones in `expected_responses`.
  - With a seed credential and both markers confirmed: the REJECTED-only
    limitation is removed and the method is promoted ACTIVE (VERIFIED
    becomes possible again).
  - Without a seed for its scope, or if the probe confirms nothing: the
    method is demoted to INACTIVE (never silently left ACTIVE with
    unverified discriminators).

Usage:
    python -m engine.upgrade_method M_AB12CD34
    python -m engine.upgrade_method M_AB12CD34 --force
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

from registry.models import MethodStatus
from registry.repository import MethodRegistry
from registry.seed_store import SeedStore, SeedStoreError, seed_scope_key
from generation.generator import (
    FAKE_PROBE_INPUTS,
    _discover_discriminators,
)

# Minimum interval between probes against the same method's live registry.
# A re-probe happens on staleness signals only, not on a schedule; this is
# a backstop against accidental repeated invocations, not a health check.
_PROBE_COOLDOWN_SECONDS = 3600


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Re-run the discriminator probe for a method and rewrite its "
            "cached markers in place. Manual trigger only — this is the only "
            "code path that re-contacts the live registry with the seed "
            "credential."
        )
    )
    parser.add_argument("method_id", help="Method ID to re-probe (e.g. M_AB12CD34).")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the re-probe cooldown (deliberate override).",
    )
    parser.add_argument(
        "--registry",
        default=None,
        help="Override the method registry DB path.",
    )
    parser.add_argument(
        "--seed-store",
        default=None,
        help="Override the encrypted seed store path.",
    )
    return parser


def _contract_from_method(method) -> tuple[dict, dict]:
    """
    Rebuild the XHR contract from the method's stored execution_steps.

    The probe must reuse the method's own confirmed endpoint/verb/param
    format — never a re-extraction (the page may have changed since the
    method was generated; the method's cached contract is what validation
    and execution actually use). Returns (contract, param_mapping).
    """
    steps = method.execution_steps or []
    if not steps:
        raise ValueError(f"Method {method.method_id} has no execution steps.")

    step = steps[0]
    if step.get("action") != "REQUEST":
        raise ValueError(
            f"Method {method.method_id} is not an HTTP REQUEST method "
            f"(action={step.get('action')!r}); re-probing is not supported."
        )

    verb = (step.get("method") or "").upper()
    url = step.get("url") or method.source_url
    if not verb or not url:
        raise ValueError(f"Method {method.method_id} lacks verb/URL in execution steps.")

    params = step.get("params") or {}
    static_params = {k: v for k, v in params.items() if not _is_placeholder(v)}
    param_mapping = {k: v for k, v in params.items() if _is_placeholder(v)}

    return {
        "endpoint": url,
        "verb": verb,
        "param_location": step.get("param_location", "body"),
        "dynamic_params": list(param_mapping.keys()),
        "static_params": static_params,
    }, param_mapping


def _is_placeholder(value) -> bool:
    return isinstance(value, str) and value.startswith("{{") and value.endswith("}}")


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    registry = MethodRegistry(args.registry) if args.registry else MethodRegistry()
    method = registry.get_method(args.method_id)
    if not method:
        print(f"ERROR: method {args.method_id} not found in the registry.", file=sys.stderr)
        return 1

    # ---- Rate-limit gate (manual trigger stays deliberate, not chatty) ----
    last_probe = (method.expected_responses or {}).get("last_probed_at")
    if last_probe and not args.force:
        try:
            elapsed = time.time() - float(last_probe)
        except (TypeError, ValueError):
            elapsed = None
        if elapsed is not None and elapsed < _PROBE_COOLDOWN_SECONDS:
            remaining = int(_PROBE_COOLDOWN_SECONDS - elapsed)
            print(
                f"Cooldown active: this method was probed {int(elapsed)}s ago "
                f"(min interval {_PROBE_COOLDOWN_SECONDS}s). Live registries are "
                "not re-probed on a schedule — run again with --force only for a "
                "deliberate re-probe."
            )
            return 1

    # ---- Rebuild the contract from the stored method ----
    try:
        contract, param_mapping = _contract_from_method(method)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # ---- Seed credential: encrypted store, scoped to the method's registry ----
    try:
        seed_store = SeedStore(store_path=args.seed_store) if args.seed_store else SeedStore()
    except Exception as e:
        print(f"ERROR: seed store unusable: {e}", file=sys.stderr)
        return 1

    scope = ""
    try:
        scope = seed_scope_key(method.country, method.document_type)
    except SeedStoreError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    seed_provider = None
    try:
        seed = seed_store.get_seed(method.country, method.document_type)
    except SeedStoreError as e:
        print(f"ERROR: seed store unusable: {e}", file=sys.stderr)
        return 1

    if seed:
        seed_provider = lambda: dict(seed)  # noqa: E731 — in-memory only
        print(f"Seed credential loaded for scope {scope} (values never displayed).")
    else:
        print(
            f"No seed credential for scope {scope}.\n"
            "Onboard one first: python -m engine.onboard_seed --country "
            f"{method.country!r} --document-type {method.document_type!r}\n"
            "Continuing WITHOUT a seed: only the rejection marker can be "
            "confirmed; the method will stay REJECTED-only."
        )

    # ---- Re-probe ----
    print(f"Probing {contract['verb']} {contract['endpoint']} (one-time, manual)...")

    try:
        expected = _discover_discriminators(
            xhr_contract=contract,
            source_url=method.source_url,
            fake_inputs=dict(FAKE_PROBE_INPUTS),
            param_mapping=param_mapping or None,
            real_inputs_provider=seed_provider,
        )
    except Exception as e:
        print(f"ERROR: probe failed: {e}", file=sys.stderr)
        return 1

    # Probe bookkeeping only — no credential material is ever written here.
    expected["last_probed_at"] = str(time.time())

    rejection = (expected.get("failure_keywords") or [])
    success = (expected.get("success_keywords") or [])

    print(f"  Rejection marker: {rejection[0] if rejection else '(none confirmed)'}")
    print(f"  Success marker  : {success[0] if success else '(none confirmed)'}")

    # ---- Rewrite the method in place ----
    method.expected_responses = expected

    fully_probed = bool(success) and bool(rejection)
    rejected_only_limitation = "REJECTED-only until a human-confirmed probe supplies the success marker."

    limitations = [
        lim for lim in (method.limitations or [])
        if rejected_only_limitation not in lim
    ]

    if fully_probed:
        method.limitations = limitations
        # TESTING methods still owe the Docker structural test — the probe
        # alone does not promote them. Waiting statuses (INACTIVE/DEGRADED/
        # UNHEALTHY) exist precisely because the success marker was missing;
        # with both markers confirmed they become usable again.
        if method.status in (MethodStatus.INACTIVE, MethodStatus.DEGRADED, MethodStatus.UNHEALTHY):
            method.status = MethodStatus.ACTIVE
        print(
            f"Method {method.method_id} upgraded: both markers confirmed, "
            "REJECTED-only limitation removed."
        )
    else:
        if rejected_only_limitation not in limitations:
            limitations.append(rejected_only_limitation)
        method.limitations = limitations
        method.status = MethodStatus.INACTIVE
        print(
            f"Method {method.method_id} NOT upgraded: success marker unconfirmed. "
            "Method set INACTIVE (VERIFIED remains mechanically impossible)."
        )

    registry.register_method(method)

    print("\nUpdated method state:")
    print(json.dumps({
        "method_id": method.method_id,
        "status": method.status.value,
        "expected_responses": method.expected_responses,
        "limitations": method.limitations,
        "probed_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))

    return 0


if __name__ == "__main__":
    sys.exit(main())
