"""compiler strategy 레지스트리 — capability 에 선언된 `compiler_strategy` 문자열을 **실제 실행 로직**에
연결한다. 지금까지 compiler_strategy 는 requirement_capabilities.json 안의 죽은 라벨이었고, 실제 컴파일은
graph_rag 빌더에 하드코딩돼 있었다. 이 모듈은 그 라벨을 dispatch 가능한 함수로 만든다.

핵심 원칙(요청 13번)
  * compiler_strategy 는 문서용 문자열이 아니라 실제 실행 로직과 연결된다.
  * supported=true 는 실제 compiler 가 등록돼 있을 때만 정적 검증(capability_registry)을 통과한다.
  * 사용자 입력을 SQL 에 직접 보간하지 않는다 → _quote 로 이스케이프, LIKE 는 N'%…%' 로 안전 비교.
  * 결과는 SQL 문자열뿐 아니라 **구조화된 evidence(CompiledFilter)** 로 반환한다 → 검증기가 문자열
    검색이 아니라 evidence 로 반영 여부를 확인한다(코드 치환·canonical 보정에도 안 깨짐).

이 모듈은 graph_rag 를 import 하지 않는다(순수). SQL 리터럴 헬퍼(_quote/_nlike)는 graph_rag._sql_quote/
_sql_nlike_contains 와 **바이트 동일** 출력을 내도록 맞췄다
(parity 를 강제하던 레지스트리 계약 테스트는 규칙 계층 철거와 함께 삭제됐다 — 현재 가드 없음).
"""

from __future__ import annotations

import re

import sql_dialect

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Callable

from join_paths import JOIN_PATHS


# ── SQL 리터럴 헬퍼(graph_rag 미러) ────────────────────────────────────────────────────────
def _quote(value: object) -> str:
    """SQL 문자열 리터럴. 구현은 sql_dialect 가 단일 소유한다.

    예전에는 graph_rag 의 같은 함수와 "동일하게 맞췄다"는 주석만 있었고 실제로는 갈라져 있었다.
    """
    return sql_dialect.quote_literal(value)


def _nlike_contains(column: str, term: object) -> str:
    """유니코드 부분일치 LIKE(N'%term%'). 구현은 sql_dialect 가 단일 소유한다."""
    return sql_dialect.nlike_contains(column, term)


# ── 컴파일 결과(구조화 evidence) ────────────────────────────────────────────────────────────
@dataclass
class CompiledFilter:
    """qualifier 하나를 컴파일한 결과. filter_expression 은 WHERE 에 넣을 SQL 조각, 나머지는 검증용 evidence."""
    base: str
    qualifier: str
    values: list[str]
    filter_expression: str
    filter_field: str
    strategy: str
    join_path: str | None = None
    match_mode: str = "contains"  # contains(LIKE) | exact(=/IN)
    resolved_via: str = "name"    # name(BRAND_NAME LIKE) | code(BRAND_ID IN) — 하이브리드 분기 기록
    extra: dict[str, Any] = field(default_factory=dict)

    def to_evidence(self) -> dict[str, Any]:
        """requirement 검증기(semantic_requirements)가 소비하는 applied_requirement evidence."""
        return {
            "base": self.base,
            "qualifier": self.qualifier,
            "values": list(self.values),
            "strategy": self.strategy,
            "join_path": self.join_path,
            "filter_field": self.filter_field,
            "filter_expression": self.filter_expression,
            "resolved_via": self.resolved_via,
        }


# ── 공통 product dimension compiler(브랜드/상품/카테고리) ────────────────────────────────────
def compile_product_dimension_filter(
    *,
    base: str,
    qualifier: str,
    product_alias: str,
    name_field: str,
    name_values: list[str] | None = None,
    code_field: str | None = None,
    code_values: list[str] | None = None,
    join_path: str | None = None,
    supports_multiple: bool = True,
) -> CompiledFilter | None:
    """상품 마스터(CRM_CM_PRODUCT)를 통해 거르는 qualifier(브랜드/상품명/카테고리)를 base 공통으로 컴파일한다.

    하이브리드(요청 3·14, 사용자 결정): dimension_catalog 로 이미 코드가 해석됐으면 `<alias>.<code_field>
    IN ('A',…)`(정밀·기존 장바구니 경로 보존), 아니면 `(<alias>.<name_field> LIKE N'%값%')`(자기완결·이식성,
    구매 경로와 동일 표현). 둘 다 CompiledFilter(evidence 포함)로 반환한다.

    purchase 와 cart 가 같은 함수를 쓰므로 base 별 중복 구현이 사라진다. 값이 없으면 None."""
    codes = [str(c) for c in (code_values or []) if str(c)]
    names = [str(n) for n in (name_values or []) if str(n)]
    if not supports_multiple:
        codes, names = codes[:1], names[:1]

    if codes:
        in_list = ", ".join(_quote(code) for code in codes)
        expr = f"{product_alias}.{code_field} IN ({in_list})"
        return CompiledFilter(
            base=base, qualifier=qualifier, values=codes,
            filter_expression=expr, filter_field=code_field or "", strategy="join_product_dimension",
            join_path=join_path, match_mode="exact", resolved_via="code",
        )
    if names:
        likes = " OR ".join(_nlike_contains(f"{product_alias}.{name_field}", term) for term in names)
        expr = f"({likes})"
        return CompiledFilter(
            base=base, qualifier=qualifier, values=names,
            filter_expression=expr, filter_field=name_field, strategy="join_product_dimension",
            join_path=join_path, match_mode="contains", resolved_via="name",
        )
    return None


