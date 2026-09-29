"""
Unit tests for the deterministic bundle contract extractor
(generation/bundle_contract.py) and the generalized doc-type workflow
evidence guard (generation/param_mapping.py::_doc_type_workflow_evidence).

The primary fixture mirrors the REAL shape observed on a React SPA that
replaced a legacy JSP portal: an empty shell page, a 3.6 MB minified bundle,
and the verification call hidden among ~110 unrelated fetch() calls
(auth/session/captcha) using a base variable that resolves to "".
"""

import unittest

from generation.bundle_contract import (
    _extract_xhr_contract_from_bundles,
    _extract_template_params,
    _resolve_base_var,
    _resolve_url_expr,
    _score_candidate,
)
from generation.generator import _doc_type_workflow_evidence


BASE_URL = "https://dgshippingbsid.example/"

# Real-shape fixture: base var resolves to empty string; the verify call is
# one fetch among noise; options object uses fetch defaults (GET).
SPA_BUNDLE_JS = '''
const Rt="";
const xg="/seafarer";
const sessionFetch=async()=>{const e=await fetch(`${Rt}/api/user/session`);return e.json()};
const loadCaptcha=async()=>{const e=await(await fetch(`${Rt}/api/captcha/generate`)).json();
  sessionStorage.setItem("sidCaptchaId",e.captchaId)};
const logout=async()=>{await fetch(`${Rt}/api/auth/logout`,{method:"POST"})};
const otpSend=async t=>{await fetch(`${Rt}/api/auth/send-login-otp`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({userId:t})})};
const verifySid=async()=>{const B=c.trim(),E=code.trim();
  const q=sessionStorage.getItem("sidCaptchaId");
  const Q=await fetch(`${Rt}/seafarer/sid/verify?inputvalue=${encodeURIComponent(B)}&captcha=${encodeURIComponent(E.trim())}&captchaId=${encodeURIComponent(q||"")}`);
  return await Q.json()};
'''


class TestBundleContractRealShape(unittest.TestCase):
    """The live-confirmed SPA shape must take the narrow path."""

    def setUp(self):
        self.contract = _extract_xhr_contract_from_bundles(SPA_BUNDLE_JS, BASE_URL)

    def test_contract_found(self):
        self.assertIsNotNone(self.contract)

    def test_endpoint_resolved_through_empty_base_var(self):
        self.assertEqual(
            self.contract["endpoint"],
            "https://dgshippingbsid.example/seafarer/sid/verify",
        )

    def test_verb_defaults_to_get(self):
        # The real call passes no options object -> fetch()'s default GET.
        self.assertEqual(self.contract["verb"], "GET")

    def test_dynamic_params_exclude_captcha_session_values(self):
        # Captcha params are session values, not document fields: they must
        # be split out of dynamic_params so the LLM never maps them.
        self.assertEqual(self.contract["dynamic_params"], ["inputvalue"])

    def test_captcha_params_classified(self):
        self.assertEqual(
            self.contract["captcha_params"],
            {"captcha": "{{captcha_text}}", "captchaId": "{{captcha_id}}"},
        )

    def test_captcha_endpoint_found(self):
        self.assertEqual(
            self.contract["captcha_endpoint"],
            "https://dgshippingbsid.example/api/captcha/generate",
        )

    def test_param_location_is_query(self):
        self.assertEqual(self.contract["param_location"], "query")

    def test_noise_calls_not_elected(self):
        endpoint = self.contract["endpoint"]
        for noise in ("/api/user/session", "/api/auth/logout",
                      "/api/captcha/generate", "/api/auth/send-login-otp"):
            self.assertNotIn(noise, endpoint)


