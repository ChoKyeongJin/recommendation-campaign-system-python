"""가입 채널(온라인/오프라인 매장) 타겟 회귀.

배경: '온라인 가입'·'오프라인 매장 가입'에서 '온라인'/'오프라인' 단독 토큰이 정규화 사전에서
구매 채널(online_buyer/offline_buyer)로 먼저 매칭돼 '가입' 문맥을 삼켰다. 이 buyer canonical 은
회원 테이블(CRM_MB_BASEINFO)로 표현할 수 없어 조용히 탈락 → SQL 이 통째로 None 이 됐다.

고정 내용:
  - 실컬럼은 online_signup eq_filter(REG_OFFSHOP_ID='O'; 'O'=온라인/몰 가입, 그 외=오프라인 매장 가입).
  - 온라인 가입 → REG_OFFSHOP_ID = 'O'(include), 온라인 가입 안함 → <> 'O'(exclude).
  - 오프라인 매장 가입 → <> 'O'(exclude; 오프라인=온라인 아님), 오프라인 가입 안함 → = 'O'(이중부정 include).
  - '가입' 문맥이 채널어에 붙은 경우만 발동 — 순수 구매 채널('온라인 구매')은 건드리지 않는다.
  - 컬럼/값 소유: member_target_filters.json eq_filters signup_store 카테고리(online_signup).

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_signup_channel_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _reg_preds(query: str) -> list[str]:
    comp = g.compile_member_target_conditions(_plan(query))
    return [p for p in comp["predicates"] if "REG_OFFSHOP_ID" in p]


def test_online_signup_include():
    plan = _plan("온라인 가입 회원")
    assert "online_signup" in plan["target_user"]["lifecycle"]
    assert _reg_preds("온라인 가입 회원") == ["B.REG_OFFSHOP_ID = 'O'"]


def test_offline_store_signup_is_exclude():
    plan = _plan("오프라인 매장에서 가입한 회원")
    assert "online_signup" in plan["exclude"]["lifecycle"]
    assert _reg_preds("오프라인 매장에서 가입한 회원") == ["B.REG_OFFSHOP_ID <> 'O'"]


def test_online_signup_negated_is_exclude():
    assert _reg_preds("온라인으로 가입하지 않은 회원") == ["B.REG_OFFSHOP_ID <> 'O'"]


def test_offline_signup_double_negative_is_online():
    # '온라인 가입 + 오프라인 매장 가입 안 함' → 둘 다 온라인 가입으로 귀결(= 'O').
    q = "온라인 가입 회원 중 오프라인 매장에서 가입하지 않은 회원만 보여줘."
    plan = _plan(q)
    assert plan["target_user"]["lifecycle"] == ["online_signup"]
    assert plan["exclude"]["lifecycle"] == []
    cand = g.build_member_targets_sql_candidate(plan)
    assert cand is not None  # 이전엔 buyer 오분류로 None 이었다.
    assert "B.REG_OFFSHOP_ID = 'O'" in cand["sql"]
    assert not cand.get("dropped_conditions")


def test_bare_store_signup_is_offline():
    assert _reg_preds("매장 가입 회원") == ["B.REG_OFFSHOP_ID <> 'O'"]


def test_pure_purchase_channel_not_promoted():
    # '가입' 문맥이 없으면 가입 채널 조건을 만들지 않는다(순수 구매 채널).
    plan = _plan("온라인 구매 고객")
    assert "online_signup" not in plan["target_user"]["lifecycle"]
    assert "online_signup" not in plan["exclude"]["lifecycle"]
    assert _reg_preds("온라인 구매 고객") == []
