"""집계 지표 보정·나열형 제외 극성·의미검증 면제의 범용 동작 회귀.

배경: '반품금액 차감', '마케팅 활용에 동의하지 않은 회원은 제외', '블랙리스트·휴면·탈퇴 … 제외',
'총액·평균을 함께 산출' 같은 표현이 한 프롬프트에서 동시에 나오면서 (1) 보정이 지표에 반영되지 않고
(2) 나열 앞 항목의 제외 극성이 뒤집히지 않고 (3) 출력 컬럼 요구·상수 라벨 프로젝션이 의미검증에서
불일치로 잡혀 SQL 이 통째로 차단됐다. 세 문제 모두 케이스별 특수 분기가 아니라 범용 규칙으로 고쳤으므로,
'다른 지표·다른 표면어에도 같은 규칙이 적용되는가'를 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_metric_adjustment_and_exclusion_scope.py -q
"""

import graph_rag as g


# ── ① 지표 보정(adjustment): 설정 선언 하나가 그 컬럼을 쓰는 모든 지표에 적용된다 ──────────────
def test_adjustment_applies_to_every_metric_using_the_substituted_column():
    metrics = g._aggregate_targets_config()["metrics"]
    # 결제금액을 집계하는 지표는 agg+column 형(누적 금액)이든 집계식 형(평균 주문 금액)이든 모두 보정된다.
    for metric_id in ("purchase_amount", "average_order_amount"):
        adjusted, applied = g._adjusted_metric(metrics[metric_id], ["net_of_returns"])
        assert applied == ["net_of_returns"], metric_id
        assert "RETURN_AMT" in adjusted["expression"], metric_id
        assert "PAYMENT_AMT" in adjusted["expression"], metric_id
    # 결제금액을 쓰지 않는 지표(주문 건수/할인 금액/상품 종수)는 같은 보정에 영향받지 않는다.
    for metric_id in ("order_count", "discount_amount", "distinct_product_count"):
        adjusted, applied = g._adjusted_metric(metrics[metric_id], ["net_of_returns"])
        assert applied == [], metric_id
        assert adjusted is metrics[metric_id], metric_id


def test_adjustment_trigger_is_detected_across_clause_distance():
    # 보정 선언은 임계 조건과 다른 문장에 있는 게 보통이다(설정의 근접 정규식이 소유).
    for query in (
        "반품금액이 있는 경우 총결제금액에서 차감해줘",
        "환불 금액은 제외하고 집계해줘",
        "순결제금액 기준으로 계산해줘",
    ):
        assert g._detect_aggregate_adjustments(query) == ["net_of_returns"], query
    assert g._detect_aggregate_adjustments("최근 90일간 30만원 이상 구매한 회원") == []


def test_adjusted_metrics_compile_into_aggregate_sql():
    query = (
        "최근 90일간 주문한 회원 중 주문 건수가 3건 이상이고 총결제금액이 300,000원 이상이며 "
        "평균 결제금액이 70,000원 이상인 고객을 타겟팅해줘. 반품금액이 있는 경우 총결제금액에서 차감해줘."
    )
    plan = g.build_query_plan(query, parser="rules")
    g._promote_unknown_intent_for_target_signal(plan)
    sql = g.build_aggregate_targets_sql_candidate(plan)["sql"]
    # 금액 임계 두 개(합계·평균)는 반품 차감식으로, 건수 임계는 원래대로 컴파일된다.
    assert sql.count("RETURN_AMT") == 2
    assert "COUNT(DISTINCT ORDER_ID) >= 3" in sql
    assert "SUM((COALESCE(PAYMENT_AMT, 0) - COALESCE(RETURN_AMT, 0))) >= 300000" in sql


