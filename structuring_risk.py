"""1차 구조화(싼 모델)가 뜻을 잃기 쉬운 문장 모양 — **모델 라우팅 전용** 판정자.

배경
----
QueryPlan v4 의 1차 추출은 지연·비용 때문에 일부러 빠른 모델로 강등된다
(:func:`rag.llm_io._structuring_llm_model`). 강한 모델은 검증기가 거절한 소수만 다시 본다.
그 강등이 옳으려면 "어떤 문장이 1차에서 무너지는가"를 **미리** 말할 수 있어야 한다.

실측(2026-08-06, `logs/rag_llm/2026-08-06/004249-1ddb98.jsonl`)::

    질의  장바구니에 서로 다른 상품을 3개 이상 담아둔 회원
    1차   {"expression": {"type":"exists","relation":{"type":"source","name":"active_cart"}}}
          → '서로 다른 … 3개 이상'이 통째로 사라졌다(리터럴 소비 가드가 반려)
    2차   expression=null + unsupported_semantics("count distinct 를 표현할 수 없다")
          → 거짓 신고로 종결. 같은 뜻을 컴파일러는 그 자리에서 만들 수 있다.

여기서 잃은 것은 **집계의 종류**다. 값 비교('금액 10만원 이상')와 달리 가짓수 집계는
슬롯 하나가 아니라 Aggregate 노드의 모양(distinct + 대상 필드 + 임계 비교)이라, 1차 모델이
가장 먼저 버린다. 그래서 이 모양은 1차부터 복구 등급 모델로 보낸다.

경계
----
이 모듈은 **라우팅만** 결정한다. 조건을 만들지도, 결핍을 선언하지도 않는다.

특히 :func:`semantic_requirements.capture_source_semantic_obligations` 의 원장에는 넣지
않는다. 그 원장의 항목은 compiler receipt 가 없으면 응답을 막으므로(fail-close), 새 항목을
넣는 순간 가짓수 질의는 **간헐적 실패에서 상시 실패로** 바뀐다. 둘은 같은 곳
(``prefer_repair``)에서 합쳐지지만 결말이 다른 판정이다.

오탐의 대가는 그 요청 하나가 느리고 비싼 모델을 쓰는 것뿐이다 — 뜻이 바뀌지는 않는다.
그래서 재현율 쪽으로 기울여 잡는다.
"""

from __future__ import annotations

import functools
import re

import lexicon_patterns

# '가짓수를 센다'는 표지('서로 다른', '여러', '종류', '가짓수' …). 어휘는 사전이 소유한다.
_DISTINCT_MARKER = lexicon_patterns.pattern("distinct_count_marker")


@functools.lru_cache(maxsize=1)
def _count_threshold_re() -> re.Pattern[str]:
    """'3개' · '두 종' · '네 종류' 같은 **개수** 임계값.

    계수 단위를 ``source_entity_counter``(개/종/가지)로 한정해 금액(원)·기간(개월)·횟수(번/회)와
    가른다. '3개월'은 ``개`` 뒤에 ``월`` 이 붙으므로 lookahead 로 뺀다 — 기간은 가짓수가 아니다.
    """
    counters = lexicon_patterns.alternation("source_entity_counter")
    korean_numerals = lexicon_patterns.alternation("source_korean_count")
    return re.compile(rf"(?:\d[\d,]*|{korean_numerals})\s*(?:{counters})(?!월)")


def states_distinct_count(query: str) -> bool:
    """이 문장이 '서로 다른 X 를 N개' 같은 **가짓수 집계**를 요구하는가.

    표지와 임계값이 둘 다 있어야 한다. 표지만 있으면 값을 세지 않는 수식어일 뿐이고
    ('여러 상품을 구매한'), 임계값만 있으면 가짓수가 아니라 수량·건수다('3개 구매').
    """
    if not isinstance(query, str) or not query.strip():
        return False
    return bool(_DISTINCT_MARKER.search(query)) and bool(_count_threshold_re().search(query))


def prefers_repair_grade_structuring(query: str) -> bool:
    """1차 구조화를 복구 등급 모델로 올려야 하는 문장인가.

    현재 아는 모양은 가짓수 집계 하나다. 새 모양이 실측되면 여기에 ``or`` 한 줄로 붙인다 —
    호출자(라우팅)는 그대로 두고 판정만 넓히기 위해 이 함수가 따로 있다.
    """
    return states_distinct_count(query)


__all__ = ["prefers_repair_grade_structuring", "states_distinct_count"]
