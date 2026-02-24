"""Output sanitization for MCP responses.

Repository content is untrusted: a symbol name, comment, or file path can carry
control characters, terminal (ANSI) escape sequences, or prompt-injection
payloads. :func:`sanitize_label` strips control characters and caps length on any
source-derived text before it is returned to an agent, so a hostile label can't
inject into the agent's context or a terminal.

Adapted from Graphify (https://github.com/safishamsi/graphify), MIT License,
Copyright (c) 2026 Safi Shamsi.
"""

from __future__ import annotations

import re

__all__ = ["sanitize_label", "MAX_LABEL_LEN"]

MAX_LABEL_LEN = 256

# C0/C1 control characters except tab (\x09), newline (\x0a) and carriage
# return (\x0d). This strips ESC (\x1b) and friends, defusing ANSI/terminal
# injection while leaving ordinary whitespace intact.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def sanitize_label(text: str | None, max_len: int = MAX_LABEL_LEN) -> str:
    """Strip control/escape characters and cap length. ``None`` -> ``""``."""
    if text is None:
        return ""
    text = _CONTROL_CHAR_RE.sub("", str(text))
    if len(text) > max_len:
        text = text[:max_len]
    return text
