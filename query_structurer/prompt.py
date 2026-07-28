from __future__ import annotations

import json

from .schema import STRUCTURED_QUERY_JSON_SCHEMA
from .campaign_plan_v2 import CAMPAIGN_QUERY_PLAN_V2_JSON_SCHEMA
from .types import QueryStructuringInput


COMPLEX_QUERY_STRUCTURER_SYSTEM_PROMPT = """너는 RAG 시스템의 Complex Query Structurer다.

사용자의 자연어 질문을 기존 Query Planner가 처리하기 쉬운 구조로 변환한다.

너의 역할은 질문의 의미와 요구사항을 구조화하는 것이다.
검색 계획을 직접 만들거나 실제 검색어를 생성하는 것이 아니다.

다음 항목을 구분한다.

1. 질문의 주요 대상
2. 시간, 수치, 상태 등의 제약 조건
3. 답변을 위해 찾아야 할 정보
4. 비교, 집계, 순위, 설명 등의 작업
5. 정보 사이의 의존관계
6. 질문 복잡도
7. Query Planner가 참고할 실행 특성

규칙:

- 사용자의 원래 의도를 변경하지 않는다.
- 질문에 없는 조건을 추측해서 추가하지 않는다.
- “비교해줘”, “요약해줘”, “표로 보여줘” 같은 표현은 operations 또는 outputPreference로 분리한다.
- outputPreference.format은 사용자가 표, 목록, JSON 같은 형식을 명시적으로 요청한 경우에만 설정한다.
- 실제 검색 문장이나 검색 단계는 만들지 않는다.
- 각 informationNeed는 하나의 명확한 정보 요구만 표현한다.
- 선행 정보가 필요한 경우 dependencies에 관계를 기록한다.
- 서로 독립적인 정보 요구에는 의존관계를 만들지 않는다.
- “해당 장애”, “그 부서”, “그중” 같은 참조는 질문과 제공된 대화 문맥 안에서만 해석한다.
- 상대 날짜는 제공된 currentDate와 timezone을 기준으로 절대 날짜로 변환한다.
- 날짜를 확정할 수 없으면 원래 표현만 유지하고 임의의 날짜를 생성하지 않는다.
- 단순 질문을 불필요하게 여러 informationNeed로 분해하지 않는다.
- 출력은 지정된 JSON 스키마만 반환한다.
- 스키마의 nullable 필드는 알 수 없거나 적용되지 않으면 JSON null로 채운다. 문자열 "null"을 쓰지 않는다.
- 배열 값이 없으면 빈 배열로 채우고 모든 필수 필드를 빠짐없이 반환한다.
- JSON 외의 설명이나 마크다운을 출력하지 않는다."""

PLANNER_STRUCTURED_QUERY_RULES = """structuredQuery가 제공되면 이를 사용자 질문의 의미 구조로 참고한다.

informationNeeds는 답변을 위해 확보해야 할 정보 목록이다.

constraints는 검색 및 필터 조건으로 해석한다.

dependencies는 계획 단계의 실행 순서를 결정할 때 참고한다.

operations는 최종적으로 수행해야 하는 비교, 집계, 순위 등의 작업이다.

plannerHints는 참고 정보이며 절대 규칙이 아니다.

structuredQuery에 없는 조건을 임의로 추가하지 않는다.

원본 query와 structuredQuery가 충돌하면 원본 query의 명시적 표현을 우선한다."""


def build_structuring_user_prompt(input: QueryStructuringInput) -> str:
    context = {
        "currentDate": input.context.current_date,
        "timezone": input.context.timezone,
        "conversationContext": input.context.conversation_context,
    }
    return "\n\n".join(
        [
            "[User Query]\n" + input.query,
            "[Structuring Context]\n" + json.dumps(context, ensure_ascii=False, indent=2),
            "[StructuredQuery JSON Schema]\n" + json.dumps(STRUCTURED_QUERY_JSON_SCHEMA, ensure_ascii=False, indent=2),
        ]
    )


def build_retry_prompt(previous_response: str, error: str) -> str:
    return "\n\n".join(
        [
            "이전 응답은 JSON 파싱 또는 스키마 검증에 실패했다.",
            "[Validation Error]\n" + error,
            "[Previous Response]\n" + previous_response,
            "지정된 JSON 스키마에 맞는 JSON object만 다시 반환하라. 설명이나 마크다운을 포함하지 마라.",
        ]
    )


def build_campaign_query_plan_v2_user_prompt(input: QueryStructuringInput) -> str:
    context = {
        "current_date": input.context.current_date,
        "timezone": input.context.timezone,
        "conversation_context": input.context.conversation_context,
    }
    return "\n\n".join(
        [
            "[User Query]\n" + input.query,
            "[Structuring Context]\n" + json.dumps(context, ensure_ascii=False, indent=2),
            "[Campaign QueryPlan v2 JSON Schema]\n"
            + json.dumps(CAMPAIGN_QUERY_PLAN_V2_JSON_SCHEMA, ensure_ascii=False, indent=2),
            (
                "raw_query와 original_query에는 User Query를 그대로 넣고, planning_query에는 "
                "현재 단계에서 해석할 User Query를 그대로 넣어라. target_user/exclude/"
                "campaign_constraints에 캠페인 조건을 직접 구조화하라."
            ),
        ]
    )
