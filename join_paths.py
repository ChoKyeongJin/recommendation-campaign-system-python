"""스키마 기반 조인 경로 레지스트리 — qualifier(브랜드/상품/카테고리 등)를 base 테이블에서 상품 마스터
등으로 연결하는 조인 경로를 **명시 metadata** 로 관리한다(빌더 내부 하드코딩 반복 금지).

배경/목표
---------
지금까지 "CART→CRM_CM_PRODUCT" 같은 조인 경로가 각 SQL 빌더 안에 개별 하드코딩돼, 같은 상품 조인이
base 마다 중복 구현되고 컬럼 이름만 보고 잘못된 조인 키(예: CART_PRODUCT_NO 는 라인 PK 인데 상품 FK 로
오인)를 넣을 위험이 있었다. 이 모듈은 조인 경로를 **한 곳**에 선언하고, 각 경로가 어떤 근거(스키마 설명/
큐레이션 FK/실빌더 검증)로 성립하는지 evidence 로 남긴다.

중요 — CRMDW 는 DB 선언 FK 가 0개다(build_table_relationships.py 참고). 모든 관계는 schema_catalog 의
curated `foreign_keys`/`join_hints` 와 member_target_filters(cart_targets.product_join,
purchase_product_target) 에서 온다. 그래서 조인 조건의 relation_type 은 기본 `logical_reference` 이며,
evidence 에 출처를 적는다. 컬럼 이름 유사성만으로 조인을 추론하지 않는다.

이 모듈은 graph_rag 를 import 하지 않는다(순수 dict/dataclass). graph_rag 의 빌더가 JOIN_PATHS[name]
으로 조인 라인을 얻고, compiler_strategies 가 필터를 붙인다.
"""

from __future__ import annotations

from dataclasses import dataclass


# 상품 라인 PK(집계 전용) — 절대 상품 마스터 조인 키로 쓰면 안 되는 컬럼. 조인 경로 등록 시 가드로 검사한다.
# CART_PRODUCT_NO 는 (STORE_ID, CART_PRODUCT_NO) 복합 PK 의 라인 일련번호이지 CRM_CM_PRODUCT.PRODUCT_ID 가
# 아니다(스키마 human_note: "장바구니 상품 라인 번호 PK(bigint)", references=null).
FORBIDDEN_PRODUCT_JOIN_KEYS = frozenset({"CART_PRODUCT_NO", "ORDER_PRODUCT_NO", "UPPER_CART_PRODUCT_NO"})


class JoinPathError(ValueError):
    """조인 경로 정의가 스키마 근거·가드를 위반했을 때(등록 시 즉시 실패)."""


@dataclass(frozen=True)
class JoinCondition:
    """조인 술어 한 개(left = right). relation_type 은 이 관계의 근거 성격을 표시한다.

      * declared_fk       : DB 엔진이 선언한 외래키(현 CRMDW 엔 없음)
      * logical_reference : 스키마 설명/큐레이션 FK/실빌더가 쓰는 논리적 참조(현 시스템 대부분)
    """
    left: str   # 예: "A.PRODUCT_ID"
    right: str  # 예: "C.PRODUCT_ID"
    relation_type: str = "logical_reference"
    evidence: str = ""

    @property
    def left_column(self) -> str:
        return self.left.rsplit(".", 1)[-1]

    @property
    def right_column(self) -> str:
        return self.right.rsplit(".", 1)[-1]


@dataclass(frozen=True)
class JoinPath:
    name: str
    source_table: str
    source_alias: str
    target_table: str
    target_alias: str
    conditions: tuple[JoinCondition, ...]
    join_type: str = "INNER JOIN"
    cardinality: str = "many_to_one"  # source 라인 여러 개 → target(상품) 하나. 회원 단위 DISTINCT 로 증폭 흡수.

    def __post_init__(self) -> None:
        if not self.conditions:
            raise JoinPathError(f"[{self.name}] 조인 조건이 최소 1개 필요")
        for cond in self.conditions:
            # 컬럼 이름만 보고 상품 FK 로 오인하는 대표 사례를 원천 차단한다.
            if cond.left_column in FORBIDDEN_PRODUCT_JOIN_KEYS or cond.right_column in FORBIDDEN_PRODUCT_JOIN_KEYS:
                raise JoinPathError(
                    f"[{self.name}] 금지된 조인 키({cond.left}={cond.right}): "
                    f"라인 PK 는 상품 조인 키가 아니다(집계 COUNT DISTINCT 전용)."
                )

    def render_join_line(self, source_alias: str | None = None, target_alias: str | None = None) -> str:
        """'INNER JOIN <target> <alias> ON <src>.<col> = <alias>.<col> [AND ...]' 한 줄.

        호출자가 별칭 관례를 덮어쓸 수 있다(빌더마다 A/C, D/P 등 관례가 다름). 컬럼명은 경로 소유."""
        src = source_alias or self.source_alias
        tgt = target_alias or self.target_alias
        ons = " AND ".join(f"{src}.{c.left_column} = {tgt}.{c.right_column}" for c in self.conditions)
        return f"     {self.join_type} {self.target_table} {tgt} ON {ons}"


# ── 등록된 조인 경로 ─────────────────────────────────────────────────────────────────────────
# 근거는 schema_catalog.json(foreign_keys, confidence=verified) + member_target_filters.json 이다.
JOIN_PATHS: dict[str, JoinPath] = {
    # 장바구니 라인 → 상품 마스터. 근거: cart_targets.product_join(C.PRODUCT_ID=CP.PRODUCT_ID),
    # schema_catalog CART.foreign_keys(PRODUCT_ID→CRM_CM_PRODUCT.PRODUCT_ID, confidence=verified).
    "cart_to_product": JoinPath(
        name="cart_to_product",
        source_table="ODS_MALL_OMS_CART",
        source_alias="A",
        target_table="CRM_CM_PRODUCT",
        target_alias="C",
        conditions=(
            JoinCondition(
                left="A.PRODUCT_ID", right="C.PRODUCT_ID",
                relation_type="logical_reference",
                evidence="member_target_filters.cart_targets.product_join + schema_catalog CART.foreign_keys(verified)",
            ),
        ),
    ),
    # 주문상세 → 상품 마스터. 근거: purchase_product_target.product.join(P.PRODUCT_ID=OD.PRODUCT_ID),
    # schema_catalog CRM_SL_ORDERDETAILMALL.PRODUCT_ID 의 column-level references(CRM_CM_PRODUCT.PRODUCT_ID).
    "purchase_to_product": JoinPath(
        name="purchase_to_product",
        source_table="CRM_SL_ORDERDETAILMALL",
        source_alias="D",
        target_table="CRM_CM_PRODUCT",
        target_alias="P",
        conditions=(
            JoinCondition(
                left="D.PRODUCT_ID", right="P.PRODUCT_ID",
                relation_type="logical_reference",
                evidence="member_target_filters.purchase_product_target.product.join + schema_catalog ORDERDETAILMALL.PRODUCT_ID.references",
            ),
        ),
    ),
}


def get_join_path(name: str) -> JoinPath | None:
    return JOIN_PATHS.get(name)
