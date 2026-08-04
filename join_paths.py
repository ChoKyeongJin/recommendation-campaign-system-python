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

이 모듈은 graph_rag 를 import 하지 않는다. 물리 테이블·컬럼은 코드에 복제하지 않고
``member_target_filters.json``의 검증된 선언에서 :func:`load_join_paths`가 주입한다. 설계상
JOIN_PATHS[name] 이 조인 라인의 단일 소스지만, 현재 실제 소비자는 capability_registry(대상 테이블 조회)와
compiler_strategies(경로 존재 확인)뿐이고 graph_rag 빌더는 이 모듈을 import 하지 않는다 —
render_join_line() 은 호출부 0건이다(TODO). 선언된 경로가 카탈로그에 실재하는지는 DB swap preflight와
physical-binding ratchet이 지킨다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import member_filters_config


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


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise JoinPathError(f"{path} must be an object")
    return value


def _text(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise JoinPathError(f"{path} must be a non-empty string")
    return value.strip()


def _qualified(value: Any, path: str) -> tuple[str, str]:
    expression = _text(value, path)
    parts = expression.split(".")
    if len(parts) != 2 or not all(part.strip() and part.strip().isidentifier() for part in parts):
        raise JoinPathError(f"{path} must be a qualified alias.column identifier")
    return parts[0].strip(), parts[1].strip()


def _equality(value: Any, path: str) -> tuple[tuple[str, str], tuple[str, str]]:
    expression = _text(value, path)
    parts = expression.split("=")
    if len(parts) != 2:
        raise JoinPathError(f"{path} must contain one equality")
    return _qualified(parts[0].strip(), path), _qualified(parts[1].strip(), path)


def _oriented_condition(
    left: tuple[str, str],
    right: tuple[str, str],
    *,
    source_alias: str,
    target_alias: str,
    path: str,
    evidence: str,
) -> JoinCondition:
    by_alias = {left[0]: left, right[0]: right}
    if set(by_alias) != {source_alias, target_alias}:
        raise JoinPathError(
            f"{path} must join aliases {source_alias!r} and {target_alias!r}"
        )
    source = by_alias[source_alias]
    target = by_alias[target_alias]
    return JoinCondition(
        left=f"{source[0]}.{source[1]}",
        right=f"{target[0]}.{target[1]}",
        relation_type="logical_reference",
        evidence=evidence,
    )


def build_join_paths(config: Mapping[str, Any]) -> dict[str, JoinPath]:
    """Build physical joins from the validated member-target registry.

    The path names and relation semantics remain code-owned. Physical tables,
    aliases, and columns must all be supplied by the registry; no legacy value
    is substituted when a binding is missing.
    """

    cart = _mapping(config.get("cart_targets"), "cart_targets")
    cart_product = _mapping(
        cart.get("product_join"), "cart_targets.product_join"
    )
    cart_source_alias, cart_source_column = _qualified(
        cart_product.get("left"), "cart_targets.product_join.left"
    )
    cart_target_alias, cart_target_column = _qualified(
        cart_product.get("right"), "cart_targets.product_join.right"
    )
    declared_cart_target_alias = _text(
        cart_product.get("alias"), "cart_targets.product_join.alias"
    )
    declared_cart_source_alias = _text(cart.get("alias"), "cart_targets.alias")
    if cart_source_alias != declared_cart_source_alias:
        raise JoinPathError(
            "cart_targets.product_join.left alias does not match cart_targets.alias"
        )
    if cart_target_alias != declared_cart_target_alias:
        raise JoinPathError(
            "cart_targets.product_join.right alias does not match product_join.alias"
        )

    purchase = _mapping(
        config.get("purchase_product_target"), "purchase_product_target"
    )
    order_detail = _mapping(
        purchase.get("order_detail"), "purchase_product_target.order_detail"
    )
    product = _mapping(
        purchase.get("product"), "purchase_product_target.product"
    )
    order_detail_alias = _text(
        order_detail.get("alias"), "purchase_product_target.order_detail.alias"
    )
    product_alias = _text(
        product.get("alias"), "purchase_product_target.product.alias"
    )
    purchase_left, purchase_right = _equality(
        product.get("join"), "purchase_product_target.product.join"
    )

    return {
        "cart_to_product": JoinPath(
            name="cart_to_product",
            source_table=_text(cart.get("table"), "cart_targets.table"),
            source_alias=cart_source_alias,
            target_table=_text(
                cart_product.get("table"), "cart_targets.product_join.table"
            ),
            target_alias=cart_target_alias,
            conditions=(
                JoinCondition(
                    left=f"{cart_source_alias}.{cart_source_column}",
                    right=f"{cart_target_alias}.{cart_target_column}",
                    relation_type="logical_reference",
                    evidence=(
                        "member_target_filters.cart_targets.product_join + "
                        "schema_catalog verified foreign key"
                    ),
                ),
            ),
        ),
        "purchase_to_product": JoinPath(
            name="purchase_to_product",
            source_table=_text(
                order_detail.get("table"),
                "purchase_product_target.order_detail.table",
            ),
            source_alias=order_detail_alias,
            target_table=_text(
                product.get("table"), "purchase_product_target.product.table"
            ),
            target_alias=product_alias,
            conditions=(
                _oriented_condition(
                    purchase_left,
                    purchase_right,
                    source_alias=order_detail_alias,
                    target_alias=product_alias,
                    path="purchase_product_target.product.join",
                    evidence=(
                        "member_target_filters.purchase_product_target.product.join + "
                        "schema_catalog column reference"
                    ),
                ),
            ),
        ),
    }


def load_join_paths(path: Path | None = None) -> dict[str, JoinPath]:
    """Load the strict registry and derive join paths from it."""

    return build_join_paths(member_filters_config.load_config(path))


# ── 등록된 조인 경로 ─────────────────────────────────────────────────────────────────────────
# 물리 좌표는 member_target_filters.json 에서 주입되고 schema catalog/preflight가 실재성을 검증한다.
JOIN_PATHS: dict[str, JoinPath] = load_join_paths()


def get_join_path(name: str) -> JoinPath | None:
    return JOIN_PATHS.get(name)
