"""capability ↔ 실행자산 정합성 contract 테스트(요청 1·2·7·8·9·13).

이 계층의 목적은 "capability 만 true 로 바꾸면 SQL 에 반영 안 되는 사일런트 실패"를 구조적으로 차단하는
것이다. 그래서 다음을 정적으로 강제한다:
  * supported=true 인 모든 base×qualifier 는 compiler_strategy(등록됨)+join_path(등록됨)+filter_field
    (스키마 존재)를 갖는다 → capability_registry.validate_capabilities() == [].
  * compiler_strategy 라벨은 죽은 문자열이 아니라 실제 dispatch 가능 함수다.
  * 조인 경로는 스키마 근거로만 등록되며, 라인 PK(CART_PRODUCT_NO)를 상품 조인 키로 쓰지 않는다.
  * compiler_strategies 의 SQL 리터럴 헬퍼는 graph_rag 의 것과 바이트 동일(출력 드리프트 방지).

실행: python -m pytest tests/test_capability_contract.py -q
"""

import pytest

import capability_registry as cr
import compiler_strategies as cs
import graph_rag as g
from join_paths import JOIN_PATHS, FORBIDDEN_PRODUCT_JOIN_KEYS, JoinPath, JoinCondition, JoinPathError


_REG = cr.CapabilityRegistry.load()
_SCHEMA = cr.SchemaFields.load()


# ── 정적 검증: supported=true 는 실행자산이 있을 때만 통과 ─────────────────────────────────────
def test_registry_static_validation_clean():
    errors = cr.validate_capabilities(_REG, _SCHEMA)
    assert errors == [], "capability 정의와 구현 불일치:\n" + "\n".join(errors)


def test_declared_supported_resolves_to_supported():
    # 선언 supported=true 인데 상태가 supported 로 산출되지 않으면(=실행자산 결여) 계약 위반.
    for cap in _REG.capabilities.values():
        if cap.supported_flag and not cap.policy_disabled:
            assert cap.resolve_status(_SCHEMA) == cr.STATUS_SUPPORTED, f"{cap.base}.{cap.qualifier}"


def test_mismatch_is_detected_stage1():
    # 1단계(불일치 탐지): supported=true 인데 등록 안 된 strategy 를 선언하면 validator 가 잡는다.
    import copy
    reg2 = copy.deepcopy(_REG)
    cap = reg2.capabilities[("cart_retention", "brand")]
    reg2.capabilities[("cart_retention", "brand")] = cr.RequirementCapability(
        base=cap.base, qualifier=cap.qualifier, supported_flag=True,
        compiler_strategy="join_product_brand_TYPO", join_path=cap.join_path, filter_field=cap.filter_field,
    )
    errors = cr.validate_capabilities(reg2, _SCHEMA)
    assert any("not registered" in e for e in errors)


def test_missing_join_path_detected():
    import copy
    reg2 = copy.deepcopy(_REG)
    cap = reg2.capabilities[("purchase", "brand")]
    reg2.capabilities[("purchase", "brand")] = cr.RequirementCapability(
        base=cap.base, qualifier=cap.qualifier, supported_flag=True,
        compiler_strategy="join_product_dimension", join_path="nonexistent_path", filter_field="BRAND_NAME",
    )
    errors = cr.validate_capabilities(reg2, _SCHEMA)
    assert any("join_path 'nonexistent_path' is not registered" in e for e in errors)


# ── 상태 taxonomy: 기술 미구현/정책 비활성 구분 ─────────────────────────────────────────────
def test_status_taxonomy_distinguishes():
    assert _REG.get("cart_retention", "brand").resolve_status(_SCHEMA) == cr.STATUS_SUPPORTED
    assert _REG.get("cart_retention", "product").resolve_status(_SCHEMA) == cr.STATUS_NOT_IMPLEMENTED
    assert _REG.get("purchase", "brand").resolve_status(_SCHEMA) == cr.STATUS_SUPPORTED


# ── compiler_strategy 는 실제 dispatch 가능 ─────────────────────────────────────────────────
def test_all_declared_strategies_are_dispatchable():
    for cap in _REG.capabilities.values():
        if cap.supported_flag and cap.compiler_strategy:
            assert cs.has_strategy(cap.compiler_strategy), f"{cap.compiler_strategy} 미등록"


