"""LLM 출력 계약 — LLM 은 **SemanticPlanV2 노드만** 낸다.

낼 수 없는 것(계약으로 차단):
    query_plan / target_user / semantic_ir.missing_fields /
    semantic_ir.unsupported_operations / semantic_ir.status

이 목록이 계약의 핵심이다. 예전에는 LLM 이 실행 슬롯과 결핍 보고와 최종 status 를 함께
냈고, 그 결과 (a) 결정론 코드가 같은 문장을 다시 읽어 슬롯을 채우고 (b) 또 다른 코드가
그 결핍 보고를 사후 삭제하는 3중 구조가 생겼다. LLM 의 산출을 '원문의 의미 노드 + 근거
구간'으로 좁히면 (a)(b) 가 존재할 자리가 없다.

스키마는 `semantic_plan` 의 노드 선언에서 파생한다 — 손으로 쓴 두 번째 권위를 만들지 않는다.
"""

from __future__ import annotations

import copy
import json
import re
from typing import Any, Callable, Mapping, Sequence

import semantic_plan
from semantic_plan import SemanticPlanV2

Completion = Callable[[list[dict[str, str]]], str]

def _compiler_owned_slot_names() -> frozenset[str]:
    """컴파일러 소유 슬롯 이름(파생 — 손 목록을 두면 새 슬롯이 조용히 LLM 에 열린다)."""
    import legacy_plan_compiler  # 순환 없음(컴파일러는 LLM 계약을 모른다)

    return frozenset(slot.rpartition(".")[2] for slot in legacy_plan_compiler.COMPILER_OWNED_SLOTS)


# LLM 이 내면 계약 위반인 키(방출되면 조용히 버리고 위반으로 기록한다).
# ① 실행 플랜 컨테이너 ② 판정 산출물(결핍·미지원·상태) ③ 컴파일러 소유 슬롯 전체.
FORBIDDEN_OUTPUT_KEYS: frozenset[str] = frozenset({
    "query_plan",
    "target_user",
    "semantic_ir",
    "missing_fields",
    "unsupported_operations",
    "status",
}) | _compiler_owned_slot_names()


def semantic_plan_tool() -> dict[str, Any]:
    """strict function-calling tool 정의(노드 선언에서 파생)."""
    parameters = semantic_plan.semantic_plan_json_schema()
    parameters["$defs"] = {"semanticNode": semantic_plan.semantic_node_json_schema()}
    return {
        "type": "function",
        "function": {
            "name": "submit_semantic_plan",
            "description": (
                "사용자 원문에서 확인되는 타겟 조건을 의미 노드로 제출한다. "
                "실행 슬롯·SQL·결핍 목록·상태는 만들지 않는다."
            ),
            "parameters": parameters,
        },
    }


def _forbidden_examples(limit: int = 4) -> str:
    """프롬프트에 넣을 금지 키 예시(파생 — 손 목록을 두면 새 슬롯이 조용히 빠진다)."""
    names = sorted(FORBIDDEN_OUTPUT_KEYS)
    return ", ".join(names[:limit]) + (" 등" if len(names) > limit else "")


# 방출 규칙과 타입 표는 **`semantic_plan` 이 소유한다**(선언 옆). 여기서 다시 쓰지 않는 이유:
# 2026-08-02 이전에는 이 프롬프트가 규칙의 유일 보관처였는데 프로덕션 경로
# (`query_structurer.campaign_plan_v4`)는 이 모듈을 import 하지 않았다 — 그래서 라이브
# 프롬프트에는 노드 타입 이야기가 한 줄도 없었고 거의 모든 요청이 타입 오분류로 막혔다.
SYSTEM_PROMPT = f"""너는 캠페인 타겟팅 요청의 **의미 추출기**다.

너의 산출물은 SemanticPlanV2 의미 노드 목록 하나뿐이다.

절대 만들지 않는 것:
- 실행 슬롯과 실행 플랜 컨테이너({_forbidden_examples()})
- SQL, 테이블명, 컬럼명
- 결핍 목록(missing_fields), 미지원 판정(unsupported_operations), 최종 상태(status)
  → 이 셋은 시스템이 네 노드에서 **계산**한다. 네가 판단하지 마라.

값은 원문 표현 그대로 써도 된다('10만 원', '이상', '2026년 2월', '상위 10%').
시스템이 정규화한다. 계수 단위를 다른 단위로 바꾸지 마라.

{semantic_plan.node_type_guidance()}
"""


def _node_type_guide() -> str:
    return semantic_plan.node_type_guidance()


def build_user_prompt(
    query: str,
    *,
    vocabulary: Mapping[str, Sequence[str]] | None = None,
    focus_spans: Sequence[str] = (),
) -> str:
    sections = [f"[원문]\n{query}", "", _node_type_guide()]
    if vocabulary:
        listed = []
        for name, values in vocabulary.items():
            shown = sorted({str(value) for value in values if str(value).strip()})
            if shown:
                listed.append(f"- {name}: {', '.join(shown[:80])}")
        if listed:
            sections += [
                "",
                "[사용 가능한 canonical 값] — metric/entity/scope 는 되도록 이 목록에서 고른다. "
                "목록에 없으면 원문 표현을 그대로 써라(시스템이 해소한다).",
                *listed,
            ]
    if focus_spans:
        sections += [
            "",
            "[재추출 요청] 아래 구절의 조건이 이전 제출에서 빠졌다. **이 구절만** 읽고 해당하는 "
            "의미 노드를 만들어라. 다른 구절의 노드는 만들지 마라.",
            *[f"- {span}" for span in focus_spans],
        ]
    sections += [
        "",
        "submit_semantic_plan 도구로 nodes 배열을 제출하라.",
    ]
    return "\n".join(sections)


