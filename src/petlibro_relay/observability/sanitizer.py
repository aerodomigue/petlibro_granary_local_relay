"""Central redaction for values that must never leave the process."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

SENSITIVE_TEXT_FIELD_PATTERN = re.compile(
    r"(?i)(?:[\"'])?(?P<field>"
    r"password|secret|authorization|token|"
    r"(?:dl[_-]?)?product[_-]?key|"
    r"(?:device|mqtt)?[_-]?username|"
    r"(?:device)?[_-]?(?:uuid|uid)|"
    r"(?:api|p2p)[_-]?key|"
    r"mqtt(?:[_-]?(?:addr|address))?|https?addr|internalurl|"
    r"tutk(?:[a-z0-9_-]*)?|kalay(?:[a-z0-9_-]*)?"
    r")(?:[\"'])?\s*[=:]\s*(?:[\"'])?[^\s,}\]]+(?:[\"'])?"
)
REDACTED_VALUE = "<redacted>"


def sanitize_text(message: str, secrets: Iterable[str] = ()) -> str:
    """Redact credentials and secret-looking fields from a log/API string.

    Args:
        message: Text that may include sensitive values.
        secrets: Exact runtime secrets to remove as an additional safeguard.

    Returns:
        Sanitized text safe to expose via the dashboard.
    """
    sanitized = SENSITIVE_TEXT_FIELD_PATTERN.sub(r"\g<field>=" + REDACTED_VALUE, message)
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
    if isinstance(value, str):
        return sanitize_text(value)
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


def sanitize_upstream_service_payload(value: Any) -> Any:
    """Recursively redact sensitive values from diagnostic cloud payload logs.

    Service payload diagnostics are intentionally more conservative than the
    dashboard state view: TUTK/Kalay data is never useful for this MQTT
    setting investigation, so any such field is redacted before it reaches a
    log sink.
    """
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE
            if _is_upstream_service_sensitive_key(str(key))
            else sanitize_upstream_service_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize_upstream_service_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_upstream_service_payload(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    """Return whether a JSON property name may carry a secret."""
    normalized = key.lower()
    return normalized in {"username", "user", "uuid", "uid", "mqttaddr", "mqttaddress", "httpsaddr", "httpaddr", "internalurl"} or any(
        fragment in normalized
        for fragment in (
            "password",
            "secret",
            "authorization",
            "token",
            "credential",
            "uuid",
            "uid",
            "apikey",
            "api_key",
            "p2p",
            "tutk",
            "kalay",
        )
    )


def _is_upstream_service_sensitive_key(key: str) -> bool:
    """Return whether a diagnostic service-payload field must be redacted."""
    normalized = key.lower()
    return normalized in {"username", "user"} or any(
        fragment in normalized
        for fragment in (
            "password",
            "secret",
            "authorization",
            "token",
            "credential",
            "tutk",
            "kalay",
        )
    )
