"""
Seed-credential storage (encrypted, at-rest)
=============================================

A *seed credential* is one real, confirmed-valid record for a specific
registry + document type, supplied ONCE per registry at onboarding time by a
consenting person. It exists solely to derive success/rejection discriminator
markers (difflib diff, Component 2) and to support manual re-probes
(Component 6). It is NOT a per-document credential: per-document lookup
values always come from the document's own raw extraction via the engine's
credential_provider — that boundary is unchanged.

Storage model (this implementation):
  - Fernet (AES-128-CBC + HMAC) encryption at rest, via the already-present
    `cryptography` package. AWS Secrets Manager/KMS was specified in the
    original instructions but is NOT in the current stack (no boto3);
    a backend swap behind this class's interface is the intended upgrade path.
  - One ciphertext per (registry, country, document_type) scope in a single
    local store file.
  - Plaintext seed values exist ONLY: (a) inside the encrypt/decrypt call,
    (b) in the probe's in-memory params. They are never logged, never
    written to the method registry, never echoed to reports.
  - A SHA-256 checksum of the PLAINTEXT seed is stored alongside the
    ciphertext. It is verified after decryption so a corrupted or
    wrongly-decrypted blob fails closed instead of yielding garbage probe
    inputs. The checksum cannot be used to recover the seed.
  - Key management: a Fernet key file (0600, gitignored, stored next to the
    seed store file). If it is lost, the seed must be re-onboarded — there
    is intentionally no recovery path.
"""

import base64
import hashlib
import json
import logging
import os
import stat
from pathlib import Path
from typing import Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

DEFAULT_SEED_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "seed_credentials.enc"
)
DEFAULT_KEY_PATH = DEFAULT_SEED_STORE_PATH + ".key"

_PBKDF2_ITERATIONS = 600_000


class SeedStoreError(RuntimeError):
    """Base error for seed-store operations."""


class SeedCorruptedError(SeedStoreError):
    """Decryption failed or checksum mismatch — the store must not be used."""


def seed_scope_key(country: str, document_type: str) -> str:
    """
    Canonical scope key for a seed credential. One seed per
    registry/document-type; a seed valid for one registry proves nothing
    about any other registry, so country + document_type must match exactly.
    """
    country_norm = (country or "").strip().upper()
    doc_norm = (document_type or "").strip().upper()
    if not country_norm or not doc_norm:
        raise SeedStoreError(
            "Seed scope requires both country and document_type; "
            f"got country={country!r}, document_type={document_type!r}."
        )
    return f"{country_norm}::{doc_norm}"


def _fernet_from_key_file(key_path: str) -> Fernet:
    """
    Load (creating on first use) the Fernet key.

    Priority order:
      1. DVS_SEED_STORE_KEY env var (base64 Fernet key) — for deployments
         that inject the key rather than keeping it on disk.
      2. Key file with passphrase-derived key material (PBKDF2-HMAC-SHA256,
         600k iterations). The passphrase itself comes from the
         DVS_SEED_STORE_PASSPHRASE env var; when absent a machine-local
         random salt file (0600) is created so the key is still stable
         across restarts without any human-managed secret.

    Fail-closed: any unreadable/undecryptable key raises SeedStoreError.
    """
    env_key = os.environ.get("DVS_SEED_STORE_KEY")
    if env_key:
        try:
            return Fernet(env_key.strip().encode("utf-8"))
        except Exception as e:
            raise SeedStoreError(f"DVS_SEED_STORE_KEY is not a valid Fernet key: {e}") from e

    key_path = key_path or DEFAULT_KEY_PATH

    if os.path.exists(key_path):
        try:
            with open(key_path, "rb") as f:
                key = f.read().strip()
        except OSError as e:
            raise SeedStoreError(f"Seed-store key file unreadable: {e}") from e
        if not key:
            raise SeedStoreError(f"Seed-store key file {key_path} is empty.")
        try:
            return Fernet(key)
        except Exception as e:
            raise SeedStoreError(f"Seed-store key file {key_path} holds an invalid key: {e}") from e

    # First run: create the key file.
    passphrase = os.environ.get("DVS_SEED_STORE_PASSPHRASE", "")
    if passphrase:
        salt_path = key_path + ".salt"
        if os.path.exists(salt_path):
            salt = Path(salt_path).read_bytes()
        else:
            salt = os.urandom(16)
            _write_private(salt_path, salt)
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        key = base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))
    else:
        key = Fernet.generate_key()

    _write_private(key_path, key)
    try:
        return Fernet(key)
    except Exception as e:
        raise SeedStoreError(f"Generated key is invalid: {e}") from e


