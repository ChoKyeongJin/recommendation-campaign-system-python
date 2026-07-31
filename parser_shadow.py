"""파서 shadow 비교 — 두 해석 경로를 나란히 돌려 슬롯 단위로 대조한다(출력에는 영향 없음).

배경: 결정론 정규식을 LLM 슬롯 추출로 바꾸려면 "LLM 이 정말 더 나은가"를 슬롯 단위로 알아야 한다.
전면 교체 후 관찰하는 것은 사고를 프로덕션에서 배우는 방식이고, 감으로 고르는 것은 근거가 없다.
shadow 는 그 사이다 — 후보 경로를 **함께 돌리되 결과는 버리고**, 조건 IR 차이만 기록한다.

    off      비교하지 않는다(기본).
    shadow   비교해 기록만 한다. 실행 플랜은 baseline 그대로다.
    enforce  슬롯 정책(:mod:`slot_policy`)이 llm 소유로 선언한 슬롯에 한해 후보 값을 채택한다.

판정 어휘(슬롯 하나에 대한 두 경로의 관계)::

    agree            두 경로가 같은 값
    only_baseline    baseline 만 만들었다(후보가 놓쳤다)
    only_candidate   후보만 만들었다(baseline 이 놓쳤다 — 이행의 근거가 되는 칸)
    value_differs    둘 다 만들었는데 값이 다르다(가장 위험한 칸)

이 판정은 :mod:`ir_snapshot` 정규형 위에서 계산한다 — SQL 이나 플랜 원형이 아니라 '해석 결과'를
비교해야 컴파일러 변경에 흔들리지 않는다. 거기서 한 겹 더 들어가, 비교 직전에 :mod:`semantic_fields`
로 **출처 필드를 걷어낸 의미 정규형**을 만든다. 두 경로는 애초에 서로 다른 입력 문자열(원문 / 재작성·
절 분리본)을 보므로 ``source_text``·``evidence`` 는 항상 다르다 — 그것을 불일치로 세면 의미가 같은
해석까지 위험 칸으로 올라가고, LLM-first 경로에서는 SQL 생성이 통째로 막힌다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다(plain dict 입력).
"""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

import ir_snapshot
import semantic_fields

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ENFORCE = "enforce"
MODES = (MODE_OFF, MODE_SHADOW, MODE_ENFORCE)

# 플랜에 붙는 비교 결과(조건이 아니라 계측이다 — ir_snapshot 의 파생 키 목록에 등록돼 있다).
SHADOW_KEY = "parser_shadow"

AGREE = "agree"
ONLY_BASELINE = "only_baseline"
ONLY_CANDIDATE = "only_candidate"
VALUE_DIFFERS = "value_differs"
VERDICTS = (AGREE, ONLY_BASELINE, ONLY_CANDIDATE, VALUE_DIFFERS)

# 후보가 baseline 을 대체해도 되는지 판단할 때 '위험한' 판정(사람 검토 없이 승격 금지).
RISKY_VERDICTS = frozenset({ONLY_BASELINE, VALUE_DIFFERS})


def mode() -> str:
    """현재 shadow 모드. 알 수 없는 값은 off 로 접는다(오타로 프로덕션 경로가 바뀌지 않게)."""
    value = os.getenv("PARSER_SHADOW_MODE", MODE_OFF).strip().casefold()
    return value if value in MODES else MODE_OFF


def enabled() -> bool:
    return mode() != MODE_OFF


def semantic_form(value: Any) -> Any:
    """슬롯 값의 **의미 비교 전용** 정규형(출처 필드 제거). 판정 어휘의 정의 그 자체다."""
    return semantic_fields.strip_provenance(value)


def _slots(plan: dict[str, Any]) -> dict[str, Any]:
    """조건 IR 정규형을 ``"컨테이너.슬롯" -> 의미 정규형`` 평면 dict 로 편다.

    스냅샷(골든·감사용)은 출처를 그대로 담고, 여기서 비교 직전에만 걷어낸다 — 진단 정보를 잃지
    않으면서 "같은 뜻인가"만 판정하기 위해서다.
    """
    snapshot = ir_snapshot.snapshot(plan)
    flat: dict[str, Any] = {}
    for container, slots in snapshot.items():
        if container == "schema_version" or not isinstance(slots, dict):
            continue
        for slot, value in slots.items():
            flat[f"{container}.{slot}"] = semantic_form(value)
    return flat


