"""슬롯 출처 구간(span) 기록과 span 기반 소유권 회수.

배경: 조건 소유권 조정이 지금까지 ``plan.pop(...)`` 이었다. 삭제 근거가 '조건의 종류'뿐이어서,
같은 종류이기만 하면 **문장의 다른 절이 만든 조건까지** 함께 지워졌다. 예:

    "2019년 가장 많이 팔린 상품 10개를 2020년 3월에 구매한 고객"

여기서 순위 절(entity_set)이 자기 기간('2019년')을 소유한다는 이유로 ``purchase_date`` 슬롯을
통째로 지우면, 실제로 지워지는 값은 뒤쪽 절의 '2020년 3월'이다 — 조용히 사라진 조건이 되고
어디에도 기록이 남지 않는다.

이 모듈은 두 가지를 준다.

  1. **출처 구간 기록**(:func:`record_slot_span`) — 어느 필터가 원문의 어느 구간을 읽어 슬롯을
     채웠는지. 결정론 파서는 전부 원문 정규식이므로 구간은 부산물로 이미 존재한다.
  2. **span 기반 회수**(:func:`claim_slots`) — 소유를 주장하는 쪽의 구간과 슬롯의 출처 구간이
     **겹칠 때만** 회수한다. 겹치지 않으면(=다른 절이 만든 조건) 그대로 둔다. 구간을 모르면
     기존 동작대로 회수한다(점진 도입: span 을 선언한 슬롯부터 정밀해진다).

회수된 값은 사라지지 않고 ``plan["superseded_conditions"]`` 에 ``superseded_by`` 와 함께 남는다 —
"무엇이 왜 빠졌는지"를 SQL 문자열 대조가 아니라 플랜 자체가 답한다. 같은 판정은
:mod:`plan_decisions` 감사 로그(``plan["decisions"]``)에도 (필터, 액션, 슬롯, 사유)로 적재돼,
파싱·정리·빌더 결정과 한 줄기로 읽힌다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다. 상태는 전부 plan dict 안에 산다.
"""

from __future__ import annotations

from typing import Any

import plan_decisions
from common_utils import compact as _compact_surface


# 슬롯별 출처 구간 저장소(내부용 — 밑줄 접두어 키는 응답 직렬화에서 제외되는 관례를 따른다).
SLOT_SPANS_KEY = "_slot_spans"
# 조건이 '읽어서 소유한' 원문 구간 대장(내부용). 슬롯 회수는 슬롯이 있을 때만 일어나지만, 소유는
# 슬롯 유무와 무관하다 — 뒤에 오는 해석기가 "이 어구는 이미 누가 읽었나"를 물을 곳이 필요하다.
OWNED_SPANS_KEY = "_owned_spans"
# 회수된(=다른 조건에 소유권을 넘긴) 조건 기록. 진단용이므로 공개 키다.
SUPERSEDED_KEY = "superseded_conditions"
# 구간을 알 수 없는 슬롯을 회수했을 때의 사유(기존 동작 = 종류 기준 회수).
UNKNOWN_SPAN_REASON = "span_unknown"

Span = tuple[int, int]


def _as_span(value: Any) -> Span | None:
    """[start, end] 형태(JSON 왕복 후에도 살아남는 list)를 정규화한다."""
    if isinstance(value, dict):
        value = (value.get("start"), value.get("end"))
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start, end = value
    if not isinstance(start, int) or not isinstance(end, int) or isinstance(start, bool) or isinstance(end, bool):
        return None
    if start < 0 or end <= start:
        return None
    return (start, end)


def spans_overlap(left: Any, right: Any) -> bool:
    """두 구간이 한 글자라도 겹치는지. 어느 한쪽이라도 구간을 모르면 False."""
    a, b = _as_span(left), _as_span(right)
    if a is None or b is None:
        return False
    return a[0] < b[1] and b[0] < a[1]


def _container(plan: dict[str, Any], name: str) -> dict[str, Any]:
    """컨테이너 이름("plan" | "target_user" | "exclude" | …) → 실제 dict."""
    if name == "plan":
        return plan
    holder = plan.get(name)
    if not isinstance(holder, dict):
        holder = {}
        plan[name] = holder
    return holder


def _spans(plan: dict[str, Any]) -> dict[str, Any]:
    store = plan.get(SLOT_SPANS_KEY)
    if not isinstance(store, dict):
        store = {}
        plan[SLOT_SPANS_KEY] = store
    return store


def _key(container: str, slot: str) -> str:
    return f"{container}.{slot}"


def _source_compatible(recorded: Any, source_text: str) -> bool:
    """구간이 이 텍스트 좌표계에서 유효한가.

    구간은 그것을 만든 텍스트의 오프셋이다. 파이프라인은 같은 프롬프트의 부분 문자열(채널 접미어를
    떼어낸 파싱용 텍스트 등)을 섞어 쓰므로, 한쪽이 다른 쪽의 **접두어**면 공유 구간의 오프셋이
    동일하다 — 그 경우만 유효로 본다. 재구성 변이처럼 아예 다른 문장이면 무효(→ 기존 동작).
    """
    if not isinstance(recorded, str) or not isinstance(source_text, str):
        return False
    return recorded.startswith(source_text) or source_text.startswith(recorded)