def _write_private(path: str, data: bytes) -> None:
    """Write a file restricted to owner read/write (0600), best-effort."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _checksum(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def _normalize_seed(values: Dict[str, str]) -> Dict[str, str]:
    """Keep only the two known credential fields, non-empty, non-token."""
    clean: Dict[str, str] = {}
    for key in ("document_number", "date_of_birth"):
        value = values.get(key)
        if isinstance(value, str) and value.strip() and not value.strip().startswith("["):
            clean[key] = value.strip()
    return clean


def _normalize_workflow_params(values: Optional[Dict[str, str]]) -> Dict[str, str]:
    """Normalize workflow-fixed values such as searchType=Indos."""
    if not isinstance(values, dict):
        return {}
    clean: Dict[str, str] = {}
    for key, value in values.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(value, str):
            norm = value.strip()
        else:
            norm = str(value).strip()
        if norm and not norm.startswith("["):
            clean[key.strip()] = norm
    return clean


class SeedStore:
    """
    Encrypted local store for seed credentials, keyed by registry scope.
    """

    def __init__(
        self,
        store_path: Optional[str] = None,
        key_path: Optional[str] = None,
    ):
        # None (explicitly passed by CLIs whose flags default to None) must
        # fall back to the defaults, not override them.
        self.store_path = store_path or DEFAULT_SEED_STORE_PATH
        self.key_path = key_path or DEFAULT_KEY_PATH
        self._fernet = _fernet_from_key_file(self.key_path)
        self._ensure_store_file()

    # ------------------------------------------------------------------
    # File handling
    # ------------------------------------------------------------------

    def _ensure_store_file(self) -> None:
        if not os.path.exists(self.store_path):
            _write_private(self.store_path, b"{}")
        try:
            os.chmod(self.store_path, 0o600)
        except OSError:
            pass

    def _read_all(self) -> Dict[str, dict]:
        try:
            with open(self.store_path, "r", encoding="utf-8") as f:
                raw = f.read().strip()
        except OSError as e:
            raise SeedStoreError(f"Seed store unreadable: {e}") from e
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise SeedCorruptedError(f"Seed store is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise SeedCorruptedError("Seed store has an unexpected structure.")
        return data

    def _write_all(self, data: Dict[str, dict]) -> None:
        _write_private(self.store_path, json.dumps(data, indent=2).encode("utf-8"))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def store_seed(
        self,
        country: str,
        document_type: str,
        document_number: str,
        date_of_birth: str = "",
        workflow_params: Optional[Dict[str, str]] = None,
    ) -> None:
        """
        Encrypt and persist a seed credential for (country, document_type).

        Only non-empty, non-token values are accepted; a seed MUST include a
        document number (the rejection path needs no DOB, but the two-sided
        diff needs a real document number to produce a success marker).
        """
        scope = seed_scope_key(country, document_type)
        seed = _normalize_seed(
            {
                "document_number": document_number,
                "date_of_birth": date_of_birth,
            }
        )
        if "document_number" not in seed:
            raise SeedStoreError(
                "Refusing to store a seed without a real document number."
            )

        workflow = _normalize_workflow_params(workflow_params)
        payload = dict(seed)
        if workflow:
            payload["workflow_params"] = workflow

        plaintext = json.dumps(payload, sort_keys=True)
        token = self._fernet.encrypt(plaintext.encode("utf-8"))

        data = self._read_all()
        data[scope] = {
            "ciphertext": token.decode("ascii"),
            "checksum": _checksum(plaintext),
        }
        self._write_all(data)
        logger.info(
            "Seed credential stored for scope %s (values never logged).", scope
        )

    def has_seed(self, country: str, document_type: str) -> bool:
        scope = seed_scope_key(country, document_type)
        return scope in self._read_all()

    def get_seed(self, country: str, document_type: str) -> Optional[Dict[str, str]]:
        """
        Decrypt and return the seed for (country, document_type), or None if
        no seed exists for that scope. Raises SeedCorruptedError on tampering
        or corruption — never returns garbage probe inputs.
        """
        scope = seed_scope_key(country, document_type)
        data = self._read_all()
        entry = data.get(scope)
        if not entry:
            return None

        ciphertext = entry.get("ciphertext", "")
        checksum = entry.get("checksum", "")
        if not ciphertext or not checksum:
            raise SeedCorruptedError(f"Seed entry for {scope} is incomplete.")

        try:
            plaintext = self._fernet.decrypt(ciphertext.encode("ascii")).decode("utf-8")
        except InvalidToken as e:
            raise SeedCorruptedError(
                f"Seed for {scope} failed decryption (wrong key or tampered store)."
            ) from e

        if _checksum(plaintext) != checksum:
            raise SeedCorruptedError(
                f"Seed for {scope} failed checksum verification."
            )

        seed = json.loads(plaintext)
        result = _normalize_seed(seed)
        workflow = _normalize_workflow_params(seed.get("workflow_params"))
        if workflow:
            result["workflow_params"] = workflow
        return result

    def delete_seed(self, country: str, document_type: str) -> bool:
        scope = seed_scope_key(country, document_type)
        data = self._read_all()
        if scope not in data:
            return False
        del data[scope]
        self._write_all(data)
        return True
