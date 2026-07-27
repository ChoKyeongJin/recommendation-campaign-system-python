"""Deterministic token roles shared by entity extraction and analytical intent parsing.

The SQL pipeline must decide whether words such as ``가장`` and ``적게`` describe
an entity or an operation *before* product extraction. Keeping the vocabulary
here prevents product, ranking, and validation paths from drifting apart.
"""

from __future__ import annotations

import re
from typing import Any


TOKEN_ROLES = frozenset({
    "entity", "metric", "dimension", "filter_value", "comparison", "extreme",
    "sort_direction", "quantity", "aggregation", "stopword",
})

EXTREME_ALIASES: dict[str, dict[str, Any]] = {
    "가장 많은": {"extreme": "MAX", "sort_direction": "DESC", "limit": 1},
    "제일 많은": {"extreme": "MAX", "sort_direction": "DESC", "limit": 1},
    "가장 많이": {"extreme": "MAX", "sort_direction": "DESC", "limit": 1},
    "제일 많이": {"extreme": "MAX", "sort_direction": "DESC", "limit": 1},
    "가장 적은": {"extreme": "MIN", "sort_direction": "ASC", "limit": 1},
    "제일 적은": {"extreme": "MIN", "sort_direction": "ASC", "limit": 1},
    "가장 적게": {"extreme": "MIN", "sort_direction": "ASC", "limit": 1},
    "제일 적게": {"extreme": "MIN", "sort_direction": "ASC", "limit": 1},
    "최대": {"extreme": "MAX", "sort_direction": "DESC", "limit": 1},
    "최소": {"extreme": "MIN", "sort_direction": "ASC", "limit": 1},
    "최고": {"extreme": "MAX", "sort_direction": "DESC", "limit": 1},
    "최저": {"extreme": "MIN", "sort_direction": "ASC", "limit": 1},
    "가장 최근": {"extreme": "MAX", "sort_direction": "DESC", "limit": 1},
    "가장 오래된": {"extreme": "MIN", "sort_direction": "ASC", "limit": 1},
}

NON_ENTITY_TERMS = frozenset({
    "가장", "제일", "많이", "적게", "높게", "낮게", "큰", "작은", "최대", "최소",
    "최고", "최저", "상위", "하위", "첫", "첫번째", "첫 번째", "마지막", "많은", "적은",
    "평균", "평균값", "합계", "총합", "총액", "개수", "건수", "횟수", "수량",
    "알려줘", "알려주세요", "보여줘", "보여주세요", "조회", "조회해줘", "계산해줘",
})

_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?(?:원|건|회|번|개|명)?|[0-9A-Za-z가-힣_+\-]+")
_QUANTITY_RE = re.compile(r"^\d+(?:\.\d+)?(?:원|건|회|번|개|명)?$")
_COMPARISON = frozenset({"이상", "이하", "초과", "미만", "보다", "같은", "높은", "낮은", "큰", "작은"})
_AGGREGATION = frozenset({"합계", "총합", "총액", "평균", "평균값", "건수", "개수", "횟수"})
_DIMENSIONS = frozenset({"회원", "고객", "사용자", "상품", "브랜드", "카테고리", "지역", "매장"})
_METRICS = frozenset({"구매금액", "결제금액", "구매액", "구매건수", "구매횟수", "로그인횟수", "적립금", "예치금", "구매일"})


def extract_extreme_semantics(text: str) -> dict[str, Any] | None:
    """Return the longest matching arg-extreme contract in ``text``."""
    compact = re.sub(r"\s+", " ", text or "").strip()
    matches = [(len(alias), alias, contract) for alias, contract in EXTREME_ALIASES.items() if alias in compact]
    if not matches:
        return None
    _length, alias, contract = max(matches)
    return {"surface": alias, **contract}


def classify_query_tokens(text: str) -> list[dict[str, str]]:
    """Classify tokens without claiming that every unknown noun is a product."""
    extreme = extract_extreme_semantics(text)
    extreme_tokens = set(_TOKEN_RE.findall(extreme["surface"])) if extreme else set()
    classified: list[dict[str, str]] = []
    for token in _TOKEN_RE.findall(text or ""):
        compact = token.replace(" ", "")
        if token in extreme_tokens or compact in {"최대", "최소", "최고", "최저"}:
            role = "extreme"
        elif _QUANTITY_RE.fullmatch(compact):
            role = "quantity"
        elif compact in _COMPARISON:
            role = "comparison"
        elif compact in _AGGREGATION:
            role = "aggregation"
        elif compact in _DIMENSIONS:
            role = "dimension"
        elif compact in _METRICS:
            role = "metric"
        elif compact in NON_ENTITY_TERMS:
            role = "stopword"
        else:
            role = "entity"
        classified.append({"token": token, "role": role})
    return classified


def is_non_entity_candidate(value: str) -> bool:
    """True when a candidate contains no lexical evidence beyond operators."""
    tokens = [token.replace(" ", "") for token in _TOKEN_RE.findall(value or "")]
    return bool(tokens) and all(
        token in NON_ENTITY_TERMS
        or token in _COMPARISON
        or token in _AGGREGATION
        or bool(_QUANTITY_RE.fullmatch(token))
        for token in tokens
    )
