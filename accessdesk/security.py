"""Signed tokens, the injection scanner and untrusted-text wrapping.

Two kinds of HMAC-signed tokens, both minted by code and never shown to the model:
- identity: who is logged in (like an SSO / OAuth on-behalf-of token). Every MCP tool that acts for a
  person takes it, so the model cannot claim to be someone else ("I'm the CTO").
- grant: proof that the policy, or every required approver, said yes to one ticket, one person and one
  entitlement. The IAM server will not change access without it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import uuid

PATTERNS = [
    ("addresses an AI", r"\b(ai|agent|assistant|bot|automated system)\b.{0,40}\b(must|should|note|instruct)"),
    ("override attempt", r"\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|all|your)\b.{0,20}\b(instructions?|rules?)"),
    ("claims pre-approval", r"\b(pre-?approved|already approved|approved by (it|the ceo|management))\b"),
    ("asks to grant others", r"\bgrant\b.{0,40}\b(to|for)\b.{0,15}\b(employee|user|E\d{3})"),
]
_RX = [(label, re.compile(rx, re.I | re.S)) for label, rx in PATTERNS]


def scan(text: str) -> list[dict]:
    hits = []
    for label, rx in _RX:
        m = rx.search(text or "")
        if m:
            s, e = max(m.start() - 30, 0), min(m.end() + 30, len(text))
            hits.append({"rule": label, "snippet": " ".join(text[s:e].split())})
    return hits


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(secret: str, claims: dict, ttl_s: int) -> str:
    body = {**claims, "exp": int(time.time()) + ttl_s, "jti": uuid.uuid4().hex[:16]}
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    return f"{_b64(raw)}.{_b64(hmac.new(secret.encode(), raw, hashlib.sha256).digest())}"


def verify(secret: str, token: str, typ: str) -> tuple[bool, str, dict]:
    try:
        raw_s, sig_s = token.split(".")
        raw, sig = _unb64(raw_s), _unb64(sig_s)
    except Exception:
        return False, "malformed token", {}
    if not hmac.compare_digest(sig, hmac.new(secret.encode(), raw, hashlib.sha256).digest()):
        return False, "bad signature", {}
    claims = json.loads(raw)
    if claims.get("typ") != typ:
        return False, f"not a {typ} token", claims
    if claims["exp"] < time.time():
        return False, "token expired", claims
    return True, "ok", claims


def identity_token(secret: str, employee_id: str, ttl_s: int = 8 * 3600) -> str:
    return sign(secret, {"typ": "identity", "sub": employee_id}, ttl_s)


def grant_token(secret: str, ticket_id: str, employee_id: str, action: str, entitlement_id: str | None,
                days: int | None, approvers: list[str], ttl_s: int = 900) -> str:
    return sign(secret, {"typ": "grant", "tck": ticket_id, "emp": employee_id, "act": action, "ent": entitlement_id,
                         "days": days, "by": approvers}, ttl_s)
