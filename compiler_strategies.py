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

import sql_dialect

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


def compile_direct_column_filter(
    *, base: str, qualifier: str, alias: str, column: str, values: list[str], operator: str = "IN",
) -> CompiledFilter | None:
    """상품 조인 없이 base 테이블 컬럼을 직접 거르는 qualifier(예: 장바구니 유형 CART_TYPE_CD)."""
    vals = [str(v) for v in (values or []) if str(v)]
    if not vals:
        return None
    if operator.upper() == "IN":
        expr = f"{alias}.{column} IN ({', '.join(_quote(v) for v in vals)})"
    else:
        expr = f"{alias}.{column} {operator} {_quote(vals[0])}"
    return CompiledFilter(
        base=base, qualifier=qualifier, values=vals,
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
