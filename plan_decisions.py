"""조건 결정 감사 로그 — 어느 **필터**가, 어떤 **액션**을, 어느 **슬롯**에, 왜(**사유**) 했는가.

배경: 지금까지 플랜은 '최종 모습'만 답할 수 있었다. 조건이 SQL에 없을 때 그것이

  * 애초에 파싱되지 않은 것인지,
  * 파싱됐다가 다른 조건이 소유권을 가져간 것인지(:mod:`slot_ownership`),
  * 스테이지 정리(집계 계약·축 정규화 등)가 지운 것인지,
  * 빌더가 표현하지 못해 떨어뜨린 것인지

를 구분하려면 매번 코드를 거슬러 읽어야 했다. 이 모듈은 그 궤적을 플랜 안에 남긴다 —
``plan["decisions"]`` 는 (filter, action, slot, reason) 네 필드를 필수로 갖는 append-only 로그이고,
응답 디버그(``debug.decisions``)와 트레이스 3단계로 그대로 노출된다.

기록 방법은 두 가지다.

  1. **명시 기록**(:func:`record`) — 소유권 회수·스테이지 드롭·빌더 채택처럼 사유가 코드에 있는 결정.
  2. **차이 기록**(:func:`snapshot` + :func:`record_changes`) — 스테이지 실행 전후 슬롯 상태를 비교해
     실제로 바뀐 것만 남긴다. 필터가 무엇을 건드렸는지 선언하지 않아도 되므로, 새 필터가 추가돼도
     감사 로그에 등록하는 것을 잊어 조용히 빠지는 일이 없다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 상태는 전부 plan dict 안에 산다.
"""

from __future__ import annotations

from typing import Any


# 감사 로그(공개 키 — 응답 디버그로 나간다).
DECISIONS_KEY = "decisions"
# 사유를 명시해 기록을 '시도한' 슬롯의 자취(내부용). 중복이라 접힌 기록도 남겨야, 두 번째 파이프라인
# 패스에서 같은 변화가 사유 없는 차이 기록으로 다시 잡히는 것을 막는다.
_MARKS_KEY = "_decision_marks"
_MAX_MARKS = 500
# 로그가 상한에 걸려 잘렸다는 표식. 잘린 사실을 숨기면 "전부 기록됐다"로 읽힌다.
TRUNCATED_KEY = "decisions_truncated"
MAX_DECISIONS = 400

# 액션 어휘. 새 값을 늘리기 전에 기존 액션으로 표현되는지 먼저 본다(진단이 목적이지 분류가 목적이 아니다).
SET = "set"                  # 빈 슬롯을 채웠다
UPDATE = "update"            # 이미 있던 값을 다른 값으로 바꿨다
CLEAR = "clear"              # 값을 비웠다(같은 스테이지가 스스로 회수)
CLAIM = "claim"              # 다른 조건이 소유권을 주장해 회수했다(slot_ownership)
KEEP = "keep"                # 회수 시도했지만 다른 절 소유라 보존했다(slot_ownership)
DROP = "drop"                # 스테이지가 조건을 버렸다(소유권 이동이 아닌 정리)
SELECT = "select"            # 후보(빌더/SQL)를 채택했다
REJECT = "reject"            # 후보를 거부했다
UNSUPPORTED = "unsupported"  # 표현은 인식했으나 물리 매핑이 없어 fail-close 했다

ACTIONS = frozenset({SET, UPDATE, CLEAR, CLAIM, KEEP, DROP, SELECT, REJECT, UNSUPPORTED})

# 조건 슬롯이 사는 컨테이너. 이 dict 들은 슬롯 단위로 펼쳐 감사한다.
AUDITED_CONTAINERS = ("target_user", "exclude", "campaign_constraints")
# plan 최상위 중 '조건'이 아닌 항목 — 원문·검색어·계측·스냅샷은 바뀌어도 결정이 아니다.
NON_CONDITION_PLAN_KEYS = frozenset({
    "raw_query", "original_query", "normalized_query", "planning_query", "schema_version",
    "query_identity_digest",
    "strict_source_coverage",
    "retrieval", "matched_terms", "parser", "complexity", "query_semantics",
    "source_requirements", "source_requirements_digest", "structured_query",
    "plan_resolution",
    "recognized_domains", "superseded_conditions", "failure_log",
    DECISIONS_KEY, TRUNCATED_KEY, *AUDITED_CONTAINERS,
})

# 로그에 싣는 값의 상한(진단용 요약이지 값 저장소가 아니다).
_MAX_VALUE_CHARS = 300


