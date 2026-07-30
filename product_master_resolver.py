"""Resolve an untyped purchase phrase against the live product master.

The resolver deliberately does not use model world knowledge to decide whether a
surface is a product, brand, or category.  It searches ``CRM_CM_PRODUCT`` with
bound parameters, builds a small normalized in-process index over the matching
rows, and only returns an executable filter set when one interpretation wins by
both confidence and margin.

무추측 폴백(no-guess fallback)
-----------------------------
상품 마스터에 근거가 없을 때(``not_found``) 이 모듈은 **추측하지 않고 폴백한다**.
:func:`no_guess_fallback` 이 그 정책의 단일 소유자다. 추측하지 않는 것 셋:

  * ``kind``       — 상품명/브랜드/카테고리 중 무엇인지 정하지 않는다(``kind: None``).
  * ``value``      — 원문 구절을 쪼개거나 다듬지 않는다(들어온 문자열 그대로).
  * ``columns``    — 컬럼을 골라 좁히지 않는다(:data:`SEARCH_COLUMNS` 전체 광역 검색).

폴백은 **실행 가능하다**(clarification 이 아니다) — 오탈자나 신규 등록 상품이 요청을 통째로
막지 않게 하기 위함이다. 대신 ``grounded=False`` 로 표시해, 근거 있는 확정(``resolved``)과
근거 없는 광역 검색을 소비처가 구분할 수 있게 한다(신뢰도 근거·경고가 이 플래그를 읽는다).

``ambiguous``(후보는 있는데 우열이 없음)와 ``unavailable``(마스터 조회 실패)은 폴백 대상이
아니다. 근거가 있는데 고를 수 없는 것과 근거 자체를 못 본 것은 광역 검색으로 덮을 문제가
아니라 확인이 필요한 문제이므로 clarification 으로 남는다.
"""

from __future__ import annotations

import functools
import math
import os
import re
from typing import Any, Callable, Iterable


PRODUCT_MASTER_CONNECTION = "CRMDW"
PRODUCT_MASTER_TABLE = "CRM_CM_PRODUCT"
MAX_LOOKUP_ROWS = 200

KIND_COLUMNS: dict[str, tuple[str, ...]] = {
    "product": ("PRODUCT_NAME",),
    "brand": ("BRAND_NAME",),
    "category": ("CATEGORY", "CATEGORYL_NAME", "CATEGORYM_NAME", "CATEGORYS_NAME"),
}
SEARCH_COLUMNS = tuple(column for columns in KIND_COLUMNS.values() for column in columns)

_TERM_RE = re.compile(r"[0-9A-Za-z가-힣_+\-]+")


def normalize_value(value: str) -> str:
    """Normalize a product-master value for deterministic comparison/indexing."""

    return re.sub(r"[^0-9a-z가-힣]", "", str(value or "").casefold())


def _phrase_terms(phrase: str) -> tuple[str, ...]:
    terms: list[str] = []
    for token in _TERM_RE.findall(phrase or ""):
        normalized = normalize_value(token)
        if len(normalized) >= 2 and normalized not in terms:
            terms.append(normalized)
    return tuple(terms[:4])


def _lookup_sql(term_count: int) -> str:
    selected = ", ".join(("PRODUCT_ID", *SEARCH_COLUMNS))
    groups = []
    for _ in range(term_count):
        groups.append("(" + " OR ".join(f"{column} LIKE %s" for column in SEARCH_COLUMNS) + ")")
    return (
        f"SELECT TOP {MAX_LOOKUP_ROWS} {selected} FROM {PRODUCT_MASTER_TABLE} WITH(NOLOCK) "
        "WHERE " + " AND ".join(groups)
    )


@functools.lru_cache(maxsize=512)
def _lookup_product_rows_cached(terms: tuple[str, ...]) -> tuple[tuple[tuple[str, Any], ...], ...]:
    """Read matching rows once per normalized term tuple (bounded process cache)."""

    if not terms:
        return ()
    from db_connections import run_read_query

    params = tuple(f"%{term}%" for term in terms for _column in SEARCH_COLUMNS)
    rows = run_read_query(
        PRODUCT_MASTER_CONNECTION,
        _lookup_sql(len(terms)),
        params,
        enforce_select=False,
    )
    frozen: list[tuple[tuple[str, Any], ...]] = []
    for row in rows[:MAX_LOOKUP_ROWS]:
        if isinstance(row, dict):
            frozen.append(tuple((column, row.get(column)) for column in ("PRODUCT_ID", *SEARCH_COLUMNS)))
    return tuple(frozen)


