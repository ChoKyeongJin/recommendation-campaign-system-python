"""audience_catalog 에 새로 추가한 회원 축 4종의 SQL 접지 고정.

네 축 모두 물리 컬럼이 schema_catalog 에 실재하고, canonical Event IR 레인에서 컴파일된다.
legacy ``member_target_filters.json`` 이 아니라 이 파일이 소유자인 이유는 두 가지다 — 그쪽에
넣으면 커버리지 래칫이 red 가 되고, subject 가 곧 기준 테이블이라 ``EXISTS`` 자기조인이
생기지 않는다.

별칭 규칙 하나를 여기서 고정한다: **다른 실재 컬럼의 표면어를 접두어로 갖는 짧은 별칭은 금지**다.
``'결혼'`` 한 낱말을 별칭에 넣으면 ``WEDDING_DAY``(결혼기념일) 를 묻는 문장이 결혼 상태 조건으로
오발화한다. 2토큰 ``'결혼 여부'`` 는 순서보존 토큰 매칭이라 안전하다.
"""

from __future__ import annotations

import audience_runtime
import event_ir
import graph_rag


def _sql(field: str, value: object, operator: str = "=") -> str:
    context = audience_runtime.resolve_audience_catalog().compile_context(literals=True)
    condition = event_ir.Comparison(
        operator, event_ir.FieldRef(field), event_ir.Literal(value)
    )
    return graph_rag.event_compiler.compile_condition(condition, context).sql


def _catalog() -> dict:
    return audience_runtime.catalog_snapshot()


# ── (a) 결혼 여부 ──────────────────────────────────────────────────────────────────────


def test_marriage_flag_compiles_to_the_base_table_column() -> None:
    assert _sql("subject.married", "married") == "B.MARRIAGE_YN = 'Y'"


def test_marriage_alias_does_not_swallow_the_wedding_anniversary_column() -> None:
    """``'결혼'`` 단독 별칭 금지 — 예27 '결혼기념일' 문장이 결혼 상태로 읽히면 안 된다."""
    aliases = _catalog()["fields"]["subject.married"]["aliases"]
    assert "결혼" not in aliases
    assert "결혼 여부" in aliases


# ── (b) 예치금·적립금 잔액 ────────────────────────────────────────────────────────────


def test_balance_axes_compile_as_numeric_comparisons() -> None:
    assert _sql("subject.deposit_balance", 10000, ">=") == "B.DEPOSIT_BALANCE_AMT >= 10000"
    assert _sql("subject.carrot_balance", 10000, ">=") == "B.CARROT_BALANCE_AMT >= 10000"


def test_balance_axes_declare_currency_so_units_are_not_guessed() -> None:
    fields = _catalog()["fields"]
    for name in ("subject.deposit_balance", "subject.carrot_balance"):
        assert fields[name]["unit"] == "currency", name
        assert fields[name]["data_type"] == "number", name


# ── (c) 신제품 선호 여부 (월 스냅샷) ──────────────────────────────────────────────────


def test_newproduct_favor_is_declared_as_a_snapshot_metric() -> None:
    """스냅샷 필드는 metric 선언이 있어야 관계가 스코프에 들어온다.

    필드만 두면 ``'…을 참조할 관계가 현재 스코프에 없습니다'`` 로 컴파일이 막힌다 — 등급·가치등급이
    metric 을 함께 갖는 이유와 같다.
    """
    metric = _catalog()["metrics"]["member_newproduct_favor"]
    assert metric["expression_field"] == "member_month_snapshot.newproduct_favor"
    assert metric["source"] == "member_month_snapshot"
    assert metric["coverage"] == "monthly_attribute_snapshot"


def test_newproduct_favor_allows_only_equality_operators() -> None:
    """Y/N 에 부등호를 허용하면 사전식 비교로 조용히 틀린다. 서열이 없는 축이므로 =·!= 만."""
    assert _catalog()["metrics"]["member_newproduct_favor"]["allowed_operators"] == ["=", "!="]


def test_newproduct_favor_field_points_at_the_monthly_snapshot_column() -> None:
    field = _catalog()["fields"]["member_month_snapshot.newproduct_favor"]
    assert field["column"] == "NEWPRODUCT_FAVOR_YN"
    assert field["source"] == "member_month_snapshot"


def test_newproduct_favor_is_not_ordered_because_yn_has_no_rank() -> None:
    """Y/N 한 글자에 부등호를 허용하면 사전식 순서로 조용히 틀린 SQL 이 나온다."""
    domain = _catalog()["value_domains"]["newproduct_favor_flag"]
    assert not domain.get("ordered", False)


def test_newproduct_favor_aliases_stay_two_tokens() -> None:
    """``'신제품'`` 단독은 캠페인 목표 규칙과 경쟁하므로 별칭이 되면 안 된다."""
    aliases = _catalog()["fields"]["member_month_snapshot.newproduct_favor"]["aliases"]
    assert "신제품" not in aliases
    assert "신상품" not in aliases


# ── (d) 캠페인 대상군 ─────────────────────────────────────────────────────────────────


def test_campaign_target_source_does_not_reuse_the_contact_alias() -> None:
    """별칭을 재사용하면 캠페인 단위 상관식이 ``ZC.x = ZC.x`` 항진명제로 조용히 무너진다.

    검증기는 별칭이 전부 허용목록에 있어 이걸 통과시킨다 — 그래서 테스트가 유일한 방어선이다.
    """
    sources = _catalog()["sources"]
    target = sources["campaign_target"]
    contact = sources["campaign_contact_success"]
    assert target["alias"] != contact["alias"]
    assert "{alias}_ZC" in target["from_sql"]

    # 매개변수화된 별칭을 지우고 나면 맨 ZC 토큰이 하나도 남지 않아야 한다.
    bare = target["from_sql"].replace("{alias}_ZC", "«joined»")
    assert "ZC" not in bare, bare
    for clause in (target["time_expression"], *target["extra_predicates"]):
        assert "ZC" not in clause.replace("{alias}_ZC", "«joined»"), clause


def test_campaign_target_excludes_cancelled_campaigns_and_control_cells() -> None:
    predicates = " ".join(_catalog()["sources"]["campaign_target"]["extra_predicates"])
    assert "CELL_TYPE_CD = 'T'" in predicates
    assert "CANCEL_YN" in predicates


def test_campaign_target_aliases_do_not_hijack_contact_requests() -> None:
    """대상군 ⊇ 접촉 성공이다. 일반어를 별칭에 넣으면 접촉 요청이 대상군으로 새어 모집단이 넓어진다."""
    aliases = _catalog()["sources"]["campaign_target"]["aliases"]
    assert "캠페인" not in aliases
    assert "발송" not in aliases


def test_campaign_target_participates_in_campaign_contact_coverage() -> None:
    """감지기가 '캠페인대상'을 campaign_contact 로 방출하므로 이 소스가 그 커버리지에 있어야 한다.

    별도 family 로 두면 선언은 죽고 드롭 감지기가 오탐 경고를 낸다.
    """
    assert "campaign_target" in _catalog()["signal_coverage"]["campaign_contact"]["sources"]


# ── 지원 조건 안내가 실제 지원 범위를 따라가는가 ─────────────────────────────────────


def test_supported_condition_hint_advertises_the_axes_that_actually_compile() -> None:
    """안내 문구가 실제로 컴파일되는 축을 빠뜨리면 사용자가 되는 조건을 안 쓴다(과소 광고)."""
    hint = graph_rag._supported_condition_hint()
    for axis in ("결혼 여부", "예치금", "적립금", "신제품 선호 여부", "캠페인 대상군"):
        assert axis in hint, axis
