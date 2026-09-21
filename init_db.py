import sqlite3
import csv
import os

DB_PATH = 'verification_sources.db'
TSV_PATH = 'sources_data.tsv'

def create_db():
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Create the table schema. 
    # Since headers are missing, we use generic but descriptive names based on manual inspection.
    cursor.execute('''
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            country TEXT,
            url TEXT,
            access_type TEXT,
            flag_1 TEXT,
            flag_2 TEXT,
            flag_3 TEXT,
            flag_4 TEXT,
            flag_5 TEXT,
            fallback_procedure TEXT,
            description TEXT,
            detailed_notes TEXT,
            supported_doc_types TEXT
        )
    ''')

    with open(TSV_PATH, 'r', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            # Skip empty rows
            if not row or not "".join(row).strip():
                continue
            
            # Ensure the row has exactly 11 columns to match our insert schema, pad if necessary
            row = row + [''] * (11 - len(row))
            row = row[:11]
            
            cursor.execute('''
                INSERT INTO sources (
                    country, url, access_type, flag_1, flag_2, flag_3, 
                    flag_4, flag_5, fallback_procedure, description, detailed_notes,
                    supported_doc_types
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', row + [
                    "IN_INDOS" if row[0].strip().upper() == "INDIA" and "indos" in row[1].lower() else ""
                ])

    conn.commit()
    conn.close()
    print(f"Database created successfully at {DB_PATH}")

if __name__ == '__main__':
    create_db()