def clear_product_lookup_cache() -> None:
    _lookup_product_rows_cached.cache_clear()


def _live_lookup(terms: tuple[str, ...]) -> list[dict[str, Any]]:
    return [dict(row) for row in _lookup_product_rows_cached(terms)]


def _quality(term: str, value: Any) -> float:
    normalized = normalize_value(value) if isinstance(value, str) else ""
    if not term or not normalized:
        return 0.0
    if normalized == term:
        return 1.0
    if normalized.startswith(term) or normalized.endswith(term):
        return 0.94
    if term in normalized:
        return 0.87
    return 0.0


def _best_kind_match(row: dict[str, Any], term: str, kind: str) -> dict[str, Any] | None:
    matches = [
        (_quality(term, row.get(column)), column, row.get(column))
        for column in KIND_COLUMNS[kind]
    ]
    score, column, value = max(matches, default=(0.0, "", None), key=lambda item: item[0])
    if score <= 0:
        return None
    return {"kind": kind, "value": term, "column": column, "matched_value": str(value), "score": score}


def _surface_splits(phrase: str) -> tuple[tuple[str, ...], ...]:
    raw = _TERM_RE.findall(phrase or "")
    normalized = [normalize_value(token) for token in raw if len(normalize_value(token)) >= 2]
    candidates: list[tuple[str, ...]] = []
    whole = normalize_value(phrase)
    if whole:
        candidates.append((whole,))
    for cut in range(1, len(normalized)):
        split = ("".join(normalized[:cut]), "".join(normalized[cut:]))
        if all(split) and split not in candidates:
            candidates.append(split)
    return tuple(candidates)


def _candidate_key(filters: Iterable[dict[str, Any]]) -> tuple[tuple[str, str], ...]:
    return tuple((str(item["kind"]), str(item["value"])) for item in filters)


def _build_candidates(phrase: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[tuple[str, str], ...], dict[str, Any]] = {}
    for parts in _surface_splits(phrase):
        for row in rows:
            assignments: list[list[dict[str, Any]]] = []
            for part in parts:
                matches = [
                    match
                    for kind in KIND_COLUMNS
                    if (match := _best_kind_match(row, part, kind)) is not None
                ]
                assignments.append(matches)
            if not assignments or any(not matches for matches in assignments):
                continue

            combinations: list[list[dict[str, Any]]] = [[]]
            for matches in assignments:
                combinations = [prefix + [match] for prefix in combinations for match in matches]
            for filters in combinations:
                # A split phrase represents different facets of the same product row.  Two fragments assigned
                # to the same kind are less precise than the unsplit candidate and are intentionally ignored.
                if len(filters) > 1 and len({item["kind"] for item in filters}) != len(filters):
                    continue
                score = sum(float(item["score"]) for item in filters) / len(filters)
                if len(filters) > 1:
                    score = min(0.995, score + 0.025)  # same-row multi-facet evidence
                key = _candidate_key(filters)
                candidate = by_key.setdefault(
                    key,
                    {
                        "filters": [
                            {"kind": item["kind"], "value": item["value"], "columns": [item["column"]]}
                            for item in filters
                        ],
                        "score": score,
                        "support_count": 0,
                        "matched_product_ids": [],
                        "_matched_product_id_set": set(),
                        "matched_values": {},
                    },
                )
                candidate["score"] = max(float(candidate["score"]), score)
                candidate["support_count"] += 1
                product_id = row.get("PRODUCT_ID")
                if product_id is not None:
                    product_id_text = str(product_id)
                    candidate["_matched_product_id_set"].add(product_id_text)
                    if product_id_text not in candidate["matched_product_ids"] and len(candidate["matched_product_ids"]) < 5:
                        candidate["matched_product_ids"].append(product_id_text)
                for index, item in enumerate(filters):
                    evidence_key = f"{item['kind']}:{item['value']}"
                    values = candidate["matched_values"].setdefault(evidence_key, [])
                    if item["matched_value"] not in values and len(values) < 3:
                        values.append(item["matched_value"])
                    columns = candidate["filters"][index]["columns"]
                    if item["column"] not in columns:
                        columns.append(item["column"])

    candidates = list(by_key.values())
    for candidate in candidates:
        support_bonus = min(0.01, math.log1p(candidate["support_count"]) / 100)
        candidate["confidence"] = round(min(0.999, float(candidate["score"]) + support_bonus), 4)
        candidate["_row_signature"] = tuple(sorted(candidate.pop("_matched_product_id_set")))
        candidate.pop("score", None)
    return sorted(candidates, key=lambda item: (item["confidence"], item["support_count"]), reverse=True)


