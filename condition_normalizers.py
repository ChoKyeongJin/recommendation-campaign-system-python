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

어휘(단위 별칭·비교어·모호 정도어)는 docs/data/runtime/language/normalization_lexicon.json 이 소유하고, 파일이
없거나 파손되면 ``_CODE_FALLBACK`` 으로 폴백한다(:mod:`lexicon_patterns` 와 같은 관례).
모호 정도어('높은/많은/자주' 등)는 어떤 연산자로도 강제 확정하지 않는다 — needs_review 로 낸다.

카탈로그/후보 제공자는 덕 타이핑으로 받는다(concept_catalog.ConceptCatalog /
CandidateProvider 프로토콜). 이 모듈은 graph_rag 를 import 하지 않는다(순수 모듈).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
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
DEFAULT_NORMALIZATION_LEXICON_PATH = (
    _REPO_ROOT / "docs" / "data" / "runtime" / "language" / "normalization_lexicon.json"
)

# JSON 과 같은 스키마의 코드 폴백(이관 시점 값 그대로). 두 소스의 드리프트는
# tests/test_condition_normalizers.py 의 parity 가드가 막는다.
_CODE_FALLBACK: dict[str, Any] = {
    "units": {
        "days": ["일", "일간", "하루", "day", "days"],
        "weeks": ["주", "주일", "week", "weeks"],
        "months": ["개월", "달", "월", "month", "months"],
        "years": ["년", "년간", "해", "year", "years"],
    },
    "unit_days": {"days": 1, "weeks": 7, "months": 30, "years": 365},
    "unit_conversions": {
        "분기": {"unit": "months", "factor": 3},
        "quarter": {"unit": "months", "factor": 3},
        "quarters": {"unit": "months", "factor": 3},
        "반기": {"unit": "months", "factor": 6},
    },
    "comparison_word_operators": {"이상": ">=", "초과": ">", "이하": "<=", "미만": "<"},
    "operator_aliases_extended": {
        ">=": ["최소", "적어도"],
        "<=": ["최대", "이내", "이내로", "안에"],
        "=": ["같은", "정확히"],
        ">": ["넘는", "넘게", "초과하는"],
    },
    "ambiguous_degree_terms": [
        "높은", "많은", "낮은", "적은", "많이", "꽤", "꽤 많이", "자주", "오랫동안", "우수한",
    ],
}

COMPARISON_SYMBOLS: frozenset[str] = frozenset({">=", ">", "<=", "<", "="})
_FIXED_DURATION_UNIT_DAYS: dict[str, int] = {"days": 1, "weeks": 7}


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
    """일수로 의미 손실 없이 환산 가능한 canonical 단위만 돌려준다.

    달과 해는 기준일에 따라 실제 일수가 달라진다. 어휘 파일의 과거 호환값(30/365)은
    정규화 의미로 사용하지 않는다.
    """
    return dict(_FIXED_DURATION_UNIT_DAYS)


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


def comparison_literal_operators() -> dict[str, str]:
    """Literal scanner가 소유할 닫힌 비교 표면 집합.

    확장 별칭은 의미 정규화 단계에서만 사용하고, scanner는 기존 네 단어와 네
    부등호만 인식한다. 이 경계를 한 선언에서 파생해 parser별 복제 표를 없앤다.
    """

    out = comparison_word_operators()
    for symbol in (">=", "<=", ">", "<"):
        out[symbol] = symbol
    return out


# 숫자 바로 뒤에 붙을 수 있는 기간 단위의 **어간**. ``-간`` 접미형은 여기서 파생한다.
_NUMERIC_DURATION_BASE_SURFACES: tuple[str, ...] = (
    "주일",
    "개월",
    "일",
    "주",
    "달",
    "년",
)
# 기간 접미 ``-간``('30일간', '3개월간'). 표면을 손으로 나열하던 동안 '일간/년간'만 있고
# '개월간/주간/달간/주일간'은 없었다 — 같은 문법인데 단위에 따라 기간이 보이기도, 안 보이기도
# 했다('3개월간 구매' → 기간 결핍으로 되묻기). 파생으로 바꿔 그 불균일이 재발하지 않게 한다.
_DURATION_SPAN_SUFFIX = "간"


