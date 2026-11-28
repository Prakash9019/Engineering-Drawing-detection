"""Shared API-key auth dependency for local/private deployments."""
from __future__ import annotations
import hmac, os
from fastapi import Header, HTTPException
def require_api_key(x_api_key: str | None = Header(default=None)) -> str:
    expected=os.getenv("CDCI_API_KEY","").strip()
    required=os.getenv("CDCI_AUTH_REQUIRED","false").lower() in {"1","true","yes"}
    if not expected:
        if required: raise HTTPException(503,"CDCI_API_KEY is required but not configured")
        return "local-demo"
    if not x_api_key or not hmac.compare_digest(x_api_key,expected):
        raise HTTPException(401,"Invalid or missing X-API-Key")
    return "authenticated"
