"""Tests for the SCRIPT method type (Phase 4 prototype).

A SCRIPT method stores LLM-authored Python transport code on the method
(``script_source``) instead of declarative ``execution_steps``. It runs in the
same Docker sandbox as every other executor and speaks the same
``input.json`` -> ``output.json`` contract through the ``dvs_io`` shim.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from execution.docker_runner import (
    DEFAULT_TIMEOUT_SECONDS,
    SCRIPT_TIMEOUT_SECONDS,
    DockerMethodRunner,
    _method_timeout,
)
from execution.models import (
    ExecutionDecisionStatus,
    ExecutionRequest,
    ExecutionResult,
)
from registry.models import ValidationMethod, MethodType
from registry.repository import MethodRegistry
from validation.models import TestCase, ValidationReportStatus
from validation.validator import MethodValidator

_EXECUTORS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "executors"
)

_SCRIPT_WITH_COVERAGE = """
from dvs_io import get_input, write_result

doc = get_input("document_number")
write_result("REJECTED", raw_response="VerificationError",
             evidence={"http_status": 200, "doc": doc})
"""


def _script_method(**overrides) -> ValidationMethod:
    base = dict(
        method_id="M_SCRIPT_TEST",
        method_type=MethodType.SCRIPT,
        source_url="http://example.test/verify",
        required_inputs=["document_number"],
        script_source=_SCRIPT_WITH_COVERAGE,
        expected_responses={"document_type_key": "MM_COC"},
    )
    base.update(overrides)
    return ValidationMethod(**base)


class TestDvsIoShim(unittest.TestCase):
    """The shim is copied flat into the sandbox; it must read the engine's
    input envelope and write the standard output contract."""

    def _run_in_sandbox(self, code: str, inputs: dict) -> dict:
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "input.json"), "w") as f:
                json.dump({"inputs": inputs}, f)
            env = dict(os.environ)
            env["PYTHONPATH"] = _EXECUTORS_DIR + os.pathsep + env.get("PYTHONPATH", "")
            proc = subprocess.run(
                [sys.executable, "-c", code],
                cwd=tmp, env=env, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(tmp, "output.json")) as f:
                return json.load(f)

    def test_get_input_reads_declared_value(self):
        out = self._run_in_sandbox(
            "from dvs_io import get_input, write_result;"
            "write_result('VERIFIED', evidence={'v': get_input('document_number')})",
            {"document_number": "ABC123"},
        )
        self.assertEqual(out["decision_status"], "VERIFIED")
        self.assertEqual(out["evidence"]["v"], "ABC123")

    def test_write_result_emits_standard_contract(self):
        out = self._run_in_sandbox(
            "from dvs_io import write_result;"
            "write_result('REJECTED', raw_response='VerificationError',"
            " evidence={'http_status': 200})",
            {},
        )
        self.assertEqual(out["decision_status"], "REJECTED")
        self.assertEqual(out["raw_response"], "VerificationError")
        self.assertEqual(out["evidence"]["http_status"], 200)

    def test_write_result_defaults_to_uncertain(self):
        out = self._run_in_sandbox("from dvs_io import write_result; write_result()", {})
        self.assertEqual(out["decision_status"], "UNCERTAIN")


class TestDvsIoRequestsKwargs(unittest.TestCase):
    """An authored script naturally uses requests-style kwargs. The shim must
    accept them instead of dying on a naming difference (live regression: a
    frontier-authored SCRIPT called http_post(..., headers=...) and the
    sandbox raised TypeError before any request went out)."""

    def setUp(self):
        if _EXECUTORS_DIR not in sys.path:
            sys.path.insert(0, _EXECUTORS_DIR)
        import dvs_io
        self.dvs_io = dvs_io
        self.captured = {}
        dvs_io._SESSION = self._fake_session()

    def tearDown(self):
        self.dvs_io._SESSION = None

    def _fake_session(self):
        captured = self.captured

        class FakeResp:
            status_code = 200
            text = "ok"

        class FakeSession:
            def request(self, method, url, **kw):
                captured.update(kw)
                captured["method"] = method
                captured["url"] = url
                return FakeResp()

        return FakeSession()

    def test_http_post_accepts_headers_and_timeout(self):
        status, body = self.dvs_io.http_post(
            "http://x/verify", data={"a": 1},
            headers={"RequestVerificationToken": "t"}, timeout=5,
        )
        self.assertEqual((status, body), (200, "ok"))
        self.assertEqual(
            self.captured["headers"], {"RequestVerificationToken": "t"}
        )
        self.assertEqual(self.captured["timeout"], 5)

    def test_http_post_json_is_an_alias_for_json_body(self):
        self.dvs_io.http_post("http://x/verify", json={"a": 1})
        self.assertEqual(self.captured["json"], {"a": 1})

    def test_http_get_accepts_headers(self):
        self.dvs_io.http_get("http://x/form", headers={"X-A": "1"})
        self.assertEqual(self.captured["headers"], {"X-A": "1"})
        self.assertEqual(self.captured["method"], "GET")


class TestScriptPersistence(unittest.TestCase):
    def test_script_source_round_trips_through_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = MethodRegistry(db_path=os.path.join(tmp, "reg.db"))
            registry.register_method(_script_method())

            loaded = registry.get_method("M_SCRIPT_TEST")
            self.assertEqual(loaded.method_type, MethodType.SCRIPT)
            self.assertEqual(loaded.script_source, _SCRIPT_WITH_COVERAGE)
            self.assertEqual(loaded.script_runtime, "python3")

            found = registry.find_methods()
            self.assertEqual(found[0].script_source, _SCRIPT_WITH_COVERAGE)


class TestScriptRunnerBranch(unittest.TestCase):
    def test_resolve_executor_returns_script_sentinel(self):
        runner = DockerMethodRunner()
        self.assertEqual(runner._resolve_executor(MethodType.SCRIPT), "__SCRIPT__")

    def test_script_timeout_is_wider_than_plain_http(self):
        self.assertGreater(SCRIPT_TIMEOUT_SECONDS, DEFAULT_TIMEOUT_SECONDS)
        self.assertEqual(_method_timeout(_script_method()), SCRIPT_TIMEOUT_SECONDS)

    def test_script_source_is_written_into_sandbox(self):
        """The authored code becomes executor.py and the shim is shipped."""
        captured = {}

        def fake_run(cmd, **kwargs):
            mount = cmd[cmd.index("-v") + 1].split(":")[0]
            with open(os.path.join(mount, "executor.py")) as f:
                captured["script"] = f.read()
            captured["has_shim"] = os.path.exists(
                os.path.join(mount, "dvs_io.py")
            )
            with open(os.path.join(mount, "output.json"), "w") as f:
                json.dump({"decision_status": "REJECTED", "evidence": {},
                           "raw_response": "VerificationError"}, f)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        runner = DockerMethodRunner()
        req = ExecutionRequest(
            method=_script_method(), inputs={"document_number": "X"}
        )
        with patch("execution.docker_runner.subprocess.run", side_effect=fake_run):
            result = runner.execute_method(req)

        self.assertEqual(captured["script"], _SCRIPT_WITH_COVERAGE)
        self.assertTrue(captured["has_shim"])
        self.assertEqual(result.decision_status, ExecutionDecisionStatus.REJECTED)


class TestScriptCoveragePrecheck(unittest.TestCase):
    def _validator(self, runner):
        return MethodValidator(
            runner=runner, registry=MagicMock(spec=MethodRegistry),
        )

    def test_script_without_get_input_for_required_field_is_refused(self):
        method = _script_method(
            script_source="from dvs_io import write_result; write_result('REJECTED')",
            required_inputs=["document_number"],
        )
        runner = MagicMock()
        report = self._validator(runner).validate(
            method,
            [TestCase(name="structural", inputs={"document_number": "TEST_STRUCTURAL_001"},
                      expected_decision="REJECTED")],
        )
        self.assertEqual(report.status, ValidationReportStatus.ERROR)
        self.assertIn("coverage", (report.failure_reason or "").lower())
        self.assertIn("document_number", report.failure_reason)
        runner.execute_method.assert_not_called()

    def test_script_with_get_input_proceeds(self):
        runner = MagicMock()
        runner.execute_method.return_value = ExecutionResult(
            decision_status=ExecutionDecisionStatus.REJECTED, evidence={},
        )
        report = self._validator(runner).validate(
            _script_method(),
            [TestCase(name="structural", inputs={"document_number": "TEST_STRUCTURAL_001"},
                      expected_decision="REJECTED")],
        )
        self.assertEqual(report.status, ValidationReportStatus.PASSED)
        runner.execute_method.assert_called()


if __name__ == "__main__":
    unittest.main()
