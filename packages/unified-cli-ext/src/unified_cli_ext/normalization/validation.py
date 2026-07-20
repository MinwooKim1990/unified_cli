"""Bounded Unicode validation shared by normalized identifiers and text."""

from __future__ import annotations

import unicodedata

from ..errors import ProtocolError


def utf8_size(value: str, *, label: str = "value") -> int:
    # Protocol values must be inert built-in strings.  A ``str`` subclass can
    # override methods such as ``encode``/``lower`` and execute attacker code
    # while a supposedly data-only event is being validated.
    if type(value) is not str:
        raise ProtocolError("{} must be a string".format(label))
    try:
        return len(value.encode("utf-8", "strict"))
    except UnicodeEncodeError:
        raise ProtocolError("{} contains invalid Unicode".format(label)) from None


def validate_unicode(
    value: str,
    *,
    label: str,
    maximum: int,
    empty: bool = False,
    allow_text_newlines: bool = False,
) -> str:
    size = utf8_size(value, label=label)
    if (not empty and not value) or size > maximum:
        raise ProtocolError("{} is empty or exceeds its size limit".format(label))
    for char in value:
        codepoint = ord(char)
        category = unicodedata.category(char)
        if allow_text_newlines and char in "\t\n\r":
            continue
        if allow_text_newlines:
            unsafe = (
                codepoint < 32
                or 127 <= codepoint <= 159
                or category in {"Cs", "Zl", "Zp"}
            )
        else:
            # Identifiers, method names, and paths must not contain invisible
            # format/bidi controls, private-use characters, or other Unicode
            # category-C code points that can spoof logs and permission UI.
            unsafe = category.startswith("C") or category in {"Zl", "Zp"}
        if unsafe:
            raise ProtocolError("{} contains unsafe control characters".format(label))
    return value


__all__ = ["utf8_size", "validate_unicode"]
