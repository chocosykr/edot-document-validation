import sqlite3
import json
import os
from typing import List, Optional
from registry.models import ValidationMethod, MethodStatus, MethodType

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
