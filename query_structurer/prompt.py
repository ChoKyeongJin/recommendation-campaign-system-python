from __future__ import annotations

import json

from .schema import STRUCTURED_QUERY_JSON_SCHEMA
from .semantic_ir import extract_literal_bindings
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


def build_campaign_query_plan_v4_user_prompt(input: QueryStructuringInput) -> str:
    context = {
        "current_date": input.context.current_date,
        "timezone": input.context.timezone,
        "conversation_context": input.context.conversation_context,
    }
    literal_bindings = extract_literal_bindings(
        input.query, current_date=input.context.current_date
    )
    return "\n\n".join(
        [
            "[User Query]\n" + input.query,
            "[Structuring Context]\n" + json.dumps(context, ensure_ascii=False, indent=2),
            "[Application-owned Literal Bindings]\n"
            + json.dumps(literal_bindings, ensure_ascii=False, indent=2),
            "응답은 submit_campaign_query_plan_v4 도구만 호출한다.",
            (
                "질의 identity와 schema_version은 애플리케이션이 주입한다. 모델은 이를 반환하지 말고 "
                "target_user/exclude/campaign_constraints와 필요한 실행 의미 슬롯만 구조화하라."
            ),
            (
                "원문을 다시 쓰지 말고 의미를 직접 구조화한다. 모든 채택 슬롯은 semantic_evidence에 "
                "경로와 원문의 정확한 문자 구간을 남긴다. 스키마 또는 닫힌 어휘로 표현할 수 없는 "
                "요구는 추측하지 말고 unresolved에 기록한다. SQL, 테이블, 컬럼은 생성하지 않는다."
            ),
            (
                "포함과 제외는 상호배타 슬롯이다. '여성 제외', '휴면 빼고', '특정 관심사가 아닌'처럼 값에 "
                "제외·부정 표현이 붙으면 대응 exclude 슬롯에만 넣고 동일한 target_user 포함 슬롯은 null 또는 "
                "빈 배열로 둔다. 스키마에 대응 exclude 슬롯이 없으면 긍정으로 뒤집지 말고 unresolved에 기록한다. "
                "원문이 같은 값을 포함과 제외 양쪽에 실제로 명시한 경우에만 양쪽에 기록한다."
            ),
            (
                "날짜·숫자·퍼센트·비교 연산자의 값은 위 literal bindings만 신뢰한다. semantic_ir의 "
                "operation은 값을 다시 쓰지 말고 literal_id를 baseline/current/threshold/comparison 역할에 "
                "연결한다. 필요한 literal이 없으면 값을 추론하지 말고 status=needs_clarification과 "
                "missing_fields를 반환한다. 지원하지 않는 연산은 status=unsupported로 반환한다."
            ),
            (
                "숫자 뒤 한국어 단위는 의미의 일부이며 절대 바꾸지 않는다. number_with_unit binding의 "
                "semantic_unit을 그대로 사용한다: 개=item_quantity(상품 수량 합계), 회/번/건=order_count"
                "(서로 다른 주문 수), 종/종류=distinct_product_count(서로 다른 상품 수). binding과 실행 슬롯이 "
                "있으면 같은 값을 missing_fields로 다시 요구하지 않는다. 단, '상위 N개 상품 중 M개'의 "
                "M은 수량 합계가 아니라 랭킹 집합과 회원 구매 집합의 distinct 엔터티 교집합 개수다. "
                "이 구조는 entity_set_condition의 ranking.limit=N과 member_set.cardinality로 표현하고 "
                "'M개만'은 operator '='로 보존한다. '같은/동일 브랜드'는 브랜드명이 "
                "'같은'이라는 필터가 아니라 회원별·브랜드별 그룹에서 임계값을 검사하는 per_brand 조건이다."
            ),
            (
                "두 개의 date_window literal이 원문 순서로 제시되고 두 기간 사이의 증가/감소를 묻는다면, "
                "문법상 반대 근거가 없는 한 첫 기간을 baseline, 둘째 기간을 current로 연결한다. 이 경우 "
                "baseline/current가 누락된 것이 아니다. 퍼센트와 비교 연산자 literal도 있으면 각각 "
                "threshold/comparison으로 연결한다. 예: 구매 금액의 기간 대비 증감은 "
                "kind=period_over_period_change, metric_id=purchase_amount로 표현한다."
            ),
            (
                "고객 리스트·회원 명단처럼 결과가 회원 행이면, 조건 계산에 SUM/COUNT가 필요해도 intent는 "
                "find_user_segment다. 집계 숫자나 그룹별 분석 행 자체를 요청한 경우에만 "
                "analyze_aggregation을 사용한다."
            ),
            (
                "missing_fields는 요청 결과를 계산하는 데 반드시 필요한 입력만 포함한다. 고객 목록을 "
                "계산하는 데 캠페인 발송 채널·혜택·판매 상품·캠페인 목적은 필요하지 않으므로 사용자가 "
                "직접 요구하지 않았다면 누락 필드로 만들지 않는다. '10% 이상 증가'의 10%는 할인율이 "
                "아니라 증감률 임계값이므로 offer_type을 설정하지 않는다. 기간 대비 증감은 "
                "target_user.purchase_date로 중복 표현하지 않고 semantic_ir operation만 사용한다."
            ),
            (
                "선택 사항은 도구 스키마상 required-but-nullable이다. 해당 의미가 없으면 null 또는 빈 배열을 "
                "사용하고, 문자열 'null'은 사용하지 않는다."
            ),
        ]
    )
