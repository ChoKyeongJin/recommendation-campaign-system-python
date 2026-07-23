"""활동회원(ACTIVITY_MEMBER_YN)·블랙리스트(BLACKLIST_YN) 회원 Y/N 플래그 타겟 회귀.

배경: "블랙리스트가 아니면서 활동회원인 사람만 조회해줘" 가 조건 0개로 SQL 생성에 실패했다.
active_member / blacklisted 매핑이 boolean_filters 섹션에만 있어(코드는 eq_filters 만 파싱) 죽어 있었고,
재작성 게이트가 회원 플래그를 보호하지 않아 targeting_label 에서 '블랙리스트가 아니면서'가 조용히 빠졌다.

고정 내용:
  - active_member(activity, B.ACTIVITY_MEMBER_YN, 'Y') / blacklisted(exclusion, B.BLACKLIST_YN, 'Y')
    를 eq_filters 로 승격 → 컴파일 가능.
  - '활동회원' → ACTIVITY_MEMBER_YN = 'Y'(include), '블랙리스트가 아니면서/제외' → BLACKLIST_YN <> 'Y'.
  - 재작성 게이트(_rewrite_dropped_signals)가 회원 플래그(극성 포함)를 서명해 소실/뒤집힘을 잡는다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_member_flag_targeting.py -q
"""

import graph_rag as g


def _plan(query: str) -> dict:
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    return plan


def _preds(query: str, needle: str) -> list[str]:
    comp = g.compile_member_target_conditions(_plan(query))
    return [p for p in comp["predicates"] if needle in p]


def test_member_flag_eq_filters_exist():
    assert g.MEMBER_EQ_FILTERS.get("active_member") == ("activity", "B.ACTIVITY_MEMBER_YN", "Y")
    assert g.MEMBER_EQ_FILTERS.get("blacklisted") == ("exclusion", "B.BLACKLIST_YN", "Y")


def test_active_member_include():
    plan = _plan("활동회원인 사람만 조회해줘")
    assert "active_member" in plan["target_user"]["lifecycle"]
    assert _preds("활동회원인 사람만 조회해줘", "ACTIVITY_MEMBER_YN") == ["B.ACTIVITY_MEMBER_YN = 'Y'"]


def test_blacklist_exclude():
    plan = _plan("블랙리스트가 아닌 회원만 조회해줘")
    assert "blacklisted" in plan["exclude"]["lifecycle"]
    assert _preds("블랙리스트가 아닌 회원만 조회해줘", "BLACKLIST_YN") == ["B.BLACKLIST_YN <> 'Y'"]
    assert _preds("블랙리스트 제외한 회원", "BLACKLIST_YN") == ["B.BLACKLIST_YN <> 'Y'"]


def test_reported_query_builds_both_conditions():
    # 실제 실패 케이스: 두 조건이 모두 SQL 로 잡혀야 하고 has_signal 이 서야 한다.
    query = "블랙리스트가 아니면서 활동회원인 사람만 조회해줘."
    comp = g.compile_member_target_conditions(_plan(query))
    assert comp["has_signal"] is True
    assert "B.ACTIVITY_MEMBER_YN = 'Y'" in comp["predicates"]
    assert "B.BLACKLIST_YN <> 'Y'" in comp["predicates"]
    cand = g.build_member_targets_sql_candidate(_plan(query))
    assert cand is not None
    assert "B.ACTIVITY_MEMBER_YN = 'Y'" in cand["sql"]
    assert "B.BLACKLIST_YN <> 'Y'" in cand["sql"]


def test_blacklist_positive_targets_flag():
    # 드물지만 '블랙리스트 회원만' 은 긍정(포함)으로 본다.
    assert _preds("블랙리스트 회원만 조회", "BLACKLIST_YN") == ["B.BLACKLIST_YN = 'Y'"]


def test_rewrite_guard_flags_dropped_blacklist():
    # 재작성이 '블랙리스트가 아니면서'를 지우면 게이트가 소실로 잡아야 한다.
    original = "블랙리스트가 아니면서 활동회원인 사람만 조회해줘"
    dropped = g._rewrite_dropped_signals(original, "활동 회원")
    assert any("블랙리스트" in d for d in dropped)
    # 조건이 모두 남아있으면 소실 없음.
    assert g._rewrite_dropped_signals(original, "블랙리스트가 아닌 활동 회원") == []


def test_non_flag_mention_not_promoted():
    # '활동' 이 회원 플래그 문맥이 아니면 조건을 만들지 않는다.
    plan = _plan("최근 구매 활동 내역이 있는 회원")
    assert "active_member" not in plan["target_user"].get("lifecycle", [])