# ── 조인 경로: 스키마 근거 + 라인 PK 금지 ────────────────────────────────────────────────────
def test_join_paths_never_use_line_pk_as_product_key():
    for path in JOIN_PATHS.values():
        for cond in path.conditions:
            assert cond.left_column not in FORBIDDEN_PRODUCT_JOIN_KEYS
            assert cond.right_column not in FORBIDDEN_PRODUCT_JOIN_KEYS


def test_forbidden_join_key_raises_at_definition():
    # CART_PRODUCT_NO(라인 PK)를 상품 조인 키로 등록하려 하면 정의 시점에 실패한다.
    with pytest.raises(JoinPathError):
        JoinPath(
            name="bad", source_table="ODS_MALL_OMS_CART", source_alias="A",
            target_table="CRM_CM_PRODUCT", target_alias="C",
            conditions=(JoinCondition(left="A.CART_PRODUCT_NO", right="C.PRODUCT_ID"),),
        )


def test_cart_to_product_join_line():
    line = JOIN_PATHS["cart_to_product"].render_join_line()
    assert "INNER JOIN CRM_CM_PRODUCT C ON A.PRODUCT_ID = C.PRODUCT_ID" in line


# ── SQL 리터럴 헬퍼 parity(공통 compiler ↔ graph_rag) ──────────────────────────────────────
@pytest.mark.parametrize("value", ["알로루", "CJ제일제당", "O'Neil", "a-b c"])
def test_quote_parity_with_graph_rag(value):
    assert cs._quote(value) == g._sql_quote(value)


@pytest.mark.parametrize("term", ["알로루", "CJ제일제당", "O'Neil"])
def test_nlike_parity_with_graph_rag(term):
    assert cs._nlike_contains("P.BRAND_NAME", term) == g._sql_nlike_contains("P.BRAND_NAME", term)


# ── 공통 product-dimension compiler: 지원 조합은 evidence 를 낸다 ────────────────────────────
@pytest.mark.parametrize("base,qualifier,alias,field", [
    ("purchase", "brand", "P", "BRAND_NAME"),
    ("purchase", "product", "P", "PRODUCT_NAME"),
    ("cart_retention", "brand", "C", "BRAND_NAME"),
])
def test_supported_qualifier_compiles_with_evidence(base, qualifier, alias, field):
    cf = cs.compile_product_dimension_filter(
        base=base, qualifier=qualifier, product_alias=alias, name_field=field, name_values=["나이키"],
        join_path=_REG.get(base, qualifier).join_path,
    )
    assert cf is not None
    assert cf.filter_expression == f"({alias}.{field} LIKE N'%나이키%')"
    ev = cf.to_evidence()
    assert ev["qualifier"] == qualifier and ev["values"] == ["나이키"] and ev["resolved_via"] == "name"


def test_hybrid_code_path_takes_precedence_and_uses_in():
    cf = cs.compile_product_dimension_filter(
        base="cart_retention", qualifier="brand", product_alias="C",
        name_field="BRAND_NAME", name_values=["CJ제일제당"], code_field="BRAND_ID", code_values=["A", "B"],
        join_path="cart_to_product",
    )
    assert cf.filter_expression == "C.BRAND_ID IN ('A', 'B')"
    assert cf.resolved_via == "code" and cf.filter_field == "BRAND_ID"


def test_multiple_values_supported():
    cf = cs.compile_product_dimension_filter(
        base="purchase", qualifier="brand", product_alias="P", name_field="BRAND_NAME",
        name_values=["나이키", "아디다스"], join_path="purchase_to_product",
    )
    assert cf.filter_expression == "(P.BRAND_NAME LIKE N'%나이키%' OR P.BRAND_NAME LIKE N'%아디다스%')"


def test_no_values_returns_none():
    assert cs.compile_product_dimension_filter(
        base="purchase", qualifier="brand", product_alias="P", name_field="BRAND_NAME") is None


# ── 미지원 조합은 반드시 안내 메시지를 갖는다(조용한 차단 방지) ────────────────────────────────
@pytest.mark.parametrize("base,qualifier", [
    ("cart_retention", "product"), ("cart_retention", "category"),
    ("coupon", "brand"), ("login", "brand"),
])
def test_unsupported_combination_has_message(base, qualifier):
    cap = _REG.get(base, qualifier)
    assert not cap.supported_flag
    assert cap.message and cap.message.strip()
