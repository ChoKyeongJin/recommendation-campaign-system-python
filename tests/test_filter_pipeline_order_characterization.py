"""결정론 필터 파이프라인 실행 순서 특성화(characterization) — 리팩터 전 안전망.

배경: 결정론 필터의 '호출 방식'은 _deterministic_filter_registry(단일 소스)가, '실행 순서'는 경로별
리스트(_RULES_PRE_FILTERS/_RULES_POST_FILTERS/_AUTO_FILTERS)가 소유한다. 이 순서는 임의가 아니라 문서화된
파싱 의존성을 인코딩한다(레지스트리 엔트리 주석 참조): 예를 들어 '추가 구매 없음'은 캠페인 반응 파싱 뒤에
실행돼야 무구매 anti-join 이 정확히 걸리고, '카트 부재'는 '카트 존재' 승격 뒤에 실행돼야 오파싱된
cart_abandoner 를 걷어낸다. test_deterministic_filter_registry 는 '리스트 == spec.paths'(멤버십)만 강제할 뿐
**순서**는 강제하지 않으므로, 순서 리스트를 재배열해도 그 테스트는 통과한다.

이 파일은 (1) 세 순서 리스트의 정확한 현재 시퀀스를 스냅샷으로 고정하고, (2) 레지스트리 주석이 명시한
'A 는 B 뒤에 실행' 의존 인접을 경로별 인덱스 비교로 강제한다. 재감지 패스 통합(C-2)·필터 공통화 리팩터가
순서를 조용히 바꾸면 여기서 실패한다. 순서를 의도적으로 바꿀 때만 스냅샷을 갱신한다.

실행: python -m pytest tests/test_filter_pipeline_order_characterization.py -q
"""

import graph_rag as g


# ── (1) 전체 시퀀스 스냅샷(현재 동작 고정) ─────────────────────────────────────────────
# 생성 기준: master 6044bb6 + 작업 트리. 필터 추가/삭제/재배열 시 여기서 먼저 실패한다.
_RULES_PRE_SNAPSHOT = (
    "age", "purchase_object", "purchase_date", "result_limit", "purchase_inactivity",
    "birthday", "signup_target", "sell_object", "dimension", "member_value", "macro_region",
    "aggregate", "purchase_count_threshold", "cart_aggregate", "cart_retention", "cart_type",
)
_RULES_POST_SNAPSHOT = (
    "cart_repurchase", "cart_presence", "cart_absence", "inactivity_period", "recent_login",
    "signup_channel", "signup_device", "balance_condition", "balance_selection", "campaign_response",
    "no_additional_purchase", "campaign_response_frequency", "campaign_buy_amount", "cell_rate",
    "children_registered", "grade_threshold", "channel_consent", "member_flag", "policy",
    "region_density", "member_metric_ranking", "purchase_count_ranking",
)
_AUTO_SNAPSHOT = (
    "sell_object", "dimension", "member_value", "macro_region", "region_density",
    "member_metric_ranking", "purchase_count_ranking", "purchase_object", "purchase_date",
    "result_limit", "purchase_inactivity", "recent_login", "signup_channel", "signup_device",
    "balance_condition", "balance_selection", "campaign_response", "no_additional_purchase",
    "cart_presence", "cart_absence", "campaign_response_frequency", "children_registered",
    "grade_threshold", "channel_consent", "member_flag", "aggregate", "purchase_count_threshold",
    "campaign_buy_amount", "cell_rate", "cart_aggregate", "cart_retention", "cart_type",
    "birthday", "signup_target",
)


def test_rules_pre_filter_order_snapshot():
    assert tuple(g._RULES_PRE_FILTERS) == _RULES_PRE_SNAPSHOT


def test_rules_post_filter_order_snapshot():
    assert tuple(g._RULES_POST_FILTERS) == _RULES_POST_SNAPSHOT


def test_auto_filter_order_snapshot():
    assert tuple(g._AUTO_FILTERS) == _AUTO_SNAPSHOT


# ── (2) 문서화된 의존 인접(순서 계약) — 스냅샷과 독립적으로 '왜'를 강제 ────────────────────
# 각 쌍 (before, after): before 는 after 보다 먼저 실행돼야 한다(레지스트리 주석 근거). 스냅샷을
# 갱신하더라도 이 관계가 깨지면 파싱 사고(오파싱 미제거·이중 파싱·조건 소실)가 재발한다.
_ORDER_DEPENDENCIES = (
    # '추가 구매 없음'(무구매 anti-join)은 캠페인 반응·미구매창 파싱 뒤.
    ("campaign_response", "no_additional_purchase"),
    # '카트 부재'는 '카트 존재/이탈 승격' 뒤에 실행해 오파싱된 cart_abandoner 를 걷어낸다.
    ("cart_presence", "cart_absence"),
    # 지표명 없는 개수 임계('2개 이상')는 지표명 명시형(aggregate) 뒤(order_count 중복 방지).
    ("aggregate", "purchase_count_threshold"),
    # '캠페인 구매금액'은 누적 금액·반응 파싱 뒤(이중 파싱 제거).
    ("campaign_response", "campaign_buy_amount"),
    # '성공률/구매율'(셀 비율)도 캠페인 반응 뒤(오배정 접촉성공 EXISTS 제거).
    ("campaign_response", "cell_rate"),
    # 광역 권역어(수도권 등)는 값 인덱스(member_value/dimension) 뒤에 실행해 명시 시도와 병합.
    ("member_value", "macro_region"),
    ("dimension", "macro_region"),
)


def _assert_before(path: tuple[str, ...], before: str, after: str, path_name: str) -> None:
    if before in path and after in path:
        assert path.index(before) < path.index(after), (
            f"{path_name}: '{before}' 는 '{after}' 보다 먼저 실행돼야 함(문서화된 파싱 의존성)"
        )


def test_documented_order_dependencies_hold_in_rules_path():
    rules = (*g._RULES_PRE_FILTERS, *g._RULES_POST_FILTERS)
    for before, after in _ORDER_DEPENDENCIES:
        _assert_before(rules, before, after, "rules")


def test_documented_order_dependencies_hold_in_auto_path():
    auto = tuple(g._AUTO_FILTERS)
    for before, after in _ORDER_DEPENDENCIES:
        _assert_before(auto, before, after, "auto")
