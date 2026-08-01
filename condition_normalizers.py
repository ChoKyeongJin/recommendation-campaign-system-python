"""공통 타입 정규화기 — 슬롯별 ``_coerce_*`` 증식을 대체하는 공유 검증 계층.

배경: 기간 창(가입/로그인/미접속/미구매), 비교(연산자+값), 날짜창, 임계값은 슬롯이 달라도
검증 규칙이 같다. 그런데 검증이 슬롯별 coerce 안에 갇혀 있어 새 슬롯마다 함수가 하나씩 늘고,
실패는 전부 ``None`` 으로 접혀 재시도·안내가 불가능했다. 이 모듈은 그 공통 타입 6종의 정규화를
한 곳에서 소유한다:

  duration           — {value, unit} 기간(+min_days 파생)
  comparison         — {operator, value} 비교
  date_window        — {from, to} 절대 날짜창
  threshold          — {concept, operator, value} 카탈로그 접지 임계
  entity_reference   — {entity_type, canonical_id} 후보 접지 참조
  generic_condition  — threshold + window/aggregation (공통 조건)

결과는 None 이 아니라 :mod:`normalization_result` 의 구조화 결과다 — 호출자는 오류 코드로
재시도(retryable)/안내(needs_review)/차단(unresolved) 을 구분한다. 기존 SlotShape coerce 계약
(raw → value | None)은 :mod:`targeting_ir` 의 얇은 어댑터가 유지한다.

어휘(단위 별칭·비교어·모호 정도어)는 docs/data/normalization_lexicon.json 이 소유하고, 파일이
없거나 파손되면 ``_CODE_FALLBACK`` 으로 폴백한다(:mod:`lexicon_patterns` 와 같은 관례).
모호 정도어('높은/많은/자주' 등)는 어떤 연산자로도 강제 확정하지 않는다 — needs_review 로 낸다.

카탈로그/후보 제공자는 덕 타이핑으로 받는다(concept_catalog.ConceptCatalog /
CandidateProvider 프로토콜). 이 모듈은 graph_rag 를 import 하지 않는다(순수 모듈).
"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

from normalization_result import (
    STATUS_INVALID,
    STATUS_NEEDS_REVIEW,
    STATUS_UNRESOLVED,
    STATUS_UNSUPPORTED,
    NormalizationError,
    NormalizationResult,
)

_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_NORMALIZATION_LEXICON_PATH = _REPO_ROOT / "docs" / "data" / "normalization_lexicon.json"

# JSON 과 같은 스키마의 코드 폴백(이관 시점 값 그대로). 두 소스의 드리프트는
# tests/test_condition_normalizers.py 의 parity 가드가 막는다.
_CODE_FALLBACK: dict[str, Any] = {
    "units": {
        "days": ["일", "하루", "day", "days"],
        "weeks": ["주", "주일", "week", "weeks"],
        "months": ["개월", "달", "월", "month", "months"],
        "years": ["년", "해", "year", "years"],
    },
    "unit_days": {"days": 1, "weeks": 7, "months": 30, "years": 365},
    "unit_conversions": {
        "분기": {"unit": "months", "factor": 3},
        "quarter": {"unit": "months", "factor": 3},
        "quarters": {"unit": "months", "factor": 3},
        "반기": {"unit": "months", "factor": 6},
    },
    "comparison_word_operators": {"이상": ">=", "초과": ">", "이하": "<=", "미만": "<"},
    "operator_aliases_extended": {">=": ["최소", "적어도"], "<=": ["최대"], "=": ["같은", "정확히"]},
    "ambiguous_degree_terms": [
        "높은", "많은", "낮은", "적은", "많이", "꽤", "꽤 많이", "자주", "오랫동안", "우수한",
    ],
}

COMPARISON_SYMBOLS: frozenset[str] = frozenset({">=", ">", "<=", "<", "="})


@lru_cache(maxsize=4)
def _load_lexicon_cached(path_text: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path_text).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(_CODE_FALLBACK)
    if not isinstance(payload, dict):
        return dict(_CODE_FALLBACK)
    return payload


def _lexicon() -> dict[str, Any]:
    path = os.getenv("GRAPH_RAG_NORMALIZATION_LEXICON") or str(DEFAULT_NORMALIZATION_LEXICON_PATH)
    return _load_lexicon_cached(str(Path(path)))


def clear_lexicon_cache() -> None:
    """어휘 파일을 바꿔 끼우는 테스트용."""
    _load_lexicon_cached.cache_clear()


def _section(name: str) -> Any:
    value = _lexicon().get(name)
    return value if value is not None else _CODE_FALLBACK.get(name)


def unit_days() -> dict[str, int]:
    """canonical 단위 → 일수."""
    raw = _section("unit_days")
    return {str(k): int(v) for k, v in raw.items()} if isinstance(raw, dict) else dict(_CODE_FALLBACK["unit_days"])


def unit_aliases() -> dict[str, str]:
    """단위 표기(한글·단수·복수) → canonical 단위."""
    raw = _section("units")
    if not isinstance(raw, dict):
        raw = _CODE_FALLBACK["units"]
    out: dict[str, str] = {}
    for canonical, aliases in raw.items():
        out[str(canonical)] = str(canonical)
        for alias in aliases if isinstance(aliases, list) else []:
            out[str(alias).strip().casefold()] = str(canonical)
    return out


def comparison_word_operators() -> dict[str, str]:
    """비교어 → 부등호(닫힌 핵심 집합). 열거 순서가 graph_rag 표면 정규식에 들어가므로 보존된다."""
    raw = _section("comparison_word_operators")
    return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else dict(_CODE_FALLBACK["comparison_word_operators"])


def operator_aliases() -> dict[str, str]:
    """확장 별칭 포함 연산자 표기 → 기호(새 정규화 계층 전용 — 표면 정규식에는 안 들어간다)."""
    out: dict[str, str] = dict(comparison_word_operators())
    extended = _section("operator_aliases_extended")
    for symbol, words in (extended.items() if isinstance(extended, dict) else ()):
        for word in words if isinstance(words, list) else []:
            out.setdefault(str(word).strip(), str(symbol))
    for symbol in COMPARISON_SYMBOLS:
        out[symbol] = symbol
    return out


def ambiguous_degree_terms() -> frozenset[str]:
    """값·연산자를 확정할 수 없는 정도어 — 정규화기가 강제 확정 대신 needs_review 로 처리한다."""
    raw = _section("ambiguous_degree_terms")
    return frozenset(str(term) for term in raw) if isinstance(raw, list) else frozenset(_CODE_FALLBACK["ambiguous_degree_terms"])


def unit_conversions() -> dict[str, dict[str, Any]]:
    """미지원 단위 표기 → canonical 단위 환산({unit, factor}). 재시도 계층의 결정론 수리 전용 —
    정규화기 본체는 이 표를 보지 않는다('한 분기'→3개월 변환은 의미/수리 계층의 일, 요청 20번)."""
    raw = _section("unit_conversions")
    if not isinstance(raw, dict):
        raw = _CODE_FALLBACK["unit_conversions"]
    out: dict[str, dict[str, Any]] = {}
    for surface, spec in raw.items():
        if isinstance(spec, dict) and spec.get("unit") and isinstance(spec.get("factor"), int):
            out[str(surface).strip().casefold()] = {"unit": str(spec["unit"]), "factor": int(spec["factor"])}
    return out


def canonical_unit(raw: Any) -> str | None:
    return unit_aliases().get(str(raw).strip().casefold()) if raw is not None else None


def canonical_operator(raw: Any) -> str | None:
    return operator_aliases().get(str(raw).strip()) if raw is not None else None


# ── 값 파싱 공통 ──────────────────────────────────────────────────────────────────────────
def _parse_number(raw: Any, *, allow_string: bool, field: str) -> tuple[float | int | None, NormalizationError | None]:
    """유한 숫자 하나. bool·비숫자 문자열·NaN/inf 는 구조화 오류로 돌려준다(SQL 문자열 삽입 차단)."""
    if raw is None:
        return None, NormalizationError("missing_value", field=field)
    if isinstance(raw, bool):
        return None, NormalizationError("invalid_type", field=field, received=raw)
    if isinstance(raw, str):
        if not allow_string:
            return None, NormalizationError("invalid_type", field=field, received=raw)
        text = raw.strip().replace(",", "")
        try:
            value: float | int = float(text)
        except ValueError:
            return None, NormalizationError("invalid_number", field=field, received=raw)
    elif isinstance(raw, (int, float)):
        value = raw
    else:
        return None, NormalizationError("invalid_type", field=field, received=raw)
    if not math.isfinite(float(value)):
        return None, NormalizationError("invalid_number", field=field, received=raw)
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return value, None


# ── Duration ──────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class DurationPolicy:
    """기간 슬롯 하나의 검증 정책(요청 19번). 슬롯 정의가 이 정책만 선언하면 정규화기는 공유된다."""

    allowed_units: frozenset[str] = frozenset({"days", "weeks", "months", "years"})
    min_value: int = 1
    max_value: int | None = None
    allow_zero: bool = False
    allow_string_numbers: bool = True


DEFAULT_DURATION_POLICY = DurationPolicy()


def normalize_duration(raw: Any, *, policy: DurationPolicy = DEFAULT_DURATION_POLICY) -> NormalizationResult:
    """{value, unit} 표준 후보 → {value, unit, min_days}. '한 분기'→3개월 같은 표현 변환은
    의미 해석 계층의 일이다 — 여기는 표준 후보만 검증한다(요청 20번)."""
    if not isinstance(raw, Mapping):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_type", field="duration", received=raw))
    value, error = _parse_number(raw.get("value"), allow_string=policy.allow_string_numbers, field="value")
    if error is not None:
        return NormalizationResult.failure(STATUS_INVALID, error)
    if not isinstance(value, int):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_number", field="value", received=raw.get("value"),
                                              detail="기간 값은 정수여야 한다"))
    if value == 0 and not policy.allow_zero:
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("out_of_range", field="value", received=0,
                                              detail="0 기간은 이 슬롯 정책에서 허용되지 않는다"))
    if value < 0:
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("out_of_range", field="value", received=value))
    if value < policy.min_value or (policy.max_value is not None and value > policy.max_value):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("out_of_range", field="value", received=value,
                                              allowed_values=(policy.min_value, policy.max_value)))
    unit_raw = raw.get("unit")
    if unit_raw is None:
        return NormalizationResult.failure(STATUS_INVALID, NormalizationError("missing_value", field="unit"))
    unit = canonical_unit(unit_raw)
    if unit is None or unit not in policy.allowed_units:
        return NormalizationResult.failure(
            STATUS_NEEDS_REVIEW,
            NormalizationError("unsupported_unit", field="unit", received=unit_raw,
                               allowed_values=tuple(sorted(policy.allowed_units))))
    return NormalizationResult.normalized(
        {"value": value, "unit": unit, "min_days": value * unit_days()[unit]})


# ── Comparison ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ComparisonPolicy:
    allowed_operators: frozenset[str] = frozenset({">=", ">", "<=", "<"})
    allow_zero: bool = True
    allow_negative: bool = False
    allow_string_numbers: bool = True
    min_value: float | None = None
    max_value: float | None = None


DEFAULT_COMPARISON_POLICY = ComparisonPolicy()


def normalize_comparison(raw: Any, *, policy: ComparisonPolicy = DEFAULT_COMPARISON_POLICY) -> NormalizationResult:
    """{operator, value} → 기호 연산자 + 유한 숫자. 모호한 정도어('높은/많은')는 어떤 연산자로도
    강제 확정하지 않고 needs_review 로 낸다(요청 11번)."""
    if not isinstance(raw, Mapping):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_type", field="comparison", received=raw))
    operator_raw = raw.get("operator")
    if operator_raw is None:
        return NormalizationResult.failure(STATUS_INVALID, NormalizationError("missing_value", field="operator"))
    operator_text = str(operator_raw).strip()
    if operator_text in ambiguous_degree_terms():
        return NormalizationResult.failure(
            STATUS_NEEDS_REVIEW,
            NormalizationError("ambiguous_expression", field="operator", received=operator_raw, retryable=False,
                               detail="정도어는 문맥으로도 연산자를 확정할 수 없다 — 사용자 확인 필요"))
    operator = canonical_operator(operator_raw)
    if operator is None or operator not in policy.allowed_operators:
        return NormalizationResult.failure(
            STATUS_NEEDS_REVIEW,
            NormalizationError("unsupported_operator", field="operator", received=operator_raw,
                               allowed_values=tuple(sorted(policy.allowed_operators))))
    value, error = _parse_number(raw.get("value"), allow_string=policy.allow_string_numbers, field="value")
    if error is not None:
        return NormalizationResult.failure(STATUS_INVALID, error)
    if value < 0 and not policy.allow_negative:
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("out_of_range", field="value", received=value))
    if value == 0 and not policy.allow_zero:
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("out_of_range", field="value", received=0))
    if (policy.min_value is not None and value < policy.min_value) or (
            policy.max_value is not None and value > policy.max_value):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("out_of_range", field="value", received=value,
                                              allowed_values=(policy.min_value, policy.max_value)))
    return NormalizationResult.normalized({"operator": operator, "value": value})


# ── DateWindow ────────────────────────────────────────────────────────────────────────────
_DATE_TOKEN_RE = re.compile(r"^(\d{4})[-./]?(\d{2})[-./]?(\d{2})$")


def _parse_date_token(raw: Any, field: str) -> tuple[str | None, NormalizationError | None]:
    if raw is None:
        return None, NormalizationError("missing_value", field=field)
    match = _DATE_TOKEN_RE.match(str(raw).strip())
    if match is None:
        return None, NormalizationError("invalid_type", field=field, received=raw,
                                        detail="YYYYMMDD 또는 YYYY-MM-DD 형식이어야 한다")
    year, month, day = (int(part) for part in match.groups())
    try:
        date(year, month, day)
    except ValueError:
        return None, NormalizationError("invalid_date_range", field=field, received=raw,
                                        detail="달력에 존재하지 않는 날짜")
    return f"{year:04d}{month:02d}{day:02d}", None


def normalize_date_window(raw: Any, *, allow_open_start: bool = False, allow_open_end: bool = False) -> NormalizationResult:
    """{from, to} 절대 날짜창 → {from: 'YYYYMMDD', to: 'YYYYMMDD'}. 상대 날짜('지난달')는 의미
    해석 계층이 절대 날짜 후보로 변환한 뒤에 들어와야 한다(요청 19번)."""
    if not isinstance(raw, Mapping):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_type", field="date_window", received=raw))
    start_raw, end_raw = raw.get("from"), raw.get("to")
    start = end = None
    errors: list[NormalizationError] = []
    if start_raw is None and allow_open_start:
        pass
    else:
        start, error = _parse_date_token(start_raw, "from")
        if error is not None:
            errors.append(error)
    if end_raw is None and allow_open_end:
        pass
    else:
        end, error = _parse_date_token(end_raw, "to")
        if error is not None:
            errors.append(error)
    if errors:
        return NormalizationResult.failure(STATUS_INVALID, *errors)
    if start is None and end is None:
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("missing_value", field="date_window",
                                              detail="from/to 중 하나는 있어야 한다"))
    if start is not None and end is not None and start > end:
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_date_range", field="date_window",
                                              received={"from": start, "to": end},
                                              detail="from 이 to 보다 늦다"))
    value: dict[str, str] = {}
    if start is not None:
        value["from"] = start
    if end is not None:
        value["to"] = end
    return NormalizationResult.normalized(value)


# ── Threshold(카탈로그 접지) ───────────────────────────────────────────────────────────────
def _concept_spec(catalog: Any, concept_id: Any) -> tuple[Any, NormalizationResult | None]:
    """카탈로그에서 개념을 찾는다. 없거나 비활성이면 unresolved(unsupported_concept) 실패를 만든다."""
    if not (isinstance(concept_id, str) and concept_id.strip()):
        return None, NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("missing_value", field="concept"))
    spec = catalog.get_concept(concept_id)
    if spec is None:
        return None, NormalizationResult.failure(
            STATUS_UNRESOLVED,
            NormalizationError("unsupported_concept", field="concept", received=concept_id, retryable=False),
            catalog_match=False, execution_support=False)
    if not getattr(spec, "active", True):
        return None, NormalizationResult.failure(
            STATUS_UNRESOLVED,
            NormalizationError("unsupported_concept", field="concept", received=concept_id, retryable=False,
                               detail="비활성 개념"),
            catalog_match=False, execution_support=False)
    return spec, None


def normalize_threshold(raw: Any, *, catalog: Any) -> NormalizationResult:
    """{concept, operator, value} — concept 는 카탈로그에 존재·활성·컴파일러 등록까지 확인한다.

    LLM 이 카탈로그에 없는 concept 를 만들어 와도 여기서 unresolved(unsupported_concept) 로
    끝난다 — 의미가 그럴듯하다는 이유로 실행되지 않는다(요청 9번)."""
    if not isinstance(raw, Mapping):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_type", field="threshold", received=raw))
    spec, failure = _concept_spec(catalog, raw.get("concept"))
    if failure is not None:
        return failure
    concept_id = spec.concept_id
    operator_text = str(raw.get("operator")).strip() if raw.get("operator") is not None else ""
    if raw.get("value") is None and operator_text not in ambiguous_degree_terms():
        # 개념 해석과 값 해석의 분리(요청 8번): 개념은 붙었지만 임계값이 원문에서 확정되지
        # 않은 경우('돈을 많이 쓴 고객')다 — missing_value(형식 문제)가 아니라 missing_threshold.
        # 정도어 연산자('높은')는 아래 comparison 이 ambiguous_expression 으로 더 구체적으로 답한다.
        return NormalizationResult.failure(
            STATUS_NEEDS_REVIEW,
            NormalizationError("missing_threshold", field="value", retryable=False,
                               detail=f"{concept_id} 임계값이 원문에서 확정되지 않았다"),
            catalog_match=True)
    comparison = normalize_comparison(
        {"operator": raw.get("operator"), "value": raw.get("value")},
        policy=ComparisonPolicy(allowed_operators=frozenset(spec.allowed_operators)))
    if not comparison.ok:
        return NormalizationResult.failure(
            comparison.status, *comparison.errors, catalog_match=True,
            execution_support=None if comparison.status == STATUS_NEEDS_REVIEW else comparison.execution_support)
    value = comparison.value["value"]
    if spec.data_type == "integer" and not isinstance(value, int):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_type", field="value", received=raw.get("value"),
                                              detail=f"{concept_id} 는 정수 지표다"),
            catalog_match=True)
    if not catalog.is_executable(concept_id):
        compiler = catalog.get_compiler(concept_id)
        return NormalizationResult.failure(
            STATUS_UNRESOLVED,
            NormalizationError("compiler_not_registered", field="concept", received=concept_id, retryable=False,
                               detail=f"선언된 컴파일러 {compiler!r} 가 레지스트리에 없다"),
            catalog_match=True, execution_support=False)
    return NormalizationResult.normalized(
        {"concept": concept_id, "operator": comparison.value["operator"], "value": value},
        catalog_match=True, execution_support=True)


# ── EntityReference(후보 접지) ─────────────────────────────────────────────────────────────
def normalize_entity_reference(raw: Any, *, provider: Any) -> NormalizationResult:
    """{entity_type, canonical_id} — canonical_id 는 CandidateProvider 에 실재해야 한다.

    후보 검색 결과에 없던 임의 ID(LLM 생성)는 unknown_entity 로 차단된다(요청 7번)."""
    if not isinstance(raw, Mapping):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_type", field="entity_reference", received=raw))
    entity_type = raw.get("entity_type")
    canonical_id = raw.get("canonical_id")
    if not (isinstance(entity_type, str) and entity_type.strip()):
        return NormalizationResult.failure(STATUS_INVALID, NormalizationError("missing_value", field="entity_type"))
    if not (isinstance(canonical_id, str) and canonical_id.strip()):
        return NormalizationResult.failure(STATUS_INVALID, NormalizationError("missing_value", field="canonical_id"))
    entity = provider.get_by_id(canonical_id)
    if entity is None:
        return NormalizationResult.failure(
            STATUS_UNRESOLVED,
            NormalizationError("unknown_entity", field="canonical_id", received=canonical_id, retryable=False),
            catalog_match=False, execution_support=False)
    found_type = entity.get("entity_type") if isinstance(entity, Mapping) else getattr(entity, "entity_type", None)
    if found_type is not None and found_type != entity_type:
        return NormalizationResult.failure(
            STATUS_NEEDS_REVIEW,
            NormalizationError("candidate_not_allowed", field="entity_type", received=entity_type,
                               allowed_values=(found_type,)),
            catalog_match=True)
    active = entity.get("active", True) if isinstance(entity, Mapping) else getattr(entity, "active", True)
    if not active:
        return NormalizationResult.failure(
            STATUS_UNRESOLVED,
            NormalizationError("candidate_not_allowed", field="canonical_id", received=canonical_id,
                               retryable=False, detail="비활성/삭제된 엔티티"),
            catalog_match=True, execution_support=False)
    return NormalizationResult.normalized(
        {"entity_type": entity_type, "canonical_id": canonical_id}, catalog_match=True)


# ── GenericCondition(공통 조건) ────────────────────────────────────────────────────────────
def normalize_generic_condition(raw: Any, *, catalog: Any) -> NormalizationResult:
    """{concept, operator, value[, window][, aggregation]} — 공통 조건의 전체 검증(요청 19번).

    window/aggregation 은 카탈로그가 그 개념에 지원을 선언했을 때만 통과한다. 지원하지 않는
    항목을 LLM 이 임의로 추가하면 unsupported(execution_not_supported) 로 끝난다(요청 4번)."""
    if not isinstance(raw, Mapping):
        return NormalizationResult.failure(
            STATUS_INVALID, NormalizationError("invalid_type", field="generic_condition", received=raw))
    threshold = normalize_threshold(raw, catalog=catalog)
    if not threshold.ok:
        return threshold
    spec = catalog.get_concept(threshold.value["concept"])
    value = dict(threshold.value)

    window_raw = raw.get("window")
    if window_raw is not None:
        if not spec.supported_windows:
            return NormalizationResult.failure(
                STATUS_UNSUPPORTED,
                NormalizationError("execution_not_supported", field="window", received=window_raw,
                                   retryable=False, detail=f"{spec.concept_id} 는 기간 조건을 지원하지 않는다"),
                catalog_match=True, execution_support=False)
        window = normalize_duration(window_raw)
        if not window.ok:
            return NormalizationResult.failure(window.status, *window.errors, catalog_match=True)
        value["window"] = {"value": window.value["value"], "unit": window.value["unit"]}
        value["window_days"] = window.value["min_days"]

    aggregation_raw = raw.get("aggregation")
    if aggregation_raw is not None:
        aggregation = str(aggregation_raw).strip().lower()
        if aggregation not in spec.supported_aggregations:
            return NormalizationResult.failure(
                STATUS_UNSUPPORTED,
                NormalizationError("execution_not_supported", field="aggregation", received=aggregation_raw,
                                   retryable=False,
                                   allowed_values=tuple(spec.supported_aggregations)),
                catalog_match=True, execution_support=False)
        value["aggregation"] = aggregation

    return NormalizationResult.normalized(value, catalog_match=True, execution_support=True)


# ── 정규화기 레지스트리 ────────────────────────────────────────────────────────────────────
# 슬롯/개념 정의는 이 이름만 선언한다("normalizer": "duration"). 새 슬롯이 새 함수를 만들 필요가
# 없는지 먼저 여기를 본다 — 여기 없는 검증 구조일 때만 전용 정규화기를 추가한다.
NORMALIZERS: dict[str, Callable[..., NormalizationResult]] = {
    "duration": normalize_duration,
    "comparison": normalize_comparison,
    "date_window": normalize_date_window,
    "threshold": normalize_threshold,
    "entity_reference": normalize_entity_reference,
    "generic_condition": normalize_generic_condition,
}


def get_normalizer(name: str) -> Callable[..., NormalizationResult]:
    normalizer = NORMALIZERS.get(name)
    if normalizer is None:
        raise KeyError(f"미등록 정규화기: {name!r} — condition_normalizers.NORMALIZERS 에 선언해야 한다.")
    return normalizer


__all__ = [
    "COMPARISON_SYMBOLS",
    "DEFAULT_COMPARISON_POLICY",
    "DEFAULT_DURATION_POLICY",
    "DEFAULT_NORMALIZATION_LEXICON_PATH",
    "NORMALIZERS",
    "ComparisonPolicy",
    "DurationPolicy",
    "ambiguous_degree_terms",
    "canonical_operator",
    "canonical_unit",
    "clear_lexicon_cache",
    "comparison_word_operators",
    "get_normalizer",
    "normalize_comparison",
    "normalize_date_window",
    "normalize_duration",
    "normalize_entity_reference",
    "normalize_generic_condition",
    "normalize_threshold",
    "operator_aliases",
    "unit_aliases",
    "unit_conversions",
    "unit_days",
]
