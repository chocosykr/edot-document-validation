import sqlite3
import os
from registry.document_types import country_key, profile_document_type_key

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'verification_sources.db')

def dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}

def lookup_source(redacted_profile: dict) -> list[dict]:
    """Return sources matching exact normalized country and document compatibility."""
    if not os.path.exists(DB_PATH):
        print(f"Warning: Database not found at {DB_PATH}")
        return []

    conn = sqlite3.connect(DB_PATH)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
    if "supported_doc_types" not in columns:
        conn.execute("ALTER TABLE sources ADD COLUMN supported_doc_types TEXT DEFAULT ''")
        conn.execute(
            "UPDATE sources SET supported_doc_types = 'IN_INDOS' "
            "WHERE upper(country) = 'INDIA' AND lower(url) LIKE '%indos%'"
        )
        conn.commit()
    conn.row_factory = dict_factory

    country = redacted_profile.get("issuing_country")
    
    # Try fallback to flag_state if country is missing
    if not country:
        authorities = redacted_profile.get("authorities", {})
        if isinstance(authorities, dict):
            country = authorities.get("flag_state")
        
    if not country:
        return []

    document_key = profile_document_type_key(redacted_profile)
    country_code = country_key(country)
    rows = conn.execute("SELECT * FROM sources").fetchall()
    conn.close()
    
    results = []
    for row in rows:
        if country_key(row.get("country", "")) != country_code:
            continue
        supported = {
            item.strip()
            for item in (row.get("supported_doc_types") or "").split(",")
            if item.strip()
        }
        if document_key is not None and document_key in supported:
            results.append(row)
    return results
