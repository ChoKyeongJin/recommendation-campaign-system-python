"""의미 노드의 **영수증** — 이 노드가 실행 가능한 조건으로 귀결됐다는 증거.

구조화기가 만든 의미 노드는 여러 컴파일러 중 하나에 착지한다. 착지하면 각 컴파일러가 자기
어휘로 영수증을 남긴다. 문제는 **착지하지 못한 노드**다. 지금까지 그 노드는 조용히 사라졌고,
남은 조건만으로 만들어진 SQL 이 성공으로 나갔다.

실측(2026-08-02, 재현 완료): "여성이면서 정상에서 휴면으로 바뀐 회원" 이
``WHERE B.MEMBER_STATE_CD='...NORMAL' AND B.GENDER_CD='...FEMALE'`` 로 출고됐다. 이력 절이
사라졌을 뿐 아니라 기본 정상회원 정책 필터 때문에 **요청과 정반대 대상**(휴면이 된 회원 →
현재 정상인 회원)이 뽑혔다. 경고 0, 되묻기 0.

드롭 감지기를 늘려서는 이걸 막을 수 없다 — 감지기는 표면어 목록이고 이 축에는 감지기가 없다.
막는 방법은 구조적이다: **노드가 있는데 영수증이 없으면 SQL 을 내지 않는다.**

판정은 노드 단위 fail-close 다. 형제 노드가 컴파일됐다는 사실은 이 노드의 면제 사유가 아니다 —
"조건 하나가 빠진 SQL" 은 부분 정답이 아니라 다른 대상이기 때문이다.

영수증 어휘가 두 벌이라는 점에 주의(둘 다 같은 ``event_expression.receipts`` 에 실린다):
  · ``campaign_plan_v4._audience_receipts`` → node_id="audience-atom-N", status="compiled"
  · ``semantic_plan_event_lowering.LoweringReceipt`` → node_id=실제 노드 id, status∈{lowered, failed}
미지의 status 는 인정하지 않는다(fail-close) — 새 어휘가 생겼는데 여기가 조용히 통과시키면
게이트가 꺼진 채로 초록이 된다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

import plan_decisions
import semantic_plan
import semantic_plan_event_lowering
import semantic_requirements

PLAN_NODES_KEY = "semantic_plan"
PIPELINE_KEY = "semantic_pipeline"
EVENT_EXPRESSION_KEY = "event_expression"

# 영수증으로 인정하는 status. 두 생산자의 어휘를 여기 한 곳에 모은다.
ACCEPTED_RECEIPT_STATUSES = frozenset({"compiled", "lowered"})

UNRECEIPTED_CODE = "semantic_plan_node_unreceipted"
_DEFAULT_REASON = (
    "'{label}' 조건이 실행 가능한 타겟 조건으로 컴파일되지 않았습니다. "
    "나머지 조건만으로 SQL 을 내보내면 다른 대상이 추출되므로 요청 전체를 중단했습니다. "
    "이 조건을 빼고 요청하시거나 지원되는 표현으로 바꿔 주세요."
)


def _nodes(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    payload = plan.get(PLAN_NODES_KEY)
    if not isinstance(payload, Mapping):
        return []
    nodes = payload.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, Mapping) and node.get("id")]


def receipted_node_ids(plan: Mapping[str, Any]) -> frozenset[str]:
    """영수증이 있는 노드 id 집합(두 생산자의 어휘를 합집합)."""
    receipted: set[str] = set()

    payload = plan.get(EVENT_EXPRESSION_KEY)
    if isinstance(payload, Mapping):
        for receipt in payload.get("receipts") or []:
            if not isinstance(receipt, Mapping):
                continue
            if str(receipt.get("status") or "") in ACCEPTED_RECEIPT_STATUSES:
                node_id = str(receipt.get("node_id") or "")
                if node_id:
                    receipted.add(node_id)

    pipeline = plan.get(PIPELINE_KEY)
    if isinstance(pipeline, Mapping):
        node_slots = pipeline.get("node_slots")
        if isinstance(node_slots, Mapping):
            receipted.update(str(node_id) for node_id in node_slots if node_id)

    return frozenset(receipted)


def _node_reason(plan: Mapping[str, Any], node: Mapping[str, Any], label: str) -> str:
    """왜 착지하지 못했는가. 더 구체적인 사유가 있으면 그것을 쓴다.

    사유가 비면 상위 표시기가 기본 문구로 뭉개고, 그건 이 게이트가 없애려는 바로 그 침묵이다.
    """
    node_id = str(node.get("id") or "")

    relational = plan.get("relational_ir")
    if isinstance(relational, Mapping) and relational.get("message"):
        return str(relational["message"])

    payload = plan.get(EVENT_EXPRESSION_KEY)
    if isinstance(payload, Mapping):
        for receipt in payload.get("receipts") or []:
            if not isinstance(receipt, Mapping) or str(receipt.get("node_id") or "") != node_id:
                continue
            message = receipt.get("message") or receipt.get("failure_code")
            if message:
                return f"'{label}' 조건을 실행 의미로 내리지 못했습니다({message})."

    for hypothesis in plan.get("audience_unsupported_hypotheses") or []:
        if isinstance(hypothesis, Mapping) and hypothesis.get("reason"):
            return f"'{label}' 조건: {hypothesis['reason']}"

    return _DEFAULT_REASON.format(label=label)


def unreceipted_nodes(plan: Mapping[str, Any], query: str) -> list[dict[str, Any]]:
    """영수증 없는 노드마다 미해결 원문 조건 행을 만든다(SQL 출고를 막는 채널)."""
    nodes = _nodes(plan)
    if not nodes:
        return []
    receipted = receipted_node_ids(plan)

    rows: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node["id"])
        if node_id in receipted:
            continue
        label = str(node.get("source_span") or node.get("type") or node_id)
        rows.append(
            {
                "id": "usr_" + hashlib.sha256(
                    f"{query}\0{node_id}\0{UNRECEIPTED_CODE}".encode("utf-8")
                ).hexdigest()[:16],
                "path": f"source_coverage.semantic_plan.{node_id}",
                "label": label,
                "source_text": query,
                "reason": _node_reason(plan, node, label),
                "code": UNRECEIPTED_CODE,
                "status": "unresolved",
                "source": "semantic_plan_receipt",
            }
        )
    return rows


def discharge_attribute_obligations(query_plan: dict[str, Any], query: str, result: Any) -> None:
    """canonical 이 이력 절을 컴파일했으면 그 원문 의무의 이행자도 canonical 이다.

    의무는 원래 legacy 컴파일러(compositional_targeting)만 이행자로 등록돼 있었다. 흡수 후에도
    그대로 두면 canonical 이 컴파일한 절이 '근거 없음'으로 남아 SQL 이 막힌다 — 컴파일은 됐는데
    막히는, 사용자에게 설명할 수 없는 상태다.
    """
    semantic_requirements.discharge_source_semantic_obligations(
        query_plan, query,
        kinds={semantic_requirements.TEMPORAL_QUALIFIER_KIND},
        status="compiled", compiler="canonical_event_ir",
        evidence={"receipts": [receipt.to_dict() for receipt in result.receipts]},
    )


def merge_into_event_expression(
    query_plan: dict[str, Any],
    query: str,
    *,
    catalog: Any,
    plan_key: str,
    expression_key: str,
) -> None:
    """남은 의미 노드를 이미 선 canonical 표현에 합류시킨다.

    실패는 여기서 판정하지 않는다 — 아무것도 바꾸지 않고 돌아가면 영수증 게이트가 막는다.
    """
    try:
        plan = semantic_plan.plan_from_dict(query_plan[plan_key], source_query=query)
        payload, result = semantic_plan_event_lowering.merge_event_expression(
            query_plan.get(expression_key), plan, catalog
        )
    except Exception as exc:  # noqa: BLE001 — 배선 사고를 '미지원'으로 표시하면 거짓 선언이 된다.
        plan_decisions.record(
            query_plan, filter_name="semantic_plan_event_merge", action=plan_decisions.REJECT,
            slot=expression_key, reason=f"합류 중 내부 오류: {exc.__class__.__name__}: {exc}",
        )
        return

    if payload is not None:
        query_plan[expression_key] = payload
        discharge_attribute_obligations(query_plan, query, result)
    plan_decisions.record(
        query_plan, filter_name="semantic_plan_event_merge",
        action=plan_decisions.SELECT if payload is not None else plan_decisions.REJECT,
        slot=expression_key,
        reason="의미 노드를 같은 Event IR 로 합류" if payload is not None else "; ".join(
            receipt.failure_code or receipt.message or "lowering_failed"
            for receipt in result.failures
        ) or "합류 실패",
    )