class TestBundleContractStrictness(unittest.TestCase):
    """A wrong extraction is worse than no extraction."""

    def test_empty_bundle_returns_none(self):
        self.assertIsNone(_extract_xhr_contract_from_bundles("", BASE_URL))
        self.assertIsNone(_extract_xhr_contract_from_bundles(None, BASE_URL))

    def test_no_fetch_calls_returns_none(self):
        self.assertIsNone(_extract_xhr_contract_from_bundles(
            'const a="just some code"; console.log(a);', BASE_URL))

    def test_bare_parameterized_fetch_without_verify_signal_returns_none(self):
        # Has params, but the path carries no verify/lookup signal at all.
        js = 'const f=async()=>{await fetch(`${Rt}/api/prefs/items?page=${p}&size=${s}`)};'
        self.assertIsNone(_extract_xhr_contract_from_bundles(
            'const Rt="";' + js, BASE_URL))

    def test_static_only_params_return_none(self):
        # Params exist but none are template-dynamic -> no per-document input.
        js = 'const v=async()=>{await fetch(`${Rt}/api/verify/all?mode=full`)}};'
        self.assertIsNone(_extract_xhr_contract_from_bundles(
            'const Rt="";' + js, BASE_URL))

    def test_unresolvable_base_var_returns_none(self):
        js = 'const v=async()=>{await fetch(`${MysteryBase}/verify?docNo=${d}`)};'
        self.assertIsNone(_extract_xhr_contract_from_bundles(js, BASE_URL))

    def test_dynamic_path_segment_returns_none(self):
        # The PATH itself varies per user -> not a fixed endpoint.
        js = 'const v=async id=>{await fetch(`${Rt}/users/${id}/verify?mode=${m}`)};'
        self.assertIsNone(_extract_xhr_contract_from_bundles(
            'const Rt="";' + js, BASE_URL))

    def test_admin_noise_is_never_elected_over_verify(self):
        # Both calls resolve; the verify call must outscore the admin call.
        js = (
            'const Rt="";'
            'const a=async d=>{await fetch(`${Rt}/admin/records?docNo=${d}`)};'
            'const v=async d=>{await fetch(`${Rt}/public/verify?docNo=${d}`)};'
        )
        contract = _extract_xhr_contract_from_bundles(js, BASE_URL)
        self.assertIsNotNone(contract)
        self.assertIn("/public/verify", contract["endpoint"])

    def test_all_captcha_params_contract_returns_none(self):
        # Every parameter is a captcha session value — nothing maps to a
        # document field, so this cannot be a verification contract.
        js = (
            'const Rt="";'
            'const v=async(c,i)=>{await fetch(`${Rt}/api/verify?captcha=${c}&captchaId=${i}`)};'
        )
        self.assertIsNone(_extract_xhr_contract_from_bundles(js, BASE_URL))


class TestHelpers(unittest.TestCase):

    def test_template_params(self):
        query = "inputvalue=${encodeURIComponent(B)}&captcha=${x}&captchaId=${q||''}&mode=full"
        self.assertEqual(
            _extract_template_params(query),
            ["inputvalue", "captcha", "captchaId"],
        )

    def test_resolve_base_var_literal(self):
        self.assertEqual(_resolve_base_var("Rt", 'const Rt="https://api.example.com";'),
                         "https://api.example.com")

    def test_resolve_base_var_empty_literal(self):
        self.assertEqual(_resolve_base_var("Rt", 'const Rt="";'), "")

    def test_resolve_base_var_origin(self):
        self.assertEqual(_resolve_base_var("Rt", 'const Rt=location.origin;'), "__ORIGIN__")

    def test_resolve_base_var_unresolvable(self):
        self.assertIsNone(_resolve_base_var("Rt", 'const Rt=computeBase();'))

    def test_resolve_url_expr_chained_base_vars(self):
        js = 'const Rt="";const xg="/seafarer";'
        self.assertEqual(
            _resolve_url_expr("${Rt}${xg}/sid/verify", js, BASE_URL),
            "https://dgshippingbsid.example/seafarer/sid/verify",
        )

    def test_resolve_url_expr_plain_path(self):
        self.assertEqual(
            _resolve_url_expr("/api/verify?docNo=${d}", "", BASE_URL),
            "https://dgshippingbsid.example/api/verify",
        )

    def test_score_prefers_path_terminal_verify(self):
        params = ["a", "b"]
        self.assertGreater(
            _score_candidate("https://x.example/seafarer/sid/verify", params),
            _score_candidate("https://x.example/verify/portal/home", params),
        )


