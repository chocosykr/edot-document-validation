"""
Tests for Component 6 — the manual re-probe CLI (engine/upgrade_method.py).

The re-probe path is the ONLY code path that may re-hit a live registry with
the seed credential, and only on manual trigger. Tests verify:
  - contract rebuild from the stored method (no re-extraction),
  - cooldown / --force gating,
  - in-place rewrite of cached discriminators,
  - promotion when both markers confirm, demotion when not,
  - seed-first sourcing from the encrypted store.

All probe calls are mocked; no live registry or LLM is contacted.
"""

import os
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from engine import upgrade_method
from registry.models import MethodStatus, MethodType, ValidationMethod
from registry.repository import MethodRegistry
from registry.seed_store import SeedStore


REJECTION_MARKER = "Database could not find the match of INDoS No."
SUCCESS_MARKER = "Search Result"


def _seeded_store():
    tmp = tempfile.mkdtemp(prefix="dvs_upg_")
    return SeedStore(
        store_path=os.path.join(tmp, "s.enc"),
        key_path=os.path.join(tmp, "k.enc"),
    )


def _method(
    method_id="M_UPG",
    country="India",
    doc_type="INDOS",
    status=MethodStatus.INACTIVE,
    limitations=None,
):
    return ValidationMethod(
        method_id=method_id,
        method_type=MethodType.HTTP,
        source_url="http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp",
        country=country,
        document_type=doc_type,
        status=status,
        required_inputs=["document_number", "date_of_birth"],
        execution_steps=[{
            "action": "REQUEST",
            "method": "POST",
            "url": "http://220.156.189.33/esamudraUI/checkerajaxservlet",
            "params": {
                "processId": "PPIndosCheck",
                "searchType": "Indos",
                "txtNo": "{{document_number}}",
                "dob": "{{date_of_birth}}",
            },
            "param_location": "query",
        }],
        expected_responses={
            "success_keywords": [],
            "failure_keywords": [REJECTION_MARKER],
        },
        limitations=limitations or [
            "REJECTED-only until a human-confirmed probe supplies the success marker."
        ],
    )


class TestContractFromMethod(unittest.TestCase):

    def test_rebuilds_contract_and_mapping(self):
        contract, mapping = upgrade_method._contract_from_method(_method())
        self.assertEqual(contract["endpoint"], "http://220.156.189.33/esamudraUI/checkerajaxservlet")
        self.assertEqual(contract["verb"], "POST")
        self.assertEqual(contract["param_location"], "query")
        self.assertEqual(
            contract["static_params"], {"processId": "PPIndosCheck", "searchType": "Indos"}
        )
        self.assertEqual(mapping, {"txtNo": "{{document_number}}", "dob": "{{date_of_birth}}"})

    def test_non_request_method_is_rejected(self):
        m = _method()
        m.execution_steps = [{"action": "FILL", "field": "x", "value": "y"}]
        with self.assertRaises(ValueError):
            upgrade_method._contract_from_method(m)

    def test_empty_steps_rejected(self):
        m = _method()
        m.execution_steps = []
        with self.assertRaises(ValueError):
            upgrade_method._contract_from_method(m)


