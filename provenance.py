"""슬롯 출처(provenance) 정규화 — 각 조건이 **무엇에 의해** 만들어졌는가.

이미 세 종류의 출처 기록이 있다.

  * :mod:`plan_decisions` — 어느 필터가, 어떤 액션을, 어느 슬롯에, 왜 (``plan["decisions"]``)
  * :mod:`slot_ownership` — 그 값이 원문의 어느 **구간**에서 왔는가 (``plan["_slot_spans"]``)
  * :mod:`plan_resolver` — 어느 **후보**(rules / llm_*)가 그 경로를 이겼는가 (``plan["plan_resolution"]``)

세 번째 기록은 감사 로그와 연결돼 있지 않았다. 후보 병합은 :mod:`plan_decisions` 를 거치지 않으므로,
LLM 후보가 채운 슬롯(예: ``purchase_object``)은 감사 로그에 **한 줄도 남지 않는다** — 정작 shadow
비교에서 귀속이 가장 필요한 값들이다. 이 모듈이 두 기록을 합쳐 그 구멍을 덮는다(병합 경로는
건드리지 않는다 — 읽기 전용 통합이라 회귀 위험이 없다).

빠져 있던 축이 하나 있다: **방법(method)**. 이 슬롯을 채운 것이 코드에 박힌 정규식인가, 데이터
사전인가, LLM 인가. 이 축이 없으면

  * shadow 모드에서 LLM 과 규칙을 비교해도 "어느 쪽이 만든 값인지"를 사후에 귀속할 수 없고,
  * 슬롯 단위로 LLM-first 를 켜고 끄는 전환을 안전하게 못 하며,
  * "정규식을 렉시콘으로 옮기는 중"이라는 이행 상태를 숫자로 볼 수 없다.

방법은 새로 심는 게 아니라 **이미 있는 생산자 이름 규약을 고정**해서 얻는다. 감사 로그의 ``filter``
필드는 이미 네임스페이스가 갈려 있다(``filter:purchase_date`` / ``normalization:vip`` / 함수명).
:data:`PRODUCER_PREFIXES` 가 그 규약을 계약으로 못 박고, 계약 테스트가 미분류 생산자를 막는다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다(plain dict 입력).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

import plan_decisions
import slot_ownership

# ── 방법(method) 어휘 ─────────────────────────────────────────────────────────
RULE = "rule"              # 코드에 박힌 결정론 정규식/파서 (_FilterSpec 등). 줄여야 할 대상.
LEXICON = "lexicon"        # 데이터 사전(정규화 룰·타겟팅 렉시콘) 매칭. 코드 수정 없이 늘릴 수 있다.
LLM = "llm"                # LLM 구조화 추출(닫힌 슬롯 스키마 경유).
STRUCTURED = "structured"  # 호출자가 이미 구조화해 넣어준 입력(structured_query / query_plan_v2).
STAGE = "stage"            # 파생·정리 스테이지(다른 슬롯에서 계산된 것 — 원문 해석이 아니다).
POLICY = "policy"          # 업무 정책/기본값 주입.

METHODS = frozenset({RULE, LEXICON, LLM, STRUCTURED, STAGE, POLICY})

# 원문을 해석해 조건을 만든 방법(= 이행 지표의 분모). STAGE/POLICY 는 파생이라 제외한다.
PARSING_METHODS = frozenset({RULE, LEXICON, LLM, STRUCTURED})

# 생산자 이름 접두어 → 방법. 감사 로그가 쓰는 이름 규약의 계약이다.
# 새 파서 경로를 열 때는 여기 접두어를 추가하고 그 이름으로 기록해야 한다(그래야 이행 지표에 잡힌다).
PRODUCER_PREFIXES: tuple[tuple[str, str], ...] = (
    ("filter:", RULE),
    ("rule:", RULE),
    ("normalization:", LEXICON),
    ("lexicon:", LEXICON),
    ("llm:", LLM),
    ("structured:", STRUCTURED),
    ("policy:", POLICY),
)

# 접두어 규약 밖의 생산자(스테이지 함수는 맨 이름으로 기록된다). 함수명 형태는 STAGE 로 본다.
# 예외(맨 이름인데 실제로는 원문 파서인 것)는 여기 명시적으로 등록한다 — 자동 추론에 맡기면
# 이행 지표가 조용히 틀린다.
PRODUCER_OVERRIDES: dict[str, str] = {
    "apply_member_metric_ranking_target": RULE,
    "apply_campaign_response_frequency_filter": RULE,
    "apply_core_membership_semantics": STAGE,
    "attach_query_output_contract": STAGE,
    "drop_dimension_consumed_set_expressions": STAGE,
}

UNKNOWN = "unknown"

# 플랜 후보 출처(plan_resolver.PlanCandidate.source) → 방법. 후보 병합은 감사 로그를 거치지 않으므로
# 이 표가 그 경로의 유일한 귀속 수단이다. 새 후보 출처를 추가하면 여기도 한 줄 추가해야 한다
# (계약 테스트가 미분류를 잡는다).
CANDIDATE_SOURCE_METHODS: dict[str, str] = {
    "rules": RULE,
    "llm_object_fallback": LLM,
    "llm_query_structurer": LLM,
    "structured_query": STRUCTURED,
    "query_plan_v2": STRUCTURED,
}

# 후보 병합에서 온 슬롯의 생산자 이름 접두어(감사 로그의 filter 이름과 구분된다).
CANDIDATE_PRODUCER_PREFIX = "candidate:"


def method_of(producer: Any) -> str:
    """생산자 이름 → 방법. 규약 밖이면 :data:`UNKNOWN` (계약 테스트가 잡는다)."""
    if not isinstance(producer, str) or not producer:
        return UNKNOWN
    if producer in PRODUCER_OVERRIDES:
        return PRODUCER_OVERRIDES[producer]
    if producer.startswith(CANDIDATE_PRODUCER_PREFIX):
        return CANDIDATE_SOURCE_METHODS.get(producer[len(CANDIDATE_PRODUCER_PREFIX):], UNKNOWN)
    for prefix, method in PRODUCER_PREFIXES:
        if producer.startswith(prefix):
            return method
    # 접두어 없는 이름은 파이프라인 스테이지 함수라는 것이 현행 규약이다. 접두어 규약을 쓰는
    # 파서가 실수로 여기 떨어지지 않도록, 콜론이 들어간 미지의 네임스페이스는 UNKNOWN 으로 남긴다.
    return UNKNOWN if ":" in producer else STAGE


def _final_decision_by_slot(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """슬롯별 **마지막** 값 설정 결정. 나중 기록이 현재 값을 설명하므로 뒤에서 덮어쓴다."""
    latest: dict[str, dict[str, Any]] = {}
    for entry in plan_decisions.decisions(plan):
        if not isinstance(entry, dict):
            continue
        slot = entry.get("slot")
        if not isinstance(slot, str):
            continue
        if entry.get("action") in {plan_decisions.SET, plan_decisions.UPDATE}:
            latest[slot] = entry
    return latest


def _slot_key_of_path(path: str) -> str | None:
    """후보 병합 경로(``target_user.purchase_object`` / ``result_limit``)를 감사 로그 슬롯 키로 접는다.

    감사 로그는 최상위 키를 ``plan.<key>`` 로 쓰고, 컨테이너 슬롯은 ``<컨테이너>.<슬롯>`` 로 쓴다.
    dict 안쪽까지 내려간 경로(``target_user.purchase_date.from``)는 슬롯 단위로 자른다 — 출처의
    단위는 슬롯이다.
    """
    parts = [part for part in str(path).split(".") if part]
    if not parts:
        return None
    if parts[0] in plan_decisions.AUDITED_CONTAINERS:
        return f"{parts[0]}.{parts[1]}" if len(parts) > 1 else None
    return f"plan.{parts[0]}"


def _candidate_owner_by_slot(plan: dict[str, Any]) -> dict[str, str]:
    """후보 병합이 각 슬롯을 어느 출처에서 가져왔는지(``plan_resolution.resolutions`` 투영).

    같은 슬롯에 여러 기록이 있으면 **처음** 것이 이긴다 — resolutions 는 우선순위 내림차순으로
    쌓이므로 먼저 select 한 후보가 실제 값의 주인이다(뒤의 union 은 덧붙임이다).
    """
    resolution = plan.get("plan_resolution")
    if not isinstance(resolution, dict):
        return {}
    owners: dict[str, str] = {}
    for entry in resolution.get("resolutions") or []:
        if not isinstance(entry, dict):
            continue
        slot_key = _slot_key_of_path(entry.get("path", ""))
        source = entry.get("source")
        if slot_key and isinstance(source, str) and slot_key not in owners:
            owners[slot_key] = source
    return owners


def slot_provenance(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """현재 살아 있는 조건 슬롯 → 출처 기록.

    반환 값의 각 항목::

        {"producer": "filter:purchase_date", "method": "rule",
         "reason": "...", "evidence": "2019년 3월",
         "span": {"start": 0, "end": 8, "text": "2019년 3월"} | None}

    조건 슬롯 목록은 :mod:`ir_snapshot` 이 아니라 감사 로그의 현재 상태(``plan_decisions.snapshot``)
    에서 얻는다 — 이 모듈이 스냅샷 정규화 규칙에 묶이지 않게 하려는 것이다.
    """
    latest = _final_decision_by_slot(plan)
    candidate_owners = _candidate_owner_by_slot(plan)
    out: dict[str, dict[str, Any]] = {}
    for slot_key in plan_decisions.snapshot(plan):
        entry = latest.get(slot_key)
        producer = entry.get("filter") if entry else None
        if producer is None and slot_key in candidate_owners:
            # 감사 로그가 설명하지 못하는 슬롯 — 후보 병합이 채운 값이다(LLM 후보 등).
            producer = f"{CANDIDATE_PRODUCER_PREFIX}{candidate_owners[slot_key]}"
        container, _, slot = slot_key.partition(".")
        span = slot_ownership.slot_span(plan, slot, container=container) if container != "plan" else None
        out[slot_key] = {
            "producer": producer,
            "method": method_of(producer) if producer else UNKNOWN,
            "reason": (entry or {}).get("reason") or (
                f"후보 병합에서 채택됨(감사 로그 없음)" if producer and producer.startswith(CANDIDATE_PRODUCER_PREFIX) else None
            ),
            "evidence": (entry or {}).get("evidence"),
            "span": {k: span[k] for k in ("start", "end", "text") if k in span} if isinstance(span, dict) else None,
        }
    return out


def method_mix(plan: dict[str, Any], *, parsing_only: bool = True) -> Counter:
    """방법별 슬롯 수. 정규식→렉시콘/LLM 이행을 숫자로 보는 지표.

    ``parsing_only`` 면 원문을 해석해 만든 슬롯만 센다(파생 스테이지 제외) — 이행 목표는
    "원문 해석 중 rule 비중을 줄이는 것"이지 파생 스테이지를 없애는 것이 아니다.
    """
    counts: Counter = Counter()
    for record in slot_provenance(plan).values():
        method = record["method"]
        if parsing_only and method not in PARSING_METHODS:
            continue
        counts[method] += 1
    return counts


def unknown_producers(plan: dict[str, Any]) -> list[str]:
    """방법을 분류할 수 없는 생산자 이름. 계약 테스트가 0 을 강제한다."""
    names = {
        record["producer"]
        for record in slot_provenance(plan).values()
        if record["method"] == UNKNOWN and record["producer"]
    }
    return sorted(names)


def render(plan: dict[str, Any]) -> list[str]:
    """사람이 읽는 한 줄 요약(``슬롯 ← 방법/생산자: 근거``)."""
    lines = []
    for slot_key, record in sorted(slot_provenance(plan).items()):
        span = record.get("span") or {}
        evidence = span.get("text") or record.get("evidence") or ""
        tail = f" ← '{evidence}'" if evidence else ""
        lines.append(f"{slot_key} ← {record['method']}/{record['producer']}{tail}")
    return lines
