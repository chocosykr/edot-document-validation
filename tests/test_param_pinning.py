"""Regression tests for the 2026-09-29 false-rejection incident.

The pinning loop leaked onto VALUE params (document number / date of
birth): txtNo was pinned to the literal "Indos", so every genuine
document was reported not-found. Pinning must only ever touch dispatch
selectors (params corresponding to a page <select>), and only from that
select's own options.
"""

import unittest

from generation.param_mapping import (
    _infer_workflow_params,
    _is_dispatch_selector,
    _resolve_param_mapping,
)


# The esamudra checker page's real dispatch select (value/text pairs as
# served), used here as structural evidence — no portal-specific code.
_ESAMUDRA_LIKE_OPTIONS = [
    {"select": "cmbSearch_by", "value": "GMDSS", "text": "GMDSS"},
    {"select": "cmbSearch_by", "value": "DC", "text": "COP - DC Endorsement"},
    {"select": "cmbSearch_by", "value": "COP", "text": "COP - DC Basic"},
    {"select": "cmbSearch_by", "value": "PP", "text": "Passport"},
    {"select": "cmbSearch_by", "value": "Indos", "text": "INDoS"},
    {"select": "cmbSearch_by", "value": "CDC", "text": "CDC"},
]


class TestDispatchSelectorDetection(unittest.TestCase):
    def test_selector_detected_via_camel_case_vocabulary(self):
        opts = [{"select": "cmbSearch_by", "value": "Indos", "text": "INDoS"}]
        self.assertTrue(_is_dispatch_selector("searchType", opts))

    def test_value_param_is_not_a_selector(self):
        opts = [{"select": "cmbSearch_by", "value": "Indos", "text": "INDoS"}]
        self.assertFalse(_is_dispatch_selector("txtNo", opts))
        self.assertFalse(_is_dispatch_selector("dob", opts))


class TestWorkflowParamPinning(unittest.TestCase):
    def test_value_params_never_pinned_selector_pinned(self):
        """The exact live shape of the INDOS run: txtNo/dob mapped to value
        fields, searchType mapped to document_type_key. Only searchType may
        be pinned — and only to its own select's option value."""
        contract = {
            "dynamic_params": ["txtNo", "dob", "searchType"],
            "static_params": {"processId": "PPIndosCheck"},
            "workflow_options": _ESAMUDRA_LIKE_OPTIONS,
        }
        llm_mapping = {
            "param_mapping": {
                "txtNo": "{{document_number}}",
                "dob": "{{date_of_birth}}",
                "searchType": "{{document_type_key}}",
            },
            "required_inputs": ["document_number", "date_of_birth"],
        }
        profile = {
            "document_type":
                "INDIAN NATIONAL DATABASE OF SEAFARERS (INDOS) Certificate",
            "issuing_country": "INDIA",
        }

        workflow = _infer_workflow_params(llm_mapping, contract, profile)

        self.assertEqual(workflow, {"searchType": "Indos"})
        self.assertNotIn("txtNo", workflow)
        self.assertNotIn("dob", workflow)

    def test_cdc_profile_pins_cdc_option(self):
        contract = {
            "dynamic_params": ["txtNo", "dob", "searchType"],
            "workflow_options": _ESAMUDRA_LIKE_OPTIONS,
        }
        llm_mapping = {"param_mapping": {"searchType": "{{document_type_key}}"}}
        profile = {
            "document_type": "Continuous Discharge Certificate (CDC)",
            "issuing_country": "INDIA",
        }

        workflow = _infer_workflow_params(llm_mapping, contract, profile)

        self.assertEqual(workflow, {"searchType": "CDC"})

    def test_punted_selector_mapping_still_pinned(self):
        """A null mapping on a real selector must not skip pinning (the
        2026-09-28 'please try later' incident)."""
        contract = {
            "dynamic_params": ["txtNo", "searchType"],
            "workflow_options": _ESAMUDRA_LIKE_OPTIONS,
        }
        llm_mapping = {"param_mapping": {"txtNo": "{{document_number}}", "searchType": None}}
        profile = {"document_type": "Continuous Discharge Certificate (CDC)"}

        workflow = _infer_workflow_params(llm_mapping, contract, profile)

        self.assertEqual(workflow, {"searchType": "CDC"})


class TestParamMappingResolution(unittest.TestCase):
    def test_value_placeholders_survive_no_literals_injected(self):
        """The mapping resolver must keep {{placeholders}} for value params
        and fill punted ones from the well-known-name table — never emit a
        literal page value into a value param."""
        llm_mapping = {
            "param_mapping": {"txtNo": "{{document_number}}", "dob": None},
            "required_inputs": ["document_number"],
        }
        contract = {"dynamic_params": ["txtNo", "dob"]}

        param_mapping, required = _resolve_param_mapping(llm_mapping, contract)

        self.assertEqual(param_mapping["txtNo"], "{{document_number}}")
        self.assertEqual(param_mapping["dob"], "{{date_of_birth}}")
        self.assertIn("date_of_birth", required)
        for value in param_mapping.values():
            self.assertNotIn("Indos", str(value))
            self.assertNotEqual(str(value).strip().upper(), "CDC")


if __name__ == "__main__":
    unittest.main()