# ── 범용 direct-column 필터(필드 레지스트리 · 런타임 IR · 컴파일) ──────────────────────────
# '특정 논리 필드 → 단일 컬럼 비교'는 필드별 predicate builder 를 만들지 않는다. 필드는 아래
# DIRECT_COLUMN_FILTER_SPECS 선언 + 설정(member_target_filters.json)의 컬럼/값 소유로만 열리고,
# 상위 빌더는 compile_registered_direct_column_filters 하나를 호출한다
# (tests/test_direct_column_filters.py 의 구조 가드가 전용 빌더 재유입을 막는다).

_DIRECT_OPERATOR_SQL = {"eq": "=", "in": "IN", "not_in": "NOT IN"}
SUPPORTED_DIRECT_OPERATORS = frozenset(_DIRECT_OPERATOR_SQL)

_SQL_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class DirectColumnFilterError(ValueError):
    """direct-column 필터의 배선/설정 계약 위반(필드 미등록·컬럼 바인딩 누락·값 레지스트리 부재).

    이 예외는 코드/설정 버그에만 쓴다 — 필수 계약이 없으면 임의 컬럼명으로 폴백하지 않고 즉시
    실패한다. 사용자 입력 문제(미등록 값·허용 밖 연산자 등)는 예외가 아니라 unsupported path 로
    기록돼 dropped_conditions 고지로 이어진다(조용한 드롭도, 조용한 폴백도 금지)."""


@dataclass(frozen=True)
class DirectColumnFilterSpec:
    """direct-column 논리 필드 하나의 선언 — 어디서 값을 읽고 어떤 물리 컬럼으로 컴파일되는지.

    물리 컬럼 이름과 허용 값은 코드가 아니라 설정(member_target_filters.json)이 소유하므로 여기는
    설정 안의 **경로**만 선언한다(column_setting/value_registry). 저장값이 도메인 접두어를 포함하면
    ('CART_TYPE_CD.REGULARDELIVERY') 완성값은 값 레지스트리가 제공하고 코드는 접두어를 조립하지
    않는다. 새 필드(예: cart.channel)는 여기 한 항목 + 설정 키 추가로 열린다."""

    logical_field: str                    # 예: "cart.type"
    source_path: tuple[str, ...]          # query_plan 입력 경로. 예: ("target_user", "cart_type")
    column_setting: tuple[str, ...]       # 설정 안 물리 컬럼 바인딩 경로. 예: ("cart_targets", "cart_type_column")
    allowed_operators: frozenset[str] = frozenset({"eq"})
    value_registry: tuple[str, ...] | None = None  # 설정 안 허용 값 목록 경로(항목: {canonical, value, …})
    allow_multiple: bool = False          # in/not_in 에 값 2개 이상 허용 여부
    compile_strategy: str = "direct_column_filter"


DIRECT_COLUMN_FILTER_SPECS: dict[str, DirectColumnFilterSpec] = {
    "cart.type": DirectColumnFilterSpec(
        logical_field="cart.type",
        source_path=("target_user", "cart_type"),
        column_setting=("cart_targets", "cart_type_column"),
        allowed_operators=frozenset({"eq", "in", "not_in"}),
        value_registry=("cart_targets", "cart_types"),
        allow_multiple=True,
    ),
}


@dataclass(frozen=True)
class CompiledDirectColumnFilters:
    """범용 컴파일 결과. consumed_paths 는 dropped_conditions 계산이 '특정 필드명이라서'가 아니라
    '실제로 컴파일러가 소비한 경로라서' 제외하는 근거이고, unsupported_paths 는 값이 있었지만
    컴파일하지 못한 경로다(조용한 드롭 대신 고지)."""

    predicates: tuple[str, ...] = ()
    consumed_paths: frozenset[str] = frozenset()
    unsupported_paths: frozenset[str] = frozenset()


def _walk_path(container: Any, path: tuple[str, ...]) -> Any:
    node = container
    for key in path:
        if not isinstance(node, Mapping):
            return None
        node = node.get(key)
    return node


