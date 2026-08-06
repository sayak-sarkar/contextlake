"""Output sanitization for untrusted repository content at an output boundary.

Repository content is untrusted: a symbol name, comment, or file path can carry
control characters, terminal (ANSI) escape sequences, or prompt-injection
payloads. :func:`sanitize_label` strips control characters and caps length on any
source-derived text before it is returned to an agent, so a hostile label can't
inject into the agent's context or a terminal.

:func:`json_for_script` covers the other output boundary: a JSON payload embedded
in an inline ``<script>`` element of a generated HTML page. Both live here because
they answer the same question -- "this text came from a repository we do not
control, what must happen before it leaves" -- and keeping them together is what
stops the next writer re-deriving half of one.

Adapted from Graphify (https://github.com/safishamsi/graphify), MIT License,
Copyright (c) 2026 Safi Shamsi.
"""

from __future__ import annotations

import json
import re

__all__ = ["sanitize_label", "MAX_LABEL_LEN", "json_for_script"]

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


def json_for_script(obj) -> str:
    """JSON safe to embed in an inline ``<script>``, whatever the data contains.

    Plain :func:`json.dumps` leaves ``<`` and ``>`` untouched, so a ``</script>``
    anywhere in the data closes the script element and the browser parses the rest
    of the payload as HTML. For contextlake that data is *arbitrary indexed
    source* -- symbol names, file paths, commit context, connector titles -- so
    the input is any repository the tool has been pointed at.

    ``<``, ``>`` and ``&`` become ``\\u003c`` / ``\\u003e`` / ``\\u0026``. Those are
    valid JSON escapes that no HTML tokenizer can read as markup (this also kills
    ``<!--``), and they decode back to the *same string* under ``JSON.parse`` -- so
    the page still renders a hostile label verbatim, as inert text, rather than
    mangling it.

    Escaping here, at the single point where data enters a script context, is what
    makes the property hold by construction: a field added to the payload later is
    covered without anyone remembering to escape it. ``json.dumps`` already emits
    U+2028/U+2029 (the other JS line terminators) as ``\\u2028``/``\\u2029`` under
    its default ``ensure_ascii=True``, so they need no separate handling -- which
    is why this deliberately takes no ``ensure_ascii`` argument to override.
    """
    return (json.dumps(obj)
            .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