# ── ② 이중부정·나열형 제외: 극성은 '표면어 극성 × 제외 여부'로 결정된다 ──────────────────────
def test_consent_double_negation_resolves_to_optin():
    # '동의하지 않은 회원은 제외' = 동의자만 남긴다. 채널 4종 모두 같은 문법을 공유한다.
    assert g._consent_context_signals("마케팅 활용에 동의하지 않았거나 블랙리스트 상태인 회원은 제외해줘") == {
        "marketing_optin": "+"
    }
    assert g._consent_context_signals("SMS 수신 거부한 회원은 제외해줘") == {"sms_optin": "+"}
    # 대칭: '동의한 회원은 제외'는 미동의자.
    assert g._consent_context_signals("이메일 수신 동의한 회원은 제외") == {"email_optin": "-"}
    # 제외 문맥이 없으면 종전대로.
    assert g._consent_context_signals("앱푸시 수신 동의한 회원") == {"app_push_optin": "+"}
    # 발송 문맥('보내지 말고')은 제외 꼬리로 오인하지 않는다.
    assert g._consent_context_signals("SMS로 보내지 말고 앱푸시 수신 동의 회원에게") == {"app_push_optin": "+"}


def test_enumerated_exclusion_flips_every_listed_term():
    # 나열형 제외는 마지막 항목뿐 아니라 나열 전체가 제외 대상이다(속성 토큰 경로).
    plan: dict = {}
    g._run_attribute_token(g._attribute_token_groups()["member_flag"], "블랙리스트·휴면·탈퇴 상태인 회원은 제외해줘", plan)
    assert plan["exclude"]["lifecycle"] == ["blacklisted"]
    assert not plan.get("target_user", {}).get("lifecycle")

    plan = {}
    g._run_attribute_token(g._attribute_token_groups()["member_flag"], "프리미엄회원과 임직원은 제외하고 발송", plan)
    assert set(plan["exclude"]["lifecycle"]) == {"premium_member", "employee"}

    # 정규화 사전 경로(휴면 → inactive_90d)도 같은 규칙을 쓴다.
    excluded = g.build_query_plan("휴면·탈퇴 회원은 제외", parser="rules")
    assert "inactive_90d" in excluded["exclude"]["lifecycle"]
    assert "inactive_90d" not in excluded["target_user"]["lifecycle"]


def test_enumerated_exclusion_does_not_swallow_other_clauses():
    # 나열 구분자 없이 뒤쪽 절에 있는 '제외'까지 삼키면 안 된다(과포획 회귀).
    plan: dict = {}
    g._run_attribute_token(g._attribute_token_groups()["member_flag"], "활동회원 중 최근 미구매 회원은 제외", plan)
    assert plan["target_user"]["lifecycle"] == ["active_member"]
    assert not plan.get("exclude", {}).get("lifecycle")
    # 긍정 문맥은 그대로 포함 조건.
    assert "inactive_90d" in g.build_query_plan("휴면 회원에게 재방문 캠페인", parser="rules")["target_user"]["lifecycle"]


# ── ③ 의미검증 면제: 행 집합과 무관함이 결정론으로 확인될 때만 차단에서 뺀다 ────────────────
_LABELED_SQL = (
    "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, 'active_member,구매 횟수' AS segment_label, 'repurchase' AS objective\n"
    "FROM CRM_MB_BASEINFO B\n"
    "WHERE B.AGREE_YN = 'Y' AND B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'"
)


def test_presentation_only_requirement_is_exempt():
    assert g._semantic_issue_exemption(
        {"type": "dropped", "condition": "총결제금액·평균결제금액·최종주문일을 함께 산출",
         "detail": "SELECT 절에 집계 컬럼이 없음"}, _LABELED_SQL) == "presentation_only_requirement"


def test_campaign_creation_request_is_exempt():
    # 캠페인·타겟리스트 생성은 이 SQL 다음 단계의 일이라 오디언스 조건이 아니다.
    assert g._semantic_issue_exemption(
        {"type": "dropped", "condition": "추출된 회원으로 재구매 캠페인을 생성하고 대상 셀 이름 설정",
         "detail": "SQL 에 캠페인 생성 로직이 없음"}, _LABELED_SQL) == "post_processing_request"
    # 캠페인 '반응' 오디언스 조건은 면제 대상이 아니다(생성 요청이 아니라 필터).
    assert g._semantic_issue_exemption(
        {"type": "dropped", "condition": "최근 3개월 캠페인에 반응한 회원",
         "detail": "캠페인 반응 팩트 조인이 없음"}, _LABELED_SQL) is None


