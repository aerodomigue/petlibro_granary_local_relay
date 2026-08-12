"""Central redaction for values that must never leave the process."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

PASSWORD_FIELD_PATTERN = re.compile(r'(?i)(password|secret)\s*([=:])\s*[^\s,}\]]+')
TOKEN_FIELD_PATTERN = re.compile(r'(?i)(authorization|token)\s*([=:])\s*[^\s,}\]]+')
REDACTED_VALUE = "<redacted>"


def sanitize_text(message: str, secrets: Iterable[str] = ()) -> str:
    """Redact credentials and secret-looking fields from a log/API string.

    Args:
        message: Text that may include sensitive values.
        secrets: Exact runtime secrets to remove as an additional safeguard.

    Returns:
        Sanitized text safe to expose via the dashboard.
    """
    sanitized = PASSWORD_FIELD_PATTERN.sub(r"\1\2" + REDACTED_VALUE, message)
    sanitized = TOKEN_FIELD_PATTERN.sub(r"\1\2" + REDACTED_VALUE, sanitized)
    for secret in secrets:
        if secret:
            sanitized = sanitized.replace(secret, REDACTED_VALUE)
    return sanitized


def mask_username(username: str) -> str:
    """Return a stable, non-secret representation of a MQTT username."""
    if len(username) <= 4:
        return "*" * len(username)
    return f"{username[:4]}****{username[-4:]}"


def sanitize_value(value: Any) -> Any:
    """Recursively redact sensitive mapping keys before an API response is sent."""
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE if _is_sensitive_key(str(key)) else sanitize_value(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_value(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    """Return whether a JSON property name may carry a secret."""
    normalized = key.lower()
    return any(fragment in normalized for fragment in ("password", "secret", "authorization", "token"))
