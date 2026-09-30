"""Deterministic field-name normalization.

Normalization provides the stable identity used for exact matching,
duplicate prevention, and runtime field insertion. It is pure and
deterministic — no LLM involvement.
"""

import re
import unicodedata

_ZERO_WIDTH = re.compile(r"[\u200b\u200d\u200e\u200f\ufeff]")


def normalize_field(raw: str) -> str:
    """Normalize a raw field name to its stable identity.

    Applies Unicode NFC normalization, removes zero-width characters, trims,
    and collapses internal whitespace. Returns "" for empty/whitespace-only
    input.
    """
    text = unicodedata.normalize("NFC", raw)
    text = _ZERO_WIDTH.sub("", text)
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text
