from __future__ import annotations

import json

import event_ir
import semantic_plan
import semantic_requirements

from . import campaign_plan_v4
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
    semantic_obligations = [
        requirement.to_dict()
        for requirement in semantic_requirements.capture_source_semantic_obligations(
            input.query
        )
    ]
    knowledge_sections: list[str] = []
    if input.context.slot_vocabulary:
        knowledge_sections.append(
            "[Allowed Canonical Values]\n"
            + json.dumps(input.context.slot_vocabulary, ensure_ascii=False, indent=2)
            + "\nUse these values only where the tool schema or semantic catalog requires a canonical value. "
            "Do not invent identifiers that are absent from the provided contract."
        )
    if input.context.slot_guidance:
        knowledge_sections.append(
            "[Audience Semantic Catalog]\n" + input.context.slot_guidance
        )
    return "\n\n".join(
        [
            "[User Query]\n" + input.query,
            "[Structuring Context]\n" + json.dumps(context, ensure_ascii=False, indent=2),
            "[Application-owned Literal Bindings]\n"
            + json.dumps(literal_bindings, ensure_ascii=False, indent=2),
            "[Application-owned Semantic Obligations]\n"
            + json.dumps(semantic_obligations, ensure_ascii=False, indent=2)
            + "\nThese immutable source meanings must each be realized exactly once by the fixed algebra. "
            "They are requirements, not prior validation errors. Use the catalog relation recipe that matches "
            "their kind and values; do not report a value as missing when it is present here.",
            *knowledge_sections,
            "응답은 submit_campaign_query_plan_v4 도구만 호출한다.",
            (
                "Return exactly the four root fields declared by the tool schema: intent, "
                "campaign_constraints, result_limit, and audience_requirement. Identity, schema version, "
                "execution fields, and compatibility fields are application-owned."
            ),
            (
                "audience_requirement is the audience-meaning contract for everything the Event IR algebra "
                "can state. Put a complete Event IR condition in audience_requirement.expression and "
                "validation or interpretation problems in audience_requirement.issues. Do not return "
                "target_user, exclude, semantic_ir, semantic_evidence, unresolved, event_expression, SQL, "
                "physical tables, or physical columns."
            ),
            (
                "semantic_plan is the narrow second surface for the one axis the Event IR algebra cannot "
                "state: a member attribute read from a **past or per-month snapshot**. Use it only when the "
                "query names a past reference month ('지난달 말 기준', '2025년 12월 기준'), a change relative "
                "to the previous snapshot ('직전 등급', '승급'), or a multi-month pattern ('3개월 내내', "
                "'2번 이상 변경', '매월', '한 번이라도').\n"
                "A condition on the member's CURRENT attribute value is NOT this axis. '현재 등급이 VIP', "
                "'골드 이상 등급', '휴면 회원' are ordinary profile predicates: put them in "
                "audience_requirement.expression as a subject field Comparison and leave semantic_plan empty. "
                "'최신 기준월 기준' also means the current value.\n"
                "A condition belongs to exactly one surface — never state it in both. When the query has no "
                "past/per-month snapshot condition, return semantic_plan={\"nodes\": []}. Never put a "
                "purchase, cart, campaign, or login condition into semantic_plan.\n"
                + semantic_plan.node_type_guidance(
                    node_types=campaign_plan_v4.LLM_SEMANTIC_PLAN_NODE_TYPES
                )
            ),
            (
                "Build the expression only with the Event IR algebra allowed by the tool schema, such as "
                "And/Or/Not, Comparison, Exists, Aggregate, Source, Filter, Join, Group, TimeFilter, and "
                "TemporalRelation, Project, Summarize, Order, and Limit. Use only source and field identifiers listed in the Audience Semantic "
                "Catalog. Preserve negation, AND/OR grouping, comparison operators, aggregation grain, and "
                "which condition owns each time window."
            ),
            (
                "A concrete product or category phrase attached to a purchase is a required open-text scope, "
                "not evidence for a bare purchase_line Source. Put it in a Filter using a catalog field whose "
                "match_mode is contains (normally purchase_line.product_text). For 'X 외 상품' or '다른 상품', "
                "use Exists(Filter(Source(purchase_line), Not(Comparison(product_text = X)))); for 'X를 구매한 "
                "적이 없다', negate Exists around the positive X comparison. When several products are "
                "explicitly quantified with '모두/전부/각각', use one independent filtered Exists per product."
            ),
            (
                "Use the exact JSON property names shown under [Fixed wire shapes]. Aggregate never has "
                "source/field keys: it has function, relation, expression, and distinct. FieldRef uses name, "
                "not field. A date window is the literal binding's normalized.event_ir_window nested in "
                "TimeFilter, never a time_window sibling or a date_window node. Do not add unit/evidence/id "
                "properties to Literal, FieldRef, Aggregate, Not, or the window object."
            ),
            (
                # 사건의 발생 시각 필드는 소스 등록에서 **파생**되므로 이름 규칙이 곧 계약이다.
                # 이 한 줄이 없던 동안 모델은 이름이 시간처럼 보이는 다른 필드를 골랐고('주문 시각'
                # HHMMSS 컬럼), 기간 조건이 컴파일 불가로 떨어져 결국 기간이 통째로 빠진 SQL 이나
                # 확인 질문으로 귀결됐다(2026-08-05 '오늘 주문한 회원' 실측).
                "A TimeFilter always constrains the source's own event time field, and that field is named "
                f"'<source>.{event_ir.TIME_FIELD_SUFFIX}' for every source (for example "
                f"purchase.{event_ir.TIME_FIELD_SUFFIX}). Never point a TimeFilter at another field whose "
                "name merely looks temporal; those are ordinary attributes and cannot carry a date window."
            ),
            (
                "Every semantic atom and every issue must carry evidence whose text is an exact substring of "
                "the User Query and whose start/end are exact zero-based Python slice offsets [start, end). "
                "Do not attach evidence to a broader or different phrase merely because it is related."
            ),
            (
                "Treat Application-owned Literal Bindings as authoritative for dates, durations, numbers, "
                "units, percentages, and comparison operators. Reference or copy only values supported by "
                "those bindings and the tool schema; never infer a missing value except for the explicit "
                "bare-'최근' default described below."
            ),
            (
                "If a material audience requirement is ambiguous, unsupported, inconsistent with the catalog, "
                "or lacks a required argument, set expression to null and add the corresponding issue using "
                "only these codes: missing_argument, ambiguous_requirement, or unsupported_semantics. "
                "validation_mismatch is application-owned and must never be authored by the model. State the "
                "missing/invalid semantic argument in issue.argument."
            ),
            (
                "Special temporal rule: when the query says '최근' but gives no duration or bounded period, "
                "do not interpret it as all history. Apply the application default of the most recent five "
                "days by putting {\"type\": \"rolling\", \"value\": 5, \"unit\": \"day\"} in the "
                "TimeFilter that owns the condition. Do not return a missing_argument issue for period."
            ),
            (
                "campaign_constraints contains campaign-delivery metadata only. A campaign objective such as "
                "재반응 유도 belongs only in campaign_constraints.objective; it must never become an audience "
                "predicate, source filter, or inferred response condition. Do not request optional campaign "
                "metadata that the user did not specify."
            ),
            (
                "For a member-list request, use intent=find_user_segment even when its audience predicate uses "
                "COUNT or another aggregate. When the user explicitly asks to create or recommend a campaign, "
                "use intent=recommend_campaign. Populate every tool-schema-required field; use JSON null or an "
                "empty array for absent nullable/collection metadata, never the string 'null'."
            ),
        ]
    )


