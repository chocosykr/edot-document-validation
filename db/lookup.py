import sqlite3
import os
from registry.document_types import country_key, profile_document_type_key

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'verification_sources.db')

def dict_factory(cursor: sqlite3.Cursor, row: tuple) -> dict:
    return {column[0]: row[index] for index, column in enumerate(cursor.description)}

def lookup_source(redacted_profile: dict) -> list[dict]:
    """Return sources matching exact normalized country and document compatibility."""
    return _lookup(redacted_profile, require_doc_match=True)


def lookup_country_sources(redacted_profile: dict) -> list[dict]:
    """Same-country sources regardless of document-type compatibility.

    Used for source REUSE: an authority that verifies one document type
    (esamudra: INDOS) often verifies sibling types on the same portal (the
    same page's search-type selector lists CDC/DC/COP/...). The caller must
    confirm document-type compatibility from the SOURCE PAGE's own evidence
    before generating — a same-country match alone proves nothing.
    """
    return _lookup(redacted_profile, require_doc_match=False)


def _lookup(redacted_profile: dict, require_doc_match: bool = True) -> list[dict]:
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
        if not require_doc_match:
            results.append(row)
            continue
        supported = {
            item.strip()
            for item in (row.get("supported_doc_types") or "").split(",")
            if item.strip()
        }
        if document_key is not None and document_key in supported:
            results.append(row)
    return results


def remember_source(country: str, url: str, document_key: str) -> bool:
    """Persist a CONFIRMED verification source discovered in the field.

    When discovery + generation + live validation have proven that ``url``
    verifies ``document_key`` documents for ``country``, the source is saved
    so every future document of that type routes straight to generation —
    no search API required (Tavily quota and outages then degrade only
    FIRST contact with a new registry, never the system as a whole).

    Only called after the full chain has succeeded; never on a guess.
    Idempotent on (country, url, document_key).
    """
    if not country or not url or not document_key:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(sources)")}
        if "supported_doc_types" not in columns:
            conn.execute(
                "ALTER TABLE sources ADD COLUMN supported_doc_types TEXT DEFAULT ''"
            )
        existing = conn.execute(
            "SELECT id, supported_doc_types FROM sources WHERE url = ? AND upper(country) = upper(?)",
            (url, country),
        ).fetchone()
        if existing:
            supported = {
                item.strip()
                for item in (existing[1] or "").split(",")
                if item.strip()
            }
            if document_key in supported:
                conn.close()
                return False
            supported.add(document_key)
            conn.execute(
                "UPDATE sources SET supported_doc_types = ? WHERE id = ?",
                (", ".join(sorted(supported)), existing[0]),
            )
        else:
            conn.execute(
                "INSERT INTO sources (country, url, access_type, description, supported_doc_types) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    country,
                    url,
                    "CONFIRMED BY PIPELINE",
                    f"Discovered + live-validated by the pipeline for {document_key}",
                    document_key,
                ),
            )
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        print(f"Warning: could not remember source {url}: {e}")
        return False
