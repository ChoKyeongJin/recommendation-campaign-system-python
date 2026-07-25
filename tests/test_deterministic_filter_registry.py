"""결정론 필터 파이프라인 레지스트리 계약(contract) 회귀.

배경: 새 타겟 조건의 정규식 파서 _apply_*_filter 를 규칙 경로(_build_rule_query_plan)와 LLM/auto 경로
(_build_single_query_plan) 두 곳에 손으로 나열해야 했다 — 한쪽을 빠뜨리면 "rules 는 되는데 auto 만 실패"가
났다(반복 사고). 이제 호출 방식은 _deterministic_filter_registry(단일 소스)가, 실행 순서는 경로별 리스트
(_RULES_PRE_FILTERS/_RULES_POST_FILTERS/_AUTO_FILTERS)가 소유한다. 아래 계약이 '경로 리스트 == spec.paths'와
'고아 없음'을 강제해, 새 필터를 spec 에만 넣고 경로 리스트에 안 넣으면(또는 그 반대면) 여기서 잡힌다.

실행: python -m pytest tests/test_deterministic_filter_registry.py -q
"""

from pathlib import Path

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
        elif spec.impl == "threshold":
            assert isinstance(spec.threshold, g._ThresholdMatcher), f"threshold '{name}' 의 matcher 누락"
            assert spec.slot, f"threshold '{name}' 의 slot 이 비어있음"
            assert spec.slot_on in {"target_user", "plan"}
        else:
            raise AssertionError(f"필터 '{name}' 의 impl 이 알 수 없음: {spec.impl}")


def test_threshold_filter_is_spec_only_no_wrapper():
    # 목표 검증: 신규 숫자형 임계 필터는 전용 _apply_* 함수 없이 _ThresholdSpec 등록만으로 동작한다.
    # (레지스트리 엔트리 = _FilterSpec(impl="threshold", threshold=<스펙 컴파일>, slot=...); 함수 0개.)
    spec = g._FilterSpec(
        impl="threshold",
        threshold=g._compile_threshold(g._ThresholdSpec("integer", r"포인트")),
        slot="points_threshold",
        gate="포인트",
    )
    plan = {"target_user": {}}
    g._dispatch_filter(spec, "적립 포인트 500 포인트 이상 회원", plan, None)
    assert plan["target_user"]["points_threshold"] == {"operator": ">=", "threshold": 500}
    # gate 불충족이면 미발동(오탐 방지).
    plan2 = {"target_user": {}}
    g._dispatch_filter(spec, "그냥 500 포인트 이상인가", plan2, None)  # '포인트' 있음 → 발동
    assert "points_threshold" in plan2["target_user"]
    plan3 = {"target_user": {}}
    g._dispatch_filter(g._FilterSpec(impl="threshold", threshold=spec.threshold, slot="x", gate="없는게이트"),
                       "500 포인트 이상", plan3, None)
    assert plan3["target_user"] == {}  # gate 불충족 → 슬롯 미세팅


def test_attribute_token_groups_reference_registered_canonicals():
    # 그룹의 canonical 은 MEMBER_EQ_FILTERS 에 있어야 실제로 승격된다(죽은 그룹 방지).
    for group, grammar in g._attribute_token_groups().items():
        assert isinstance(grammar, g._AttributeTokenGroup), f"그룹 '{group}' 이 선언형 스펙(_AttributeTokenGroup)이 아님"
        assert grammar.canonicals, f"attribute_token 그룹 '{group}' 의 canonicals 가 비어있음"
        for canonical, terms in grammar.canonicals:
            assert canonical in g.MEMBER_EQ_FILTERS, f"그룹 '{group}' 의 canonical '{canonical}' 이 eq_filters 에 없음"
            assert terms, f"그룹 '{group}' canonical '{canonical}' 의 기본 표면어가 비어있음"


# 속성 토큰 클러스터의 선언형/예외 분류(단일 소스). 새 속성형 필터는 반드시 둘 중 하나로 분류돼야 하며,
# 미분류·오분류는 test_attribute_token_cluster_classification_is_enforced 가 잡는다.
_DECLARATIVE_ATTRIBUTE = {"member_flag", "children_registered", "signup_device"}
_EXCEPTION_ATTRIBUTE = {"grade_threshold", "channel_consent", "signup_channel"}