class _Unset:
    pass


_UNSET = _Unset()


def decisions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """이 플랜에 기록된 결정 목록(없으면 빈 리스트)."""
    entries = plan.get(DECISIONS_KEY)
    return entries if isinstance(entries, list) else []


def _jsonable(value: Any, depth: int = 0) -> Any:
    """응답 직렬화가 깨지지 않게 값을 요약한다(로그는 진단용이므로 손실 요약이 허용된다)."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_VALUE_CHARS else value[:_MAX_VALUE_CHARS] + "…"
    if depth >= 3:
        return _jsonable(repr(value), depth)
    if isinstance(value, dict):
        return {str(key): _jsonable(item, depth + 1) for key, item in list(value.items())[:20]}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
        return [_jsonable(item, depth + 1) for item in items[:20]]
    return _jsonable(repr(value), depth)


def record(
    plan: dict[str, Any],
    *,
    filter_name: str,
    action: str,
    slot: str,
    reason: str,
    value: Any = _UNSET,
    **fields: Any,
) -> dict[str, Any] | None:
    """결정 하나를 기록한다. 같은 판정의 중복 적재는 접는다(파이프라인은 결정론 스테이지를 두 번 돈다).

    반환: 새로 적재된 기록(중복이거나 상한 초과면 None).
    """
    entries = plan.get(DECISIONS_KEY)
    if not isinstance(entries, list):
        entries = []
        plan[DECISIONS_KEY] = entries

    entry: dict[str, Any] = {
        "seq": len(entries),
        "filter": str(filter_name),
        "action": str(action),
        "slot": str(slot),
        "reason": str(reason),
    }
    if not isinstance(value, _Unset):
        entry["value"] = _jsonable(value)
    for key, item in fields.items():
        if item is not None:
            entry[key] = _jsonable(item)

    marks = plan.get(_MARKS_KEY)
    if not isinstance(marks, list):
        marks = []
        plan[_MARKS_KEY] = marks
    marks.append((len(entries), entry["slot"]))
    if len(marks) > _MAX_MARKS:
        del marks[:-_MAX_MARKS]

    signature = _signature_of(entry)
    if any(_signature_of(existing) == signature for existing in entries):
        return None
    if len(entries) >= MAX_DECISIONS:
        plan[TRUNCATED_KEY] = True
        return None
    entries.append(entry)
    return entry


def _signature_of(entry: dict[str, Any]) -> tuple[Any, ...]:
    return (entry.get("filter"), entry.get("action"), entry.get("slot"), repr(entry.get("value")))


def _is_empty(value: Any) -> bool:
    return value is None or value == [] or value == {} or value == "" or value is False


def _slot_key(container: str, slot: str) -> str:
    return f"{container}.{slot}"


def snapshot(plan: dict[str, Any]) -> dict[str, str]:
    """조건 슬롯의 현재 상태를 ``"컨테이너.슬롯" -> 값 서명`` 으로 찍는다.

    값이 빈 슬롯(None/[]/{}/""/False)은 담지 않는다 — 선초기화(setdefault)가 결정으로 잡히면
    로그가 잡음으로 덮인다. 그래서 '없다 → 있다'는 :data:`SET`, '있다 → 없다'는 :data:`CLEAR` 로 갈린다.
    """
    state: dict[str, str] = {}
    for key, value in plan.items():
        if not isinstance(key, str) or key.startswith("_"):
            continue
        if key in AUDITED_CONTAINERS:
            if not isinstance(value, dict):
                continue
            for slot, slot_value in value.items():
                if isinstance(slot, str) and not slot.startswith("_") and not _is_empty(slot_value):
                    state[_slot_key(key, slot)] = repr(slot_value)
            continue
        if key in NON_CONDITION_PLAN_KEYS or _is_empty(value):
            continue
        state[_slot_key("plan", key)] = repr(value)
    return state


def read_slot(plan: dict[str, Any], slot_key: str) -> Any:
    """``"target_user.gender"`` 같은 슬롯 키의 현재 값."""
    container, _, slot = slot_key.partition(".")
    holder = plan if container == "plan" else plan.get(container)
    if not isinstance(holder, dict):
        return None
    return holder.get(slot)


def record_changes(
    plan: dict[str, Any],
    before: dict[str, str],
    *,
    filter_name: str,
    reason: str,
    since: int,
    evidence: Any = None,
) -> list[dict[str, Any]]:
    """``before`` 스냅샷 이후 실제로 바뀐 슬롯만 기록한다.

    ``since`` 는 이 스테이지가 시작될 때의 로그 길이다(기본값을 두지 않는다 — 0 을 넘기면 이전
    스테이지가 건드린 슬롯까지 '이미 기록됨'으로 보고 전부 건너뛴다). 그 이후 **명시 기록**(회수/드롭)이 남긴
    슬롯은 건너뛴다 — 같은 변화를 사유 있는 기록과 사유 없는 차이 기록으로 두 번 남기지 않는다.
    ``evidence`` 가 호출 가능하면 슬롯 키를 받아 근거(원문 구간 등)를 슬롯별로 만들어 붙인다.
    """
    marks = plan.get(_MARKS_KEY) if isinstance(plan.get(_MARKS_KEY), list) else []
    explicit = {slot for index, slot in marks if index >= since}
    after = snapshot(plan)
    recorded: list[dict[str, Any]] = []

    def _evidence(slot_key: str) -> Any:
        return evidence(slot_key) if callable(evidence) else evidence

    for slot_key, signature in after.items():
        if slot_key in explicit or before.get(slot_key) == signature:
            continue
        action = SET if slot_key not in before else UPDATE
        entry = record(
            plan, filter_name=filter_name, action=action, slot=slot_key, reason=reason,
            value=read_slot(plan, slot_key),
            previous=before.get(slot_key) if action == UPDATE else None,
            evidence=_evidence(slot_key),
        )
        if entry is not None:
            recorded.append(entry)

    for slot_key, signature in before.items():
        if slot_key in explicit or slot_key in after:
            continue
        entry = record(
            plan, filter_name=filter_name, action=CLEAR, slot=slot_key, reason=reason,
            value=None, previous=signature, evidence=_evidence(slot_key),
        )
        if entry is not None:
            recorded.append(entry)
    return recorded


def drop_slots(
    plan: dict[str, Any],
    targets: "list[tuple[str, str]] | tuple[tuple[str, str], ...]",
    *,
    owner: str,
    reason: str,
    mode: str = "pop",
    empty: Any = _UNSET,
) -> list[dict[str, Any]]:
    """``(컨테이너, 슬롯)`` 목록을 사유와 함께 버린다(``plan.pop(...)`` 의 기록 남기는 판).

    슬롯 이름이 ``"behaviors:no_purchase"`` 형태면 리스트에서 그 값만 뺀다. ``mode="pop"`` 은 키를
    제거하고 ``"clear"`` 는 타입에 맞춰(None/[]) 비운다 — 둘 다 기존 pop/대입 동작과 같다.
    슬롯이 항상 리스트여야 하는 등 빈 값이 정해져 있으면 ``empty`` 로 못 박는다(타입 추론 금지).
    기록은 실제로 값이 사라졌을 때만 남긴다(무동작 pop 은 결정이 아니다).
    """
    recorded: list[dict[str, Any]] = []
    for container, slot in targets:
        name, _, value = slot.partition(":")
        holder = plan if container == "plan" else plan.get(container)
        if not isinstance(holder, dict):
            continue
        current = holder.get(name)
        if value:
            if not isinstance(current, list) or value not in current:
                continue
            holder[name] = [item for item in current if item != value]
            removed: Any = value
        else:
            removed = current
            if mode == "pop":
                holder.pop(name, None)
            elif not isinstance(empty, _Unset):
                holder[name] = empty
            elif isinstance(current, list):
                holder[name] = []
            else:
                holder[name] = None
            if _is_empty(removed):
                continue
        entry = record(
            plan, filter_name=owner, action=DROP, slot=_slot_key(container, slot),
            reason=reason, value=removed,
        )
        if entry is not None:
            recorded.append(entry)
    return recorded


def render(plan: dict[str, Any], limit: int = 40) -> list[str]:
    """사람이 읽는 한 줄 요약(트레이스 표시용). ``필터 → 액션 슬롯: 사유`` 형태."""
    lines = []
    for entry in decisions(plan)[:limit]:
        if not isinstance(entry, dict):
            continue
        head = f"{entry.get('filter')} → {entry.get('action')} {entry.get('slot')}"
        evidence = entry.get("evidence")
        tail = f": {entry.get('reason')}" if entry.get("reason") else ""
        lines.append(head + tail + (f" (근거: {evidence})" if evidence else ""))
    remaining = len(decisions(plan)) - len(lines)
    if remaining > 0:
        lines.append(f"… 외 {remaining}건 (전체는 debug.decisions)")
    return lines
