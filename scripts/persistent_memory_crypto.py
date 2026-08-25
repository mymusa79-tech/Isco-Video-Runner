from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC


FORMAT = "isco-agent-state"
VERSION = 2
KDF_NAME = "pbkdf2-hmac-sha256"
CIPHER_NAME = "aes-256-gcm"
PBKDF2_ITERATIONS = 600_000
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32

# The canonical production workflow currently needs one controlled migration from the
# pre-hardening OpenSSL-CBC state. Bind that compatibility window to the already-known
# next production sequence so an unprotected agent-state branch cannot replay the
# legacy ciphertext indefinitely after AES-GCM state has become canonical.
LEGACY_OPENSSL_MAGIC = b"Salted__"
LEGACY_MIGRATION_RUN_NUMBER = "111"
CANONICAL_PRODUCTION_WORKFLOW_MARKER = "/.github/workflows/produce-resilient-v4.yml@"


@dataclass(frozen=True)
class EnvelopeMetadata:
    run_number: str
    sequence: int
    previous_state_commit: str


def _b64e(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64d(name: str, value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise ValueError(f"persistent memory envelope missing {name}")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError(f"persistent memory envelope has invalid {name}") from exc


def _positive_sequence(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("persistent memory sequence must be a positive integer")
    try:
        sequence = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("persistent memory sequence must be a positive integer") from exc
    if sequence <= 0:
        raise ValueError("persistent memory sequence must be a positive integer")
    return sequence


def _validate_commit(value: object) -> str:
    commit = str(value or "").strip().lower()
    if commit == "none":
        return commit
    if len(commit) != 40 or any(ch not in "0123456789abcdef" for ch in commit):
        raise ValueError("persistent memory previous_state_commit must be a 40-hex SHA or 'none'")
    return commit


def metadata_from_values(*, run_number: str, previous_state_commit: str) -> EnvelopeMetadata:
    sequence = _positive_sequence(run_number)
    return EnvelopeMetadata(
        run_number=str(sequence),
        sequence=sequence,
        previous_state_commit=_validate_commit(previous_state_commit),
    )


def _aad(metadata: EnvelopeMetadata) -> bytes:
    payload = {
        "format": FORMAT,
        "version": VERSION,
        "metadata": {
            "run_number": metadata.run_number,
            "sequence": metadata.sequence,
            "previous_state_commit": metadata.previous_state_commit,
        },
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")


def _derive_key(secret: str, salt: bytes, *, iterations: int) -> bytes:
    if not secret:
        raise ValueError("STATE_ENCRYPTION_KEY is missing")
    if len(salt) != SALT_BYTES:
        raise ValueError("persistent memory KDF salt length is invalid")
    if iterations != PBKDF2_ITERATIONS:
        raise ValueError("persistent memory KDF iterations do not match the approved contract")
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=KEY_BYTES,
        salt=salt,
        iterations=iterations,
    )
    return kdf.derive(secret.encode("utf-8"))


def seal(plaintext: bytes, secret: str, *, metadata: EnvelopeMetadata) -> bytes:
    if not isinstance(plaintext, (bytes, bytearray)) or not plaintext:
        raise ValueError("persistent memory plaintext is empty")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    key = _derive_key(secret, salt, iterations=PBKDF2_ITERATIONS)
    ciphertext = AESGCM(key).encrypt(nonce, bytes(plaintext), _aad(metadata))
    envelope = {
        "format": FORMAT,
        "version": VERSION,
        "kdf": {
            "name": KDF_NAME,
            "iterations": PBKDF2_ITERATIONS,
            "salt_b64": _b64e(salt),
        },
        "cipher": {
            "name": CIPHER_NAME,
            "nonce_b64": _b64e(nonce),
            "ciphertext_b64": _b64e(ciphertext),
        },
        "metadata": {
            "run_number": metadata.run_number,
            "sequence": metadata.sequence,
            "previous_state_commit": metadata.previous_state_commit,
        },
    }
    return (json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")


def parse_envelope(payload: bytes) -> tuple[dict[str, Any], EnvelopeMetadata]:
    try:
        envelope = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("persistent memory payload is not an authenticated v2 envelope") from exc
    if not isinstance(envelope, dict):
        raise ValueError("persistent memory envelope root must be an object")
    if envelope.get("format") != FORMAT or envelope.get("version") != VERSION:
        raise ValueError("persistent memory envelope format/version is unsupported")
    kdf = envelope.get("kdf")
    cipher = envelope.get("cipher")
    raw_meta = envelope.get("metadata")
    if not isinstance(kdf, dict) or not isinstance(cipher, dict) or not isinstance(raw_meta, dict):
        raise ValueError("persistent memory envelope sections are malformed")
    if kdf.get("name") != KDF_NAME:
        raise ValueError("persistent memory KDF is unsupported")
    if cipher.get("name") != CIPHER_NAME:
        raise ValueError("persistent memory cipher is unsupported")
    sequence = _positive_sequence(raw_meta.get("sequence"))
    run_number = str(raw_meta.get("run_number") or "").strip()
    if run_number != str(sequence):
        raise ValueError("persistent memory run_number/sequence mismatch")
    metadata = EnvelopeMetadata(
        run_number=run_number,
        sequence=sequence,
        previous_state_commit=_validate_commit(raw_meta.get("previous_state_commit")),
    )
    return envelope, metadata


def open_envelope(payload: bytes, secret: str) -> tuple[bytes, EnvelopeMetadata]:
    envelope, metadata = parse_envelope(payload)
    kdf = envelope["kdf"]
    cipher = envelope["cipher"]
    iterations = _positive_sequence(kdf.get("iterations"))
    salt = _b64d("kdf.salt_b64", kdf.get("salt_b64"))
    nonce = _b64d("cipher.nonce_b64", cipher.get("nonce_b64"))
    ciphertext = _b64d("cipher.ciphertext_b64", cipher.get("ciphertext_b64"))
    if len(nonce) != NONCE_BYTES:
        raise ValueError("persistent memory AES-GCM nonce length is invalid")
    if len(ciphertext) < 17:
        raise ValueError("persistent memory AES-GCM ciphertext is truncated")
    key = _derive_key(secret, salt, iterations=iterations)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, _aad(metadata))
    except InvalidTag as exc:
        raise ValueError("persistent memory authentication tag verification failed") from exc
    return plaintext, metadata


def _enforce_legacy_migration_epoch(payload: bytes) -> None:
    if not payload.startswith(LEGACY_OPENSSL_MAGIC):
        return
    workflow_ref = str(os.environ.get("GITHUB_WORKFLOW_REF") or "")
    if CANONICAL_PRODUCTION_WORKFLOW_MARKER not in workflow_ref:
        return
    run_number = str(os.environ.get("GITHUB_RUN_NUMBER") or "").strip()
    if run_number != LEGACY_MIGRATION_RUN_NUMBER:
        raise ValueError(
            "legacy persistent-memory migration is authorized only for canonical production run 111"
        )


def is_authenticated_v2(payload: bytes) -> bool:
    # This guard deliberately runs before the v2 parser. In canonical production it
    # closes the legacy-compatibility epoch after Run 111, so rolling agent-state back
    # to a pre-GCM CBC blob cannot silently reopen migration on future run numbers.
    _enforce_legacy_migration_epoch(payload)
    try:
        parse_envelope(payload)
        return True
    except ValueError:
        return False