def test_attribute_token_cluster_classification_is_enforced():
    # 요구사항 강제: 속성 토큰 클러스터 소속 필터는 '선언형(impl=attribute_token)'이거나 '사유 있는 예외
    # (impl=custom + family=attribute_token + exception_reason)'여야 한다. 새 필터를 추가하면 아래 두 집합 중
    # 하나에 넣도록 강제돼(안 넣으면 실패) 선언형/예외 선택이 의식적 결정이 된다.
    reg = _registry()
    cluster = {n for n, s in reg.items() if s.impl == "attribute_token" or s.family == "attribute_token"}
    expected = _DECLARATIVE_ATTRIBUTE | _EXCEPTION_ATTRIBUTE
    assert cluster == expected, (
        f"속성 토큰 클러스터 미분류/이탈: {cluster ^ expected} — 새 속성형 필터는 선언형(impl=attribute_token) "
        "또는 예외(family='attribute_token'+exception_reason)로 분류하고 이 테스트의 집합에 등록해야 함"
    )
    groups = g._attribute_token_groups()
    for name in _DECLARATIVE_ATTRIBUTE:
        s = reg[name]
        assert s.impl == "attribute_token", f"선언형이어야 할 '{name}' 의 impl 이 '{s.impl}' — 전용 함수로 회귀했는지 확인"
        assert s.apply is None, f"선언형 '{name}' 은 전용 _apply_* 함수를 두면 안 됨(스펙만으로 동작)"
        assert s.group in groups, f"선언형 '{name}' 의 group '{s.group}' 미등록"
    for name in _EXCEPTION_ATTRIBUTE:
        s = reg[name]
        assert s.impl == "custom" and callable(s.apply), f"예외 '{name}' 은 커스텀 _apply_* 여야 함"
        assert s.family == "attribute_token", f"예외 '{name}' 은 family='attribute_token' 태그 필수"
        assert s.exception_reason, f"예외 '{name}' 은 exception_reason(공통화 불가 사유) 명시 필수"


def test_attribute_token_groups_declared_in_json():
    # 선언 외부화 회귀: 그룹 스펙이 attribute_token_groups.json 에서 실제로 읽힌다(코드 폴백이 아니라 파일).
    assert g.DEFAULT_ATTRIBUTE_TOKEN_GROUPS_PATH.exists(), "attribute_token_groups.json 선언 파일이 없음"
    raw = g._load_attribute_token_groups_raw()
    assert set(raw) == {"member_flag", "children", "signup_device"}, "JSON 선언에서 그룹을 읽지 못함"
    # 파일 선언과 코드 폴백이 동일 스펙을 만든다(외부화가 동작을 바꾸지 않음).
    from_json = g._attribute_token_groups()
    from_code = {n: g._coerce_attribute_token_group(r) for n, r in g._default_attribute_token_groups_raw().items()}
    assert from_json == from_code, "JSON 선언과 코드 폴백의 그룹 스펙이 어긋남"


def test_attribute_token_groups_fallback_on_missing_file():
    # 파일 부재/파손 시 코드 폴백으로 안전하게 동작한다(동작 불변 보장).
    fallback = g._load_attribute_token_groups_raw(Path("__no_such_attribute_token_groups__.json"))
    assert fallback == g._default_attribute_token_groups_raw()
    assert "member_flag" in fallback and fallback["member_flag"]["canonicals"]


def test_new_attribute_filter_needs_only_a_spec():
    # 데이터-구동 증명: 전용 _apply_* 함수 없이 _AttributeTokenGroup 스펙만으로 표면어 승격이 동작한다
    # (신규 단순 속성형 필터 = 스펙 등록만). JSON surface_terms 간섭을 피하려 그것이 없는 실제 canonical 사용.
    canonical = next(c for c in g.MEMBER_EQ_FILTERS if c not in g._MEMBER_SURFACE_TERMS)
    spec = g._AttributeTokenGroup(canonicals=((canonical, ("좀비합성표면어",)),))
    plan = {"target_user": {"lifecycle": []}, "exclude": {"lifecycle": []}}
    g._run_attribute_token(spec, "좀비합성표면어 회원만 뽑아줘", plan)
    assert canonical in plan["target_user"]["lifecycle"], "스펙만으로 표면어→lifecycle 승격이 동작해야 함"
    # 부정 문법도 스펙 필드로 선언 가능(→ exclude.lifecycle).
    neg_spec = g._AttributeTokenGroup(canonicals=((canonical, ("좀비합성표면어",)),), neg=r"제외")
    neg_plan = {"target_user": {"lifecycle": []}, "exclude": {"lifecycle": []}}
    g._run_attribute_token(neg_spec, "좀비합성표면어 제외하고", neg_plan)
    assert canonical in neg_plan["exclude"]["lifecycle"]


def test_surface_terms_externalized_to_json():
    # 하드코딩 맵의 eq_filters JSON 외부화 회귀: surface_terms 가 config 에서 실제로 읽힌다.
    surface = g._MEMBER_SURFACE_TERMS
    assert surface, "eq_filters JSON 에서 surface_terms 를 하나도 읽지 못함 — 외부화 경로 끊김"
    assert "active_member" in surface
    # 표면어는 그룹 선언 + surface_terms 의 합집합(어느 쪽에 넣어도 매칭; 한쪽이 가리지 않음).
    merged = g._attribute_terms("active_member", ("그룹선언전용표면어",))
    assert "그룹선언전용표면어" in merged, "그룹 선언 표면어가 surface_terms 에 가려지면 안 됨"
    assert all(t in merged for t in surface["active_member"]), "surface_terms 표면어가 합집합에 포함돼야 함"
    # surface_terms 가 없는 canonical 은 그룹 선언(default) 그대로.
    assert g._attribute_terms("__missing_canonical__", ("폴백",)) == ("폴백",)
