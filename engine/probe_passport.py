"""
Passport classification probe — dmamyanmar.org CrewPassport field
=================================================================

Answers ONE question with live structural evidence: does the DMA
SelfVerification portal VALIDATE the CrewPassport field against its records,
or is it cosmetic metadata (like ReplyEmail, which is only where results are
mailed)?

Three submissions, same anti-forgery-tokened form flow a browser uses:

  A. real CDC No + real Serial + obviously-fake Passport
  B. real CDC No + real Serial + real Passport
  C. known-bogus CDC No (+ fake passport)

Classification:
  - A ≈ B and both differ from C  -> CrewPassport is COSMETIC
    -> safe to declare contact_only.
  - A differs from B              -> the portal cross-checks it
    -> CrewPassport stays a hard identity field.
  - A ≈ B ≈ C (or all error)      -> inconclusive; keep identity, re-probe.

Values come from --cdc/--serial/--passport or from an existing person-folder
fixture (--person/--document), which ties Part A to the folder fixtures.
Everything is read-only with respect to the registry: this script never
writes method status; it only records evidence for the human/agent to act on.

Ethical note: three small POSTs to a public verification form with obviously
fake probe values, no PII beyond the operator-supplied real pair, no
automation beyond what the form's own browser flow does.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests  # noqa: E402

BASE = "https://www.dmamyanmar.org"
PAGE_URL = f"{BASE}/AllInOneCertificate/SelfVerification"
VERIFY_URL = f"{BASE}/AllInOneCertificate/Verify"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36")

# Project-root probe_evidence/ — the same path mcp_server/server.py reads
# (PASSPORT_PROBE_EVIDENCE). Keeping both in sync is load-bearing: a mismatch
# silently disables the evidence-based escape hatch.
EVIDENCE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "probe_evidence")

FAKE_PASSPORT = "ZZ0000000"
FAKE_CDC = "ZZ9999999"


def _fetch_session_and_token() -> tuple:
    """One page load, one token — reused across all three probes so the
    only variable between them is the form values."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA})
    resp = s.get(PAGE_URL, timeout=30)
    resp.raise_for_status()
    m = re.search(
        r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', resp.text
    )
    if not m:
        raise RuntimeError("No __RequestVerificationToken found on the page")
    return s, m.group(1)