def numeric_duration_unit_semantics() -> dict[str, str]:
    """숫자 기간 scanner가 허용하는 한국어 표면 → canonical 단위.

    언어 별칭의 소유자는 normalization lexicon이고 이 함수는 그중 숫자 바로 뒤에
    붙을 수 있는 닫힌 부분집합만 선언한다. ``-간`` 접미형은 어간에서 **파생**하므로
    단위마다 표면을 다시 적지 않는다.

    ``주간``은 '주간 리포트'(weekly)로도 쓰이지만 이 표면 목록은 숫자가 바로 앞에 붙은
    자리에서만 소비된다(scanner의 ``\\d+`` 선행 조건). 숫자 없는 '주간'은 여기 걸리지 않는다.
    """

    aliases = unit_aliases()
    base = {
        surface: aliases[surface]
        for surface in _NUMERIC_DURATION_BASE_SURFACES
        if surface in aliases
    }
    derived = dict(base)
    for surface, canonical in base.items():
        derived.setdefault(surface + _DURATION_SPAN_SUFFIX, canonical)
    return derived


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
def _parse_number(
    raw: Any, *, allow_string: bool, field: str
) -> tuple[Decimal | int | None, NormalizationError | None]:
    """유한 숫자 하나를 이진 부동소수점 왕복 없이 읽는다."""

    if raw is None:
        return None, NormalizationError("missing_value", field=field)
    if isinstance(raw, bool):
        return None, NormalizationError("invalid_type", field=field, received=raw)
    if isinstance(raw, str):
        if not allow_string:
            return None, NormalizationError("invalid_type", field=field, received=raw)
        text = raw.strip().replace(",", "")
        if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text) is None:
            return None, NormalizationError("invalid_number", field=field, received=raw)
        try:
            exact = Decimal(text)
        except InvalidOperation:
            return None, NormalizationError("invalid_number", field=field, received=raw)
    elif isinstance(raw, Decimal):
        exact = raw
    elif isinstance(raw, (int, float)):
        try:
            exact = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None, NormalizationError("invalid_number", field=field, received=raw)
    else:
        return None, NormalizationError("invalid_type", field=field, received=raw)
    if not exact.is_finite():
        return None, NormalizationError("invalid_number", field=field, received=raw)
    integral = exact.to_integral_value()
    return (int(integral) if exact == integral else exact), None


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
    """{value, unit} 표준 후보를 검증한다.

    ``min_days`` 는 일/주처럼 정확히 환산되는 단위에만 파생한다. 월/년을 30/365일로
    근사하지 않으며, 그 단위는 원래 ``{value, unit}`` 의미를 그대로 보존한다.
    """
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
    normalized: dict[str, Any] = {"value": value, "unit": unit}
    fixed_days = unit_days().get(unit)
    if fixed_days is not None:
        normalized["min_days"] = value * fixed_days
    return NormalizationResult.normalized(normalized)


# ── Comparison ────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ComparisonPolicy:
    allowed_operators: frozenset[str] = frozenset({">=", ">", "<=", "<"})
    allow_zero: bool = True
    allow_negative: bool = False
    allow_string_numbers: bool = True
    min_value: int | Decimal | None = None
    max_value: int | Decimal | None = None


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
        if "min_days" in window.value:
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
    "comparison_literal_operators",
    "comparison_word_operators",
    "get_normalizer",
    "normalize_comparison",
    "normalize_date_window",
    "normalize_duration",
    "normalize_entity_reference",
    "normalize_generic_condition",
    "normalize_threshold",
    "numeric_duration_unit_semantics",
    "operator_aliases",
    "unit_aliases",
    "unit_conversions",
    "unit_days",
]