class TestUpgradeMethodCLI(unittest.TestCase):

    def setUp(self):
        self.db_path = tempfile.mktemp(prefix="dvs_upg_db_", suffix=".db")
        self.registry = MethodRegistry(db_path=self.db_path)
        self.store = _seeded_store()

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _store_method(self, **kwargs):
        m = _method(**kwargs)
        self.registry.register_method(m)
        return m

    def _argv(self, method_id="M_UPG", extra=None):
        """CLI args pointed at the test registry DB."""
        return [method_id, "--registry", self.db_path] + (extra or [])

    @patch("engine.upgrade_method._discover_discriminators")
    @patch("engine.upgrade_method.SeedStore")
    def test_fully_probed_promotes_to_active(self, mock_store_cls, mock_discover):
        mock_store_cls.return_value = self.store
        self.store.store_seed(country="India", document_type="INDOS", document_number="SEED9")
        m = self._store_method(status=MethodStatus.INACTIVE)

        mock_discover.return_value = {
            "success_keywords": [SUCCESS_MARKER],
            "failure_keywords": [REJECTION_MARKER],
        }

        rc = upgrade_method.main(self._argv())
        self.assertEqual(rc, 0)

        updated = self.registry.get_method("M_UPG")
        self.assertEqual(updated.status, MethodStatus.ACTIVE)
        self.assertEqual(updated.expected_responses["success_keywords"], [SUCCESS_MARKER])
        self.assertEqual(updated.expected_responses["failure_keywords"], [REJECTION_MARKER])
        self.assertFalse(
            any("REJECTED-only" in lim for lim in updated.limitations),
            "REJECTED-only limitation must be removed after a fully-confirmed probe.",
        )
        self.assertIn("last_probed_at", updated.expected_responses)
        # The seed values must never appear in the registry
        import json as _json
        self.assertNotIn("SEED9", _json.dumps(updated.expected_responses))

    @patch("engine.upgrade_method._discover_discriminators")
    @patch("engine.upgrade_method.SeedStore")
    def test_unconfirmed_success_demotes_to_inactive(self, mock_store_cls, mock_discover):
        mock_store_cls.return_value = self.store
        self.store.store_seed(country="India", document_type="INDOS", document_number="SEED9")
        self._store_method(status=MethodStatus.ACTIVE)

        mock_discover.return_value = {
            "success_keywords": [],
            "failure_keywords": [REJECTION_MARKER],
        }

        rc = upgrade_method.main(self._argv())
        self.assertEqual(rc, 0)

        updated = self.registry.get_method("M_UPG")
        self.assertEqual(updated.status, MethodStatus.INACTIVE)
        self.assertTrue(
            any("REJECTED-only" in lim for lim in updated.limitations)
        )

    @patch("engine.upgrade_method.SeedStore")
    def test_missing_method_errors(self, mock_store_cls):
        mock_store_cls.return_value = self.store
        rc = upgrade_method.main(self._argv("M_MISSING"))
        self.assertEqual(rc, 1)

    @patch("engine.upgrade_method.SeedStore")
    def test_cooldown_blocks_second_probe(self, mock_store_cls):
        mock_store_cls.return_value = self.store
        m = self._store_method()
        m.expected_responses["last_probed_at"] = "99999999999.0"  # future timestamp
        self.registry.register_method(m)

        with patch("engine.upgrade_method._discover_discriminators") as mock_disc:
            rc = upgrade_method.main(self._argv())
            self.assertEqual(rc, 1)
            mock_disc.assert_not_called()

    @patch("engine.upgrade_method._discover_discriminators")
    @patch("engine.upgrade_method.SeedStore")
    def test_force_bypasses_cooldown(self, mock_store_cls, mock_discover):
        mock_store_cls.return_value = self.store
        m = self._store_method()
        m.expected_responses["last_probed_at"] = "99999999999.0"
        self.registry.register_method(m)

        mock_discover.return_value = {
            "success_keywords": [SUCCESS_MARKER],
            "failure_keywords": [REJECTION_MARKER],
        }
        rc = upgrade_method.main(self._argv(extra=["--force"]))
        self.assertEqual(rc, 0)
        mock_discover.assert_called_once()

    @patch("engine.upgrade_method._discover_discriminators")
    @patch("engine.upgrade_method.SeedStore")
    def test_no_seed_still_probes_but_stays_rejected_only(self, mock_store_cls, mock_discover):
        """Without a seed, only the rejection marker can be confirmed and the
        method must NOT be promoted to ACTIVE."""
        mock_store_cls.return_value = self.store  # empty store
        self._store_method(status=MethodStatus.INACTIVE)

        mock_discover.return_value = {
            "success_keywords": [],
            "failure_keywords": [REJECTION_MARKER],
        }

        rc = upgrade_method.main(self._argv())
        self.assertEqual(rc, 0)

        updated = self.registry.get_method("M_UPG")
        self.assertEqual(updated.status, MethodStatus.INACTIVE)
        self.assertTrue(any("REJECTED-only" in lim for lim in updated.limitations))
        # Only the fake probe fired: no real-input request was possible
        kwargs = mock_discover.call_args.kwargs
        self.assertIsNone(kwargs.get("real_inputs_provider"))

    def test_seed_store_error_fails_closed(self):
        self._store_method()
        with patch("engine.upgrade_method.SeedStore") as mock_store_cls:
            mock_store_cls.side_effect = Exception("corrupt store")
            rc = upgrade_method.main(self._argv())
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
