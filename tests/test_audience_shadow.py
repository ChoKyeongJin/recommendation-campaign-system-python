"""shadow 검증 계층의 계약.

가장 중요한 두 축:

    **teeth**   비교기가 차이를 실제로 잡아내는가(항상 통과하는 비교기는 검증이 아니라 장식이다)
    **gate**    미실행·미분류·불완전 승인이 cut-over 를 확실히 막는가

전자를 negative control(의도적으로 다른 두 산출물)로, 후자를 게이트 계약으로 고정한다.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import audience_shadow as shadow  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import graph_rag  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "audience_shadow_fixture.json"


@pytest.fixture(scope="module")
def anchor() -> date:
    # 파이썬의 오늘이 아니라 **검증 엔진의 오늘**로 행을 만든다(경계가 하루 어긋나면 검증이 무의미).
    return shadow.engine_anchor_date()


@pytest.fixture(scope="module")
def tables(anchor: date):
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return shadow.build_fixture_tables(spec, anchor=anchor)


def _event_sql(expression: event_ir.Condition, anchor: date) -> str:
    catalog = audience_runtime.resolve_audience_catalog()
    return event_compiler.compile_expression(
        expression, context=catalog.compile_context(literals=True, today=anchor)
    ).sql


# ── teeth: 비교기가 차이를 잡는가 ─────────────────────────────────────────────────


def test_fixture_detects_the_predicted_inactivity_divergence(tables, anchor) -> None:
    """`inactivity_period` 가 A 그룹이 아닌 이유를 **측정으로** 고정한다.

    legacy  = LAST_LOGIN_DATE IS NOT NULL AND <= 컷오프   (무접속 회원 제외 · 경계일 포함)
    EventIR = NOT(로그인이 컷오프 이후에 있음)             (무접속 회원 포함 · 경계일 제외)

    이 테스트가 green 이 되면(=차이가 사라지면) 그때는 분류를 A 로 올릴 근거가 생긴 것이므로,
    실패가 아니라 **분류를 다시 보라는 신호**다.
    """
    legacy_sql = graph_rag._member_activity_predicate(30)
    event_sql = _event_sql(
        event_ir.Not(operand=event_ir.Exists(
            relation=event_ir.event_relation("login", event_ir.RollingWindow(value=30, unit="day")))),
        anchor,
    )

    result = shadow.run_fixture_comparison(
        legacy_sql=legacy_sql, event_ir_sql=event_sql, tables=tables)

    assert not result.matches, "미접속 조건의 두 해석이 같아졌다 — 슬롯 분류(D)를 다시 판정하라"
    assert 1009 in result.only_in_legacy   # 마지막 접속이 컷오프 정확히 그날
    assert 1004 in result.only_in_event_ir  # 한 번도 접속하지 않은 회원(NULL)

    stage = shadow.fixture_stage(result)
    assert stage.status == shadow.FAIL
    assert stage.divergences[0].kind == shadow.UNEXPLAINED_DIVERGENCE
    assert stage.divergences[0].blocks_cutover


def test_fixture_confirms_the_purchase_absence_equivalence(tables, anchor) -> None:
    """A 그룹의 근거 반대편 — 미구매 조건은 두 해석의 결과 집합이 **같다**."""
    legacy_sql = graph_rag._purchase_inactivity_predicate(90)
    event_sql = _event_sql(
        event_ir.Not(operand=event_ir.Exists(
            relation=event_ir.event_relation("purchase", event_ir.RollingWindow(value=90, unit="day")))),
        anchor,
    )

    result = shadow.run_fixture_comparison(
        legacy_sql=legacy_sql, event_ir_sql=event_sql, tables=tables)

    assert result.matches
    assert not result.has_duplicates
    # 경계에 놓인 세 회원이 실제로 판정 대상이 됐는지 확인한다(공허한 통과 방지).
    assert 1001 not in result.legacy_members  # 경계 그날 주문 → 창 안 → 미구매 아님
    assert 1002 in result.legacy_members      # 경계 하루 전 주문 → 창 밖 → 미구매
    assert 1003 not in result.legacy_members  # 경계 다음날 주문 → 창 안 → 미구매 아님
    assert shadow.fixture_stage(result).status == shadow.PASS


def test_fixture_flags_join_amplified_duplicates(tables) -> None:
    """조인 증폭으로 회원이 중복되면 결과 집합이 같아도 잡아낸다."""
    amplified = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
        "INNER JOIN CRM_SL_ORDERHEADERMALL O ON O.MEMBER_NO = B.MEMBER_NO"
    )
    deduped = (
        "SELECT DISTINCT B.MEMBER_NO FROM CRM_MB_BASEINFO B "
        "INNER JOIN CRM_SL_ORDERHEADERMALL O ON O.MEMBER_NO = B.MEMBER_NO"
    )

    result = shadow.run_fixture_comparison(
        legacy_sql=amplified, event_ir_sql=deduped, tables=tables)

    assert result.matches          # 집합은 같지만
    assert result.has_duplicates   # 행은 증폭됐다
    stage = shadow.fixture_stage(result)
    assert stage.status == shadow.FAIL
    assert "중복" in stage.divergences[0].summary


def test_fixture_dates_follow_the_anchor_instead_of_drifting(anchor) -> None:
    spec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    today = shadow.build_fixture_tables(spec, anchor=anchor)
    tomorrow = shadow.build_fixture_tables(spec, anchor=anchor + timedelta(days=1))

    orders = {table.name: table for table in today}["CRM_SL_ORDERHEADERMALL"]
    moved = {table.name: table for table in tomorrow}["CRM_SL_ORDERHEADERMALL"]
    index = orders.columns.index("ORDER_DATE")

    assert orders.rows[0][index] == shadow.offset_date(anchor, -90)
    assert moved.rows[0][index] == shadow.offset_date(anchor + timedelta(days=1), -90)
    # 절대 날짜로 적은 행(20190301 등)은 앵커가 움직여도 그대로다.
    assert "20190301" in {row[index] for row in orders.rows}


# ── ③ 구조 서명 ───────────────────────────────────────────────────────────────────


def test_structure_signature_reads_real_predicate_fields() -> None:
    """서명이 빈 값끼리 같아지는 퇴화를 막는다 — 추출 필드명이 바뀌면 여기서 red."""
    signature = shadow.sql_structure_signature(
        graph_rag._purchase_inactivity_predicate(90))

    assert signature["exists"], "EXISTS 구조를 하나도 읽지 못했다"
    negated, tables, _correlated, filters = signature["exists"][0]
    assert negated is True
    assert "CRM_SL_ORDERHEADERMALL" in tables
    assert any(field and operator for field, operator, _v, _n, _d in filters)


def test_structure_signature_refuses_to_call_an_unreadable_sql_equal() -> None:
    """술어를 하나도 못 읽었으면 '같다'가 아니라 '읽지 못했다'다.

    빈 서명끼리는 항상 같으므로, 이 가드가 없으면 해석되지 않는 두 SQL 이 조용히 '구조 동일'로
    보고된다 — 검증 단계가 통과 도장으로 퇴화하는 정확한 경로다.
    """
    unreadable = "B.ACTIVE_FLAG"  # 비교가 아닌 진리값 컬럼 참조 — 분해할 술어가 없다

    with pytest.raises(shadow.ShadowVerificationError):
        shadow.sql_structure_signature(unreadable)
    with pytest.raises(shadow.ShadowVerificationError):
        shadow.sql_structure_stage(unreadable, unreadable)


def test_incomparable_shapes_are_not_run_rather_than_divergent() -> None:
    """조인 문장 ↔ 술어는 '다르다'가 아니라 '대조하지 않았다'다(잡음 divergence 금지)."""
    join_statement = (
        "SELECT B.MEMBER_NO FROM CRM_MB_BASEINFO B INNER JOIN ("
        "SELECT MEMBER_NO FROM CRM_SL_ORDERHEADERMALL WHERE MEMBER_NO IS NOT NULL "
        "GROUP BY MEMBER_NO HAVING COUNT(DISTINCT ORDER_ID) >= 3) A ON A.MEMBER_NO = B.MEMBER_NO"
    )
    predicate = (
        "(SELECT COUNT(DISTINCT EO.ORDER_ID) FROM CRM_SL_ORDERHEADERMALL EO "
        "WHERE EO.MEMBER_NO = B.MEMBER_NO) >= 3"
    )

    stage = shadow.sql_structure_stage(join_statement, predicate)

    assert stage.status == shadow.NOT_RUN
    assert not stage.divergences
    assert "결과 집합 대조" in stage.detail


def test_identical_predicates_pass_the_structure_stage() -> None:
    legacy = graph_rag._purchase_membership_predicate(30)
    event_like = legacy.replace(" O ", " EO ").replace("O.", "EO.")

    assert shadow.sql_structure_stage(legacy, event_like).status == shadow.PASS


# ── gate: 미실행·미분류·불완전 승인은 통과가 아니다 ───────────────────────────────


def _report(*stages: shadow.StageResult) -> shadow.ShadowReport:
    return shadow.ShadowReport(asset_id="a", asset_revision=1, stages=tuple(stages))


def _passing(stage: str) -> shadow.StageResult:
    return shadow.StageResult(stage=stage, status=shadow.PASS)


def test_a_missing_required_stage_blocks_cutover() -> None:
    report = _report(*[_passing(name) for name in shadow.REQUIRED_STAGES[:-1]])

    assert report.missing_stages == (shadow.STAGE_PERFORMANCE,)
    assert report.cutover_allowed is False
    assert any("미실행" in reason for reason in report.blocking_reasons())


def test_all_stages_passing_allows_cutover() -> None:
    report = _report(*[_passing(name) for name in shadow.REQUIRED_STAGES])

    assert report.cutover_allowed is True
    assert report.blocking_reasons() == ()


def test_not_run_requires_a_reason() -> None:
    with pytest.raises(shadow.ShadowVerificationError):
        shadow.not_run(shadow.STAGE_PERFORMANCE, "   ")


def test_unexplained_divergence_is_the_default_and_blocks() -> None:
    divergence = shadow.Divergence(stage=shadow.STAGE_SQL_STRUCTURE, summary="차이")

    assert divergence.kind == shadow.UNEXPLAINED_DIVERGENCE
    assert divergence.blocks_cutover


@pytest.mark.parametrize(
    "kind", [shadow.DATA_VOLATILITY, shadow.NONDETERMINISTIC_SOURCE, shadow.EXPECTED_BINDING_CHANGE]
)
def test_explained_kinds_do_not_block(kind: str) -> None:
    assert not shadow.Divergence(
        stage=shadow.STAGE_SQL_STRUCTURE, summary="차이", kind=kind).blocks_cutover


def _approval(**overrides: object) -> shadow.Approval:
    payload = {
        "before_meaning": "무접속 회원 제외",
        "after_meaning": "무접속 회원 포함",
        "affected_member_count": 12,
        "approver": "crm-owner",
        "approved_at": "2026-08-03T09:00:00+09:00",
        "reference": "docs/plans_legacy_audience_strangler.md",
        "rollback_criterion": "발송 대상 수가 기준 대비 5% 이상 증가하면 되돌린다",
    }
    payload.update(overrides)
    return shadow.Approval(**payload)  # type: ignore[arg-type]


def test_approved_bug_fix_needs_every_field_to_stop_blocking() -> None:
    complete = shadow.Divergence(
        stage=shadow.STAGE_ADVERSARIAL_FIXTURE, summary="차이",
        kind=shadow.APPROVED_LEGACY_BUG_FIX, approval=_approval())
    without_approver = shadow.Divergence(
        stage=shadow.STAGE_ADVERSARIAL_FIXTURE, summary="차이",
        kind=shadow.APPROVED_LEGACY_BUG_FIX, approval=_approval(approver=""))
    without_record = shadow.Divergence(
        stage=shadow.STAGE_ADVERSARIAL_FIXTURE, summary="차이",
        kind=shadow.APPROVED_LEGACY_BUG_FIX)

    assert not complete.blocks_cutover
    assert without_approver.blocks_cutover, "승인 기록이 비어도 분류 이름만으로 통과됐다"
    assert without_record.blocks_cutover


def test_approval_loading_rejects_incomplete_records() -> None:
    with pytest.raises(shadow.ShadowVerificationError):
        shadow.approvals_from_dicts([{"key": "x:y", "before_meaning": "a"}])

    loaded = shadow.approvals_from_dicts([{"key": "x:y", **_approval().to_dict()}])
    assert loaded["x:y"].is_complete


def test_approvals_only_upgrade_unexplained_divergences() -> None:
    stage = shadow.StageResult(
        stage=shadow.STAGE_ADVERSARIAL_FIXTURE, status=shadow.FAIL,
        divergences=(
            shadow.Divergence(stage=shadow.STAGE_ADVERSARIAL_FIXTURE, summary="설명되지 않은 차이"),
            shadow.Divergence(stage=shadow.STAGE_ADVERSARIAL_FIXTURE, summary="변동성",
                              kind=shadow.DATA_VOLATILITY),
        ),
    )
    approvals = {
        f"{shadow.STAGE_ADVERSARIAL_FIXTURE}:설명되지 않은 차이": _approval(),
        f"{shadow.STAGE_ADVERSARIAL_FIXTURE}:변동성": _approval(),
    }

    updated = shadow.apply_approvals(_report(stage), approvals)
    kinds = {item.summary: item.kind for item in updated.divergences}

    assert kinds["설명되지 않은 차이"] == shadow.APPROVED_LEGACY_BUG_FIX
    assert kinds["변동성"] == shadow.DATA_VOLATILITY  # 이미 분류된 것은 덮어쓰지 않는다
    # 단계가 차이를 찾았다는 **관측 사실**은 그대로 남는다(승인이 관측을 지우지 않는다).
    assert shadow.STAGE_ADVERSARIAL_FIXTURE in updated.failed_stages
    assert not updated.blocking_divergences


def test_a_fully_approved_difference_can_pass_the_gate() -> None:
    """승인 계약이 장식이 아니려면, 완전한 승인은 실제로 통과시켜야 한다."""
    approved = shadow.StageResult(
        stage=shadow.STAGE_ADVERSARIAL_FIXTURE, status=shadow.FAIL,
        divergences=(shadow.Divergence(
            stage=shadow.STAGE_ADVERSARIAL_FIXTURE, summary="무접속 회원 포함 여부",
            kind=shadow.APPROVED_LEGACY_BUG_FIX, approval=_approval()),),
    )
    others = [_passing(name) for name in shadow.REQUIRED_STAGES
              if name != shadow.STAGE_ADVERSARIAL_FIXTURE]

    report = _report(approved, *others)

    assert report.cutover_allowed is True
    assert report.failed_stages == (shadow.STAGE_ADVERSARIAL_FIXTURE,)


def test_a_failure_without_a_recorded_reason_always_blocks() -> None:
    """상태와 분류를 나눈 대가로 생기는 구멍(사유 없는 FAIL)을 막는다."""
    silent = shadow.StageResult(stage=shadow.STAGE_SQL_STRUCTURE, status=shadow.FAIL)
    others = [_passing(name) for name in shadow.REQUIRED_STAGES
              if name != shadow.STAGE_SQL_STRUCTURE]

    report = _report(silent, *others)

    assert report.unaccounted_stages == (shadow.STAGE_SQL_STRUCTURE,)
    assert report.cutover_allowed is False


def test_divergence_kind_vocabulary_is_closed() -> None:
    assert set(shadow.DIVERGENCE_KINDS) == {
        "UNEXPLAINED_DIVERGENCE", "APPROVED_LEGACY_BUG_FIX", "DATA_VOLATILITY",
        "NONDETERMINISTIC_SOURCE", "EXPECTED_BINDING_CHANGE",
    }
    with pytest.raises(shadow.ShadowVerificationError):
        shadow.Divergence(stage=shadow.STAGE_SQL_STRUCTURE, summary="x", kind="PROBABLY_FINE")