def record_slot_span(
    plan: dict[str, Any],
    slot: str,
    span: Any,
    *,
    source_text: str,
    container: str = "target_user",
    filter_name: str | None = None,
) -> None:
    """슬롯 값이 원문의 어느 구간에서 왔는지 기록한다.

    ``slot`` 은 ``"purchase_date"`` 처럼 슬롯 이름이거나, 리스트 슬롯의 개별 값이면
    ``"behaviors:cart_abandoner"`` 형태다. 같은 슬롯을 다시 기록하면 마지막 기록이 이긴다
    (auto 경로는 결정론 필터를 두 번 돌리므로 덮어쓰기가 곧 멱등이다).
    """
    normalized = _as_span(span)
    if normalized is None:
        return
    store = _spans(plan)
    store[_key(container, slot)] = {
        "start": normalized[0],
        "end": normalized[1],
        "text": source_text[normalized[0]: normalized[1]],
        "source": source_text,
        "filter": filter_name,
    }


def slot_span(plan: dict[str, Any], slot: str, *, container: str = "target_user") -> dict[str, Any] | None:
    """기록된 출처 구간(없으면 None). 진단·테스트용."""
    entry = _spans(plan).get(_key(container, slot))
    return entry if isinstance(entry, dict) else None


# ── 구간 소유 대장 ────────────────────────────────────────────────────────────────────────
# 슬롯 회수(claim_slot)가 "이미 만들어진 값"을 조정하는 장치라면, 이 대장은 "원문의 이 구간은
# 내가 읽었다"는 선언이다. 둘을 나누는 이유: 같은 어구를 **뒤늦게 다시 읽는** 해석기(집계 의도
# 판정 등)는 슬롯을 거치지 않고 플랜 전체를 갈아치우므로, 슬롯 단위 회수로는 막을 수 없다.


