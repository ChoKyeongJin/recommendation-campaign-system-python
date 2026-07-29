"""표면어의 LLM 소유 계층 — 낱말 목록 대신 '뜻'의 닫힌 집합을 둔다.

배경
----
타겟팅 신호어 사전(``docs/data/targeting_lexicon.json``)은 "이 문장이 채널 발송 얘기인가",
"재구매 유도인가" 같은 **뜻**을 판정하려고 낱말 목록을 쌓아 왔다. 낱말은 끝이 없어서 새 말투가
나올 때마다 사람이 한 줄씩 추가해야 했고, 추가하기 전까지 그 신호는 조용히 침묵했다.

이 모듈은 그 판정의 소유권을 LLM 으로 옮긴다. 다만 옮기는 것은 **표면어(어떻게 말하는가)뿐**이고,
개념 자체(무엇이 존재하는가)는 여전히 JSON 이 소유한다 — :data:`DEFAULT_CONCEPTS_PATH` 의
``concepts`` 가 그 닫힌 집합이다. 이 경계가 "LLM 이 개념을 지어내지 않는다"의 실질이다.

    개념(concept)  — 판정 대상인 뜻. id·라벨·설명만 있고 낱말은 없다. JSON 이 소유(사실).
    표면어(surface) — 그 뜻을 사람이 말하는 방식. LLM 이 소유(끝없이 늘어나는 목록).

채택 규약은 기존 조건 슬롯 보완(``graph_rag._apply_llm_condition_slot_fallback``)과 같다:

  1. **닫힌 집합에서 고르기만** — ``concepts`` 에 없는 id 는 버린다.
  2. **근거는 원문 그대로** — 근거 조각이 질의에 글자 그대로 없으면 버린다. 그래서 하위 호출자가
     문장의 일부(절)에 대해 물어도 같은 근거로 답할 수 있다(스팬 포함 검사).
  3. **빈칸만** — 호출자는 규칙(동결 백스톱)이 못 읽었을 때만 이 결과를 얹는다. 규칙 판정을
     뒤집지 않는다.

키가 없거나 호출이 실패하면 빈 결과를 돌려주고, 호출자는 동결 백스톱 낱말만으로 동작한다
(기존 결정론 동작 그대로 — 골든 코퍼스가 이 경로로 돈다).
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_CONCEPTS_PATH = Path(
    os.getenv("SURFACE_CONCEPTS_PATH", str(Path(__file__).resolve().parent / "docs" / "data" / "surface_concepts.json"))
)

PROMPT_FILENAME = "surface_signal_extract_system.txt"


@dataclass(frozen=True)
class SurfaceConcept:
    """판정 대상 개념 하나. 낱말이 아니라 뜻만 담는다."""

    concept_id: str
    label: str
    description: str


def enabled() -> bool:
    """표면어 LLM 해석을 켤지. 끄면 호출자는 동결 백스톱 낱말만으로 동작한다."""
    value = os.getenv("SURFACE_LEXICON_LLM", "true").strip().casefold()
    return value not in {"0", "false", "no", "off"}


@functools.lru_cache(maxsize=4)
def load_concepts(path_text: str = str(DEFAULT_CONCEPTS_PATH)) -> tuple[SurfaceConcept, ...]:
    """개념 선언을 읽는다. 파일이 없거나 파손되면 빈 튜플(= LLM 계층 비활성, 백스톱만)."""
    path = Path(path_text)
    if not path.exists():
        return ()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return ()
    raw = payload.get("concepts") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return ()
    concepts: list[SurfaceConcept] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        concept_id = item.get("concept_id")
        description = item.get("description")
        if not (isinstance(concept_id, str) and concept_id and isinstance(description, str) and description):
            continue
        concepts.append(
            SurfaceConcept(
                concept_id=concept_id,
                label=str(item.get("label") or concept_id),
                description=description,
            )
        )
    return tuple(concepts)


def concept_ids(concepts: Iterable[SurfaceConcept] | None = None) -> tuple[str, ...]:
    return tuple(concept.concept_id for concept in (concepts if concepts is not None else load_concepts()))


def concept_catalog(concepts: Iterable[SurfaceConcept] | None = None) -> str:
    """프롬프트에 끼워 넣을 개념 목록 블록. 낱말이 아니라 뜻만 나열한다."""
    return "\n".join(
        f"- {concept.concept_id} ({concept.label}): {concept.description}"
        for concept in (concepts if concepts is not None else load_concepts())
    )


def compact(text: str) -> str:
    """스팬 비교용 정규형 — 공백 제거 + 소문자. 호출자의 compact_query 규약과 같다."""
    return (text or "").replace(" ", "").casefold()


def validate(payload: Any, allowed: Iterable[str], query: str) -> dict[str, tuple[str, ...]]:
    """LLM 응답을 닫힌 집합 + 원문 근거로 검증해 ``concept_id → 근거 스팬들`` 로 만든다.

    근거를 스팬으로 보관하는 이유: 상위 호출자가 질의 전체가 아니라 그 일부(타겟팅 절/채널 절)에
    대해 신호를 물을 수 있기 때문이다. 스팬이 그 조각 안에 있어야 그 조각의 신호로 인정한다.
    """
    allowed_ids = set(allowed)
    compact_query = compact(query)
    out: dict[str, list[str]] = {}
    signals = payload.get("signals") if isinstance(payload, dict) else None
    if not isinstance(signals, list):
        return {}
    for item in signals:
        if not isinstance(item, dict):
            continue
        concept_id = item.get("concept_id")
        evidence = item.get("evidence")
        if not (isinstance(concept_id, str) and concept_id in allowed_ids):
            continue
        if not isinstance(evidence, str):
            continue
        span = compact(evidence)
        # 근거는 원문에 글자 그대로 있어야 한다(번역·요약·유추 금지). 한 글자 근거는 어떤 문장에도
        # 걸리므로 스팬 검사가 무력해진다 — 최소 길이를 둔다.
        if len(span) < 2 or span not in compact_query:
            continue
        spans = out.setdefault(concept_id, [])
        if span not in spans:
            spans.append(span)
    return {concept_id: tuple(spans) for concept_id, spans in out.items() if spans}


def resolve(
    query: str,
    extract: Callable[[str, tuple[SurfaceConcept, ...]], Any],
    concepts: tuple[SurfaceConcept, ...] | None = None,
) -> dict[str, tuple[str, ...]]:
    """질의 하나에 대한 표면 신호 해석. 실패·비활성 시 빈 dict(= 백스톱만으로 동작)."""
    if not enabled() or not (query or "").strip():
        return {}
    catalog = concepts if concepts is not None else load_concepts()
    if not catalog:
        return {}
    try:
        payload = extract(query, catalog)
    except Exception:  # noqa: BLE001 - 표면어 해석 실패가 파싱을 막아서는 안 된다.
        return {}
    if payload is None:
        return {}
    return validate(payload, concept_ids(catalog), query)


def evidence(signals: dict[str, tuple[str, ...]], concept_id: str, text: str) -> str | None:
    """신호를 성립시킨 근거 스팬(정규형). 성립하지 않으면 None.

    판정(bool)만이 아니라 **무엇을 읽고 그렇게 판정했는지**를 돌려주는 이유: 소비자가 그 표현을
    원문에서 다시 찾아, 같은 어구를 이미 다른 조건이 소유했는지 판정할 수 있어야 하기 때문이다.
    """
    spans = signals.get(concept_id)
    if not spans:
        return None
    haystack = compact(text)
    return next((span for span in spans if span in haystack), None)


def hit(signals: dict[str, tuple[str, ...]], concept_id: str, text: str) -> bool:
    """해석된 신호가 주어진 텍스트 조각에서 성립하는가 — 근거 스팬이 그 조각 안에 있어야 한다."""
    return evidence(signals, concept_id, text) is not None


# ── 질의 스코프 ────────────────────────────────────────────────────────────────────────────
# 해석은 질의당 한 번이고, 그 결과를 파이프라인 어느 모듈에서든 읽는다. 여기 두는 이유: 표면 신호를
# 소비하는 곳이 파서 한 곳이 아니라(의도·목적·집계 판정 …) 모듈에 걸쳐 있는데, 소비 지점마다 질의를
# 인자로 꿰면 호출 사슬 전부를 고쳐야 하기 때문이다.
#
# (해석 대상 질의의 compact 형, 신호). 질의를 함께 들고 있는 이유는 하위 단계가 **부분 문자열**로 다시
# 들어오기 때문이다(전체 프롬프트 → 타겟팅 절 → 절 조각). 상위에서 이미 해석했으면 부분 질의는 스팬
# 포함 검사로 같은 결과를 재사용하므로 LLM 을 다시 부르지 않는다.
_SIGNALS: contextvars.ContextVar[tuple[str, dict[str, tuple[str, ...]]] | None] = contextvars.ContextVar(
    "lexicon_llm_signals", default=None
)


@contextlib.contextmanager
def signal_scope(query: str, resolver: Callable[[str], dict[str, tuple[str, ...]]]):
    """질의 하나 동안 표면 신호 해석을 열어 둔다.

    이미 열린 스코프의 질의가 이번 질의를 포함하면 그대로 재사용한다 — 파이프라인이 전체 프롬프트로
    한 번 열고 그 안에서 타겟팅 절만 다시 계획하기 때문에, 그러지 않으면 같은 문장을 두 번 부른다."""
    current = _SIGNALS.get()
    if current is not None and compact(query) in current[0]:
        yield
        return
    token = _SIGNALS.set((compact(query), resolver(query)))
    try:
        yield
    finally:
        _SIGNALS.reset(token)


def current_signals() -> dict[str, tuple[str, ...]]:
    """지금 열려 있는 스코프의 신호. 스코프 밖이면 빈 dict(= 백스톱만으로 동작)."""
    current = _SIGNALS.get()
    return current[1] if current else {}


def signal_hit(concept_id: str, text: str) -> bool:
    """열린 스코프의 신호로 판정. 스코프가 없으면 항상 False라 결정론 경로가 그대로 유지된다."""
    return hit(current_signals(), concept_id, text)


def signal_evidence(concept_id: str, text: str) -> str | None:
    """열린 스코프에서 이 판정을 만든 근거 표면형(정규형). 스코프가 없거나 미성립이면 None."""
    return evidence(current_signals(), concept_id, text)