def _spec_for(field_name: str, specs: Mapping[str, DirectColumnFilterSpec]) -> DirectColumnFilterSpec:
    spec = specs.get(field_name)
    if spec is None:
        raise DirectColumnFilterError(
            f"미등록 direct-column 필드: {field_name!r} — DIRECT_COLUMN_FILTER_SPECS 에 선언해야 컴파일된다."
        )
    return spec


def _resolve_column(config: Mapping[str, Any], spec: DirectColumnFilterSpec) -> str:
    """설정이 소유한 물리 컬럼 바인딩('C.CART_TYPE_CD' — 접두어는 설정 문서 관례)에서 짧은 컬럼명."""
    configured = _walk_path(config, spec.column_setting)
    column = str(configured).split(".")[-1].strip() if isinstance(configured, str) else ""
    if not _SQL_IDENTIFIER_RE.match(column):
        raise DirectColumnFilterError(
            f"{spec.logical_field} 의 물리 컬럼 바인딩({'.'.join(spec.column_setting)})이 "
            f"설정에 없거나 식별자가 아니다: {configured!r}"
        )
    return column


def resolve_direct_filter_column(config: Mapping[str, Any], field_name: str) -> str:
    """등록 필드의 물리 컬럼명(설정 소유). 누락 시 즉시 실패 — 임의 컬럼명 폴백 금지."""
    return _resolve_column(config, _spec_for(field_name, DIRECT_COLUMN_FILTER_SPECS))


def _value_entries(
    config: Mapping[str, Any], spec: DirectColumnFilterSpec, *, missing_ok: bool
) -> tuple[dict[str, Any], ...]:
    if spec.value_registry is None:
        return ()
    entries = _walk_path(config, spec.value_registry)
    valid = tuple(
        entry
        for entry in (entries if isinstance(entries, list) else [])
        if isinstance(entry, dict)
        and isinstance(entry.get("canonical"), str) and entry["canonical"]
        and isinstance(entry.get("value"), str) and entry["value"]
    )
    if not valid and not missing_ok:
        raise DirectColumnFilterError(
            f"{spec.logical_field} 의 값 레지스트리({'.'.join(spec.value_registry)})가 설정에 없거나 비었다."
        )
    return valid


def direct_filter_value_entries(
    config: Mapping[str, Any], field_name: str, *, missing_ok: bool = False
) -> tuple[dict[str, Any], ...]:
    """등록 필드의 허용 값 레지스트리 항목({canonical, value, …})들.

    missing_ok=True 는 어휘 노출 경로 전용(레지스트리가 없으면 그 필드를 어휘에 안 올릴 뿐) —
    컴파일 경로는 기본값(False)으로 즉시 실패해야 한다."""
    return _value_entries(config, _spec_for(field_name, DIRECT_COLUMN_FILTER_SPECS), missing_ok=missing_ok)


def render_direct_column_predicate(
    *, column: str, operator: str, values: tuple[str, ...] | list[str], alias: str = ""
) -> str:
    """direct-column 술어 렌더의 단일 소유자(식별자/값 역할 분리, 값은 _quote 로만 진입).

    다중값은 정렬·중복 제거로 입력 순서와 무관하게 결정론적이다. alias 가 빈 문자열이면 점을 붙이지
    않는다."""
    if operator not in _DIRECT_OPERATOR_SQL:
        raise DirectColumnFilterError(f"지원하지 않는 direct-column 연산자: {operator!r}")
    if not isinstance(column, str) or not _SQL_IDENTIFIER_RE.match(column):
        raise DirectColumnFilterError(f"컬럼 식별자가 아니다: {column!r}")
    ordered = sorted({str(value) for value in values if str(value)})
    if not ordered:
        raise DirectColumnFilterError("빈 값 목록은 렌더할 수 없다(호출자가 unsupported 로 처리한다).")
    qualified = f"{alias}.{column}" if alias else column
    if operator == "eq":
        if len(ordered) != 1:
            raise DirectColumnFilterError(f"eq 는 단일 값만 허용한다: {ordered!r}")
        return f"{qualified} = {_quote(ordered[0])}"
    return f"{qualified} {_DIRECT_OPERATOR_SQL[operator]} ({', '.join(_quote(value) for value in ordered)})"


