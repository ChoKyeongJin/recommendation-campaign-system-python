"""브랜드·주문 디바이스 축 — **선언만으로** 열리고 기존 범용 컴파일러를 그대로 탄다.

이 두 축을 연 방법이 이 파일의 요점이다. 새 컴파일러 분기도, 새 IR 노드도, 새 빌더도
없다. ``audience_catalog.json`` 에 필드 선언과 값 도메인을 넣었을 뿐이고, 서로 다른 브랜드
개수는 장바구니 상품 종류를 세던 그 ``aggregate.count_distinct`` 로 낮춰진다.

물리 근거(실측 2026-08-08, CRMDW)::

    CRM_CM_PRODUCT.BRAND_ID / BRAND_NAME    nvarchar, 199행 전부 채워짐 · 25개 브랜드
    CRM_SL_ORDERHEADERMALL.DEVICE_TYPE_CD   PC 116,530 · APP 69,195 · MW 44,717

브랜드 조인은 이미 있었다 — ``purchase_line`` 소스의 ``from_sql`` 이 상품마스터를
``{alias}_PRODUCT`` 로 LEFT JOIN 한다. 그래서 브랜드는 조인 문제가 아니라 **선언 부재**였다.
"""

from __future__ import annotations

import audience_runtime
import event_compiler
import event_ir


def _context() -> event_compiler.CompileContext:
    return audience_runtime.resolve_audience_catalog().compile_context(literals=True)


def _sql(condition: event_ir.Condition) -> str:
    return event_compiler.compile_condition(condition, _context()).sql


def _catalog() -> dict:
    return audience_runtime.catalog_snapshot()


def _purchase_line() -> event_ir.Source:
    return event_ir.Source(name="purchase_line", correlation="subject")


def _purchase() -> event_ir.Source:
    return event_ir.Source(name="purchase", correlation="subject")


def _device_is(value: str) -> event_ir.Comparison:
    return event_ir.Comparison(
        operator="=",
        left=event_ir.FieldRef(name="purchase.device"),
        right=event_ir.Literal(value=value),
    )


# ── 브랜드 ───────────────────────────────────────────────────────────────────────


def test_brand_fields_resolve_through_the_existing_product_join() -> None:
    """브랜드는 상품마스터 컬럼이고 그 조인은 소스 선언이 이미 갖고 있다."""
    fields = audience_runtime.resolve_audience_catalog().fields

    assert "purchase_line.brand_name" in fields
    assert "purchase_line.brand_id" in fields
    # 조인 별칭을 쓰는 파생식이라야 상품마스터 컬럼에 닿는다(새 조인을 만들지 않았다는 증거).
    declaration = _catalog()["fields"]["purchase_line.brand_name"]
    assert declaration["expression"] == "{alias}_PRODUCT.BRAND_NAME"
    assert _catalog()["sources"]["purchase_line"]["from_sql"].count("CRM_CM_PRODUCT") == 1


def test_distinct_brand_count_reuses_the_generic_count_distinct() -> None:
    """#46 ``서로 다른 브랜드를 정확히 두 개 구매한 회원`` — 새 컴파일러 분기 없음."""
    condition = event_ir.Comparison(
        operator="=",
        left=event_ir.Aggregate(
            function="count",
            expression=event_ir.FieldRef(name="purchase_line.brand_name"),
            distinct=True,
            relation=_purchase_line(),
        ),
        right=event_ir.Literal(value=2),
    )

    sql = _sql(condition)

    assert "COUNT(DISTINCT OD_PRODUCT.BRAND_NAME) = 2" in sql
    assert "LEFT JOIN CRM_CM_PRODUCT OD_PRODUCT" in sql


def test_advertised_brand_support_is_backed_by_a_real_catalog_field() -> None:
    """능력 선언이 약속한 것을 카탈로그가 실제로 표현할 수 있어야 한다.

    ``requirement_capabilities.json`` 은 ``purchase.qualifiers.brand.supported = true`` 라고
    광고해 왔는데 카탈로그에는 브랜드 필드가 **하나도 없었다**(실측 2026-08-08). 능력 계층이
    카탈로그가 못 하는 일을 약속하면 그 약속은 사용자에게 도달하는 순간 거짓이 된다.

    이 검사가 red 가 되면 고칠 방향은 둘 중 하나다 — 필드를 선언하거나, 광고를 내리거나.
    조용히 두는 선택지는 없다.
    """
    import json
    from pathlib import Path

    declared = json.loads(
        Path("docs/data/runtime/semantics/requirement_capabilities.json").read_text(
            encoding="utf-8"
        )
    )
    purchase_brand = declared["capabilities"]["purchase"]["qualifiers"]["brand"]
    fields = audience_runtime.resolve_audience_catalog().fields

    if purchase_brand.get("supported"):
        assert any("brand" in name for name in fields), (
            "구매 조건의 브랜드 지원을 광고하는데 카탈로그에 브랜드 필드가 없다"
        )