class SemanticPlanContractError(ValueError):
    """LLM 출력이 SemanticPlanV2 계약을 위반했을 때."""


def parse_semantic_plan_response(
    response: Any, query: str, *, producer: str = "llm"
) -> tuple[SemanticPlanV2, list[str]]:
    """LLM 응답 → (SemanticPlanV2, 계약 위반 목록).

    금지 키가 오면 **버리고 위반으로 기록한다** — 받아들이면 LLM 이 다시 최종 플랜의
    생산자가 된다.
    """
    payload = response
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise SemanticPlanContractError(f"SemanticPlan 응답이 JSON 이 아니다: {exc}") from exc
    if not isinstance(payload, dict):
        raise SemanticPlanContractError("SemanticPlan 응답은 객체여야 한다")

    violations = sorted(FORBIDDEN_OUTPUT_KEYS & set(payload))
    cleaned = {key: value for key, value in payload.items() if key not in FORBIDDEN_OUTPUT_KEYS}
    try:
        plan = semantic_plan.plan_from_dict(cleaned, source_query=query, producer=producer)
    except semantic_plan.SemanticPlanError as exc:
        raise SemanticPlanContractError(str(exc)) from exc
    _repair_spans(plan, query)
    return plan, [f"llm_emitted_forbidden_key:{key}" for key in violations]


def _repair_spans(plan: SemanticPlanV2, query: str) -> None:
    """모호하지 않은 좌표만 고친다 — 근거 문구 자체는 절대 만들어내지 않는다."""
    for node in plan.walk():
        text = node.source_span
        start, end = node.source_start, node.source_end
        if (
            isinstance(start, int) and isinstance(end, int)
            and 0 <= start <= end <= len(query) and query[start:end] == text
        ):
            continue
        if not text:
            continue
        occurrences = [match.start() for match in re.finditer(re.escape(text), query)]
        if len(occurrences) == 1:
            node.source_start = occurrences[0]
            node.source_end = occurrences[0] + len(text)
        else:
            node.source_start = node.source_end = None


class SemanticPlanExtractor:
    """1차 추출 + uncovered span 재추출(둘 다 LLM). 결정론 파서는 여기 없다."""

    def __init__(
        self,
        complete: Completion,
        *,
        vocabulary: Mapping[str, Sequence[str]] | None = None,
        on_event: Callable[[str, dict[str, Any]], None] | None = None,
        max_retries: int = 1,
    ) -> None:
        self._complete = complete
        self._vocabulary = dict(vocabulary or {})
        self._on_event = on_event
        self._max_retries = max_retries

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        if self._on_event is not None:
            self._on_event(event, payload)

    def _call(self, query: str, *, focus_spans: Sequence[str] = ()) -> tuple[SemanticPlanV2, list[str]]:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(query, vocabulary=self._vocabulary,
                                                          focus_spans=focus_spans)},
        ]
        last_error = "unknown"
        for attempt in range(self._max_retries + 1):
            try:
                raw = self._complete(messages)
                plan, violations = parse_semantic_plan_response(raw, query)
                self._emit("semantic_plan_extracted", {
                    "attempt": attempt + 1,
                    "nodes": len(plan.nodes),
                    "violations": violations,
                    "focus_spans": list(focus_spans),
                })
                return plan, violations
            except (SemanticPlanContractError, semantic_plan.SemanticPlanError, TypeError, ValueError) as exc:
                last_error = f"{exc.__class__.__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 — 공급자 실패는 내부 사고로 보고한다.
                last_error = f"{exc.__class__.__name__}: {exc}"
            self._emit("semantic_plan_attempt_failed", {"attempt": attempt + 1, "error": last_error})
        raise SemanticPlanContractError(last_error)

    def extract(self, query: str) -> tuple[SemanticPlanV2, list[str]]:
        return self._call(query)

    def reextract(self, query: str, spans: Sequence[str]) -> tuple[SemanticPlanV2, list[str]]:
        """uncovered span 만 다시 읽는다. 전체 재생성이 아니다."""
        return self._call(query, focus_spans=list(spans))


def contract_documentation() -> dict[str, Any]:
    """계약 문서(테스트가 읽는다) — LLM 이 만들 수 있는 것과 없는 것."""
    return {
        "produces": ["semantic_plan.nodes"],
        "forbidden": sorted(FORBIDDEN_OUTPUT_KEYS),
        "node_types": semantic_plan.node_requirement_documentation(),
        "tool": copy.deepcopy(semantic_plan_tool()),
    }


__all__ = [
    "Completion",
    "FORBIDDEN_OUTPUT_KEYS",
    "SYSTEM_PROMPT",
    "SemanticPlanContractError",
    "SemanticPlanExtractor",
    "build_user_prompt",
    "contract_documentation",
    "parse_semantic_plan_response",
    "semantic_plan_tool",
]
