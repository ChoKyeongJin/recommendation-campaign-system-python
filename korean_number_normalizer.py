"""Shared Korean numeral grammar.

This leaf module owns the closed Korean-number declarations that used to live
inside individual semantic consumers.  It intentionally knows nothing about
campaign slots, units, or SQL, so parsers (including ``graph_rag``) can reuse
the same value grammar without importing a domain layer.

Compatibility is deliberately narrow:

* native Korean counter forms are the existing ``한`` through ``다섯`` set;
* Sino-Korean numbers use digits plus the small units ``십/백/천``;
* unsupported, approximate, zero-only, or malformed surfaces return ``None``.

Large amount multipliers such as ``만`` and ``억`` remain a separate unit
grammar.  Combining a numeric value with those units is the caller's job.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal


KoreanNumberForm = Literal["any", "native", "sino"]

# Native Korean adnominal numerals are dangerous as free-standing tokens:
# ``세`` and ``네`` are common syllables.  Consumers must still bind them to a
# declared counter; this module only owns their value.
NATIVE_KOREAN_NUMBER_VALUES: Mapping[str, int] = MappingProxyType(
    {
        "한": 1,
        "두": 2,
        "세": 3,
        "네": 4,
        "다섯": 5,
    }
)

SINO_KOREAN_DIGIT_VALUES: Mapping[str, int] = MappingProxyType(
    {
        "영": 0,
        "공": 0,
        "일": 1,
        "이": 2,
        "삼": 3,
        "사": 4,
        "오": 5,
        "육": 6,
        "칠": 7,
        "팔": 8,
        "구": 9,
    }
)
SINO_KOREAN_SMALL_UNIT_VALUES: Mapping[str, int] = MappingProxyType(
    {
        "십": 10,
        "백": 100,
        "천": 1_000,
    }
)

_SINO_KOREAN_RE = re.compile(
    "["
    + re.escape(
        "".join(
            (
                *SINO_KOREAN_DIGIT_VALUES,
                *SINO_KOREAN_SMALL_UNIT_VALUES,
            )
        )
    )
    + "]+"
)


def parse_native_korean_number(surface: object) -> int | None:
    """Return the declared native Korean counter value, or ``None``.

    The function does not infer a value from a similar-looking word and does
    not decide whether the surrounding unit is a valid counter.
    """

    if not isinstance(surface, str):
        return None
    return NATIVE_KOREAN_NUMBER_VALUES.get(surface)


def parse_sino_korean_number(surface: object) -> int | None:
    """Parse the existing positive Sino-Korean small-number grammar.

    This preserves the former ``semantic_normalizers`` behavior byte for byte:
    a missing digit before ``십/백/천`` means one, while zero-only forms and
    undeclared large units such as ``만`` are not values here.
    """

    if not isinstance(surface, str) or _SINO_KOREAN_RE.fullmatch(surface) is None:
        return None

    section = 0
    digit = 0
    for character in surface:
        if character in SINO_KOREAN_DIGIT_VALUES:
            digit = SINO_KOREAN_DIGIT_VALUES[character]
        else:
            section += (digit or 1) * SINO_KOREAN_SMALL_UNIT_VALUES[character]
            digit = 0
    value = section + digit
    return value or None


def normalize_korean_number(
    surface: object,
    *,
    form: KoreanNumberForm = "any",
) -> int | None:
    """Normalize a Korean numeral through the public, domain-neutral API.

    ``form`` lets a caller keep its grammar explicit.  For example, an entity
    counter can allow only ``native`` forms while an amount prefix can allow
    only ``sino`` forms.  ``any`` is convenient for future graph-level scanners
    that have already established a safe token boundary.
    """

    if form == "native":
        return parse_native_korean_number(surface)
    if form == "sino":
        return parse_sino_korean_number(surface)
    if form != "any":
        raise ValueError(f"unknown Korean number form: {form!r}")
    return parse_native_korean_number(surface) or parse_sino_korean_number(surface)


def native_korean_number_surfaces() -> tuple[str, ...]:
    """Return the immutable native-numeral surface contract."""

    return tuple(NATIVE_KOREAN_NUMBER_VALUES)


def native_korean_number_alternation() -> str:
    """Return a safe, deterministic regex alternation for native numerals."""

    surfaces = sorted(
        NATIVE_KOREAN_NUMBER_VALUES,
        key=lambda value: (-len(value), value),
    )
    return "|".join(re.escape(surface) for surface in surfaces)


__all__ = [
    "KoreanNumberForm",
    "NATIVE_KOREAN_NUMBER_VALUES",
    "SINO_KOREAN_DIGIT_VALUES",
    "SINO_KOREAN_SMALL_UNIT_VALUES",
    "native_korean_number_alternation",
    "native_korean_number_surfaces",
    "normalize_korean_number",
    "parse_native_korean_number",
    "parse_sino_korean_number",
]
