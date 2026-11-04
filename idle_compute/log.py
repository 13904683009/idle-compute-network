"""Log redaction rules (Phase 2, F-4).

The logger is part of the attack surface: one careless ``logger.info`` must not
leak key material, full signatures, or payload contents. Three layers:

1. helpers for call sites (``short_id``, ``short_hex``, ``payload_summary``);
2. ``redact_text`` for free-form strings;
3. ``RedactionFilter`` installed on the ``idle_compute`` logger, so even a call
   site that forgets the helpers cannot emit a full signature, a 64-hex hash, a
   full node id, or a ``*.key`` path.

Rules (tested in tests/test_log.py):
- node ids: first 12 characters, then ``…``;
- hashes: first 8 hex characters, then ``…``;
- signatures: first 8 hex characters, then ``…``;
- payloads: shape and counts only, never values;
- key-file paths: replaced by ``<key-file>``.
"""

from __future__ import annotations

import logging
import re
from typing import Any

NODE_ID_VISIBLE = 12
HEX_VISIBLE = 8
ELLIPSIS = "…"
KEY_PATH_PLACEHOLDER = "<key-file>"
REDACTED = "<redacted>"

# 52-char libp2p Ed25519 peer id ("12D3KooW" + 44 base58 chars).
_NODE_ID_PATTERN = r"\b12D3KooW[1-9A-HJ-NP-Za-km-z]{44}\b"
# 128 hex = Ed25519 signature; 64 hex = sha256 hash. Longest first.
_HEX128_PATTERN = r"\b[0-9a-fA-F]{128}\b"
_HEX64_PATTERN = r"\b[0-9a-fA-F]{64}\b"
# Windows and POSIX key-file paths.
_KEY_PATH_PATTERN = r"(?:[A-Za-z]:\\[^\s\"']*?\.key\b|(?:/[^\s\"']*?)+\.key\b)"


def short_id(value: Any) -> str:
    text = str(value)
    return text if len(text) <= NODE_ID_VISIBLE else text[:NODE_ID_VISIBLE] + ELLIPSIS


def short_hex(value: Any) -> str:
    text = str(value)
    return text if len(text) <= HEX_VISIBLE else text[:HEX_VISIBLE] + ELLIPSIS


def payload_summary(payload: Any) -> dict[str, Any]:
    """Describe a payload without leaking its values."""
    if not isinstance(payload, dict):
        return {"type": type(payload).__name__}
    summary: dict[str, Any] = {"keys": sorted(payload), "fields": len(payload)}
    numbers = payload.get("numbers")
    if isinstance(numbers, list):
        summary["numbers_count"] = len(numbers)
    return summary


def redact_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(_KEY_PATH_PATTERN, KEY_PATH_PLACEHOLDER, text)
    text = re.sub(_NODE_ID_PATTERN, lambda m: short_id(m.group(0)), text)
    text = re.sub(_HEX128_PATTERN, lambda m: short_hex(m.group(0)), text)
    text = re.sub(_HEX64_PATTERN, lambda m: short_hex(m.group(0)), text)
    return text


class RedactionFilter(logging.Filter):
    """Rewrites every record's rendered message through ``redact_text``."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # a broken format string must not break logging
            return True
        redacted = redact_text(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


def install_redaction(
    logger_name: str = "idle_compute", handlers: Any = ()
) -> RedactionFilter:
    """Install the filter on a logger and/or handlers exactly once.

    Handler-level filters matter because logger filters are only applied at the
    logger where the record was created, not on ancestors during propagation:
    attaching to the console/file handlers covers every ``idle_compute.*``
    logger.
    """
    logger = logging.getLogger(logger_name)
    redaction = next(
        (f for f in logger.filters if isinstance(f, RedactionFilter)), None
    )
    if redaction is None:
        redaction = RedactionFilter()
        logger.addFilter(redaction)
    for handler in handlers or ():
        if not any(isinstance(f, RedactionFilter) for f in handler.filters):
            handler.addFilter(redaction)
    return redaction
