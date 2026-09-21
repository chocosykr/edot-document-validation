import unittest
import os
from registry.models import ValidationMethod, MethodType, MethodStatus
from registry.repository import MethodRegistry

class TestMethodRegistry(unittest.TestCase):
    def setUp(self):
        # Use an in-memory DB for tests if possible, or a temp file
        self.db_path = "test_registry.db"
        self.registry = MethodRegistry(db_path=self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_create_and_retrieve_method(self):
        method = ValidationMethod(
            method_id="M_TEST_001",
            document_type="CDC",
            country="India",
            method_type=MethodType.HTTP,
            source_url="https://example.com/api",
            required_inputs=["document_number", "dob"]
        )
        
        # Test Pydantic validation
        self.assertEqual(method.status, MethodStatus.TESTING)
        self.assertEqual(method.version, 1)

        # Register
        self.registry.register_method(method)
        
        # Retrieve
        retrieved = self.registry.get_method("M_TEST_001")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.country, "India")
        self.assertEqual(retrieved.method_type, MethodType.HTTP)
        self.assertEqual(retrieved.required_inputs, ["document_number", "dob"])

    def test_find_methods(self):
        m1 = ValidationMethod(
            method_id="M1", country="India", document_type="CDC",
            method_type=MethodType.HTTP, source_url="url1"
        )
        m2 = ValidationMethod(
            method_id="M2", country="Panama", document_type="CoC",
            method_type=MethodType.WEB_FORM, source_url="url2"
        )
        self.registry.register_method(m1)
        self.registry.register_method(m2)
        
        results = self.registry.find_methods(country="India")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].method_id, "M1")
        
        results2 = self.registry.find_methods(document_type="CoC")
        self.assertEqual(len(results2), 1)
        self.assertEqual(results2[0].method_id, "M2")

    def test_update_status(self):
        m1 = ValidationMethod(
            method_id="M1", country="India", document_type="CDC",
            method_type=MethodType.HTTP, source_url="url1"
        )
        self.registry.register_method(m1)
        
        self.registry.update_status("M1", MethodStatus.ACTIVE)
        
        retrieved = self.registry.get_method("M1")
        self.assertEqual(retrieved.status, MethodStatus.ACTIVE)

if __name__ == "__main__":
    unittest.main()