def build_campaign_query_plan_v4_retry_prompt(
    previous_response: str, error: str
) -> str:
    """Retry instructions for the fixed canonical audience algebra."""
    return "\n\n".join(
        [
            "The previous campaign tool arguments failed canonical validation.",
            "[Validation Error]\n" + error,
            "[Previous Tool Arguments]\n" + previous_response,
            (
                "Submit one complete corrected tool object. Emit no text or closing characters outside "
                "that JSON object. Rebuild the meaning; do not add unrelated predicates merely to consume "
                "a literal binding."
            ),
            (
                "Keep each time window inside the Filter of the relation it constrains. A subject.* profile "
                "field is a scalar FieldRef used directly by Comparison; 'subject' is not an event Source "
                "and must not be wrapped in Source, Filter, or Exists."
            ),
            (
                "The audience expression root must be a Condition. Join, Limit, Order, Summarize, Filter, "
                "and Source are Relations, so wrap a membership Join in Exists. For ranked membership, use "
                "a member-correlated left Source with the correlation key omitted. On the Source nested under "
                "the right Summarize, correlation='none' is mandatory; omitting it changes the global rank "
                "into a per-member aggregate."
            ),
            (
                "Use the exact singular wire shapes: Not is "
                "{\"type\":\"not\",\"operand\":<Condition>} and never has operands. Exists has only "
                "type/relation/evidence; it never has where. A row predicate belongs in "
                "{\"type\":\"filter\",\"relation\":<Relation>,\"where\":<Condition>}, and Filter never "
                "has evidence. audience_requirement contains both expression and issues; semantic_plan is "
                "its root-level sibling."
            ),
            (
                "Summarize output names are local aliases used by Order.keys.name, not FieldRef names. Join.on "
                "uses catalog FieldRefs on both sides; their left/right relation scopes disambiguate identical "
                "canonical field IDs. For a ranked obligation, both Join.on sides use its entity_field, never "
                "the member_id. If it has time_window, Filter the global right Source before Summarize. Internal "
                "a ranked count belongs in Limit.count and a ranked percentage belongs in Limit.percent, "
                "never root result_limit. Use desc for top and asc for bottom, followed by the entity key "
                "ascending as the deterministic tie-break."
            ),
            (
                "Comparison evidence must be the exact query slice containing the comparison's source value "
                "and comparison-operator wording. Use only catalog source/field IDs and preserve every "
                "application-owned binding once in its semantic owner."
            ),
            (
                "If no faithful corrected expression exists, return expression=null and explicit issues. "
                "Never return a non-null expression together with issues."
            ),
        ]
    )
