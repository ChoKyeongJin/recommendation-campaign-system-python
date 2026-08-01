"""정규화 결과의 구조화 타입 — 닫힌 상태 집합·닫힌 오류 코드·재시도 가능성의 단일 소유자.

배경: 슬롯별 ``_coerce_*`` 는 실패를 전부 ``None`` 하나로 접는다. 호출자는 "값이 이상했는지",
"단위가 미지원인지", "개념 자체가 카탈로그에 없는지"를 구분할 수 없어서 재시도·안내·unresolved
분기가 불가능했다. 이 모듈은 그 실패를 구조화한다:

  NormalizationError  — 오류 하나(code/field/received/allowed_values/retryable).
  NormalizationResult — 정규화 한 번의 전체 결과(ok/status/value/errors + 실행 가능성 축).

세 축의 분리(요청 3번)가 이 타입의 핵심이다:
  semantic_match    — 의미를 해석할 수 있었는가(해석 계층이 판단).
  catalog_match     — 연결된 등록 데이터 개념이 있는가(concept_catalog 가 판단).
  execution_support — 등록된 컴파일러로 SQL 화가 가능한가(compiler_registry 가 판단).
confidence 가 아무리 높아도 이 축들이 거짓이면 SQL 생성이 허용되지 않는다.

닫힌 집합 관례는 :mod:`unsupported_reasons` 와 같다 — 식별자로 분기하는 이름은 여기 선언된
것만 쓰고, 계약 테스트(tests/test_normalization_result.py)가 생산·소비 양방향을 강제한다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다(stdlib 만).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 상태 닫힌 집합 ─────────────────────────────────────────────────────────────────────────
STATUS_CANDIDATE = "candidate"          # 의미 해석이 만든 미검증 후보
STATUS_NORMALIZED = "normalized"        # 결정론 검증 통과(SQL 진행 가능 유일 상태)
STATUS_NEEDS_REVIEW = "needs_review"    # 개념은 해석됐지만 값/연산자가 모호 — 사람 확인 필요
STATUS_UNRESOLVED = "unresolved"        # 카탈로그 개념·후보·컴파일러 부재 — 실행 불가
STATUS_INVALID = "invalid"              # 값 자체가 유효하지 않음(타입/범위/형식)
STATUS_UNSUPPORTED = "unsupported"      # 개념은 있으나 요청한 조합(window/aggregation 등) 미지원

ALL_STATUSES: frozenset[str] = frozenset({
    STATUS_CANDIDATE,
    STATUS_NORMALIZED,
    STATUS_NEEDS_REVIEW,
    STATUS_UNRESOLVED,
    STATUS_INVALID,
    STATUS_UNSUPPORTED,
})

# SQL 생성 단계로 진행할 수 있는 상태(요청 17번: 이 밖의 상태는 전부 차단).
SQL_ALLOWED_STATUSES: frozenset[str] = frozenset({STATUS_NORMALIZED})

# ── 오류 코드 닫힌 집합(요청 12번 최소 목록) ────────────────────────────────────────────────
ERROR_CODES: frozenset[str] = frozenset({
    "missing_value",
    "invalid_type",
    "invalid_number",
    "out_of_range",
    "unsupported_unit",
    "unsupported_operator",
    "unknown_slot",
    "unknown_metric",
    "unknown_entity",
    "unknown_concept",
    "unsupported_concept",
    "compiler_not_registered",
    "ambiguous_expression",
    "candidate_not_found",
    "candidate_not_allowed",
    "invalid_date_range",
    "source_span_missing",
    "source_span_mismatch",
    "low_confidence",
    "missing_threshold",
    "execution_not_supported",
})

# 재시도 가능 오류(요청 13번): 의미 해석 계층이 허용값 안에서 고쳐 다시 시도할 수 있는 것만.
# 개념·컴파일러 부재는 재해석으로 해결되지 않으므로 재시도 대상이 아니다.
RETRYABLE_CODES: frozenset[str] = frozenset({
    "unsupported_unit",
    "unsupported_operator",
    "candidate_not_allowed",
    "invalid_type",
})

assert RETRYABLE_CODES <= ERROR_CODES, "RETRYABLE_CODES 는 ERROR_CODES 의 부분집합이어야 한다"


@dataclass(frozen=True)
class NormalizationError:
    """정규화 오류 하나. ``retryable`` 은 코드에서 파생되는 기본값을 갖는다(명시 시 명시가 이긴다)."""

    code: str
    field: str = ""
    received: Any = None
    allowed_values: tuple[Any, ...] = ()
    retryable: bool | None = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.code not in ERROR_CODES:
            raise ValueError(f"미등록 정규화 오류 코드: {self.code!r} — normalization_result.ERROR_CODES 에 선언해야 한다.")
        if self.retryable is None:
            object.__setattr__(self, "retryable", self.code in RETRYABLE_CODES)
        if not isinstance(self.allowed_values, tuple):
            object.__setattr__(self, "allowed_values", tuple(self.allowed_values))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "retryable": bool(self.retryable)}
        if self.field:
            out["field"] = self.field
        if self.received is not None:
            out["received"] = self.received
        if self.allowed_values:
            out["allowed_values"] = list(self.allowed_values)
        if self.detail:
            out["detail"] = self.detail
        return out


@dataclass(frozen=True)
class NormalizationResult:
    """정규화 한 번의 전체 결과. ``value`` 는 status=normalized 일 때만 채워진다.

    catalog_match/execution_support 는 3값이다 — ``None`` 은 "이 정규화기는 그 축을 판정하지
    않았다"(예: Duration 은 카탈로그와 무관)이고, False 는 "판정했고 불일치"다. SQL 허용 판정
    (:meth:`sql_generation_allowed`)은 None 을 통과로, False 를 차단으로 본다."""

    ok: bool
    status: str
    value: Any = None
    errors: tuple[NormalizationError, ...] = ()
    catalog_match: bool | None = None
    execution_support: bool | None = None
    semantic_description: str = ""  # unresolved 시 관리자 등록 참고용(실행에는 사용 금지)

    def __post_init__(self) -> None:
        if self.status not in ALL_STATUSES:
            raise ValueError(f"미등록 정규화 상태: {self.status!r} — normalization_result.ALL_STATUSES 에 선언해야 한다.")
        if self.ok and self.status not in SQL_ALLOWED_STATUSES:
            raise ValueError(f"ok=True 는 status={sorted(SQL_ALLOWED_STATUSES)} 에서만 허용된다(받은 값: {self.status!r}).")
        if self.ok and self.errors:
            raise ValueError("ok=True 결과는 오류를 가질 수 없다.")
        if not self.ok and self.value is not None:
            raise ValueError("실패 결과는 value 를 실을 수 없다(검증 안 된 값의 하류 유출 차단).")
        if not isinstance(self.errors, tuple):
            object.__setattr__(self, "errors", tuple(self.errors))

    # ── 팩토리 ────────────────────────────────────────────────────────────────────────────
    @classmethod
    def normalized(
        cls,
        value: Any,
        *,
        catalog_match: bool | None = None,
        execution_support: bool | None = None,
    ) -> "NormalizationResult":
        return cls(ok=True, status=STATUS_NORMALIZED, value=value,
                   catalog_match=catalog_match, execution_support=execution_support)

    @classmethod
    def failure(
        cls,
        status: str,
        *errors: NormalizationError,
        catalog_match: bool | None = None,
        execution_support: bool | None = None,
        semantic_description: str = "",
    ) -> "NormalizationResult":
        if status in SQL_ALLOWED_STATUSES:
            raise ValueError(f"실패 상태로 {status!r} 를 쓸 수 없다.")
        return cls(ok=False, status=status, errors=tuple(errors),
                   catalog_match=catalog_match, execution_support=execution_support,
                   semantic_description=semantic_description)

    # ── 판정 ──────────────────────────────────────────────────────────────────────────────
    @property
    def sql_generation_allowed(self) -> bool:
        """SQL 생성 허용 여부(요청 17번). ok·상태·카탈로그·실행 지원이 전부 통과해야 한다."""
        return (
            self.ok
            and self.status in SQL_ALLOWED_STATUSES
            and self.catalog_match is not False
            and self.execution_support is not False
        )

    def retryable_errors(self) -> tuple[NormalizationError, ...]:
        return tuple(error for error in self.errors if error.retryable)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "value": self.value,
            "errors": [error.to_dict() for error in self.errors],
            "sql_generation_allowed": self.sql_generation_allowed,
        }
        if self.catalog_match is not None:
            out["catalog_match"] = self.catalog_match
        if self.execution_support is not None:
            out["execution_support"] = self.execution_support
        if self.semantic_description:
            out["semantic_description"] = self.semantic_description
        return out


__all__ = [
    "ALL_STATUSES",
    "ERROR_CODES",
    "RETRYABLE_CODES",
    "SQL_ALLOWED_STATUSES",
    "STATUS_CANDIDATE",
    "STATUS_INVALID",
    "STATUS_NEEDS_REVIEW",
    "STATUS_NORMALIZED",
    "STATUS_UNRESOLVED",
    "STATUS_UNSUPPORTED",
    "NormalizationError",
    "NormalizationResult",
]