GROUNDED_STATUSES = frozenset({"resolved"})
# 폴백이 추측하지 않은 것들. 소비처가 "무엇을 안 정했는지"를 문자열 비교 없이 읽을 수 있게 남긴다.
NO_GUESS_DIMENSIONS = ("kind", "value_split", "column_subset")


def no_guess_fallback(resolution: dict[str, Any]) -> dict[str, Any]:
    """근거 없는 구절을 **추측 없이** 실행 가능한 광역 검색으로 바꾼다(정책 단일 소유자).

    ``not_found`` 이외의 상태는 그대로 돌려준다 — 근거가 있는데 고를 수 없는 것(``ambiguous``)과
    조회 자체가 실패한 것(``unavailable``)은 광역 검색으로 덮지 않는다.
    """
    if resolution.get("status") != "not_found":
        return resolution
    return {
        **resolution,
        "lookup_status": "not_found",
        "status": "fallback",
        "grounded": False,
        "fallback_strategy": "whole_phrase_broad_match",
        "fallback_value": resolution.get("input"),
        "fallback_columns": list(SEARCH_COLUMNS),
        "no_guess": list(NO_GUESS_DIMENSIONS),
    }


def _threshold(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, value))


def resolve_product_phrase(
    phrase: str,
    *,
    lookup: Callable[[tuple[str, ...]], list[dict[str, Any]]] | None = None,
    min_confidence: float | None = None,
    min_margin: float | None = None,
) -> dict[str, Any]:
    """Return a grounded same-row product facet resolution for an untyped phrase."""

    normalized_phrase = " ".join(_TERM_RE.findall(phrase or "")).strip()
    terms = _phrase_terms(normalized_phrase)
    base = {
        "input": normalized_phrase,
        "source": "product_master_lookup",
        "connection": PRODUCT_MASTER_CONNECTION,
        "table": PRODUCT_MASTER_TABLE,
        "lookup_terms": list(terms),
    }
    if not normalized_phrase or not terms:
        return {**base, "status": "not_found", "grounded": False, "confidence": 0.0, "filters": [], "alternatives": []}

    try:
        rows = (lookup or _live_lookup)(terms)
    except Exception as exc:  # DB/driver availability is reported without leaking connection details.
        return {
            **base,
            "status": "unavailable",
            "grounded": False,
            "confidence": 0.0,
            "filters": [],
            "alternatives": [],
            "reason": f"product_master_lookup_failed:{exc.__class__.__name__}",
        }

    candidates = _build_candidates(normalized_phrase, rows)
    if not candidates:
        return {
            **base,
            "status": "not_found",
            "grounded": False,
            "confidence": 0.0,
            "filters": [],
            "alternatives": [],
            "matched_row_count": len(rows),
        }

    confidence_floor = min_confidence if min_confidence is not None else _threshold("PRODUCT_RESOLUTION_MIN_CONFIDENCE", 0.88)
    margin_floor = min_margin if min_margin is not None else _threshold("PRODUCT_RESOLUTION_MIN_MARGIN", 0.04)
    best = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    margin = round(best["confidence"] - (runner_up["confidence"] if runner_up else 0.0), 4)
    equivalent_rows = bool(
        runner_up
        and best.get("_row_signature")
        and best.get("_row_signature") == runner_up.get("_row_signature")
    )
    status = "resolved"
    reason = None
    if best["confidence"] < confidence_floor:
        status, reason = "ambiguous", "confidence_below_threshold"
    elif runner_up is not None and margin < margin_floor and not equivalent_rows:
        status, reason = "ambiguous", "candidate_margin_below_threshold"

    return {
        **base,
        "status": status,
        "grounded": status in GROUNDED_STATUSES,
        "confidence": best["confidence"],
        "margin": margin,
        "equivalent_alternatives": equivalent_rows,
        "filters": best["filters"] if status == "resolved" else [],
        "matched_values": best["matched_values"],
        "matched_product_ids": best["matched_product_ids"],
        "support_count": best["support_count"],
        "matched_row_count": len(rows),
        "reason": reason,
        "alternatives": [
            {
                "confidence": item["confidence"],
                "support_count": item["support_count"],
                "filters": item["filters"],
                "matched_values": item["matched_values"],
            }
            for item in candidates[:3]
        ],
    }
