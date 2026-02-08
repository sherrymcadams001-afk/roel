from __future__ import annotations

import re
from dataclasses import dataclass

from config import settings


_PHONE_RE = re.compile(r"[^0-9+]")


def canonicalize_handle(handle: str) -> str:
    """Best-effort canonicalization for handles used across transports.

    - Preserves leading '+' for E.164-ish phone handles.
    - Strips whitespace and common punctuation.
    - Leaves non-phone handles (emails, group ids) mostly intact.
    """

    raw = (handle or "").strip()
    if not raw:
        return ""

    # Keep WhatsApp/iMessage phone handles stable.
    if raw.startswith("+") or raw.isdigit():
        cleaned = _PHONE_RE.sub("", raw)
        if cleaned.startswith("+"):
            return "+" + re.sub(r"\D", "", cleaned[1:])
        return "+" + re.sub(r"\D", "", cleaned)

    # Non-phone handles: normalize whitespace only.
    return raw


def get_allowlist() -> set[str]:
    """Canonical allowlist derived from settings (env or contacts dir)."""
    return {canonicalize_handle(h) for h in settings.get_target_handles() if canonicalize_handle(h)}


def get_operator_handle() -> str | None:
    op = settings.OPERATOR_HANDLE
    if not op:
        return None
    op_canon = canonicalize_handle(op)
    return op_canon if op_canon else None


@dataclass(frozen=True)
class InboundDecision:
    kind: str  # 'operator' | 'allowlisted' | 'unknown'
    handle: str


def decide_inbound(handle: str) -> InboundDecision:
    """Single source of truth for inbound routing decisions."""
    canon = canonicalize_handle(handle)
    op = get_operator_handle()
    if op and canon == op:
        return InboundDecision(kind="operator", handle=canon)

    if canon and canon in get_allowlist():
        return InboundDecision(kind="allowlisted", handle=canon)

    return InboundDecision(kind="unknown", handle=canon or (handle or "").strip())