class TestMethodBuilderCaptchaSequence(unittest.TestCase):
    """_build_method_from_contract must emit the captcha sequence engine-side."""

    BASE_CONTRACT = {
        "endpoint": "https://x.example/seafarer/sid/verify",
        "verb": "GET",
        "param_location": "query",
        "dynamic_params": ["inputvalue"],
        "static_params": {},
    }

    def _build(self, contract):
        from generation.generator import _build_method_from_contract
        return _build_method_from_contract(
            param_mapping={"inputvalue": "{{document_number}}"},
            required_inputs=["document_number"],
            redacted_profile={"document_type": "SID", "issuing_country": "India"},
            source_info={"source_url": "https://x.example/"},
            xhr_contract=contract,
            expected_responses={"comparison_mode": "field_match"},
        )

    def test_captcha_contract_emits_three_step_sequence(self):
        contract = dict(self.BASE_CONTRACT)
        contract["captcha_params"] = {
            "captcha": "{{captcha_text}}", "captchaId": "{{captcha_id}}"
        }
        contract["captcha_endpoint"] = "https://x.example/api/captcha/generate"
        method = self._build(contract)
        actions = [s["action"] for s in method.execution_steps]
        self.assertEqual(actions, ["FETCH_CAPTCHA", "SOLVE_CAPTCHA", "REQUEST"])
        request_step = method.execution_steps[-1]
        self.assertEqual(request_step["params"]["captcha"], "{{captcha_text}}")
        self.assertEqual(request_step["params"]["captchaId"], "{{captcha_id}}")
        self.assertEqual(request_step["params"]["inputvalue"], "{{document_number}}")

    def test_plain_contract_emits_single_request(self):
        method = self._build(dict(self.BASE_CONTRACT))
        actions = [s["action"] for s in method.execution_steps]
        self.assertEqual(actions, ["REQUEST"])

    def test_captcha_params_without_endpoint_omits_captcha_steps(self):
        contract = dict(self.BASE_CONTRACT)
        contract["captcha_params"] = {"captcha": "{{captcha_text}}"}
        # captcha_endpoint missing -> steps omitted (method will fail
        # validation, which is the honest outcome), not fabricated.
        method = self._build(contract)
        actions = [s["action"] for s in method.execution_steps]
        self.assertEqual(actions, ["REQUEST"])


class TestDocTypeWorkflowEvidence(unittest.TestCase):
    """The IN_SID guard must generalize — no hardcoded site/doc names."""

    def test_option_evidence(self):
        options = [{"select": "cmbSearch_by", "value": "Sid", "text": "SID"}]
        self.assertTrue(_doc_type_workflow_evidence("IN_SID", options, "/search"))

    def test_path_evidence_spa(self):
        # SPA shell: zero workflow options, but the API path carries the token.
        self.assertTrue(_doc_type_workflow_evidence("IN_SID", [], "https://x.example/seafarer/sid/verify"))

    def test_no_evidence_fails_closed(self):
        self.assertFalse(_doc_type_workflow_evidence("IN_SID", [], "https://x.example/api/lookup"))

    def test_wrong_doc_token_fails(self):
        self.assertFalse(_doc_type_workflow_evidence("IN_SID", [], "https://x.example/indos/verify"))

    def test_other_doc_types_get_the_same_rule(self):
        self.assertTrue(_doc_type_workflow_evidence("IN_INDOS", [], "https://x.example/indos/check"))
        self.assertFalse(_doc_type_workflow_evidence("IN_INDOS", [], "https://x.example/sid/verify"))


if __name__ == "__main__":
    unittest.main()
