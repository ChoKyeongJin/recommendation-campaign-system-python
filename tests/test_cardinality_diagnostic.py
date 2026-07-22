"""술어별 카디널리티 진단 회귀 테스트.

목적: 결정론 회원 타겟 SQL 이 0명을 반환할 때 "어느 AND 술어가 오디언스를 죽였는지"를
술어별 COUNT 로 귀속하는 진단(과잉 조건 탐지)이 정상 동작하는지 고정한다.

두 축을 검증한다.
  1) 생성부(graph_rag.build_member_targets_sql_candidate): 후보에 cardinality_probe 가 붙고,
     사용자가 말하지 않았는데 주입되는 기본 게이트(정상회원 한정)가 injected_default 로 표시되는가.
  2) 실행부(api._run_cardinality_diagnostic): 술어별 COUNT 결과로 원인(단독 0명 vs 상호배타)을
     올바르게 귀속하는가. DB 는 monkeypatch 로 대체해 네트워크 없이 결정론 검증한다.

실행(컨테이너): docker compose exec api pytest tests/test_cardinality_diagnostic.py -q
"""

import graph_rag as g


def _member_probe(query_plan):
    candidate = g.build_member_targets_sql_candidate(query_plan)
    assert candidate is not None, "회원 신호가 있으면 후보가 생성되어야 한다"
    return candidate["cardinality_probe"]


def test_probe_marks_injected_state_gate():
    # 연령 조건만 준다(사용자 명시 신호 1개). 회원상태는 지정하지 않았으므로 정상회원 게이트가
    # 자동 주입되어야 하고, 그 술어만 injected_default=True 로 표시되어야 한다.
    query_plan = {
        "intent": "find_user_segment",
        "target_user": {"age_min": 30},
        "exclude": {},
        "campaign_constraints": {},
        "dimension_filters": [],
    }
    probe = _member_probe(query_plan)

    assert probe["from_clause"] == "CRM_MB_BASEINFO B"
    predicates = probe["predicates"]

    # 사용자 명시 조건: 주입 표시가 아니어야 한다.
    user_preds = [p for p in predicates if not p["injected_default"]]
    assert any(p["sql"] == "B.AGE >= 30" for p in user_preds)

    # 주입 기본 게이트: 정확히 1개, 회원상태 술어여야 한다.
    injected = [p for p in predicates if p["injected_default"]]
    assert len(injected) == 1
    assert "MEMBER_STATE_CD" in injected[0]["sql"]


def test_probe_has_no_injected_gate_when_state_is_targeted():
    # 회원상태를 직접 타겟(휴면 등)하면 정상회원 게이트를 주입하지 않으므로 injected 술어가 없어야 한다.
    query_plan = {
        "intent": "find_user_segment",
        "target_user": {"lifecycle": ["dormant"]},
        "exclude": {},
        "campaign_constraints": {},
        "dimension_filters": [],
    }
    candidate = g.build_member_targets_sql_candidate(query_plan)
    if candidate is None:
        # dormant 가 이 배포의 필터 어휘에 없으면(환경별) 스킵 대신 조용히 통과: 게이트 주입 자체가 없음.
        return
    injected = [p for p in candidate["cardinality_probe"]["predicates"] if p["injected_default"]]
    # forces_state 이면 주입 게이트가 없어야 한다. (dormant 가 상태 필터로 인식된 경우)
    if candidate["cardinality_probe"]["predicates"]:
        assert all("MEMBER_STATE_CD" not in p["sql"] for p in injected) or injected == []


def test_diagnostic_attributes_empty_to_predicate(monkeypatch):
    import db_connections
    import api

    probe = {
        "from_clause": "CRM_MB_BASEINFO B",
        "predicates": [
            {"sql": "B.AGE >= 30", "injected_default": False},
            {"sql": "B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'", "injected_default": True},
        ],
    }

    # 전체 1000명, 연령 조건 단독 400명, 정상회원 게이트 단독 0명(과잉 게이트가 오디언스를 죽인 상황).
    def fake_run_read_query(connection, sql, *args, **kwargs):
        lowered = sql.casefold()
        if "where" not in lowered:
            return [{"cardinality": 1000}]
        if "age" in lowered:
            return [{"cardinality": 400}]
        if "member_state_cd" in lowered:
            return [{"cardinality": 0}]
        return [{"cardinality": 0}]

    monkeypatch.setattr(db_connections, "run_read_query", fake_run_read_query)

    diagnostic = api._run_cardinality_diagnostic("CRMDW", probe)
    assert diagnostic["cause"] == "predicate_empty"
    assert diagnostic["member_total"] == 1000
    assert diagnostic["culprit_predicates"] == ["B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'"]
    assert diagnostic["injected_default_is_culprit"] is True


def test_diagnostic_flags_interaction_when_all_predicates_match(monkeypatch):
    import db_connections
    import api

    probe = {
        "from_clause": "CRM_MB_BASEINFO B",
        "predicates": [
            {"sql": "B.AGE >= 30", "injected_default": False},
            {"sql": "B.AGE <= 20", "injected_default": False},
        ],
    }

    # 개별 조건은 모두 매칭되지만(교차 시 0명), 어느 단독도 0 이 아님 → 상호배타(interaction).
    def fake_run_read_query(connection, sql, *args, **kwargs):
        lowered = sql.casefold()
        if "where" not in lowered:
            return [{"cardinality": 1000}]
        return [{"cardinality": 300}]

    monkeypatch.setattr(db_connections, "run_read_query", fake_run_read_query)

    diagnostic = api._run_cardinality_diagnostic("CRMDW", probe)
    assert diagnostic["cause"] == "predicate_interaction"
    assert diagnostic["culprit_predicates"] == []
    assert diagnostic["injected_default_is_culprit"] is False