def test_threshold_condition_is_never_exempt_even_with_presentation_words():
    # 같은 지표라도 임계 조건('이상')이면 필터이므로 면제하지 않는다.
    assert g._semantic_issue_exemption(
        {"type": "dropped", "condition": "총결제금액이 300,000원 이상인 고객만 보여줘",
         "detail": "금액 임계 조건이 SQL 에 없음"}, _LABELED_SQL) is None


def test_constant_projection_label_spurious_is_exempt_but_real_filter_is_not():
    assert g._semantic_issue_exemption(
        {"type": "spurious", "condition": "segment_label 문구",
         "detail": "SELECT 에 'active_member,구매 횟수' 라벨이 있는데 원문엔 없음"}, _LABELED_SQL) == "constant_projection_label"
    # 필터 컬럼을 지목한 spurious 는 면제되지 않는다(값 리터럴 'MEMBER_STATE_CD.NORMAL' 은 컬럼이 아니다).
    assert g._semantic_issue_exemption(
        {"type": "spurious", "condition": "AGREE_YN 필터",
         "detail": "원문에 없는 AGREE_YN 조건이 WHERE 에 있고 segment_label 도 붙음"}, _LABELED_SQL) is None
    # 극성 판정(inverted)·값 판정(wrong_value)은 면제 대상이 아니다.
    for issue_type in ("inverted", "wrong_value"):
        assert g._semantic_issue_exemption(
            {"type": issue_type, "condition": "segment_label", "detail": "라벨"}, _LABELED_SQL) is None


def test_adjustment_present_in_sql_refutes_dropped_verdict():
    # 보정은 컬럼 산술로 인코딩돼 판정 모델이 자주 오독한다. 설정이 선언한 구성 컬럼이 SQL 에 다 있으면
    # 그 요구는 반영된 것이므로 차단하지 않는다(설정에 보정을 추가하면 자동 적용).
    with_deduction = "SELECT B.MEMBER_NO AS CUST_ID FROM CRM_MB_BASEINFO B WHERE B.MEMBER_NO IN (SELECT MEMBER_NO FROM CRM_SL_ORDERHEADERMALL GROUP BY MEMBER_NO HAVING SUM((COALESCE(PAYMENT_AMT, 0) - COALESCE(RETURN_AMT, 0))) >= 300000)"
    without_deduction = with_deduction.replace("(COALESCE(PAYMENT_AMT, 0) - COALESCE(RETURN_AMT, 0))", "PAYMENT_AMT")
    issue = {"type": "dropped", "condition": "반품금액이 있는 경우 총결제금액에서 차감",
             "detail": "SQL 집계는 PAYMENT_AMT 합계만 쓰고 반품 차감이 없다"}
    assert g._semantic_issue_exemption(issue, with_deduction) == "adjustment_present_in_sql"
    assert g._semantic_issue_exemption(issue, without_deduction) is None
    # 보정과 무관한 dropped 는 영향받지 않는다.
    assert g._semantic_issue_exemption(
        {"type": "dropped", "condition": "VIP 등급", "detail": "등급 필터 없음"}, with_deduction) is None


