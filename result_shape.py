"""요청된 **결과 형태**(result shape) — canonical IR 의 필수 계약이자 투영 단계의 입력.

배경(실측 2026-08-06)
---------------------
``어제 하루 동안 구매한 회원 수를 알려줘`` 가 ``intent_sql_contract_failed`` 로 닫혔다.
오디언스 필터는 정확히 컴파일됐다 — 막힌 것은 그 다음이다. 사용자가 요청한 것은 **회원 수
하나**인데 컴파일러는 회원 **목록**을 냈고, 계약 검증기가 그 형태 불일치를 잡았다.

검증기는 옳았다. 없던 것은 **형태를 만드는 단계**다. 결과 형태를 SQL 생성 뒤의 선택지로
두면 이 자리는 영원히 "검증은 하는데 만들지는 않는" 상태로 남는다. 그래서 컴파일 순서를
이렇게 고정한다::

    Audience Filter IR → Relational Plan → Projection/Aggregation Plan → SQL

이 모듈이 세 번째 화살표를 소유한다. 회원 목록 SQL 을 성공 처리하고 끝내지 않는다.

**추측하지 않는다.** 형태를 확정할 수 없으면 :data:`KIND_ENTITY_LIST` 가 아니라 ``None`` 을
돌려주고, 호출자가 그것을 결핍으로 회계한다.

실행: python -m pytest tests/test_result_shape.py -q
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

# 결과 형태(닫힌 집합).
KIND_ENTITY_LIST = "entity_list"
KIND_SCALAR = "scalar"
KIND_GROUPED_TABLE = "grouped_table"
KIND_RANKED_TABLE = "ranked_table"
KIND_TIME_SERIES = "time_series"
KIND_BOOLEAN = "boolean"

KINDS: frozenset[str] = frozenset({
    KIND_ENTITY_LIST,
    KIND_SCALAR,
    KIND_GROUPED_TABLE,
    KIND_RANKED_TABLE,
    KIND_TIME_SERIES,
    KIND_BOOLEAN,
})

RESULT_SHAPE_KEY = "result_shape"

# 분석 의도 계층(:mod:`analytical_intent`)의 형태 어휘 ↔ 이 모듈의 어휘. 두 어휘를 합치지
# 않는 이유는 축이 다르기 때문이다 — 저쪽은 '등록된 지표 질의의 산출 모양'이고 이쪽은
# '요청 하나의 최종 결과 모양'이다. 사상은 표 한 줄이고, 표에 없으면 사상하지 않는다.
_ANALYTICAL_SHAPE_KINDS: dict[str, str] = {
    "scalar": KIND_SCALAR,
    "grouped_rows": KIND_GROUPED_TABLE,
    "single_member": KIND_RANKED_TABLE,
    "member_rows": KIND_ENTITY_LIST,
}


class ResultShapeError(ValueError):
    """형태 선언 자체가 어긋났다."""


@dataclass(frozen=True, slots=True)
class ResultShape:
    """요청의 결과 형태. ``kind`` 는 필수, 나머지는 그 형태가 요구할 때만 채워진다."""

    kind: str
    metric: str | None = None
    entity: str | None = None
    distinct: bool = False
    grouping: tuple[str, ...] = ()
    ordering: tuple[str, ...] = ()
    limit: int | None = None
    source: str = "unknown"

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ResultShapeError(f"unknown result shape kind: {self.kind!r}")
        if self.kind == KIND_SCALAR and not self.metric:
            raise ResultShapeError("scalar result shape requires a metric")

    @property
    def is_scalar_entity_count(self) -> bool:
        """'회원 수 하나' — 오디언스 SQL 위에 결정론 투영을 걸 수 있는 유일한 형태."""
        return (
            self.kind == KIND_SCALAR
            and str(self.metric).casefold() == "count"
            and bool(self.entity)
            and not self.grouping
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "metric": self.metric,
            "entity": self.entity,
            "distinct": self.distinct,
            "grouping": list(self.grouping),
            "ordering": list(self.ordering),
            "limit": self.limit,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ResultShape | None":
        if not isinstance(raw, Mapping):
            return None
        kind = raw.get("kind")
        if not isinstance(kind, str) or kind not in KINDS:
            return None
        limit = raw.get("limit")
        return cls(
            kind=kind,
            metric=raw.get("metric") if isinstance(raw.get("metric"), str) else None,
            entity=raw.get("entity") if isinstance(raw.get("entity"), str) else None,
            distinct=bool(raw.get("distinct")),
            grouping=tuple(str(item) for item in (raw.get("grouping") or ()) if item),
            ordering=tuple(str(item) for item in (raw.get("ordering") or ()) if item),
            limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
            source=str(raw.get("source") or "unknown"),
        )


ENTITY_LIST_DEFAULT = ResultShape(
    kind=KIND_ENTITY_LIST, entity="member", source="audience_default"
)


def _analytical_shape(plan: Mapping[str, Any]) -> ResultShape | None:
    intent = plan.get("analytical_intent")
    if not isinstance(intent, Mapping):
        return None
    kind = _ANALYTICAL_SHAPE_KINDS.get(str(intent.get("result_shape") or ""))
    if kind is None:
        return None
    if kind == KIND_SCALAR:
        function = str(intent.get("aggregate_function") or "").casefold()
        if not function:
            return None
        return ResultShape(
            kind=KIND_SCALAR,
            metric=function,
            entity=str(intent.get("target_entity") or "member"),
            # COUNT 는 회원을 세는 것이고 조인이 행을 부풀릴 수 있다 — 중복 제거가 기본이다.
            distinct=function == "count",
            source="analytical_intent",
        )
    if kind == KIND_GROUPED_TABLE:
        return ResultShape(
            kind=KIND_GROUPED_TABLE,
            metric=str(intent.get("aggregate_function") or "").casefold() or None,
            grouping=tuple(str(item) for item in (intent.get("dimensions") or ()) if item),
            source="analytical_intent",
        )
    if kind == KIND_RANKED_TABLE:
        ranking = intent.get("comparison")
        limit = ranking.get("limit") if isinstance(ranking, Mapping) else None
        return ResultShape(
            kind=KIND_RANKED_TABLE,
            metric=str(intent.get("aggregate_function") or "").casefold() or None,
            entity=str(intent.get("target_entity") or "member"),
            limit=limit if isinstance(limit, int) and not isinstance(limit, bool) else None,
            source="analytical_intent",
        )
    return ResultShape(kind=kind, entity="member", source="analytical_intent")


def _output_contract_shape(plan: Mapping[str, Any]) -> ResultShape | None:
    """출력 계약이 이미 '회원 목록이 아니다'라고 선언했다면 그것도 형태 근거다.

    조건 판정 IR(``condition_evaluations``)이 스칼라 카운트를 선언하는 경로가 그렇다 —
    분석 지표 레지스트리를 타지 않으므로 ``analytical_intent`` 가 비어 있다.
    """
    contract = plan.get("output_contract")
    if not isinstance(contract, Mapping):
        return None
    if contract.get("expected_grain") != "analytical" or contract.get("requires_member_id") is True:
        return None
    return ResultShape(
        kind=KIND_SCALAR,
        metric="count",
        entity="member",
        distinct=True,
        source=f"output_contract:{contract.get('source') or 'unknown'}",
    )


def _query_shape(query: str) -> ResultShape | None:
    """원문만으로 형태를 읽는다(플랜이 아직 분석 의도를 싣기 전에도 답이 있어야 한다).

    형태는 **모든 응답**이 들고 있어야 하는 계약이다. 플랜 슬롯이 채워지는 시점에만 계산하면
    되묻기·미지원처럼 일찍 닫히는 경로에서 형태가 ``null`` 로 나가고, 그러면 "이 요청이 무엇을
    요구했나"를 응답만 보고 알 수 없다(실측: 회원수 질의가 되묻기로 닫히며 형태를 잃었다).
    """
    import analytical_intent

    intent = analytical_intent.analyze_analytical_intent(query)
    return _analytical_shape({"analytical_intent": intent}) if isinstance(intent, Mapping) else None


def resolve_result_shape(plan: Mapping[str, Any], *, query: str | None = None) -> ResultShape:
    """이 요청의 결과 형태. 근거가 없으면 오디언스 기본(회원 목록).

    근거의 우선순위는 **구체적인 것이 먼저**다: 분석 의도가 형태를 명시했으면 그것이,
    없으면 출력 계약이, 그다음이 원문 판정이고, 전부 없으면 오디언스 기본이다.
    """
    for shape in (_analytical_shape(plan), _output_contract_shape(plan)):
        if shape is not None:
            return shape
    if query:
        shape = _query_shape(query)
        if shape is not None:
            return shape
    return ENTITY_LIST_DEFAULT


def write_result_shape(plan: dict[str, Any], shape: ResultShape) -> ResultShape:
    plan[RESULT_SHAPE_KEY] = shape.to_dict()
    return shape


def read_result_shape(plan: Mapping[str, Any]) -> ResultShape | None:
    return ResultShape.from_dict(plan.get(RESULT_SHAPE_KEY))


# ── 투영 단계: Relational Plan → Projection/Aggregation Plan ──────────────────────
#
# 이 단계의 산출은 SQL 문자열이 아니라 **투영 계획**(SELECT 목록)이다. 완성된 SQL 을 나중에
# 감싸거나 재작성하면 두 가지가 깨진다: (a) 파생 테이블 별칭 때문에 집계 검증기가 물리 컬럼을
# 잃고(실측: ``COUNT(DISTINCT audience.MEMBER_NO)`` 가 MISSING_DISTINCT 로 떨어졌다),
# (b) 원문 조건 토큰을 SQL 문자열에서 찾는 하류 검사들이 재작성된 텍스트를 못 읽는다.
# 관계 계획을 만드는 컴파일러가 이 계획을 받아 그대로 렌더링하는 것이 순서상 옳다.

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass(frozen=True, slots=True)
class ProjectionPlan:
    """형태가 요구하는 SELECT 목록과 그 근거.

    ``applied=False`` 면 ``select_columns`` 는 비어 있고 호출자는 기존 목록을 그대로 쓴다.
    """

    applied: bool
    reason: str
    shape: ResultShape
    select_columns: tuple[str, ...] = ()
    artifacts: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "applied": self.applied,
            "reason": self.reason,
            "shape": self.shape.to_dict(),
            "select_columns": list(self.select_columns),
            "artifacts": [dict(item) for item in self.artifacts],
        }


def plan_scalar_entity_count(
    shape: ResultShape,
    *,
    entity_expression: str,
    output_alias: str = "MEMBER_COUNT",
) -> ProjectionPlan:
    """엔터티 스칼라 카운트의 SELECT 목록.

    ``entity_expression`` 은 관계 계획이 회원을 가리키는 **물리 식**이다(예: ``B.MEMBER_NO``).
    식별자 형태가 아니면 계획을 세우지 않는다 — 임의 문자열을 SELECT 에 넣는 자리를 만들지 않는다.
    """

    if not shape.is_scalar_entity_count:
        return ProjectionPlan(False, "shape_is_not_scalar_entity_count", shape)
    expression = (entity_expression or "").strip()
    if not _IDENTIFIER_RE.match(expression):
        return ProjectionPlan(False, f"entity_expression_not_an_identifier:{expression!r}", shape)
    distinct = "DISTINCT " if shape.distinct else ""
    measure = f"COUNT({distinct}{expression}) AS {output_alias}"
    artifact = {
        "kind": "aggregation",
        "sql_fragment": measure,
        "detail": {"function": "count", "distinct": shape.distinct, "entity": shape.entity},
    }
    return ProjectionPlan(
        True, "scalar_entity_count_planned", shape, (measure,), (artifact,)
    )


def plan_projection(
    shape: ResultShape | None,
    *,
    entity_expression: str,
    output_alias: str = "MEMBER_COUNT",
) -> ProjectionPlan:
    """형태가 요구하는 투영 계획(요구가 없으면 무동작).

    오늘 결정론으로 낮출 수 있는 형태는 '엔터티 스칼라 카운트' 하나다. 나머지 형태는
    각자의 컴파일러가 이미 소유하고 있으므로 여기서 다시 만들지 않는다 — 그 사실을
    ``reason`` 으로 남겨 "적용 안 함"과 "적용할 것이 없음"을 구분한다.
    """
    if shape is None:
        return ProjectionPlan(False, "no_result_shape", ENTITY_LIST_DEFAULT)
    if shape.kind == KIND_ENTITY_LIST:
        return ProjectionPlan(False, "entity_list_needs_no_projection", shape)
    if shape.is_scalar_entity_count:
        return plan_scalar_entity_count(
            shape, entity_expression=entity_expression, output_alias=output_alias
        )
    return ProjectionPlan(False, f"shape_owned_elsewhere:{shape.kind}", shape)


# 형태별로 SQL 에 남아야 하는 artifact 종류. '투영이 하나라도 있으면 통과'로 두면 회원 목록
# SQL 이 스칼라 요청을 만족한 것으로 잡힌다(실측: 그 규칙에서 #71 계열이 게이트를 통과했다).
_REQUIRED_ARTIFACT_KINDS: dict[str, frozenset[str]] = {
    KIND_SCALAR: frozenset({"aggregation"}),
    KIND_GROUPED_TABLE: frozenset({"grouping"}),
    KIND_RANKED_TABLE: frozenset({"ordering", "limit"}),
    KIND_TIME_SERIES: frozenset({"grouping"}),
    KIND_BOOLEAN: frozenset({"aggregation"}),
}


def shape_requirement_satisfied(shape: ResultShape | None, artifacts: Sequence[Mapping[str, Any]]) -> bool:
    """형태 요구가 그 형태**에 맞는** artifact 로 이어졌는가(원장 회계용)."""
    if shape is None or shape.kind == KIND_ENTITY_LIST:
        return True
    wanted = _REQUIRED_ARTIFACT_KINDS.get(shape.kind)
    if wanted is None:
        return True
    return any(
        str(item.get("kind")) in wanted for item in artifacts if isinstance(item, Mapping)
    )


# ── 요청된 결과 **주체** ────────────────────────────────────────────────────────
#
# 형태(shape)와 주체(subject)는 다른 축이다. ``회원 수를 알려줘`` 는 형태가 스칼라이고 주체는
# 회원이지만, ``구매 회원이 100명 이상인 브랜드를 알려줘`` 는 주체 자체가 회원이 아니다.
#
# 후자가 감사 #44 다. 실제 귀결은 ``failure``(``rules_fallback:required_candidate_not_constructed``)
# 였고 ``clarification_questions`` 는 **비어 있었다** — 사용자와 운영자 모두 다음 행동이 없었다.
# ``failure`` 는 귀결이 아니라 배선 결함의 이름이므로, 이 요청은 정직한 미지원이어야 한다.

# 결과 주체를 읽는 창의 크기(글자). 지시어 바로 앞의 명사구만 본다 — 넓히면 조건절의 명사를
# 결과 주체로 오인한다('브랜드를 2회 이상 구매한 회원을 찾아줘').
_SUBJECT_WINDOW = 14


def requested_non_subject_entity(query: str) -> tuple[str, int, int] | None:
    """결과로 요청된 것이 **선언된 주체가 아닌** 엔터티면 (표면어, 시작, 끝). 아니면 ``None``.

    규칙은 한국어 어순 하나다: 요청 지시어(``알려줘``·``추출해줘``) 바로 앞의 명사구가 결과
    주체다. 그 자리에 카탈로그 엔터티 도메인 낱말이 있고 주체 낱말(회원·고객)이나 계수 명사
    (수·인원)가 **없으면**, 사용자는 회원이 아닌 것을 행으로 달라고 한 것이다.

    어휘의 소유자는 :mod:`lexicon_patterns` 다 — 여기에 낱말을 적지 않는다.
    """
    import lexicon_patterns  # 지연 import(순환 방지)

    if not isinstance(query, str) or not query.strip():
        return None
    directives = sorted(
        (match.start(), match.end())
        for term in lexicon_patterns.vocabulary("request_directive")
        if term
        for match in re.finditer(re.escape(term), query)
    )
    if not directives:
        return None
    # 가장 뒤의 지시어가 요청의 머리다(앞쪽 지시어는 인용·수식일 수 있다).
    directive_start = directives[-1][0]
    window_start = max(0, directive_start - _SUBJECT_WINDOW)
    window = query[window_start:directive_start]
    if any(
        term and term in window
        for name in ("member_noun", "member_noun_honorific", "member_noun_informal",
                     "count_result_noun")
        for term in lexicon_patterns.vocabulary(name)
    ):
        return None
    hits = sorted(
        (window_start + match.start(), window_start + match.end(), term)
        for term in lexicon_patterns.vocabulary("source_entity_domain")
        if term
        for match in re.finditer(re.escape(term), window)
    )
    if not hits:
        return None
    # 지시어에 가장 가까운 엔터티가 결과 주체다.
    start, end, term = hits[-1]
    return term, start, end


__all__ = [
    "ENTITY_LIST_DEFAULT",
    "KINDS",
    "KIND_BOOLEAN",
    "KIND_ENTITY_LIST",
    "KIND_GROUPED_TABLE",
    "KIND_RANKED_TABLE",
    "KIND_SCALAR",
    "KIND_TIME_SERIES",
    "RESULT_SHAPE_KEY",
    "ProjectionPlan",
    "ResultShape",
    "ResultShapeError",
    "plan_projection",
    "plan_scalar_entity_count",
    "read_result_shape",
    "resolve_result_shape",
    "shape_requirement_satisfied",
    "write_result_shape",
]
