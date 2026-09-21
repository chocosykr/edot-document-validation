"""
Unit tests for the deterministic XHR contract extractor (Component 1)
and the narrow mapping path (Component 3) in generation/generator.py.

The extractor is a pure function: no network, no LLM. The INDOS-confirmed
JS shape is the primary fixture.
"""

import unittest

from generation.generator import (
    _extract_xhr_contract,
    _extract_dynamic_param_names,
    _extract_static_params,
    _resolve_js_url,
    FAKE_PROBE_INPUTS,
)


# INDOS live-confirmed JS shape (legacy xmlHttp servlet pattern)
INDOS_JS = """
function checkIndos() {
    var xmlHttp = getXMLHttp();
    var txtNoVal = document.getElementById('txtNo').value;
    var dobVal = document.getElementById('dob').value;
    var url = "/esamudraUI/checkerajaxservlet";
    xmlHttp.open("POST", url + "?txtNo=" + txtNoVal + "&dob=" + dobVal + "&processId=PPIndosCheck&searchType=Indos", true);
    xmlHttp.send(null);
}
"""

BASE_URL = "http://220.156.189.33/esamudraUI/jsp/examination/checker/PP_IndosChecker.jsp"


class TestXhrExtractorIndos(unittest.TestCase):
    """The primary, live-confirmed contract."""

    def setUp(self):
        self.contract = _extract_xhr_contract(INDOS_JS, BASE_URL)

    def test_contract_found(self):
        self.assertIsNotNone(self.contract)

    def test_endpoint_resolved(self):
        self.assertEqual(
            self.contract["endpoint"],
            "http://220.156.189.33/esamudraUI/checkerajaxservlet",
        )

    def test_verb_is_post(self):
        self.assertEqual(self.contract["verb"], "POST")

    def test_send_null_means_query_location(self):
        self.assertEqual(self.contract["param_location"], "query")

    def test_dynamic_params_found(self):
        self.assertIn("txtNo", self.contract["dynamic_params"])
        self.assertIn("dob", self.contract["dynamic_params"])

    def test_static_params_found(self):
        self.assertEqual(
            self.contract["static_params"].get("processId"), "PPIndosCheck"
        )
        self.assertEqual(
            self.contract["static_params"].get("searchType"), "Indos"
        )

    def test_static_params_do_not_shadow_dynamic(self):
        for name in self.contract["dynamic_params"]:
            self.assertNotIn(name, self.contract["static_params"])


class TestXhrExtractorFallbacks(unittest.TestCase):
    """Strict success definition: partial matches must return None."""

    def test_empty_js_returns_none(self):
        self.assertIsNone(_extract_xhr_contract("", BASE_URL))
        self.assertIsNone(_extract_xhr_contract(None, BASE_URL))

    def test_no_xhr_returns_none(self):
        js = "console.log('nothing relevant here');"
        self.assertIsNone(_extract_xhr_contract(js, BASE_URL))

    def test_url_without_dynamic_params_returns_none(self):
        js = """
        var xmlHttp = getXMLHttp();
        var url = "/esamudraUI/checkerajaxservlet?processId=PPIndosCheck";
        xmlHttp.open("POST", url, true);
        xmlHttp.send(null);
        """
        self.assertIsNone(_extract_xhr_contract(js, BASE_URL))

    def test_unresolvable_url_returns_none(self):
        # URL built from a variable we can't trace
        js = """
        var xmlHttp = getXMLHttp();
        xmlHttp.open("POST", someUnknownVar, true);
        xmlHttp.send(null);
        """
        self.assertIsNone(_extract_xhr_contract(js, BASE_URL))

    def test_minified_js_returns_none(self):
        # One long line → avg_line_length > 200 → out of scope
        js = ("var x='" + "A" * 3000 + "';"
              "xmlHttp.open('POST','/e?txtNo='+n,true);xmlHttp.send(null);")
        self.assertIsNone(_extract_xhr_contract(js, BASE_URL))

    def test_send_data_means_body_location(self):
        js = """
        var xmlHttp = getXMLHttp();
        var url = "/esamudraUI/checkerajaxservlet?txtNo=" + txtNoVal;
        xmlHttp.open("POST", url, true);
        xmlHttp.send(postData);
        """
        contract = _extract_xhr_contract(js, BASE_URL)
        self.assertIsNotNone(contract)
        self.assertEqual(contract["param_location"], "body")

    def test_fetch_pattern(self):
        js = """
        fetch("/api/verify?docNo=" + docNumber, {method: "POST"})
        """
        contract = _extract_xhr_contract(js, BASE_URL)
        self.assertIsNotNone(contract)
        self.assertEqual(contract["verb"], "POST")
        self.assertIn("docNo", contract["dynamic_params"])