def test_brand_name_is_a_contains_search_like_the_other_product_text() -> None:
    """브랜드명은 자유 텍스트다 — 상품명·카테고리와 같은 안전한 LIKE 경로를 쓴다."""
    declaration = _catalog()["fields"]["purchase_line.brand_name"]

    assert declaration["match_mode"] == "contains"
    assert declaration["allowed_operators"] == ["=", "!="]


# ── 주문 디바이스 ────────────────────────────────────────────────────────────────


def test_order_device_compiles_to_the_order_header_column() -> None:
    """값 별칭('app')이 물리 코드로 바뀌고 주문 헤더 별칭에 붙는다.

    팩트 테이블 필드라 관계 스코프 안에서만 컴파일된다 — 그래서 ``Exists(Filter(...))``
    로 감싼다. 이 제약 자체가 '주문 건의 속성'이라는 축의 정체를 강제한다.
    """
    sql = _sql(
        event_ir.Exists(
            relation=event_ir.Filter(relation=_purchase(), where=_device_is("app"))
        )
    )

    assert "EO.DEVICE_TYPE_CD = 'DEVICE_TYPE_CD.APP'" in sql
    assert "FROM CRM_SL_ORDERHEADERMALL EO" in sql


def test_order_device_is_a_separate_axis_from_the_member_login_channel() -> None:
    """같은 코드 사전이라도 축이 다르다 — 회원의 마지막 상태 vs 주문 건의 속성.

    회원측 ``login_channel`` 은 ``B.LAST_LOGIN_CHANNEL`` 에 묶인 eq_filters 정체성이라
    주문 헤더 컬럼을 대신 소유할 수 없다.
    """
    fields = _catalog()["fields"]

    assert fields["purchase.device"]["value_domain"] == "order_device"
    assert fields["subject.last_login_channel"]["value_domain"] == "login_channel"
    assert fields["purchase.device"]["column"] == "DEVICE_TYPE_CD"


def test_every_declared_device_value_maps_to_a_physical_code_that_exists() -> None:
    """실측한 세 코드만 선언한다 — 없는 값을 열면 조용히 0건이 나온다."""
    values = _catalog()["value_domains"]["order_device"]["values"]

    assert {entry["physical"] for entry in values.values()} == {
        "DEVICE_TYPE_CD.APP",
        "DEVICE_TYPE_CD.MW",
        "DEVICE_TYPE_CD.PC",
    }


def test_device_value_aliases_do_not_reach_into_another_axis_surface() -> None:
    """짧은 값 별칭이 **다른 축**의 표면어 안에서 매치되면 안 된다.

    실측(2026-08-08): 값 별칭 ``앱`` 을 넣자 ``앱푸시 수신 동의``의 ``앱푸시`` 안에서
    매치돼 동의 축 4종이 ``catalog_value.purchase.device`` 극성 불일치로 깨졌다. 새 축을
    여는 비용이 남의 축을 부수는 것이어서는 안 된다.

    전 카탈로그에 이 규칙을 거는 것은 지금 옳지 않다 — ``'동의' ⊂ '이메일 수신 동의'`` 처럼
    **같은 축 안에서는** 부분 문자열이 정상이고, 그런 쌍이 이미 84개다. 그래서 이 검사는
    새로 연 값 도메인만 본다.
    """
    catalog = _catalog()
    device_aliases = {
        alias
        for entry in catalog["value_domains"]["order_device"]["values"].values()
        for alias in entry["aliases"]
    }
    foreign_surfaces = {
        alias
        for name, spec in catalog["fields"].items()
        if name != "purchase.device"
        for alias in (spec.get("aliases") or [])
    }

    collisions = [
        (alias, other)
        for alias in device_aliases
        for other in foreign_surfaces
        if alias != other and alias in other
    ]

    assert not collisions, f"다른 축의 표면어를 잠식하는 값 별칭: {collisions}"


def test_both_channels_are_two_independent_exists_not_one_row() -> None:
    """#53 ``앱과 PC 양쪽 채널에서 모두 주문`` — 한 주문이 두 디바이스일 수는 없다."""
    condition = event_ir.And(operands=tuple(
        event_ir.Exists(relation=event_ir.Filter(relation=_purchase(), where=_device_is(value)))
        for value in ("app", "pc")
    ))

    sql = _sql(condition)

    assert sql.count("EXISTS (") == 2
    assert "DEVICE_TYPE_CD.APP" in sql and "DEVICE_TYPE_CD.PC" in sql


def test_one_of_channels_keeps_the_correlation_inside_the_disjunction() -> None:
    """#54 — 새 축에서도 상관 술어가 OR 밖으로 새지 않는다.

    실DB 실측(2026-08-08): 괄호가 빠진 형태는 **69,609명**(전체 회원)을 냈고, 괄호를 씌운
    형태는 61,212명이었다. Boolean 합성 안전성이 나중에 추가되는 축까지 지킨다는 증거다.
    """
    condition = event_ir.Exists(relation=event_ir.Filter(
        relation=_purchase(),
        where=event_ir.Or(operands=tuple(
            _device_is(value) for value in ("app", "mobile_web", "pc")
        )),
    ))

    sql = _sql(condition)

    assert not event_compiler.has_top_level_disjunction(sql)
    assert "= B.MEMBER_NO AND ((" in sql
