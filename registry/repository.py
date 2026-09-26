import sqlite3
import json
import os
from typing import List, Optional

from registry.models import ValidationMethod, MethodStatus, MethodType
from registry.document_types import document_type_key, country_key

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'method_registry.db')

class MethodRegistry:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS methods (
                method_id TEXT PRIMARY KEY,
                document_type TEXT,
                country TEXT,
                issuer TEXT,
                method_type TEXT,
                version INTEGER,
                source_url TEXT,
                required_inputs TEXT,
                execution_steps TEXT,
                expected_responses TEXT,
                limitations TEXT,
                status TEXT
            )
        ''')
        conn.commit()
        # Methods created before canonical document keys were introduced must
        # not remain reusable ACTIVE methods under the new routing contract.
        cursor.execute(
            "UPDATE methods SET status = ? "
            "WHERE status = ? AND expected_responses NOT LIKE '%document_type_key%'",
            (MethodStatus.INACTIVE.value, MethodStatus.ACTIVE.value),
        )
        conn.commit()
        conn.close()

    def _dict_factory(self, cursor, row):
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def register_method(self, method: ValidationMethod):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT OR REPLACE INTO methods (
                method_id, document_type, country, issuer, method_type, 
                version, source_url, required_inputs, execution_steps, 
                expected_responses, limitations, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            method.method_id,
            method.document_type,
            method.country,
            method.issuer,
            method.method_type.value,
            method.version,
            method.source_url,
            json.dumps(method.required_inputs),
            json.dumps(method.execution_steps),
            json.dumps(method.expected_responses),
            json.dumps(method.limitations),
            method.status.value
        ))
        
        conn.commit()
        conn.close()

    def get_method(self, method_id: str) -> Optional[ValidationMethod]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = self._dict_factory
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM methods WHERE method_id = ?', (method_id,))
        row = cursor.fetchone()
        conn.close()
        
        if not row:
            return None
            
        return ValidationMethod(
            method_id=row['method_id'],
            document_type=row['document_type'],
            country=row['country'],
            issuer=row['issuer'],
            method_type=MethodType(row['method_type']),
            version=row['version'],
            source_url=row['source_url'],
            required_inputs=json.loads(row['required_inputs']),
            execution_steps=json.loads(row['execution_steps']),
            expected_responses=json.loads(row['expected_responses']),
            limitations=json.loads(row['limitations']),
            status=MethodStatus(row['status'])
        )

    def find_methods(self, country: Optional[str] = None, document_type: Optional[str] = None) -> List[ValidationMethod]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = self._dict_factory
        cursor = conn.cursor()
        
        query = 'SELECT * FROM methods WHERE 1=1'
        params = []
        
        if country:
            query += ' AND country LIKE ?'
            params.append(f"%{country}%")
        
        if document_type:
            # Match on the CANONICAL document-type key, not raw text. The
            # profile's free-text document_type (e.g. "INDos Certificate")
            # rarely equals the method's stored wording
            # ("INDIAN NATIONAL DATABASE OF SEAFARERS (INDOS) CERTIFICATE"),
            # and SQL LIKE substring matching breaks on punctuation — a
            # canonically-matching ACTIVE method was invisible to the engine
            # because of the ")" in the stored text. Fall back to LIKE only
            # when the profile text yields no canonical key at all.
            canonical = document_type_key(document_type)
            if canonical:
                # A COC/CDC/SID registry is defined per country, so stored
                # methods carry country-scoped keys (MM_COC). Match both the
                # base key (legacy India-era rows) and the variant scoped to
                # the country filter supplied above, if any.
                from registry.document_types import COUNTRY_SCOPED_SUFFIXES
                suffix = COUNTRY_SCOPED_SUFFIXES.get(canonical)
                variants = [canonical]
                if suffix and country:
                    variants.append(f"{country_key(country)}_{suffix}")
                placeholders = ", ".join("?" for _ in variants)
                query += ' AND ('
                query += f' json_extract(expected_responses, "$.document_type_key") IN ({placeholders})'
                params.extend(variants)
                query += ' OR upper(document_type) = ?'
                params.append(document_type.upper())
                query += ')'
            else:
                query += ' AND document_type LIKE ?'
                params.append(f"%{document_type}%")
            
        cursor.execute(query, params)
        rows = cursor.fetchall()
        conn.close()
        
        results = []
        for row in rows:
            results.append(ValidationMethod(
                method_id=row['method_id'],
                document_type=row['document_type'],
                country=row['country'],
                issuer=row['issuer'],
                method_type=MethodType(row['method_type']),
                version=row['version'],
                source_url=row['source_url'],
                required_inputs=json.loads(row['required_inputs']),
                execution_steps=json.loads(row['execution_steps']),
                expected_responses=json.loads(row['expected_responses']),
                limitations=json.loads(row['limitations']),
                status=MethodStatus(row['status'])
            ))
            
        return results

    def update_status(self, method_id: str, status: MethodStatus):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('UPDATE methods SET status = ? WHERE method_id = ?', (status.value, method_id))
        conn.commit()
        conn.close()

    def update_expected_responses(self, method_id: str, expected_responses: dict) -> bool:
        """Persist expected_responses (field_mapping, not_found_signatures, ...).

        A field-comparison method is not fully known when it is generated: the
        response-field mapping and the site's confirmed not-found signature
        are only observable from its first real executions. Those observations
        belong on the SAME method row, not in a cascade of upserts that bump
        the version and reset the status.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE methods SET expected_responses = ? WHERE method_id = ?',
            (json.dumps(expected_responses or {}), method_id),
        )
        changed = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    def delete_method(self, method_id: str) -> bool:
        """Delete a method from the registry. Returns True if a row was deleted."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM methods WHERE method_id = ?', (method_id,))
        deleted = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return deleted
