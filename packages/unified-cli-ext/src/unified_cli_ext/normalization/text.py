"""Stateful, bounded de-duplication for cumulative and restarted text blocks."""

from __future__ import annotations

from array import array
from dataclasses import dataclass
from typing import Dict, List, Tuple

from ..errors import LimitExceeded, ProtocolError
from .validation import utf8_size, validate_unicode


_OVERLAP_WINDOW = 1024 * 1024
_BLOCK_WINDOW = 64 * 1024
_MAX_BLOCKS = 256


def _linear_overlap(existing_tail: str, candidate: str) -> int:
    """KMP overlap on bounded windows, O(n) time and bounded memory."""

    prefix = candidate[:_OVERLAP_WINDOW]
    haystack = existing_tail[-_OVERLAP_WINDOW:]
    if not prefix or not haystack:
        return 0
    combined = prefix + "\x00" + haystack
    table = array("I", [0]) * len(combined)
    for index in range(1, len(combined)):
        matched = table[index - 1]
        while matched and combined[index] != combined[matched]:
            matched = table[matched - 1]
        if combined[index] == combined[matched]:
            matched += 1
        table[index] = matched
    return min(table[-1], len(prefix))


@dataclass
class _BlockState:
    characters: int
    tail: str


class TextDeduplicator:
    """Normalize cumulative partials, deltas, finals, and block restarts.

    Per-block state stores only character count and a bounded tail. Prefix
    checks inspect only a bounded boundary window, preventing repeated growing
    cumulative partials from creating quadratic work or unbounded duplicate
    state.
    """

    def __init__(self, max_text_bytes: int = 16 * 1024 * 1024) -> None:
        if type(max_text_bytes) is not int or max_text_bytes <= 0:
            raise ValueError("max_text_bytes must be a positive integer")
        self._max_text_bytes = max_text_bytes
        self._blocks: Dict[str, _BlockState] = {}
        self._chunks: List[str] = []
        self._bytes = 0
        self._characters = 0
        self._tail = ""

    @property
    def complete_text(self) -> str:
        return "".join(self._chunks)

    def _append(self, value: str) -> str:
        if not value:
            return ""
        size = utf8_size(value, label="normalized text")
        if self._bytes + size > self._max_text_bytes:
            raise LimitExceeded("normalized text exceeds configured limit")
        self._chunks.append(value)
        self._bytes += size
        self._characters += len(value)
        self._tail = (self._tail + value)[-_OVERLAP_WINDOW:]
        return value

    @staticmethod
    def _validate(block_id: str, text: str) -> None:
        validate_unicode(block_id, label="text block id", maximum=256, empty=False)
        validate_unicode(
            text,
            label="text payload",
            maximum=1024 * 1024,
            empty=True,
            allow_text_newlines=True,
        )

    @staticmethod
    def _matches_boundary(text: str, characters: int, tail: str) -> bool:
        if len(text) < characters:
            return False
        width = min(len(tail), characters, _OVERLAP_WINDOW)
        return not width or text[characters - width : characters] == tail[-width:]

    @staticmethod
    def _is_stale_partial(text: str, characters: int, tail: str) -> bool:
        """Recognize an older cumulative snapshot without trusting length.

        Some CLIs restart block numbering.  A shorter, unrelated snapshot is
        therefore new text, not necessarily a stale copy.  Suppressing solely
        because of its length loses output; require a matching emitted suffix.
        """

        if len(text) > characters:
            return False
        width = min(len(text), len(tail), _OVERLAP_WINDOW)
        return not text or (bool(width) and text[-width:] == tail[-width:])

    def _set_block(self, block_id: str, text: str) -> None:
        if block_id not in self._blocks and len(self._blocks) >= _MAX_BLOCKS:
            raise LimitExceeded("too many text blocks")
        self._blocks[block_id] = _BlockState(len(text), text[-_BLOCK_WINDOW:])

    def delta(self, text: str, block_id: str = "default") -> str:
        self._validate(block_id, text)
        state = self._blocks.get(block_id, _BlockState(0, ""))
        self._set_block(
            block_id,
            (state.tail + text)[-_BLOCK_WINDOW:],
        )
        # Preserve total character count separately from the bounded tail.
        self._blocks[block_id].characters = state.characters + len(text)
        return self._append(text)

    def partial(self, text: str, block_id: str = "default") -> str:
        self._validate(block_id, text)
        state = self._blocks.get(block_id)
        if state is not None and self._matches_boundary(text, state.characters, state.tail):
            unseen = text[state.characters:]
        elif state is not None and self._is_stale_partial(
            text, state.characters, state.tail
        ):
            unseen = ""
        elif self._matches_boundary(text, self._characters, self._tail):
            unseen = text[self._characters:]
        elif self._is_stale_partial(text, self._characters, self._tail):
            unseen = ""
        else:
            unseen = text[_linear_overlap(self._tail, text) :]
        self._set_block(block_id, text)
        return self._append(unseen)

    def final(self, text: str) -> Tuple[str, str]:
        self._validate("final", text)
        if self._matches_boundary(text, self._characters, self._tail):
            unseen = text[self._characters:]
        elif self._is_stale_partial(text, self._characters, self._tail):
            unseen = ""
        else:
            unseen = text[_linear_overlap(self._tail, text) :]
        self._append(unseen)
        # Reconstruction is authoritative after any restart/mismatch fallback.
        return unseen, self.complete_text


__all__ = ["TextDeduplicator"]
