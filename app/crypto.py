"""
AES-256-GCM encryption for PHI fields and HMAC-SHA256 phone fingerprinting.

  encrypt(text)      -> bytes  (nonce + ciphertext+tag), or None for None input
  decrypt(blob)      -> str,   or None for None input
  phone_hash(phone)  -> hex string (HMAC-SHA256 keyed fingerprint)

Run `python -m app.crypto` to print a fresh ENCRYPTION_KEY for your .env.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import os
import secrets
import warnings


_DEV_KEY = b"insecure-dev-key-do-not-use-prod"   # exactly 32 bytes


def _key() -> bytes:
    raw = os.getenv("ENCRYPTION_KEY")
    if not raw:
        warnings.warn(
            "ENCRYPTION_KEY not set — using insecure dev key. Fine for testing only.",
            stacklevel=3,
        )
        return _DEV_KEY
    return base64.b64decode(raw)


def encrypt(text: str | None) -> bytes | None:
    if text is None:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    nonce = secrets.token_bytes(12)
    # AESGCM.encrypt returns ciphertext + 16-byte tag appended
    ct = AESGCM(_key()).encrypt(nonce, text.encode("utf-8"), None)
    return nonce + ct   # 12-byte nonce || ciphertext+tag


def decrypt(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    return AESGCM(_key()).decrypt(blob[:12], blob[12:], None).decode("utf-8")


def phone_hash(phone: str) -> str:
    key = (os.getenv("PHONE_HASH_HMAC_KEY") or "insecure-hmac-key").encode("utf-8")
    return hmac.new(key, (phone or "").encode("utf-8"), hashlib.sha256).hexdigest()


if __name__ == "__main__":
    key = base64.b64encode(secrets.token_bytes(32)).decode()
    print(f"ENCRYPTION_KEY={key}")