# ── ④ 창·보정 소실 복원과 스코프 분리 ─────────────────────────────────────────────────
def test_leading_window_clause_is_shared_by_following_metric_clauses():
    def windows(query):
        plan: dict = {"target_user": {}}
        g._apply_named_filter("aggregate", query, plan)
        return [(c["metric_id"], c.get("window_days")) for c in plan["target_user"].get("aggregate_conditions", [])]

    # 문장 선행 창은 뒤따르는 구매 지표 절들의 공유 창이 된다.
    assert windows("최근 90일간 주문한 회원 중 주문 건수가 3건 이상이고 총결제금액이 300,000원 이상") == [
        ("order_count", 90), ("purchase_amount", 90)]
    # 다른 도메인(로그인/캠페인)의 창은 상속되지 않는다 — 도메인 누수 방지.
    assert windows("최근 30일간 접속하지 않은 회원 중 누적 구매 금액이 300,000원 이상") == [("purchase_amount", None)]
    assert windows("최근 60일간 캠페인에 반응한 회원 중 구매 금액이 50,000원 이상") == [("purchase_amount", None)]


def test_source_restore_fills_lost_window_and_adjustment_only():
    source = ("최근 90일간 주문한 회원 중 주문 건수가 3건 이상이고 총결제금액이 300,000원 이상. "
              "반품금액이 있는 경우 총결제금액에서 차감한다.")
    plan = {"target_user": {"aggregate_conditions": [
        {"metric_id": "purchase_amount", "operator": ">=", "threshold": 300000.0, "window_days": None, "label": "누적 구매 금액"}]}}
    g._restore_aggregate_conditions_from_source(source, plan)
    restored = plan["target_user"]["aggregate_conditions"][0]
    assert restored["window_days"] == 90 and restored["adjustments"] == ["net_of_returns"]
    # 임계값·지표는 그대로다(덮어쓰지 않는다).
    assert restored["metric_id"] == "purchase_amount" and restored["threshold"] == 300000.0
    # 원문에 집계 조건이 없으면 무동작(기존 조건 보존).
    untouched = {"target_user": {"aggregate_conditions": [dict(restored)]}}
    g._restore_aggregate_conditions_from_source("서울에 사는 30대 여성", untouched)
    assert untouched["target_user"]["aggregate_conditions"][0] == restored


def test_scope_split_never_cuts_audience_clause_into_channel_scope():
    # '함께'의 '께'처럼 낱말 안에 든 표지로는 자르지 않는다 — 여기서 잘리면 뒤따르는 오디언스 조건
    # (반품 차감·동의·제외)이 채널 절로 빠져 Query Plan 에서 통째로 사라진다.
    assert g._rule_split_prompt_scopes(
        "총결제금액을 함께 산출해줘. 블랙리스트 회원은 제외하고 캠페인을 만들어줘") is None
    # 낱말 안 표지를 건너뛴 뒤 **그 다음** 유효 표지에서는 정상적으로 분리한다.
    assert g._rule_split_prompt_scopes("총결제금액을 함께 산출한 고객에게 쿠폰을 발송해줘") == (
        "총결제금액을 함께 산출한 고객에게", "쿠폰을 발송해줘")
    # 정상적인 '[오디언스]에게 [발송 액션]' 구조는 종전대로 분리된다.
    assert g._rule_split_prompt_scopes("서울에 사는 30대 여성 고객에게 쿠폰 메시지를 발송해줘") == (
        "서울에 사는 30대 여성 고객에게", "쿠폰 메시지를 발송해줘")


def test_excluded_activity_filter_compiles_to_complement():
    # '휴면(N일 이상 미접속) 제외'의 여집합은 '최근 N일 내 접속'이다 — 미지원으로 SQL 을 막지 않는다.
    compiled = g.compile_member_target_conditions(
        {"intent": "find_user_segment", "target_user": {"lifecycle": ["normal_member"]},
         "exclude": {"lifecycle": ["inactive_90d"]}})
    assert compiled["unsupported"] == []
    assert any("LAST_LOGIN_DATE >=" in predicate for predicate in compiled["predicates"])
    assert "non_inactive_90d" in compiled["labels"]


