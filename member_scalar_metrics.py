"""회원별 스칼라 지표 계약 → canonical Event IR lowering.

무엇이 다른가
-------------
카탈로그에는 같은 물리 컬럼을 가리키는 지표가 두 종류 있다.

``member_metric_*`` (kind=aggregate, ranking_* 선언)
    **전역 순위**의 재료다. ``Limit(Order(Summarize(전역 Source)))`` 로 모집단을 만들고 회원과
    semi-join 한다. 그래서 모집단·정렬 방향·동점자 정책·최소 표본이 선언에 함께 있다.

``member_scalar_*`` (kind=member_scalar, cardinality=scalar)
    **회원 한 명의 값**이다. 최신 스냅샷 행이 회원당 0개 또는 1개이므로 접을 것이 없고,
    순위 의미도 없다(순위는 모집단 개념인데 이 계약은 회원 한 명만 말한다).

이 둘을 한 계약으로 쓰면 '구매주기가 30일 이하인 회원'이 전역 순위 선언을 타면서 모집단
정책이 조용히 사라진다. 그래서 :mod:`resolved_semantic_catalog` 가 kind 를 나누고, 이 모듈이
**회원별 스칼라 쪽만** 낮춘다. 요청이 순위 지표를 이 자리에 넣으면
``catalog_contract_mismatch`` 로 이름을 대며 막힌다.

낮추는 모양
-----------
새 IR 노드가 필요 없다 — 기존 조합 하나다::

    Exists(Filter(Source(<member_scalar 소스>),
                  Comparison(FieldRef(<값 필드>), <연산자>, Literal(<임계값>))))

소스 선언의 고정 술어가 최신 월 스냅샷을 고정하고, 상관식이 회원을 묶는다. 컴파일 결과는
기존 프로필 지표 SQL 과 같은 모양이다::

    EXISTS (SELECT 1 FROM CRM_MB_MONTHCRMINFO MS
            WHERE MS.MEMBER_NO = B.MEMBER_NO
              AND MS.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)
              AND MS.BUY_CYCLE <= 30)

NULL·0행 정책
-------------
둘 다 선언(``null_behavior`` / ``zero_row_behavior``)이고 둘 다 ``exclude`` 다. 값이 NULL 이면
비교가 UNKNOWN 이라 그 회원은 빠지고, 스냅샷 행 자체가 없으면 ``EXISTS`` 가 거짓이라 빠진다.
이 두 결말을 SQL 이 만드는 것이지 이 모듈이 따로 술어를 덧붙이는 것이 아니다 — 정책과 렌더가
어긋날 여지를 두지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import event_compiler
import event_ir
import resolved_semantic_catalog
import sql_dialect

# 이 모듈이 제공하는 계약 capability. 표현 모양이 아니라 '회원당 0..1 값을 읽는 지표를 낮출 수
# 있다'는 선언이고, 컴파일러 capability 와 합쳐 요구 집합을 판정한다.
SUPPORTED_CAPABILITIES: frozenset[str] = frozenset({"metric.member_scalar"})

MEMBER_SCALAR_KIND = "member_scalar"
CONTRACT_MISMATCH = "catalog_contract_mismatch"
CAPABILITY_UNSUPPORTED = "catalog_capability_unsupported"
VALUE_TYPE_MISMATCH = "catalog_value_type_mismatch"

# 서열이 필요한 비교. 값 타입에 순서가 없으면(문자열 코드 등) 부등호는 사전식 비교가 되어
# 조용히 틀린 집합을 만든다 — 저장소가 등급 컬럼에서 이미 겪은 실패다.
_ORDERED_OPERATORS = frozenset({">", ">=", "<", "<="})
_ORDERED_VALUE_TYPES = frozenset({"number", "integer", "decimal", "money"})
# 값 타입 → 리터럴로 허용되는 Python 타입. bool 은 어디에도 넣지 않는다(숫자로 승격되면
# True 가 1 이 되어 뜻이 바뀐다).
_VALUE_TYPE_LITERALS: dict[str, tuple[type, ...]] = {
    "number": (int, float, Decimal),
    "integer": (int,),
    "decimal": (int, float, Decimal),
    "money": (int, float, Decimal),
    "string": (str,),
}


class MemberScalarContractError(ValueError):
    """회원별 스칼라 지표 계약 위반. 어떤 심볼의 무엇이 왜 어긋났는지 이름을 댄다."""

    def __init__(self, code: str, message: str, *, symbol: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.symbol = symbol


@dataclass(frozen=True)
class MemberScalarPredicate:
    """낮춘 조건 + 무엇이 그것을 증명했는지의 영수증."""

    expression: event_ir.Condition
    receipt: dict[str, Any]


def supported_capabilities(
    dialect: sql_dialect.SqlDialect | None = None,
) -> frozenset[str]:
    """이 경로 전체(계약 + 컴파일러)가 제공하는 capability 집합."""

    return SUPPORTED_CAPABILITIES | event_compiler.compiler_capabilities(dialect)


def member_scalar_metric_ids(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
) -> tuple[str, ...]:
    """카탈로그가 회원별 스칼라로 선언한 지표 id 전부(정렬 — 결정론)."""

    return tuple(
        sorted(
            metric_id
            for metric_id, metric in catalog.metrics.items()
            if metric.kind == MEMBER_SCALAR_KIND
        )
    )


def validate_member_scalar_contract(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric_id: str,
    *,
    operator: str,
    value: Any,
    dialect: sql_dialect.SqlDialect | None = None,
) -> resolved_semantic_catalog.MetricSpec:
    """요청이 회원별 스칼라 계약을 만족하는지 검증하고 지표 선언을 돌려준다.

    검증을 lowering 과 분리해 두는 이유: 계약 위반은 **SQL 을 만들다 터지는 예외**가 아니라
    선언을 짚어 답할 수 있는 결과여야 한다. 호출자는 이 함수의 오류 코드를 그대로 issue 로
    바꿀 수 있다.
    """

    try:
        metric = catalog.metric(metric_id)
    except resolved_semantic_catalog.CatalogError as exc:
        raise MemberScalarContractError(
            str(exc.code), str(exc), symbol=exc.symbol or metric_id
        ) from exc

    if metric.kind != MEMBER_SCALAR_KIND:
        raise MemberScalarContractError(
            CONTRACT_MISMATCH,
            f"metric {metric.id!r} is declared as {metric.kind!r}, not {MEMBER_SCALAR_KIND!r}; "
            "a global ranking metric cannot be used as a per-member scalar threshold",
            symbol=metric.id,
        )
    if metric.grain != resolved_semantic_catalog.SUBJECT_GRAIN:
        raise MemberScalarContractError(
            CONTRACT_MISMATCH,
            f"metric {metric.id!r} is grained on {metric.grain!r}; a member scalar must be "
            f"grained on {resolved_semantic_catalog.SUBJECT_GRAIN!r}",
            symbol=metric.id,
        )
    if metric.cardinality != "scalar":
        raise MemberScalarContractError(
            CONTRACT_MISMATCH,
            f"metric {metric.id!r} declares {metric.cardinality!r} cardinality; a member "
            "scalar threshold needs exactly zero or one value per member",
            symbol=metric.id,
        )
    if metric.aggregate_function is not None:
        raise MemberScalarContractError(
            CONTRACT_MISMATCH,
            f"metric {metric.id!r} declares aggregate function "
            f"{metric.aggregate_function!r}; a member scalar reads a stored value",
            symbol=metric.id,
        )

    missing = sorted(set(metric.required_capabilities) - supported_capabilities(dialect))
    if missing:
        raise MemberScalarContractError(
            CAPABILITY_UNSUPPORTED,
            f"metric {metric.id!r} requires unavailable capabilities {missing}",
            symbol=metric.id,
        )

    symbol = event_ir.canonical_comparison_operator(operator)
    if symbol is None or symbol not in metric.allowed_operators:
        raise MemberScalarContractError(
            CONTRACT_MISMATCH,
            f"metric {metric.id!r} does not allow comparison operator {operator!r}; "
            f"declared operators are {sorted(metric.allowed_operators)}",
            symbol=metric.id,
        )

    allowed_literals = _VALUE_TYPE_LITERALS.get(metric.value_type)
    if allowed_literals is None:
        raise MemberScalarContractError(
            VALUE_TYPE_MISMATCH,
            f"metric {metric.id!r} has value type {metric.value_type!r}, which has no "
            "declared literal form for threshold comparison",
            symbol=metric.id,
        )
    if isinstance(value, bool) or not isinstance(value, allowed_literals):
        raise MemberScalarContractError(
            VALUE_TYPE_MISMATCH,
            f"metric {metric.id!r} expects a {metric.value_type} threshold, "
            f"got {type(value).__name__}",
            symbol=metric.id,
        )
    if symbol in _ORDERED_OPERATORS and metric.value_type not in _ORDERED_VALUE_TYPES:
        raise MemberScalarContractError(
            VALUE_TYPE_MISMATCH,
            f"metric {metric.id!r} has unordered value type {metric.value_type!r}; "
            f"operator {symbol!r} would compare stored values lexically",
            symbol=metric.id,
        )

    # 소스·필드 유효성은 카탈로그 로딩이 이미 교차검증했지만, 여기서 다시 조회하는 이유는
    # 이 함수 하나가 "이 요청으로 IR 을 만들어도 되는가"의 완전한 답이어야 하기 때문이다.
    catalog.source(metric.source)
    catalog.field(str(metric.expression_field))
    return metric


def lower_member_scalar_metric(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric_id: str,
    *,
    operator: str,
    value: Any,
    evidence: event_ir.Evidence | None = None,
    dialect: sql_dialect.SqlDialect | None = None,
) -> MemberScalarPredicate:
    """검증된 회원별 스칼라 임계값을 canonical Event IR 조건으로 낮춘다."""

    metric = validate_member_scalar_contract(
        catalog, metric_id, operator=operator, value=value, dialect=dialect
    )
    symbol = str(event_ir.canonical_comparison_operator(operator))
    field = event_ir.FieldRef(name=str(metric.expression_field))
    # Decimal 은 IR 리터럴 어휘 밖이다(JSON 경계에서 표현이 흔들린다). 정수로 정확히 떨어지는
    # 값만 정수로 접고, 그 밖에는 소수 표기를 잃지 않도록 float 로 넘기지 않고 거절한다.
    literal_value = _literal_value(metric, value)
    expression: event_ir.Condition = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name=metric.source),
            where=event_ir.Comparison(
                operator=symbol,
                left=field,
                right=event_ir.Literal(value=literal_value),
                evidence=evidence,
            ),
        ),
        evidence=evidence,
    )
    receipt = {
        "metric_id": metric.id,
        "kind": metric.kind,
        "cardinality": metric.cardinality,
        "grain": metric.grain,
        "source": metric.source,
        "value_field": metric.expression_field,
        "value_type": metric.value_type,
        "operator": symbol,
        "threshold": literal_value,
        "null_behavior": metric.null_behavior,
        "zero_row_behavior": metric.zero_row_behavior,
        "required_capabilities": list(metric.required_capabilities),
    }
    return MemberScalarPredicate(expression=expression, receipt=receipt)


def _literal_value(metric: resolved_semantic_catalog.MetricSpec, value: Any) -> Any:
    if isinstance(value, Decimal):
        if value == value.to_integral_value():
            return int(value)
        # 소수 임계값을 float 로 접으면 이 저장소가 금액·비율에서 피하는 바로 그 손실이 난다.
        raise MemberScalarContractError(
            VALUE_TYPE_MISMATCH,
            f"metric {metric.id!r} cannot carry the fractional decimal threshold "
            f"{value} through the JSON literal boundary",
            symbol=metric.id,
        )
    return value


__all__ = [
    "CAPABILITY_UNSUPPORTED",
    "CONTRACT_MISMATCH",
    "MEMBER_SCALAR_KIND",
    "SUPPORTED_CAPABILITIES",
    "VALUE_TYPE_MISMATCH",
    "MemberScalarContractError",
    "MemberScalarPredicate",
    "lower_member_scalar_metric",
    "member_scalar_metric_ids",
    "supported_capabilities",
    "validate_member_scalar_contract",
]
