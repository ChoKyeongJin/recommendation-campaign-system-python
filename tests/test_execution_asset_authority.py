"""capability 판정의 권위는 모델이 아니라 실행 자산이다 (P2).

실측(2026-08-03, campaign_query_failure_logs 30건): `semantic_ir_unsupported` **30/30 전부**가
모델이 쓴 산문 판정이었고, 모델이 지어낸 `kind` 는 23종이었다. 그리고 그 판정은 틀렸다 —
'구매주기'는 `buy_cycle` 로 물리 컬럼까지 선언돼 있었고, '이메일 수신 동의'는 `eq_filters` 의
`email_optin` 이었다.

이 파일이 지키는 것은 셋이다:
  1. 판정자는 **선언된 실행 자산 색인**이고, 그 색인이 no-op 이 아니다(코퍼스 대다수를 안다).
  2. 모델의 자유 텍스트는 `kind` 도 사용자 문장도 될 수 없다.
  3. 강등은 **모델 신고에만** 적용된다 — 애플리케이션이 계산한 판정까지 뒤집으면
     조용한 오답을 막으려던 장치가 그것을 만드는 장치가 된다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import execution_assets  # noqa: E402
import semantic_plan  # noqa: E402
from query_structurer import campaign_plan_v4  # noqa: E402

CORPUS = REPO_ROOT / "docs" / "data" / "test_baselines" / "live_prompts.json"

# 색인이 조용히 비면 P2 의 판정이 전부 no-op 이 된다(강등 0건). 그 상태는 green 이면 안 된다.
# 실측 2026-08-03: 카탈로그의 죽은 aliases 선언을 살린 뒤 74/77.
_MIN_CORPUS_COVERAGE = 70


def _corpus_texts() -> list[str]:
    return [entry["text"] for entry in json.loads(CORPUS.read_text(encoding="utf-8"))["prompts"]]


def test_declared_surface_index_is_not_empty() -> None:
    surfaces = execution_assets.declared_surfaces()
    assert surfaces, "선언된 실행 자산 표면어가 하나도 없다 — 판정이 전부 no-op 이 된다."
    layers = {asset.layer for asset in surfaces}
    assert {execution_assets.CANONICAL, execution_assets.MEMBER_FILTER} <= layers, (
        f"계층이 비었다: {sorted(layers)}. 레지스트리 접근자가 조용히 깨졌을 수 있다."
    )


def test_index_knows_most_of_the_live_corpus() -> None:
    """판정이 실제로 작동하는지 코퍼스로 잰다 — '히트 0'이면 P2 는 아무것도 하지 않는다."""
    texts = _corpus_texts()
    hits = [text for text in texts if execution_assets.has_execution_asset(text)]
    assert len(hits) >= _MIN_CORPUS_COVERAGE, (
        f"실행 자산 색인이 코퍼스 {len(hits)}/{len(texts)} 만 안다"
        f"(기준 {_MIN_CORPUS_COVERAGE}). 레지스트리 별칭 배선이 끊겼는지 확인하라."
    )


def test_particle_tolerant_matching_finds_the_consent_axis() -> None:
    """조사가 끼면 못 찾던 결함의 회귀 고정.

    카탈로그 표면어는 '이메일 수신 동의' 인데 실제 원문은 '이메일 수신에 동의한' 이다.
    부분문자열 매칭이면 히트 0 이고, 그러면 R1 이 지목한 최대 군집(수신동의 축)이
    통째로 판정 밖으로 빠진다.
    """
    assets = execution_assets.assets_for_text("이메일 수신에 동의한 회원")
    assert any(asset.symbol == "email_optin" for asset in assets), (
        f"수신동의 축을 못 찾는다: {[a.symbol for a in assets]}"
    )


def test_matching_does_not_fire_on_unrelated_text() -> None:
    """상시 히트는 상시 강등이다 — 과매칭은 조용한 오답 출고로 이어진다."""
    assert not execution_assets.assets_for_text("오늘 날씨가 좋다")
    assert not execution_assets.assets_for_text("")


def test_canonical_layer_is_excluded_from_the_demotion_question() -> None:
    """canonical 은 이미 자기 차례에 실패했다 — 강등 판정이 묻는 것은 그 밖의 계층이다."""
    for asset in execution_assets.non_canonical_assets_for_text("이메일 수신에 동의한 회원"):
        assert asset.layer != execution_assets.CANONICAL


def _payload_with_model_unsupported(query: str, span: str, *, nodes: list | None = None) -> dict:
    start = query.index(span)
    return {
        "intent": "find_user_segment",
        "original_query": query,
        "literal_bindings": [],
        "semantic_plan": {"nodes": list(nodes or [])},
        "audience_requirement": {
            "expression": None,
            "issues": [{
                "code": "unsupported_semantics",
                "argument": "email_consent_flag",
                "message": "이메일 수신 동의 조건은 Canonical Event IR로 표현할 수 없습니다.",
                "evidence": {"text": span, "start": start, "end": start + len(span)},
            }],
        },
    }


def test_model_prose_never_becomes_the_kind_or_the_user_sentence() -> None:
    """모델의 자유 텍스트는 판정 코드도 사용자 문장도 될 수 없다.

    닫힌 어휘가 아니면 집계도 테스트도 신뢰도 불가능하다 — 실측된 kind 23종이 그 증거다.
    """
    query = "지난주에 승급 예측이 높은 회원을 찾아줘"
    payload = _payload_with_model_unsupported(query, "승급 예측이 높은")
    campaign_plan_v4._derive_audience_execution(payload, query, current_date=None)

    semantic_ir = payload["semantic_ir"]
    if semantic_ir["status"] == "unsupported":
        kinds = {item["kind"] for item in semantic_ir["unsupported_operations"]}
        assert kinds <= semantic_plan.FAILURE_CODES, (
            f"닫힌 어휘 밖의 kind 가 나갔다: {sorted(kinds - semantic_plan.FAILURE_CODES)}"
        )
        assert "email_consent_flag" not in kinds, "모델의 자유 텍스트가 kind 가 됐다."
        assert semantic_ir["message"] != (
            "이메일 수신 동의 조건은 Canonical Event IR로 표현할 수 없습니다."
        ), "모델이 쓴 문장이 그대로 사용자에게 나간다."


def test_declared_asset_contradicts_the_model_claim() -> None:
    """모델이 '표현 불가'라고 한 축에 선언된 실행 자산이 있으면 그 판정은 종결이 아니다.

    예전 조건은 '다른 생산자가 노드를 냈는가'였다 — 모델이 미지원을 선언할 때는 노드를 내지
    않으므로 **보호가 필요한 경우에만 정확히 발동하지 않는** 자기무력화 가드였다.
    """
    query = "이메일 수신에 동의한 회원을 찾아줘"
    payload = _payload_with_model_unsupported(query, "이메일 수신에 동의한")
    campaign_plan_v4._derive_audience_execution(payload, query, current_date=None)

    semantic_ir = payload["semantic_ir"]
    assert semantic_ir["status"] != "unsupported", (
        "실행 자산(eq_filters.email_optin)이 선언돼 있는데도 '표현할 수 없다'로 종결됐다."
    )
    assert payload.get("audience_execution_assets"), "무엇이 그 판정을 반박했는지 남지 않았다."


def test_application_computed_verdicts_are_not_demoted() -> None:
    """애플리케이션이 계산한 판정은 강등 대상이 아니다.

    그 issue 들의 근거 구간은 **원문 전체**라, 표면어가 하나만 걸려도 event_compiler 의
    권위 있는 판정까지 뒤집힌다. 그러면 이 장치가 조용한 오답을 만든다.
    """
    # 미등록 심볼 판정은 애플리케이션이 **append** 한다(모델의 issues 가 아니라). 그 근거 구간은
    # 원문 전체라, 강등 판정을 여기까지 적용하면 표면어 하나로 뒤집힌다. 원문에 실행 자산
    # 표면어('구매')를 일부러 넣어 그 함정을 재현한다.
    query = "미등록 사건이 있는 VIP 회원"
    payload = {
        "intent": "find_user_segment",
        "original_query": query,
        "literal_bindings": [],
        "semantic_plan": {"nodes": []},
        "audience_requirement": {
            "expression": {
                "type": "exists",
                "relation": {"type": "source", "name": "unregistered_event"},
                "evidence": {"text": query, "start": 0, "end": len(query)},
            },
            "issues": [],
        },
    }
    assert execution_assets.non_canonical_assets_for_text(query), (
        "이 테스트는 원문에 실행 자산 표면어가 있어야 함정을 재현한다."
    )
    campaign_plan_v4._derive_audience_execution(payload, query, current_date=None)

    assert payload["semantic_ir"]["status"] == "unsupported", (
        "애플리케이션이 계산한 미지원이 표면어 우연 일치로 강등됐다."
    )


def test_model_prose_never_becomes_a_plan_validation_code() -> None:
    """판정 코드의 어휘가 모델 산문으로 오염되지 않는다 (P5 의 계약 부분).

    `plan_validation._marker` 는 code/reason/kind 순으로 자유 텍스트를 읽는다. 미지원
    컬렉션에서 그것을 쓰면 이슈 코드가 모델이 쓴 한국어 문장이 된다(실측: '표현_불가').
    닫힌 집합이 깨지면 그 위에 세울 수 있는 것이 아무것도 없다.
    """
    import plan_validation

    plan = {
        "semantic_ir": {
            "status": "unsupported",
            "operations": [],
            "missing_fields": [],
            "policy_applications": [],
            "unsupported_operations": [{
                "kind": "표현_불가",
                "reason": "회원 등급의 전이 조건은 현재 Canonical Event IR에서 표현할 수 없습니다",
                "evidence": "골드에서 VIP로",
            }],
            "message": None,
        }
    }
    result = plan_validation.validate_executable_plan(plan)
    codes = {issue.code for issue in result.issues}
    assert "표현_불가" not in codes, f"모델 산문이 판정 코드가 됐다: {sorted(codes)}"
    assert "semantic_operation_unsupported" in codes


def test_unsupported_claim_naming_a_registered_symbol_is_self_refuting() -> None:
    """등록된 canonical 심볼을 지목한 '표현 불가'는 종결이 아니라 재시도 사유다.

    실측(2026-08-03 라이브) — 단일 성별 조건 하나가 이렇게 막혔다:

        '여성 회원을 찾아줘' → unsupported_semantics / argument='subject.gender'
        "The compiler cannot represent direct profile field comparison…"

    `subject.gender` 는 카탈로그 필드다. 등록됐다는 것이 곧 표현할 수 있다는 뜻이므로
    그 주장은 스스로 반박된다 — 사용자에게 미지원이라고 답할 근거가 되지 못한다.
    """
    from query_structurer import structurer

    raw = {"audience_requirement": {"expression": None, "issues": [{
        "code": "unsupported_semantics",
        "argument": "subject.gender",
        "message": "The compiler cannot represent direct profile field comparison.",
        "evidence": {"text": "여성 회원을 찾아줘", "start": 0, "end": 10},
    }]}}

    repair = structurer._audience_repair_error(raw, {})
    assert repair and "subject.gender" in repair, (
        f"등록된 심볼을 지목한 미지원 주장이 그대로 종결됐다: {repair!r}"
    )


def test_unregistered_symbol_claims_are_still_honoured() -> None:
    """반대 방향 — 정말 등록되지 않은 심볼이면 재시도로 몰아붙이지 않는다.

    목표는 미지원을 줄이는 것이 아니라 **틀린** 미지원을 없애는 것이다.
    """
    from query_structurer import structurer

    raw = {"audience_requirement": {"expression": None, "issues": [{
        "code": "unsupported_semantics",
        "argument": "weather_forecast",
        "message": "not representable",
        "evidence": {"text": "비 오는 날 구매한 회원", "start": 0, "end": 11},
    }]}}
    assert structurer._audience_repair_error(raw, {}) is None