def _extract_operator_and_values(
    node: Any, spec: DirectColumnFilterSpec, allowed_values: frozenset[str] | None
) -> tuple[str, tuple[str, ...]] | None:
    """plan 노드에서 (operator, values) 를 범용 해석한다. 해석 불가면 None(호출자가 unsupported 기록).

    지원 입력: {"value": v}(eq 기본), {"operator": "eq", "value": v},
    {"operator": "in"|"not_in", "values": [...]}(단일 value 도 허용). 필드별 분기는 없다."""
    if not isinstance(node, Mapping):
        return None
    operator = node.get("operator") or "eq"
    if operator not in SUPPORTED_DIRECT_OPERATORS or operator not in spec.allowed_operators:
        return None
    if operator == "eq":
        raw_values: Any = [node.get("value")]
    else:
        raw_values = node.get("values")
        if raw_values is None and node.get("value") is not None:
            raw_values = [node.get("value")]
    if not isinstance(raw_values, list) or not raw_values:
        return None  # 빈 IN 목록/값 부재 — 컴파일 불가를 명시적으로 돌려보낸다.
    if not all(isinstance(value, str) and value for value in raw_values):
        return None  # 빈 문자열·비문자 값 거부(값 타입 검증).
    ordered = tuple(sorted(set(raw_values)))
    if len(ordered) > 1 and not spec.allow_multiple:
        return None  # 단일값 필드에 다중값 입력.
    if allowed_values is not None and not set(ordered) <= allowed_values:
        return None  # 값 레지스트리에 없는 값.
    return str(operator), ordered


def compile_registered_direct_column_filters(
    *,
    query_plan: Mapping[str, Any],
    field_names: frozenset[str] | set[str],
    alias: str,
    config: Mapping[str, Any],
    specs: Mapping[str, DirectColumnFilterSpec] | None = None,
) -> CompiledDirectColumnFilters:
    """등록된 direct-column 필드들을 query_plan 에서 읽어 WHERE 술어로 컴파일하는 단일 경로.

    field_names 는 이 빌더 스코프에 적용할 필드의 명시 목록이다(스코프 밖 필터가 의도치 않게 적용되는
    것을 막는다). 슬롯이 비어 있으면 건드리지 않고, 값이 있는데 컴파일할 수 없으면 unsupported_paths 로
    기록한다. 미등록 필드명·설정 누락은 DirectColumnFilterError(fail fast). 동일 입력은 항상 동일 SQL."""
    registry = DIRECT_COLUMN_FILTER_SPECS if specs is None else specs
    predicates: list[str] = []
    consumed: set[str] = set()
    unsupported: set[str] = set()
    for field_name in sorted(field_names):
        spec = _spec_for(field_name, registry)
        node = _walk_path(query_plan, spec.source_path)
        if node is None:
            continue
        path = ".".join(spec.source_path)
        column = _resolve_column(config, spec)
        allowed_entries = _value_entries(config, spec, missing_ok=False)
        allowed_values = frozenset(entry["value"] for entry in allowed_entries) if allowed_entries else None
        extracted = _extract_operator_and_values(node, spec, allowed_values)
        if extracted is None:
            unsupported.add(path)
            continue
        operator, values = extracted
        predicates.append(render_direct_column_predicate(column=column, operator=operator, values=values, alias=alias))
        consumed.add(path)
    return CompiledDirectColumnFilters(
        predicates=tuple(predicates),
        consumed_paths=frozenset(consumed),
        unsupported_paths=frozenset(unsupported),
    )


def compile_direct_column_filter(
    *, base: str, qualifier: str, alias: str, column: str, values: list[str], operator: str = "in",
) -> CompiledFilter | None:
    """상품 조인 없이 base 테이블 컬럼을 직접 거르는 qualifier 의 capability strategy 진입점.

    연산자 계약은 범용 렌더러와 동일하다(eq/in/not_in). 렌더는 render_direct_column_predicate 가
    단일 소유하므로 이 함수는 evidence(CompiledFilter) 포장만 담당한다."""
    ordered = sorted({str(value) for value in (values or []) if str(value)})
    if not ordered:
        return None
    effective = "in" if operator == "eq" and len(ordered) > 1 else operator
    expr = render_direct_column_predicate(column=column, operator=effective, values=ordered, alias=alias)
    return CompiledFilter(
        base=base, qualifier=qualifier, values=ordered,
        filter_expression=expr, filter_field=column, strategy="direct_column_filter",
        join_path=None, match_mode="exact", resolved_via="value",
    )


# ── dispatch 레지스트리 ─────────────────────────────────────────────────────────────────────
# capability.compiler_strategy → 실제 함수. capability_registry 정적 검증이 '선언된 strategy 가 여기
# 등록됐는지'를 확인한다. 등록 안 된 strategy 로 supported=true 를 선언하면 시작/CI 단계에서 실패한다.
COMPILER_STRATEGIES: dict[str, Callable[..., CompiledFilter | None]] = {
    "join_product_dimension": compile_product_dimension_filter,
    "direct_column_filter": compile_direct_column_filter,
}


def has_strategy(name: str | None) -> bool:
    return bool(name) and name in COMPILER_STRATEGIES


def join_path_exists(name: str | None) -> bool:
    """strategy 가 조인을 요구하지 않을 수도 있으므로 name 이 None 이면 True(조인 불필요)로 본다."""
    return name is None or name in JOIN_PATHS