def _submit(session, token: str, cdc: str, serial: str, passport: str, email: str) -> dict:
    resp = session.post(
        VERIFY_URL,
        data={
            "CrewCDCNo": cdc,
            "Serial": serial,
            "CrewPassport": passport,
            "ReplyEmail": email,
            "__RequestVerificationToken": token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=30,
    )
    body = resp.text or ""
    return {
        "http_status": resp.status_code,
        "length": len(body),
        "sha256_head": body[:200],
        "error_signature": bool(
            re.search(r"exception|stack trace|NullReference", body, re.I)
        ),
        "not_found_signature": bool(
            re.search(r"not\s*found|no\s*record|no\s*matching|does\s*not\s*exist",
                      body, re.I)
        ),
        "record_found_signature": bool(
            re.search(r"dateOfExpiry|identificationMark|dateOfIssue", body)
        ),
    }


def _fingerprint(r: dict) -> str:
    """Coarse structural fingerprint — deliberately coarse so near-identical
    pages compare equal regardless of timestamps/session noise."""
    parts = [
        str(r["http_status"]),
        "err" if r["error_signature"] else "-",
        "nf" if r["not_found_signature"] else "-",
        "rec" if r["record_found_signature"] else "-",
        str(r["length"] // 512),  # 512-byte length buckets
    ]
    return "|".join(parts)


def _values_from_args_or_fixture(args) -> dict:
    if args.cdc and args.serial:
        return {"cdc": args.cdc.strip(), "serial": args.serial.strip(),
                "passport": (args.passport or "").strip() or None}
    if args.person and args.document:
        from fixtures.person_folder import load_or_extract_fixture
        fx = load_or_extract_fixture(args.document, args.person)
        creds = fx.get("raw_lookup_credentials") or {}
        return {
            "cdc": creds.get("document_number") or creds.get("cdc_number"),
            "serial": creds.get("serial_number"),
            "passport": creds.get("passport_number"),
        }
    raise SystemExit(
        "Provide --cdc and --serial, or --person + --document to read them "
        "from a person-folder fixture."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--cdc", help="real CDC No as printed on the certificate")
    ap.add_argument("--serial", help="real certificate Serial")
    ap.add_argument("--passport", help="real passport number (for probe B)")
    ap.add_argument("--person", help="person folder name (fixture mode)")
    ap.add_argument("--document", help="document path inside the person folder")
    ap.add_argument("--email", default="probenotification@dvs.invalid")
    ap.add_argument("--skip-real-passport", action="store_true",
                    help="run A and C only (when no real passport is known)")
    args = ap.parse_args()

    vals = _values_from_args_or_fixture(args)
    if not vals["cdc"] or not vals["serial"]:
        raise SystemExit(
            "A real CDC No and Serial are required (from args or fixture). "
            "Without them the probe cannot distinguish 'passport ignored' "
            "from 'record not found'."
        )

    print("Fetching session + anti-forgery token...")
    session, token = _fetch_session_and_token()
    email = args.email

    probes = {"A_fake_passport": None, "B_real_passport": None, "C_bogus_cdc": None}

    print("Probe A: real CDC+Serial, FAKE passport...")
    probes["A_fake_passport"] = _submit(
        session, token, vals["cdc"], vals["serial"], FAKE_PASSPORT, email
    )

    if not args.skip_real_passport and vals["passport"]:
        print("Probe B: real CDC+Serial, REAL passport...")
        probes["B_real_passport"] = _submit(
            session, token, vals["cdc"], vals["serial"], vals["passport"], email
        )
    else:
        print("Probe B: SKIPPED (no real passport available)")

    print("Probe C: bogus CDC (negative control)...")
    probes["C_bogus_cdc"] = _submit(
        session, token, FAKE_CDC, vals["serial"], FAKE_PASSPORT, email
    )

    fpA = _fingerprint(probes["A_fake_passport"])
    fpB = _fingerprint(probes["B_real_passport"]) if probes["B_real_passport"] else None
    fpC = _fingerprint(probes["C_bogus_cdc"])

    print()
    print(f"  A (fake passport): {fpA}")
    print(f"  B (real passport): {fpB or 'skipped'}")
    print(f"  C (bogus CDC)    : {fpC}")

    if fpB is not None:
        if fpA == fpB and fpA != fpC:
            verdict = "COSMETIC"
            reason = ("fake and real passport produced structurally identical "
                      "responses; the bogus CDC differs — CrewPassport is not "
                      "validated against records.")
        elif fpA != fpB:
            verdict = "VALIDATED"
            reason = ("fake and real passport produced different responses — "
                      "the portal cross-checks CrewPassport; it must remain a "
                      "hard identity field.")
        else:
            verdict = "INCONCLUSIVE"
            reason = ("all three probes look structurally identical — the "
                      "endpoint may not be processing submissions at all; "
                      "do not change the method based on this.")
    else:
        verdict = "NEEDS_PROBE_B"
        reason = ("probe B was skipped (no real passport available); the "
                  "A-vs-C comparison alone cannot separate 'passport ignored' "
                  "from 'record not found'.")

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "verdict": verdict,
        "reason": reason,
        # Explicit scope: WHICH field this evidence may mark contact_only.
        # The upsert gate vouches nothing without this list (fail-closed).
        "vouches_for": (["passport_number"]
                        if verdict == "COSMETIC" else []),
        "probes": probes,
        "fingerprints": {"A": fpA, "B": fpB, "C": fpC},
        "inputs_used": {
            "cdc": vals["cdc"], "serial": vals["serial"],
            "passport_real_supplied": bool(vals["passport"]),
            "email": email,
        },
    }

    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    out = os.path.join(EVIDENCE_DIR, "passport_probe_result.json")
    with open(out, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nVERDICT: {verdict}\n{reason}\n\nEvidence saved to: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
