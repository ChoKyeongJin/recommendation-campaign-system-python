"""전이 지표 계약 → canonical Event IR lowering.

무엇을 하는 모듈인가
--------------------
"현재값이 B 이고 직전값이 A 였다"는 조건을 **선언만 읽어서** Event IR 로 조립한다. 실행하지
않는다 — 데이터베이스에 연결하지 않고, 그 값이 실제로 존재하는지도 묻지 않는다. 값의 유효성은
카탈로그의 값 사전 하나로 판정한다(사전 밖 문자열이 SQL 로 새는 것을 막는 것이 그 검증의 목적이고,
"실데이터에 그 값이 있는가"는 이 계층의 질문이 아니다).

축 이름을 모른다
----------------
이 파일에는 ``grade``·``prev_grade``·``worth_grade``·``ZTS_GRADE``·``MEM_GRADE_CD``·
``member_month_snapshot`` 같은 이름이 **없다**. 현재값 필드·직전값 필드·소스·값 사전·서열은
전부 지표 선언에서 온다. 그래서 회원등급 전이와 가치등급 전이가 같은 코드로 낮아지고,
새 전이 축은 카탈로그 한 항목으로 는다.

전략(strategy)
--------------
``current_previous_columns`` 하나만 낮춘다 — 한 행에 현재값 컬럼과 직전값 컬럼이 함께 있는
모양이다. ``history_rows``·``event`` 는 :data:`resolved_semantic_catalog.TRANSITION_STRATEGIES`
가 선언한 **예약 어휘**이고 여기서는 ``transition_strategy_unsupported`` 로 닫는다. 어휘와
구현을 나눈 이유는 사유의 정확성이다 — 오타는 카탈로그 문법 오류이고, 예약어는 미지원이다.

낮추는 모양
-----------
새 IR 노드가 필요 없다 — 기존 조합 하나다::

    Exists(Filter(Source(<선언된 소스>),
                  And(Comparison(FieldRef(<현재값 필드>),  "=", Literal(<to>)),
                      Comparison(FieldRef(<직전값 필드>), "=", Literal(<from>)))))

두 비교가 **한 Filter 안**에 있는 것이 이 지표의 존재 이유다. 두 조건을 각각 EXISTS 로 쪼개면
서로 다른 행에서 만족되어도 통과한다("1월에 골드→실버, 3월에 X→VIP" 인 회원이 '골드에서 VIP로'에
걸린다). 적재가 늘면 조용히 뒤바뀌는 오답이므로 모양으로 막는다.

리터럴은 canonical 값이다
-------------------------
IR 에는 canonical 값(``gold_grade``)이 들어가고 물리 코드(``MEM_GRADE_CD.GOLD``)로의 변환은
:class:`event_compiler.FieldSpec` 이 한다. 여기서 물리 코드를 만들면 같은 표가 두 곳에 생기고,
두 표는 곧 갈라진다. 이 모듈은 그 표를 **읽기만** 한다(사전에 없는 값을 미리 거절하기 위해).

최신 월 고정
------------
이 모듈의 책임이 **아니다**. 고정이 필요하면 소스 선언(``extra_predicates``)이 소유하고 source
lowering 이 붙인다. 전이라는 이유만으로 술어를 지어내지 않는다 — 선언에 없는 술어를 여기서
덧붙이면 같은 소스를 읽는 다른 지표와 SQL 이 달라진다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import event_compiler
import event_ir
import resolved_semantic_catalog
import sql_dialect

# 이 모듈이 제공하는 계약 capability. 표현 모양이 아니라 '현재값과 직전값을 같은 행에서 함께
# 읽는 지표를 낮출 수 있다'는 선언이다.
SUPPORTED_CAPABILITIES: frozenset[str] = frozenset({"metric.transition"})

TRANSITION_KIND = resolved_semantic_catalog.TRANSITION_KIND
# 카탈로그가 **선언할 수 있는** 전략 어휘(소유자는 카탈로그 계층이다).
DECLARED_STRATEGIES: frozenset[str] = resolved_semantic_catalog.TRANSITION_STRATEGIES
# 이 모듈이 **낮출 수 있는** 전략. 어휘의 부분집합이며, 나머지는 미지원으로 닫힌다.
SUPPORTED_STRATEGIES: frozenset[str] = frozenset({"current_previous_columns"})

# 전이 비교에 쓰는 유일한 연산자. 부등호는 값 사전의 서열이 아니라 저장 문자열을 비교하게 되므로
# 전이 지표의 allowed_operators 에서도 제외돼 있다.
EQUALITY = "="

CONTRACT_MISMATCH = "catalog_contract_mismatch"
CAPABILITY_UNSUPPORTED = "catalog_capability_unsupported"
INVALID_DECLARATION = "invalid_catalog_declaration"
STRATEGY_MISSING = "transition_strategy_missing"
STRATEGY_UNSUPPORTED = "transition_strategy_unsupported"
DOMAIN_MISMATCH = "transition_domain_mismatch"
VALUE_UNKNOWN = "transition_value_unknown"
VALUES_IDENTICAL = "transition_values_identical"
DIRECTION_UNVERIFIABLE = "transition_direction_unverifiable"
DIRECTION_CONTRADICTED = "transition_direction_contradicted"
METRIC_NOT_DECLARED = "transition_metric_not_declared"
METRIC_AMBIGUOUS = "transition_metric_ambiguous"

# 방향 어휘의 값. 표면어(승급/강등)의 소유자는 :mod:`targeting_domain` 이고, 여기서는 그것이
# 사상되는 **서열 방향** 두 개만 안다.
ASCENDING = "ascending"
DESCENDING = "descending"
DIRECTIONS: frozenset[str] = frozenset({ASCENDING, DESCENDING})


class TransitionContractError(ValueError):
    """전이 계약 위반. 어떤 심볼의 무엇이 왜 어긋났는지 이름을 댄다."""

    def __init__(self, code: str, message: str, *, symbol: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.symbol = symbol


@dataclass(frozen=True)
class LoweredTransition:
    """낮춘 조건 + 어떤 선언과 어떤 값으로 그것을 만들었는지의 영수증."""

    expression: event_ir.Condition
    receipt: dict[str, Any]


def supported_capabilities(
    dialect: sql_dialect.SqlDialect | None = None,
) -> frozenset[str]:
    """이 경로 전체(계약 + 컴파일러)가 제공하는 capability 집합."""

    return SUPPORTED_CAPABILITIES | event_compiler.compiler_capabilities(dialect)


def transition_metric_ids(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
) -> tuple[str, ...]:
    """카탈로그가 전이로 선언한 지표 id 전부(정렬 — 결정론)."""

    return tuple(
        sorted(
            metric_id
            for metric_id, metric in catalog.metrics.items()
            if metric.kind == TRANSITION_KIND
        )
    )


def transition_value_domain(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric: resolved_semantic_catalog.MetricSpec,
) -> str:
    """이 전이 지표가 다루는 값 도메인.

    현재값과 직전값이 같은 도메인이어야 한다. 다르면 두 컬럼의 값을 같은 사전으로 읽을 수 없고,
    그때 만들어지는 SQL 은 한쪽 값이 다른 사전의 코드로 나가는 조용한 오답이다.
    """

    current = catalog.field(str(metric.expression_field))
    previous = catalog.field(str(metric.prev_expression_field))
    if not current.value_domain:
        raise TransitionContractError(
            DOMAIN_MISMATCH,
            f"transition metric {metric.id!r} reads {current.id!r}, which declares no value "
            "domain; a transition compares declared values, not free strings",
            symbol=current.id,
        )
    if current.value_domain != previous.value_domain:
        raise TransitionContractError(
            DOMAIN_MISMATCH,
            f"transition metric {metric.id!r} compares {current.value_domain!r} with "
            f"{previous.value_domain!r}; both sides must read the same value domain",
            symbol=metric.id,
        )
    return str(current.value_domain)


def transition_metric_for_domain(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    value_domain: str,
) -> resolved_semantic_catalog.MetricSpec:
    """값 도메인 → 그 도메인의 전이 지표. 축 이름 문자열로 고르지 않는다.

    후보가 둘 이상이면 고르지 않는다 — 어느 쪽을 골라도 절반은 다른 축의 SQL 이 되고, 그 오답은
    실행 결과로만 드러난다.
    """

    candidates = []
    for metric_id in transition_metric_ids(catalog):
        metric = catalog.metric(metric_id)
        try:
            domain = transition_value_domain(catalog, metric)
        except (TransitionContractError, resolved_semantic_catalog.CatalogError):
            # 선언이 깨진 지표는 후보가 아니다. 그 선언의 오류는 그 지표를 직접 요청할 때
            # 이름과 함께 드러난다(여기서 삼키면 다른 축의 요청까지 함께 죽는다).
            continue
        if domain == value_domain:
            candidates.append(metric)
    if not candidates:
        raise TransitionContractError(
            METRIC_NOT_DECLARED,
            f"no transition metric declares value domain {value_domain!r}",
            symbol=value_domain,
        )
    if len(candidates) > 1:
        raise TransitionContractError(
            METRIC_AMBIGUOUS,
            f"value domain {value_domain!r} is claimed by more than one transition metric "
            f"{[metric.id for metric in candidates]}",
            symbol=value_domain,
        )
    return candidates[0]


def validate_transition_contract(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric_id: str,
    *,
    dialect: sql_dialect.SqlDialect | None = None,
) -> resolved_semantic_catalog.MetricSpec:
    """요청이 전이 계약을 만족하는지 검증하고 지표 선언을 돌려준다.

    검증을 lowering 과 분리해 두는 이유: 계약 위반은 **SQL 을 만들다 터지는 예외**가 아니라
    선언을 짚어 답할 수 있는 결과여야 한다. 호출자는 이 함수의 오류 코드를 그대로 issue 로
    바꿀 수 있다.
    """

    try:
        metric = catalog.metric(metric_id)
    except resolved_semantic_catalog.CatalogError as exc:
        raise TransitionContractError(
            str(exc.code), str(exc), symbol=exc.symbol or metric_id
        ) from exc

    if metric.kind != TRANSITION_KIND:
        raise TransitionContractError(
            CONTRACT_MISMATCH,
            f"metric {metric.id!r} is declared as {metric.kind!r}, not {TRANSITION_KIND!r}; "
            "a single-value metric cannot express a change between two observations",
            symbol=metric.id,
        )
    if not (metric.expression_field and metric.prev_expression_field):
        raise TransitionContractError(
            INVALID_DECLARATION,
            f"transition metric {metric.id!r} needs expression_field and prev_expression_field",
            symbol=metric.id,
        )
    if metric.strategy is None:
        raise TransitionContractError(
            STRATEGY_MISSING,
            f"transition metric {metric.id!r} declares no strategy; declared strategies are "
            f"{sorted(DECLARED_STRATEGIES)}",
            symbol=metric.id,
        )
    if metric.strategy not in DECLARED_STRATEGIES:
        raise TransitionContractError(
            INVALID_DECLARATION,
            f"transition metric {metric.id!r} declares unknown strategy {metric.strategy!r}; "
            f"declared strategies are {sorted(DECLARED_STRATEGIES)}",
            symbol=metric.id,
        )
    if metric.strategy not in SUPPORTED_STRATEGIES:
        raise TransitionContractError(
            STRATEGY_UNSUPPORTED,
            f"transition strategy {metric.strategy!r} of metric {metric.id!r} is declared but "
            f"not lowered yet; supported strategies are {sorted(SUPPORTED_STRATEGIES)}",
            symbol=metric.id,
        )
    if EQUALITY not in metric.allowed_operators:
        raise TransitionContractError(
            CONTRACT_MISMATCH,
            f"transition metric {metric.id!r} does not allow {EQUALITY!r}; a transition is "
            f"expressed as two equalities, declared operators are {sorted(metric.allowed_operators)}",
            symbol=metric.id,
        )

    missing = sorted(set(metric.required_capabilities) - supported_capabilities(dialect))
    if missing:
        raise TransitionContractError(
            CAPABILITY_UNSUPPORTED,
            f"transition metric {metric.id!r} requires unavailable capabilities {missing}",
            symbol=metric.id,
        )

    # 소스·필드 유효성은 카탈로그 로딩이 이미 교차검증했지만 여기서 다시 조회한다 — 이 함수
    # 하나가 "이 요청으로 IR 을 만들어도 되는가"의 완전한 답이어야 하기 때문이다.
    catalog.source(metric.source)
    transition_value_domain(catalog, metric)
    return metric


def declared_values(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric: resolved_semantic_catalog.MetricSpec,
) -> dict[str, Any]:
    """이 전이 축의 canonical → 물리 값 표(선언 사본이 아니라 조회)."""

    return dict(catalog.field(str(metric.expression_field)).compiler_field.value_map)


def declared_order(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric: resolved_semantic_catalog.MetricSpec,
) -> tuple[str, ...]:
    """이 전이 축의 서열(낮은 등급부터). 서열 없는 도메인이면 빈 튜플."""

    current = catalog.field(str(metric.expression_field)).compiler_field.value_order
    previous = catalog.field(str(metric.prev_expression_field)).compiler_field.value_order
    if current != previous:
        raise TransitionContractError(
            DOMAIN_MISMATCH,
            f"transition metric {metric.id!r} reads two columns whose declared value orders "
            "differ; the same domain must give the same ranking on both sides",
            symbol=metric.id,
        )
    return tuple(current)


def describe_transition(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric_id: str,
    *,
    from_value: str,
    to_value: str,
) -> dict[str, str]:
    """설명 문구용 두 대조 절(선언에서 렌더 — 필드 이름을 문자열로 적지 않는다)."""

    metric = catalog.metric(metric_id)
    return {
        "current": f"{metric.expression_field} = {to_value}",
        "previous": f"{metric.prev_expression_field} = {from_value}",
    }


def validate_transition_request(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric_id: str,
    *,
    from_value: str,
    to_value: str,
    direction: str | None = None,
    dialect: sql_dialect.SqlDialect | None = None,
) -> tuple[resolved_semantic_catalog.MetricSpec, str, tuple[str, ...]]:
    """전이 요청(지표 + 값 쌍 + 방향)의 계약 검증. 낮추지 않는다.

    낮춤과 분리해 두는 이유는 소비자가 둘이기 때문이다 — 이 저장소의 전이 실행 IR 을
    :mod:`transition_metrics` 가 만들 때도, :mod:`temporal_ir` 의 전이 술어로 옮길 때도
    **같은** 계약이 적용돼야 한다. 검증을 복사하면 한쪽만 방향 모순을 잡는 상태가 생긴다.
    """

    metric = validate_transition_contract(catalog, metric_id, dialect=dialect)
    domain = transition_value_domain(catalog, metric)
    values = declared_values(catalog, metric)

    for label, value in (("from_value", from_value), ("to_value", to_value)):
        if not isinstance(value, str) or value not in values:
            raise TransitionContractError(
                VALUE_UNKNOWN,
                f"{label}={value!r} is not a declared value of domain {domain!r}; declared "
                f"values are {sorted(values)}",
                symbol=domain,
            )
    if from_value == to_value:
        raise TransitionContractError(
            VALUES_IDENTICAL,
            f"transition metric {metric.id!r} was given the same value {from_value!r} on both "
            "sides; a change needs two different values",
            symbol=metric.id,
        )

    order = _verified_direction_order(
        catalog, metric, domain, direction, from_value, to_value
    )
    return metric, domain, order


def lower_transition_metric(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric_id: str,
    *,
    from_value: str,
    to_value: str,
    direction: str | None = None,
    evidence: event_ir.Evidence | None = None,
    dialect: sql_dialect.SqlDialect | None = None,
) -> LoweredTransition:
    """검증된 전이 요청을 canonical Event IR 조건으로 낮춘다.

    ``from_value``/``to_value`` 는 **canonical 값**이다. 표면어→canonical 은 원문을 읽는 계층
    (:mod:`transition_claims`)의 일이고, canonical→물리는 컴파일러의 일이다.
    """

    metric, domain, order = validate_transition_request(
        catalog,
        metric_id,
        from_value=from_value,
        to_value=to_value,
        direction=direction,
        dialect=dialect,
    )
    values = declared_values(catalog, metric)

    current_comparison = event_ir.Comparison(
        operator=EQUALITY,
        left=event_ir.FieldRef(name=str(metric.expression_field)),
        right=event_ir.Literal(value=to_value),
        evidence=evidence,
    )
    previous_comparison = event_ir.Comparison(
        operator=EQUALITY,
        left=event_ir.FieldRef(name=str(metric.prev_expression_field)),
        right=event_ir.Literal(value=from_value),
        evidence=evidence,
    )
    expression: event_ir.Condition = event_ir.Exists(
        relation=event_ir.Filter(
            relation=event_ir.Source(name=metric.source),
            where=event_ir.And(operands=(current_comparison, previous_comparison)),
        ),
        evidence=evidence,
    )
    receipt = {
        "metric_id": metric.id,
        "kind": metric.kind,
        "strategy": metric.strategy,
        "source": metric.source,
        "expression_field": metric.expression_field,
        "prev_expression_field": metric.prev_expression_field,
        "value_domain": domain,
        "from_value": from_value,
        "to_value": to_value,
        # 물리 값은 컴파일러가 붙이지만 영수증에는 남긴다 — "무엇이 SQL 에 들어갈 것인가"를
        # 실행 없이 설명할 수 있어야 하고, 그것이 이 영수증의 용도다.
        "from_physical_value": values[from_value],
        "to_physical_value": values[to_value],
        "direction": direction,
        "value_order": list(order),
        "required_capabilities": list(metric.required_capabilities),
        "conditions": describe_transition(
            catalog, metric.id, from_value=from_value, to_value=to_value
        ),
    }
    return LoweredTransition(expression=expression, receipt=receipt)


def _verified_direction_order(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    metric: resolved_semantic_catalog.MetricSpec,
    domain: str,
    direction: str | None,
    from_value: str,
    to_value: str,
) -> tuple[str, ...]:
    """방향어가 값 순서와 모순되지 않는지 확인하고 서열을 돌려준다.

    방향어는 SQL 조건을 **더하지 않는다**. '승급'은 이미 두 값이 말한 것을 다시 말한 것이고,
    그것이 값 순서와 어긋나면 문장 자체가 모순이다 — 그때 한쪽을 골라 SQL 을 내면 사용자가
    말하지 않은 집합이 나간다.
    """

    order = declared_order(catalog, metric)
    if direction is None:
        return order
    if direction not in DIRECTIONS:
        raise TransitionContractError(
            CONTRACT_MISMATCH,
            f"unknown transition direction {direction!r}; declared directions are "
            f"{sorted(DIRECTIONS)}",
            symbol=metric.id,
        )
    if not order or from_value not in order or to_value not in order:
        raise TransitionContractError(
            DIRECTION_UNVERIFIABLE,
            f"value domain {domain!r} declares no ranking that covers "
            f"{from_value!r} and {to_value!r}; a directional cue cannot be checked against "
            "stored codes without one",
            symbol=domain,
        )
    ascends = order.index(to_value) > order.index(from_value)
    if (direction == ASCENDING) != ascends:
        raise TransitionContractError(
            DIRECTION_CONTRADICTED,
            f"the request reads {direction!r} but {from_value!r} -> {to_value!r} moves the "
            f"other way in the declared ranking {list(order)}",
            symbol=domain,
        )
    return order


def transition_metric_declarations(
    catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
) -> Mapping[str, str]:
    """(전이 지표 id → 값 도메인). 진단·테스트가 읽는 파생 표 — 손 목록을 두지 않는다."""

    declarations: dict[str, str] = {}
    for metric_id in transition_metric_ids(catalog):
        try:
            declarations[metric_id] = transition_value_domain(
                catalog, catalog.metric(metric_id)
            )
        except (TransitionContractError, resolved_semantic_catalog.CatalogError):
            continue
    return declarations


__all__ = [
    "ASCENDING",
    "CAPABILITY_UNSUPPORTED",
    "CONTRACT_MISMATCH",
    "DECLARED_STRATEGIES",
    "DESCENDING",
    "DIRECTIONS",
    "DIRECTION_CONTRADICTED",
    "DIRECTION_UNVERIFIABLE",
    "DOMAIN_MISMATCH",
    "EQUALITY",
    "INVALID_DECLARATION",
    "METRIC_AMBIGUOUS",
    "METRIC_NOT_DECLARED",
    "STRATEGY_MISSING",
    "STRATEGY_UNSUPPORTED",
    "SUPPORTED_CAPABILITIES",
    "SUPPORTED_STRATEGIES",
    "TRANSITION_KIND",
    "VALUES_IDENTICAL",
    "VALUE_UNKNOWN",
    "LoweredTransition",
    "TransitionContractError",
    "declared_order",
    "declared_values",
    "describe_transition",
    "lower_transition_metric",
    "supported_capabilities",
    "transition_metric_declarations",
    "transition_metric_for_domain",
    "transition_metric_ids",
    "validate_transition_request",
    "transition_value_domain",
    "validate_transition_contract",
]