def test_planner_lifecycle_aliases_resolve_to_compilable_canonicals():
    # 플래너 허용 어휘(LIFECYCLE_TERMS)가 컴파일러 매핑보다 넓어 별칭이 나오면 SQL 이 통째로 막혔다.
    # 별칭 표(설정)가 이를 흡수하므로 미지원이 남지 않는다.
    plan = {"intent": "find_user_segment", "target_user": {"lifecycle": ["active_user"]},
            "exclude": {"lifecycle": ["withdrawn_user", "dormant_user"]}}
    compiled = g.compile_member_target_conditions(plan)
    assert compiled["unsupported"] == []
    assert set(compiled["labels"]) >= {"active_member", "non_withdrawn", "non_dormant"}
    # 원본 plan 은 건드리지 않는다(다른 소비자가 원 canonical 을 그대로 본다).
    assert plan["exclude"]["lifecycle"] == ["withdrawn_user", "dormant_user"]


def test_exclusion_implied_by_equality_include_is_not_duplicated():
    # 상태='NORMAL' 이면 <>'WITHDRAW'/<>'SLEEP' 은 자명하므로 술어를 중복해 걸지 않는다(라벨은 유지).
    compiled = g.compile_member_target_conditions(
        {"intent": "find_user_segment", "target_user": {"lifecycle": ["normal_member"]},
         "exclude": {"lifecycle": ["withdrawn", "dormant"]}})
    predicates = " ".join(compiled["predicates"])
    assert "MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'" in predicates
    assert "<>" not in predicates
    assert {"non_withdrawn", "non_dormant"} <= set(compiled["labels"])
    # 같은 컬럼에 포함 조건이 없으면 제외 술어는 그대로 나간다.
    only_exclude = g.compile_member_target_conditions(
        {"intent": "find_user_segment", "target_user": {}, "exclude": {"lifecycle": ["withdrawn"]}})
    assert any("<> 'MEMBER_STATE_CD.WITHDRAW'" in predicate for predicate in only_exclude["predicates"])


def test_same_canonical_in_include_and_exclude_does_not_produce_contradiction():
    # 포함·제외에 같은 canonical 이 들어오면 제외가 이긴다(둘 다 걸면 항상 0명).
    compiled = g.compile_member_target_conditions(
        {"intent": "find_user_segment", "target_user": {"lifecycle": ["blacklisted"]},
         "exclude": {"lifecycle": ["blacklisted"]}})
    predicates = " ".join(compiled["predicates"])
    assert "B.BLACKLIST_YN <> 'Y'" in predicates
    assert "B.BLACKLIST_YN = 'Y'" not in predicates


def test_delivery_contract_releases_only_when_every_issue_is_exempt():
    plan = {"intent": "find_user_segment", "semantic_conditions": [],
            "output_contract": {"expected_grain": "member", "requires_member_id": True}}
    exempt_issue = {"type": "spurious", "condition": "segment_label 문구",
                    "detail": "SELECT 에 'active_member,구매 횟수' 라벨이 있는데 원문엔 없음"}
    blocking_issue = {"type": "inverted", "condition": "구매하지 않은", "detail": "EXISTS 로 뒤집힘"}

    released = g._validate_sql_delivery_contract(
        "우수 구매 고객", plan, _LABELED_SQL,
        semantic_verification={"ran": True, "faithful": False, "issues": [exempt_issue]})
    assert released["is_satisfied"] is True
    assert released["semantic_issues"][0]["exempt_reason"] == "constant_projection_label"
    assert released["semantic_issues"][0]["severity"] == "warning"

    blocked = g._validate_sql_delivery_contract(
        "우수 구매 고객", plan, _LABELED_SQL,
        semantic_verification={"ran": True, "faithful": False, "issues": [exempt_issue, blocking_issue]})
    assert blocked["is_satisfied"] is False
    assert "critical_semantic_issue" in blocked["failure_reasons"]

    # 판정이 비어 있으면(분류 불가) 종전대로 차단한다.
    empty = g._validate_sql_delivery_contract(
        "우수 구매 고객", plan, _LABELED_SQL,
        semantic_verification={"ran": True, "faithful": False, "issues": []})
    assert empty["is_satisfied"] is False