class TestHelpers(unittest.TestCase):

    def test_resolve_js_url_direct_literal(self):
        self.assertEqual(
            _resolve_js_url("'/esamudraUI/checkerajaxservlet'", "", BASE_URL),
            "http://220.156.189.33/esamudraUI/checkerajaxservlet",
        )

    def test_resolve_js_url_via_variable(self):
        js = 'var url = "/esamudraUI/checkerajaxservlet";'
        self.assertEqual(
            _resolve_js_url("url", js, BASE_URL),
            "http://220.156.189.33/esamudraUI/checkerajaxservlet",
        )

    def test_resolve_js_url_unknown_variable_returns_none(self):
        self.assertIsNone(_resolve_js_url("mystery", "", BASE_URL))

    def test_dynamic_param_names_from_concat(self):
        expr = 'url + "?txtNo=" + txtNoVal + "&dob=" + dobVal + "&processId=PPIndosCheck"'
        names = _extract_dynamic_param_names(expr, "var url = '/x';")
        self.assertEqual(names, ["txtNo", "dob"])

    def test_dynamic_and_static_params_from_multi_reassignment_chain(self):
        js = '''
        var url = "/esamudraUI/checkerajaxservlet";
        url = url + "?txtNo=" + txtNoVal;
        url = url + "&dob=" + dobVal;
        url = url + "&processId=PPIndosCheck";
        url = url + "&searchType=Indos";
        xmlHttp.open("POST", url, true);
        xmlHttp.send(null);
        '''
        contract = _extract_xhr_contract(js, BASE_URL)
        self.assertIsNotNone(contract)
        self.assertEqual(contract["dynamic_params"], ["txtNo", "dob"])
        self.assertEqual(contract["static_params"], {"processId": "PPIndosCheck", "searchType": "Indos"})

    def test_workflow_fixed_params_are_baked_into_static_and_excluded_from_llm_mapping(self):
        js = '''
        var xmlHttp = getXMLHttp();
        var processId = "PPIndosCheck";
        var searchType = document.form.cmbSearch_by.value;
        var url = "/esamudraUI/checkerajaxservlet";
        url = url + "?txtNo=" + txtNoVal;
        url = url + "&dob=" + dobVal;
        url = url + "&processId=" + processId;
        url = url + "&searchType=" + searchType;
        xmlHttp.open("POST", url, true);
        xmlHttp.send(null);
        '''
        contract = _extract_xhr_contract(js, BASE_URL, workflow_fixed_params={"searchType": "Indos"})
        self.assertEqual(contract["dynamic_params"], ["txtNo", "dob"])
        self.assertEqual(contract["static_params"]["processId"], "PPIndosCheck")
        self.assertEqual(contract["static_params"]["searchType"], "Indos")

    def test_static_params_from_concat(self):
        expr = 'url + "?txtNo=" + txtNoVal + "&processId=PPIndosCheck&searchType=Indos"'
        static = _extract_static_params(expr, "var url = '/x';")
        self.assertEqual(static, {"processId": "PPIndosCheck", "searchType": "Indos"})


class TestFakeProbeInputs(unittest.TestCase):
    """FAKE_PROBE_INPUTS must stay in sync with _GENERIC_TEST_CASES (C6)."""

    def test_fake_inputs_match_generic_test_case(self):
        from engine.validation_engine import _GENERIC_TEST_CASES

        structural = next(
            tc for tc in _GENERIC_TEST_CASES if tc.name == "structural_check"
        )
        self.assertEqual(structural.inputs, FAKE_PROBE_INPUTS)


if __name__ == "__main__":
    unittest.main()
