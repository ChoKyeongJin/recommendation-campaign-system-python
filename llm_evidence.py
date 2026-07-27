"""LLM 근거 탐색기(§5) — 역할이 엄격히 제한된 의미 검증 보조. 규칙으로 결론 못 낸 항목에만 쓴다.

LLM 은 **새 SQL 을 만들어 비교하지 않는다**. 현재 SQL 에서 각 요구사항의 구현 근거를 찾고, 규칙이 판단
못 한 의미 관계를 분석하며, 모호한 비즈니스 정의를 식별하고, 누락/추가 제약을 설명하는 역할만 한다(§5).
응답은 JSON Schema 구조화 출력으로만 받고, 파싱 불가/스키마 위반은 허용하지 않는다(→ review 로 폴백).

핵심 안전장치: LLM 은 판정을 **완화(pass 승격)하지 못한다**. 규칙이 review/모호로 둔 항목을 근거와 함께
matched/equivalent 로 올리거나, 여전히 불명확하면 review 를 유지할 뿐이다. LLM 단독으로 fail 을 새로 만들려면
반드시 요구 id·SQL 근거·반례 중 하나를 제시해야 한다(decide 가 근거 없는 fail 을 받지 않음).

이 모듈은 OpenAI 클라이언트를 **주입**받는다(테스트는 스텁 주입). 클라이언트가 없으면 no-op(규칙 결과 유지).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Protocol

from sql_semantics import SqlSemantics
from target_spec import RequirementCheck, TargetSpecification


LLM_SYSTEM_PROMPT = (
    "SQL의 문법적 형태가 아니라 결과 집합의 의미를 기준으로 판단한다.\n\n"
    "JOIN, EXISTS, IN, CTE, 서브쿼리, 범위 조건 등 표현이 다르더라도 "
    "결과 집합이 동일하면 의미상 동등한 것으로 판단한다.\n\n"
    "조건이 다른 쿼리 블록에 있더라도 실제 필터링 효과가 동일하면 누락으로 판단하지 않는다.\n\n"
    "확실한 의미 위반이 없는 경우 fail을 반환하지 않는다.\n\n"
    "근거가 부족하거나 정의가 모호하면 fail 대신 review를 반환한다.\n\n"
    "fail을 반환하려면 누락된 요구사항 ID, 잘못된 SQL 구문, 정책 위반 또는 구체적인 반례 중 "
    "하나 이상을 반드시 제시한다.\n\n"
    "새로운 SQL을 생성하거나 자신의 SQL과 원본 SQL을 비교하지 않는다. "
    "현재 SQL에서 각 요구사항이 구현된 근거만 찾는다."
)

# 구조화 출력 스키마(§5 마지막). 각 항목은 요구 id 별 판정 근거.
RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "checks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "requirement_id": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["matched", "equivalent", "partially_matched",
                                 "missing", "contradicted", "ambiguous", "not_applicable"],
                    },
                    "sql_evidence": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "counterexample": {"type": "string"},
                },
                "required": ["requirement_id", "status", "reason"],
                "additionalProperties": False,
            },
        },
        "ambiguous_business_terms": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["checks"],
    "additionalProperties": False,
}


class ChatClient(Protocol):
    """주입되는 최소 LLM 인터페이스. graph_rag 의 _openai_chat_create 시그니처와 호환되게 감싼다."""

    def __call__(self, system: str, user: str) -> str: ...


class LlmEvidenceError(Exception):
    """LLM 호출/파싱 실패 — 규칙 결과를 유지하고 review 로 폴백해야 하는 기술 오류."""


def _undecided(checks: list[RequirementCheck]) -> list[str]:
    """규칙이 결론 못 낸(모호/부분) 요구 id — LLM 근거 탐색 대상."""
    return [c.requirement_id for c in checks if c.status in ("ambiguous", "partially_matched")]


def build_user_prompt(original_query: str, sql: str, spec: TargetSpecification,
                      undecided_ids: list[str]) -> str:
    reqs = [r.to_dict() for r in spec.requirements if r.id in undecided_ids]
    excs = [e.to_dict() for e in spec.exclusions if e.id in undecided_ids]
    payload = {
        "original_request": original_query,
        "sql": sql,
        "requirements_to_locate": reqs or [r.to_dict() for r in spec.requirements],
        "exclusions_to_locate": excs,
        "instruction": (
            "각 요구사항이 이 SQL의 어느 구문(WHERE/JOIN/HAVING/EXISTS/서브쿼리/CTE)에 구현됐는지 근거를 찾아라. "
            "표현이 달라도 결과 집합 의미가 같으면 equivalent 로 판정하라. 근거를 못 찾겠으면 ambiguous 로 두고, "
            "명백한 반대/누락이면 contradicted/missing 과 함께 구체적 근거(SQL 구문 또는 반례)를 제시하라."
        ),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def refine_checks(
    original_query: str,
    sql: str,
    spec: TargetSpecification,
    semantics: SqlSemantics,
    rule_checks: list[RequirementCheck],
    chat: ChatClient | None,
) -> tuple[list[RequirementCheck], list[str]]:
    """규칙 결과를 LLM 근거로 보강한다. 반환 (보강된 checks, 모호 비즈니스 용어 목록).

    - chat 이 None 이면 규칙 결과를 그대로 돌린다(no-op).
    - LLM 은 모호/부분 항목만 다룬다. matched/equivalent(확정)·contradicted/missing(규칙 확정 위반)은 건드리지 않는다.
    - LLM 이 확정 항목을 뒤집으려 해도 무시한다(규칙 우선). 단 모호→matched/equivalent 승격, 또는 모호→
      contradicted/missing(구체 근거 동반 시)만 허용한다.
    - 파싱/스키마 위반은 LlmEvidenceError 로 승격하지 않고 규칙 결과 유지(안전).
    """
    if chat is None:
        return rule_checks, []
    undecided = _undecided(rule_checks)
    if not undecided:
        return rule_checks, []
    try:
        raw = chat(LLM_SYSTEM_PROMPT, build_user_prompt(original_query, sql, spec, undecided))
        data = json.loads(raw or "{}")
    except Exception:  # noqa: BLE001 - LLM/파싱 실패 → 규칙 결과 유지(정상 SQL 막지 않음)
        return rule_checks, []
    if not isinstance(data, dict) or not isinstance(data.get("checks"), list):
        return rule_checks, []

    llm_by_id: dict[str, dict[str, Any]] = {}
    for item in data["checks"]:
        if isinstance(item, dict) and isinstance(item.get("requirement_id"), str):
            llm_by_id[item["requirement_id"]] = item

    merged: list[RequirementCheck] = []
    for check in rule_checks:
        if check.requirement_id not in undecided:
            merged.append(check)  # 규칙 확정은 LLM 이 못 건드림
            continue
        llm = llm_by_id.get(check.requirement_id)
        if not llm:
            merged.append(check)
            continue
        status = llm.get("status")
        reason = str(llm.get("reason") or "").strip()
        evidence = [str(x) for x in (llm.get("sql_evidence") or []) if isinstance(x, (str, int))]
        counterexample = str(llm.get("counterexample") or "").strip()
        # 승격 허용: 모호 → 확정 통과.
        if status in ("matched", "equivalent") and evidence:
            merged.append(RequirementCheck(check.requirement_id, status, evidence,
                                           "llm_evidence_found", reason or "LLM이 구현 근거를 찾음"))
        # 강등 허용: 모호 → 위반, 단 구체 근거(반례/누락 SQL) 필수(§5).
        elif status in ("contradicted", "missing") and (counterexample or evidence):
            merged.append(RequirementCheck(check.requirement_id, status, evidence,
                                           "llm_violation_evidenced",
                                           reason + (f" 반례: {counterexample}" if counterexample else "")))
        else:
            merged.append(check)  # 근거 없으면 모호 유지 → review

    ambiguous_terms = [str(t) for t in (data.get("ambiguous_business_terms") or []) if isinstance(t, str)]
    return merged, ambiguous_terms


def make_openai_chat(openai_chat_create: Callable[..., Any], model: str,
                     timeout: float | None = None) -> ChatClient:
    """graph_rag 의 _openai_chat_create 를 ChatClient 로 감싼다(structured/JSON 강제).

    openai_chat_create(client, model=..., messages=..., response_format=..., ...) 형태를 기대한다.
    graph_rag 가 client 를 이미 바인딩해 넘기므로 여기선 파라미터만 채운다."""
    def _chat(system: str, user: str) -> str:
        response = openai_chat_create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            timeout=timeout,
        )
        return response.choices[0].message.content or "{}"
    return _chat
