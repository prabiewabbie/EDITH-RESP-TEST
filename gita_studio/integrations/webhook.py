from __future__ import annotations

import hashlib
import hmac
import json
import os
from typing import Any

import httpx2 as httpx


def post_json(url_env: str, payload: dict[str, Any], timeout: float = 60) -> dict[str, Any]:
    url = os.environ.get(url_env)
    if not url:
        raise RuntimeError(f"{url_env} is not set")
    body = json.dumps(payload, ensure_ascii=False).encode()
    headers = {"Content-Type": "application/json"}
    if secret := os.environ.get("WEBHOOK_SECRET"):
        headers["X-Gita-Studio-Signature"] = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    resp = httpx.post(url, content=body, headers=headers, timeout=timeout)
    resp.raise_for_status()
    try:
        return resp.json()
    except ValueError:
        return {"status": resp.status_code, "text": resp.text[:500]}
