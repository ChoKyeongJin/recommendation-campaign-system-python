"""결정론 필터 파이프라인 레지스트리 계약(contract) 회귀.

배경: 새 타겟 조건의 정규식 파서 _apply_*_filter 를 규칙 경로(_build_rule_query_plan)와 LLM/auto 경로
(_build_single_query_plan) 두 곳에 손으로 나열해야 했다 — 한쪽을 빠뜨리면 "rules 는 되는데 auto 만 실패"가
났다(반복 사고). 이제 호출 방식은 _deterministic_filter_registry(단일 소스)가, 실행 순서는 경로별 리스트
(_RULES_PRE_FILTERS/_RULES_POST_FILTERS/_AUTO_FILTERS)가 소유한다. 아래 계약이 '경로 리스트 == spec.paths'와
'고아 없음'을 강제해, 새 필터를 spec 에만 넣고 경로 리스트에 안 넣으면(또는 그 반대면) 여기서 잡힌다.

실행: python -m pytest tests/test_deterministic_filter_registry.py -q
"""

import graph_rag as g


def _registry() -> dict:
    return g._deterministic_filter_registry()


def test_every_pipeline_name_is_registered():
    registry = _registry()
    for name in (*g._RULES_PRE_FILTERS, *g._RULES_POST_FILTERS, *g._AUTO_FILTERS):
        assert name in registry, f"파이프라인이 미등록 필터 '{name}' 참조 — 레지스트리에 spec 추가 필요"


def test_no_duplicate_within_a_path():
    rules = (*g._RULES_PRE_FILTERS, *g._RULES_POST_FILTERS)
    assert len(rules) == len(set(rules)), "규칙 경로에 중복 필터(같은 이름 두 번 실행)"
    assert len(g._AUTO_FILTERS) == len(set(g._AUTO_FILTERS)), "auto 경로에 중복 필터"


def test_rules_list_matches_registry_membership():
    # spec.paths 에 'rules' 를 단 필터는 정확히 규칙 경로 리스트에 있어야 한다(등록 누락/고아 동시 차단).
    declared = {name for name, spec in _registry().items() if "rules" in spec.paths}
    listed = {*g._RULES_PRE_FILTERS, *g._RULES_POST_FILTERS}
    assert declared == listed, (
        f"규칙 경로 불일치 — 리스트에 없는 spec: {declared - listed}, spec 없는 리스트 항목: {listed - declared}"
    )


def test_auto_list_matches_registry_membership():
    declared = {name for name, spec in _registry().items() if "auto" in spec.paths}
    listed = set(g._AUTO_FILTERS)
    assert declared == listed, (
        f"auto 경로 불일치 — 리스트에 없는 spec: {declared - listed}, spec 없는 리스트 항목: {listed - declared}"
    )


def test_no_orphan_registry_entry():
    # 어느 경로에도 안 붙은 필터는 죽은 코드(호출되지 않음) — paths 는 비어있으면 안 된다.
    for name, spec in _registry().items():
        assert spec.paths, f"필터 '{name}' 의 paths 가 비어 어느 경로에서도 실행되지 않음"
        assert spec.paths <= {"rules", "auto"}, f"필터 '{name}' 이 알 수 없는 경로 참조: {spec.paths}"


def test_impl_specs_are_well_formed():
    # impl 별로 필요한 필드가 채워졌는지 — custom=apply 콜러블, slot_setter=detect 콜러블+slot,
    # attribute_token=group 이 _attribute_token_groups 에 존재.
    groups = g._attribute_token_groups()
    for name, spec in _registry().items():
        if spec.impl == "custom":
            assert callable(spec.apply), f"custom 필터 '{name}' 의 apply 가 호출 가능하지 않음"
        elif spec.impl == "slot_setter":
            assert callable(spec.detect), f"slot_setter '{name}' 의 detect 가 호출 가능하지 않음"
            assert spec.slot, f"slot_setter '{name}' 의 slot 이 비어있음"
            assert spec.mode in {"set", "append"}, f"slot_setter '{name}' 의 mode 가 잘못됨: {spec.mode}"
            assert spec.slot_on in {"target_user", "plan"}
        elif spec.impl == "attribute_token":
            assert spec.group in groups, f"attribute_token '{name}' 의 group '{spec.group}' 미등록"
        else:
            raise AssertionError(f"필터 '{name}' 의 impl 이 알 수 없음: {spec.impl}")


def test_attribute_token_groups_reference_registered_canonicals():
    # 그룹의 canonical 은 MEMBER_EQ_FILTERS 에 있어야 실제로 승격된다(죽은 그룹 방지).
    for group, grammar in g._attribute_token_groups().items():
        assert grammar["canonicals"], f"attribute_token 그룹 '{group}' 의 canonicals 가 비어있음"
        for canonical, terms in grammar["canonicals"]:
            assert canonical in g.MEMBER_EQ_FILTERS, f"그룹 '{group}' 의 canonical '{canonical}' 이 eq_filters 에 없음"
            assert terms, f"그룹 '{group}' canonical '{canonical}' 의 기본 표면어가 비어있음"


def test_surface_terms_externalized_to_json():
    # 하드코딩 맵의 eq_filters JSON 외부화 회귀: surface_terms 가 config 에서 실제로 읽혀 코드 기본값을 덮는다.
    surface = g._MEMBER_SURFACE_TERMS
    assert surface, "eq_filters JSON 에서 surface_terms 를 하나도 읽지 못함 — 외부화 경로 끊김"
    # JSON 에 있으면 _attribute_terms 는 코드 기본값이 아니라 JSON 값을 돌려준다.
    assert "active_member" in surface
    assert g._attribute_terms("active_member", ("코드기본값과다름",)) == surface["active_member"]
    # JSON 에 없는 canonical 은 코드 기본값으로 폴백한다(동작 불변 보장).
    assert g._attribute_terms("__missing_canonical__", ("폴백",)) == ("폴백",)