def owned_spans(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """조건들이 소유를 선언한 원문 구간 목록."""
    entries = plan.get(OWNED_SPANS_KEY)
    return entries if isinstance(entries, list) else []


def record_owned_span(
    plan: dict[str, Any],
    *,
    owner: str,
    span: Any,
    source_text: str,
    reason: str = "",
) -> None:
    """``owner`` 가 원문의 ``span`` 구간을 읽어 소유했음을 기록한다(같은 선언의 중복은 접는다)."""
    normalized = _as_span(span)
    if normalized is None or not isinstance(source_text, str):
        return
    entries = plan.get(OWNED_SPANS_KEY)
    if not isinstance(entries, list):
        entries = []
        plan[OWNED_SPANS_KEY] = entries
    record = {
        "owner": str(owner),
        "start": normalized[0],
        "end": normalized[1],
        "text": source_text[normalized[0]: normalized[1]],
        "source": source_text,
        "reason": reason,
    }
    signature = (record["owner"], record["start"], record["end"], record["source"])
    for existing in entries:
        if (existing.get("owner"), existing.get("start"), existing.get("end"), existing.get("source")) == signature:
            return
    entries.append(record)


def owning_condition(
    plan: dict[str, Any],
    span: Any = None,
    *,
    source_text: str,
    surface: str | None = None,
) -> dict[str, Any] | None:
    """이 구간(또는 표면형)을 이미 소유한 조건. 없으면 None.

    판정 순서는 좌표가 먼저다. 좌표계가 같으면(``_source_compatible``) 겹침 여부가 곧 답이고,
    **겹치지 않으면 문장의 다른 절이므로 표면 비교로 뒤집지 않는다** — 같은 낱말이 두 절에 나오는
    문장에서 남의 절 근거까지 소유로 삼는 것을 막는다. 좌표계가 다를 때만(재작성본 vs 원문)
    같은 표현인지로 판정한다.
    """
    normalized = _as_span(span)
    needle = _compact_surface(surface) if isinstance(surface, str) else ""
    for entry in owned_spans(plan):
        if not isinstance(entry, dict):
            continue
        entry_span = _as_span((entry.get("start"), entry.get("end")))
        if entry_span is None:
            continue
        if normalized is not None and _source_compatible(entry.get("source"), source_text):
            if spans_overlap(normalized, entry_span):
                return entry
            continue
        if needle and needle in _compact_surface(str(entry.get("text") or "")):
            return entry
    return None


def superseded_conditions(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """회수(또는 회수 시도 후 보존)된 조건 기록."""
    entries = plan.get(SUPERSEDED_KEY)
    return entries if isinstance(entries, list) else []


def _append_record(plan: dict[str, Any], record: dict[str, Any]) -> None:
    """같은 판정의 중복 적재를 막고 기록한다(소유권 해소는 파이프라인에서 두 번 실행될 수 있다).

    같은 판정을 감사 로그에도 흘린다 — 소유권 회수만 별도 키에 남으면 "이 조건이 왜 없나"를
    두 곳(superseded_conditions / decisions)에서 따로 읽어야 한다."""
    plan_decisions.record(
        plan,
        filter_name=record["owner"],
        action=plan_decisions.CLAIM if record.get("outcome") == "removed" else plan_decisions.KEEP,
        slot=record["slot"],
        reason=record.get("reason", ""),
        value=record.get("value"),
        evidence=record.get("source_text"),
        detail=record.get("detail"),
    )
    entries = plan.get(SUPERSEDED_KEY)
    if not isinstance(entries, list):
        entries = []
        plan[SUPERSEDED_KEY] = entries
    signature = (record["owner"], record["slot"], record["outcome"], repr(record.get("value")))
    for existing in entries:
        if (existing.get("owner"), existing.get("slot"), existing.get("outcome"), repr(existing.get("value"))) == signature:
            return
    entries.append(record)


def claim_slot(
    plan: dict[str, Any],
    container: str,
    slot: str,
    *,
    owner: str,
    reason: str,
    source_text: str,
    owner_span: Any = None,
    value: Any = None,
    mode: str = "pop",
) -> str:
    """소유권을 주장해 슬롯(또는 리스트 슬롯의 한 값)을 회수한다.

    반환: ``"removed"`` | ``"kept"``(다른 절 소유라 보존) | ``"absent"``(애초에 값이 없음).

    ``owner_span`` 과 슬롯의 출처 구간이 **겹치지 않으면 회수하지 않는다** — 같은 종류라는 이유로
    다른 절의 조건을 지우던 사고를 여기서 막는다. 둘 중 하나라도 구간을 모르면 기존 동작(회수)이다.
    ``mode="pop"`` 은 키 자체를 제거(기존 ``plan.pop`` 과 동일), ``"clear"`` 는 None/[] 로 비운다.
    """
    # 소유 선언은 슬롯에 값이 있었는지와 무관하다 — 회수할 값이 없어도 그 어구를 읽은 사실은 남는다.
    record_owned_span(plan, owner=owner, span=owner_span, source_text=source_text, reason=reason)
    holder = _container(plan, container)
    current = holder.get(slot)
    if value is not None and isinstance(current, list):
        if value not in current:
            return "absent"
    elif current is None or current == [] or current == {} or current == "":
        return "absent"

    span_key = f"{slot}:{value}" if value is not None else slot
    recorded = _spans(plan).get(_key(container, span_key))
    recorded_span = _as_span(recorded) if isinstance(recorded, dict) else None
    trusted = (
        recorded_span is not None
        and isinstance(recorded, dict)
        and _source_compatible(recorded.get("source"), source_text)
    )
    owner_normalized = _as_span(owner_span)

    record: dict[str, Any] = {
        "owner": owner,
        "slot": _key(container, span_key),
        "reason": reason,
        "owner_span": list(owner_normalized) if owner_normalized else None,
        "source_span": [recorded_span[0], recorded_span[1]] if trusted and recorded_span else None,
        "source_text": recorded.get("text") if trusted and isinstance(recorded, dict) else None,
    }

    if trusted and owner_normalized is not None and not spans_overlap(recorded_span, owner_normalized):
        # 다른 절이 만든 조건이다 — 종류가 같아도 이 소유자의 것이 아니다.
        record.update({"outcome": "kept", "value": current if value is None else value,
                       "detail": "출처 구간이 소유 구간과 겹치지 않아 보존"})
        _append_record(plan, record)
        return "kept"

    if value is not None and isinstance(current, list):
        holder[slot] = [item for item in current if item != value]
        removed: Any = value
    else:
        removed = current
        if mode == "pop":
            holder.pop(slot, None)
        elif isinstance(current, list):
            holder[slot] = []
        else:
            holder[slot] = None

    _spans(plan).pop(_key(container, span_key), None)
    record.update({
        "outcome": "removed",
        "value": removed,
        "detail": None if trusted else UNKNOWN_SPAN_REASON,
    })
    _append_record(plan, record)
    return "removed"


def claim_slots(
    plan: dict[str, Any],
    targets: "list[tuple[str, str]] | tuple[tuple[str, str], ...]",
    *,
    owner: str,
    reason: str,
    source_text: str,
    owner_span: Any = None,
    mode: str = "pop",
) -> dict[str, str]:
    """``(container, slot)`` 목록을 한 소유 구간으로 일괄 회수한다. 반환: 슬롯키 → 판정."""
    outcomes: dict[str, str] = {}
    for container, slot in targets:
        slot_name, _, value = slot.partition(":")
        outcomes[_key(container, slot)] = claim_slot(
            plan, container, slot_name,
            owner=owner, reason=reason, source_text=source_text,
            owner_span=owner_span, value=value or None, mode=mode,
        )
    return outcomes