def compare(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    baseline_label: str = "rules",
    candidate_label: str = "llm",
    query: str | None = None,
) -> dict[str, Any]:
    """두 플랜의 조건 IR 을 슬롯 단위로 대조한다. 어느 쪽도 변경하지 않는다."""
    left, right = _slots(baseline), _slots(candidate)
    slots: dict[str, dict[str, Any]] = {}
    for slot in sorted(set(left) | set(right)):
        in_left, in_right = slot in left, slot in right
        if in_left and in_right:
            verdict = AGREE if left[slot] == right[slot] else VALUE_DIFFERS
        else:
            verdict = ONLY_BASELINE if in_left else ONLY_CANDIDATE
        entry: dict[str, Any] = {"verdict": verdict}
        if verdict != AGREE:
            if in_left:
                entry["baseline"] = left[slot]
            if in_right:
                entry["candidate"] = right[slot]
        slots[slot] = entry

    counts = Counter(entry["verdict"] for entry in slots.values())
    return {
        "baseline": baseline_label,
        "candidate": candidate_label,
        "query": query,
        "slots": slots,
        "counts": {verdict: counts.get(verdict, 0) for verdict in VERDICTS},
        "agreed": counts.get(AGREE, 0) == len(slots),
    }


def attach(plan: dict[str, Any], comparison: dict[str, Any]) -> None:
    """비교 결과를 플랜에 붙인다(디버그 응답으로 그대로 나간다)."""
    plan[SHADOW_KEY] = comparison


def comparison_of(plan: dict[str, Any]) -> dict[str, Any] | None:
    value = plan.get(SHADOW_KEY)
    return value if isinstance(value, dict) else None


def divergent_slots(comparison: dict[str, Any]) -> list[str]:
    """일치하지 않은 슬롯 목록(진단·리포트용)."""
    return sorted(
        slot for slot, entry in (comparison.get("slots") or {}).items()
        if entry.get("verdict") != AGREE
    )


# ── 누적 관찰(슬롯 승격 근거) ──────────────────────────────────────────────────
# 슬롯 하나를 LLM-first 로 넘길지는 한 번의 비교가 아니라 누적 일치율로 판단한다. 기록은 append-only
# JSONL 이고, 경로가 없으면 아무것도 쓰지 않는다(관찰이 프로덕션 경로를 막지 않게).
LOG_PATH_ENV = "PARSER_SHADOW_LOG"


def log_path() -> Path | None:
    raw = os.getenv(LOG_PATH_ENV, "").strip()
    return Path(raw) if raw else None


def record(comparison: dict[str, Any], *, path: Path | None = None) -> bool:
    """비교 한 건을 관찰 로그에 덧붙인다. 기록했으면 True.

    쓰기 실패는 삼킨다 — 관찰이 실패해서 요청이 실패하면 안 된다(관찰은 부수 효과다).
    """
    target = path or log_path()
    if target is None:
        return False
    line = json.dumps({
        "query": comparison.get("query"),
        "baseline": comparison.get("baseline"),
        "candidate": comparison.get("candidate"),
        "counts": comparison.get("counts"),
        "slots": {slot: entry.get("verdict") for slot, entry in (comparison.get("slots") or {}).items()},
    }, ensure_ascii=False)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError:
        return False
    return True


def load_observations(path: Path | None = None) -> list[dict[str, Any]]:
    """관찰 로그를 읽는다(없으면 빈 목록). 깨진 줄은 건너뛴다 — 로그는 진단용이다."""
    target = path or log_path()
    if target is None or not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


def agreement_by_slot(observations: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """슬롯별 누적 판정 분포와 일치율. 슬롯 승격(6단계)의 유일한 근거다.

    반환::

        {"target_user.gender": {"observed": 12, "agree": 12, "rate": 1.0,
                                "counts": {...}, "risky": 0}}
    """
    tally: dict[str, Counter] = {}
    for observation in observations:
        for slot, verdict in (observation.get("slots") or {}).items():
            if verdict in VERDICTS:
                tally.setdefault(str(slot), Counter())[verdict] += 1

    out: dict[str, dict[str, Any]] = {}
    for slot, counts in sorted(tally.items()):
        observed = sum(counts.values())
        agree = counts.get(AGREE, 0)
        out[slot] = {
            "observed": observed,
            "agree": agree,
            "rate": round(agree / observed, 4) if observed else 0.0,
            "risky": sum(counts.get(verdict, 0) for verdict in RISKY_VERDICTS),
            "counts": {verdict: counts.get(verdict, 0) for verdict in VERDICTS},
        }
    return out
