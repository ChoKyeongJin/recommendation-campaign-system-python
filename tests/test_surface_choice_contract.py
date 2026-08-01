"""레지스트리 → 선택지 파생의 계약.

이 계층의 목적은 "새 등급·새 지표를 JSON 에 한 줄 넣으면 낱말을 안 써도 인식된다"이다. 그 성질은
코드를 읽어서는 확인되지 않고 **레지스트리를 실제로 바꿔 끼워야** 확인된다 — 그래서 여기서 임시
레지스트리를 물려 파생 결과를 본다.
"""

from __future__ import annotations

import json

import pytest

import lexicon_llm
import member_filters_config
import surface_choices


@pytest.fixture
def fresh_choices():
    surface_choices.invalidate()
    yield surface_choices
    surface_choices.invalidate()


def _swap_member_filters(monkeypatch, tmp_path, payload: dict) -> None:
    path = tmp_path / "member_target_filters.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(member_filters_config, "DEFAULT_PATH", path)
    surface_choices.invalidate()


def test_every_query_namespace_derives_something(fresh_choices):
    """빈 통과 방지 — 축이 조용히 () 로 강등되면 이 계층은 있으나 마나다."""
    for spec in surface_choices.NAMESPACES:
        if spec.substrate != "query":
            continue
        assert surface_choices.choice_set(spec.namespace), f"{spec.namespace} 파생이 비었다"


def test_derived_ids_never_collide_with_hand_written_concepts(fresh_choices):
    hand = {concept.concept_id for concept in lexicon_llm.load_concepts()}
    derived = {concept.concept_id for concept in surface_choices.query_concepts()}
    assert not (hand & derived)
    # 파생 id 는 전부 네임스페이스가 붙어 있어야 한다(손 개념과 구조적으로 겹칠 수 없게).
    assert all(lexicon_llm.split_namespace(cid)[0] for cid in derived)


def test_new_grade_needs_no_synonyms(monkeypatch, tmp_path):
    """**이 작업의 목적 그 자체** — synonyms 없이 등급을 추가해도 선택지가 생긴다."""
    _swap_member_filters(
        monkeypatch,
        tmp_path,
        {
            "eq_filters": [
                {
                    "canonical": "platinum_grade",
                    "category": "grade",
                    "column": "B.EMART_GRADE_CD",
                    "value": "MEM_GRADE_CD.PLATINUM",
                    "rank": 6,
                    "ko_label": "플래티넘",
                }
            ]
        },
    )
    choices = {choice.choice_id: choice for choice in surface_choices.choice_set("member_grade")}
    assert "platinum_grade" in choices
    assert choices["platinum_grade"].label == "플래티넘"
    assert "PLATINUM" in choices["platinum_grade"].hint


def test_label_falls_back_to_humanised_canonical(monkeypatch, tmp_path):
    """이름이 아무것도 없어도 파생은 멈추지 않는다(미검출이지 오류가 아니다)."""
    _swap_member_filters(
        monkeypatch,
        tmp_path,
        {"eq_filters": [{"canonical": "diamond_grade", "category": "grade", "value": "X.DIAMOND"}]},
    )
    choices = {c.choice_id: c for c in surface_choices.choice_set("member_grade")}
    assert choices["diamond_grade"].label == "diamond grade"


def test_derived_catalog_never_lists_synonyms(fresh_choices):
    """드리프트 가드 — 설명에 동의어를 나열하기 시작하면 낱말 목록이 이름만 바꿔 되돌아온다."""
    for concept in surface_choices.query_concepts():
        # label 은 이름 '하나'다. 쉼표/가운뎃점으로 이어 붙인 나열이면 동의어가 새어 들어온 것.
        label = concept.label
        assert "," not in label and "·" not in label, f"{concept.concept_id} label 이 나열이다: {label}"


def test_derived_catalog_leaks_no_physical_schema(fresh_choices):
    """프롬프트에 컬럼·테이블이 새면 DB 스왑 때 프롬프트까지 따라 고쳐야 한다."""
    blob = " ".join(
        f"{concept.label} {concept.description}" for concept in surface_choices.query_concepts()
    )
    for physical in ("B.", "CRM_", "MCS_", "EMART_GRADE_CD", "PAYMENT_AMT", "LAST_LOGIN_DATE"):
        assert physical not in blob, f"파생 카탈로그에 물리 이름 누출: {physical}"


def test_namespace_is_demoted_when_over_cap(monkeypatch, tmp_path):
    """상한을 넘으면 '일부만 보이는' 애매한 상태 대신 축 전체를 강등한다."""
    _swap_member_filters(
        monkeypatch,
        tmp_path,
        {
            "eq_filters": [
                {"canonical": f"g{i}", "category": "grade", "value": f"C.G{i}"} for i in range(200)
            ]
        },
    )
    assert surface_choices.choice_set("member_grade") == ()


def test_kill_switch_disables_one_axis_only(monkeypatch, fresh_choices):
    monkeypatch.setenv("SURFACE_CHOICE_LLM", "only=member_grade")
    surface_choices.invalidate()
    assert surface_choices.choice_set("member_grade")
    assert surface_choices.choice_set("analytics_scope") == ()


def test_kill_switch_off_disables_everything(monkeypatch, fresh_choices):
    monkeypatch.setenv("SURFACE_CHOICE_LLM", "off")
    surface_choices.invalidate()
    assert surface_choices.query_concepts() == ()


def test_structural_noise_is_not_empty():
    """비면 reject_evidence 가 무력해지고 구조 접두어가 아무 후보나 정당화한다."""
    assert surface_choices.structural_identifier_noise()
