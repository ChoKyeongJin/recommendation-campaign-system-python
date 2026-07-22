"""재작성 검증 게이트(_rewrite_dropped_signals) 회귀 테스트.

배경: normalize_prompt 의 재작성 LLM 이 원문의 타겟 조건을 조용히 지울 수 있다(예: "20대 여성"
→ "여성" 으로 연령 소실). effective_query 가 이후 모든 파싱·SQL 의 기준이 되므로 이 소실은 파급이
크다. 게이트는 재작성본이 원문의 핵심 리터럴 신호(숫자·성별)를 하나라도 잃으면 재작성을 폐기하고
원문으로 되돌린다. 폴백은 항상 안전하므로(원문 의미 보존) 오탐이 있어도 손해는 '재작성 미적용'뿐이다.

LLM 없이 순수 함수만 검증한다.

실행(컨테이너): docker compose exec -w /app -e PYTHONPATH=/app api pytest tests/test_rewrite_guard.py -q
"""

import graph_rag as g


# (원문, 재작성본) — 게이트가 소실을 '잡아야'(비어있지 않은 dropped) 하는 케이스.
DROP_CASES = [
    ("20대 여성 고객", "여성 고객", ["숫자 '20'"]),                       # 연령 소실
    ("20대 여성 고객", "20대 고객", ["성별 '여성'"]),                     # 성별 소실
    ("30세 이상 남성", "고객", ["숫자 '30'", "성별 '남성'"]),            # 둘 다 소실
    ("최근 90일 미구매 회원", "미구매 회원", ["숫자 '90'"]),              # 일수 창 소실
    ("2회 이상 구매한 고객", "재구매 고객", ["숫자 '2'"]),               # 횟수 소실('이상'은 상품 아님)
    ("최근 화장품을 구매한 20~30대 여성 고객", "20~30대 여성 고객", ["구매 상품 '화장품'"]),  # 구매 상품 조건 소실(라벨)
]

# 게이트가 '통과'(빈 dropped)시켜야 하는 케이스 — 표현만 바뀌고 핵심 신호는 보존.
KEEP_CASES = [
    ("20대 여성한테 쿠폰 주는 캠페인", "20대 여성 대상 쿠폰 캠페인"),      # 구어체 정리, 신호 보존
    ("여자 고객", "여성 고객"),                                          # 여자→여성(같은 canonical)
    ("30,000원 이상 구매 고객", "30000원 이상 구매 고객"),               # 천단위 콤마만 제거
    ("이십대 여성", "20대 여성"),                                       # 원문에 숫자 없음 → 추가는 소실 아님
    ("VIP 등급 고객", "VIP 등급 고객"),                                 # 숫자·성별 무관 조건은 게이트 대상 아님
    ("화장품을 구매한 고객", "화장품 구매 고객"),                        # 구매 표현형 변화(구매한→구매)여도 상품명 보존
    ("기저귀를 구매한 30대", "30대 기저귀 구매 고객"),                   # 어순 바뀌어도 상품명 남으면 보존
]


def test_guard_flags_dropped_signals():
    for original, rewritten, expected in DROP_CASES:
        dropped = g._rewrite_dropped_signals(original, rewritten)
        assert dropped == expected, f"{original!r} → {rewritten!r}: {dropped} != {expected}"


def test_guard_passes_when_signals_preserved():
    for original, rewritten in KEEP_CASES:
        dropped = g._rewrite_dropped_signals(original, rewritten)
        assert dropped == [], f"{original!r} → {rewritten!r}: 오탐 {dropped}"


def test_guard_enabled_default_and_toggle(monkeypatch):
    monkeypatch.delenv("PROMPT_REWRITE_GUARD", raising=False)
    assert g._rewrite_guard_enabled() is True
    for off in ("0", "false", "off", "no", "OFF"):
        monkeypatch.setenv("PROMPT_REWRITE_GUARD", off)
        assert g._rewrite_guard_enabled() is False
    monkeypatch.setenv("PROMPT_REWRITE_GUARD", "true")
    assert g._rewrite_guard_enabled() is True
