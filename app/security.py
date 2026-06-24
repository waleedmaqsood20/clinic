"""
Webhook authentication for Retell.

Retell signs every request (custom-function calls AND call events) with an
`X-Retell-Signature` header. We verify it against your Retell API key using the
official SDK's verifier, over the *raw* request body. If no API key is set
(local dev), we skip the check.
"""
from __future__ import annotations
import os

from fastapi import HTTPException


def verify_retell_request(raw_body: bytes, signature: str | None) -> None:
    api_key = os.getenv("RETELL_API_KEY")
    if not api_key:
        return  # dev mode: no key configured
    try:
        from retell import Retell
    except ImportError as e:                      # pragma: no cover
        raise HTTPException(status_code=500,
                            detail="retell-sdk not installed") from e
    ok = Retell(api_key=api_key).verify(
        raw_body.decode("utf-8"), api_key, signature or "")
    if not ok:
        raise HTTPException(status_code=401, detail="invalid webhook signature")
