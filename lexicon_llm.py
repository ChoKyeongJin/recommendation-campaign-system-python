"""표면어의 LLM 소유 계층 — 낱말 목록 대신 '뜻'의 닫힌 집합을 둔다.

배경
----
타겟팅 신호어 사전(``docs/data/runtime/language/targeting_lexicon.json``)은 "이 문장이 채널 발송 얘기인가",
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
    os.getenv(
        "SURFACE_CONCEPTS_PATH",
        str(
            Path(__file__).resolve().parent
            / "docs" / "data" / "runtime" / "semantics" / "surface_concepts.json"
        ),
    )
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


# ── 네임스페이스: 레지스트리에서 파생한 선택지 ─────────────────────────────────────────────
# 위의 개념 25개는 사람이 surface_concepts.json 에 손으로 쓴 것이다. 그 방식은 '뜻'이 코드에 박힌
# 판정에만 맞는다 — 등급·지표·requirement 처럼 **닫힌 집합이 이미 레지스트리 안에 있는** 축은
# 사람이 개념을 또 쓰면 같은 사실을 두 곳이 소유하게 된다. 그런 축은 런타임에 파생해서 붙이고,
# 파생 id 는 ``<namespace>:<key>`` 로 네임스페이스를 준다(손 개념은 콜론이 없으므로 충돌 불가).
NAMESPACE_SEP = ":"


def namespaced(namespace: str, key: str) -> str:
    """'member_grade' + 'gold_grade' → 'member_grade:gold_grade'. 작명 규칙은 여기 하나뿐이다."""
    return f"{namespace}{NAMESPACE_SEP}{key}"


def split_namespace(concept_id: str) -> tuple[str, str]:
    """네임스페이스와 키로 가른다. 콜론이 없으면 ('', concept_id) — 손 개념은 언제나 이쪽이다."""
    namespace, sep, key = (concept_id or "").partition(NAMESPACE_SEP)
    return (namespace, key) if sep else ("", concept_id or "")


def _evidence_with(
    spans: Iterable[str], text: str, normalize: Callable[[str], str]
) -> str | None:
    """근거 스팬이 이 텍스트 조각 안에 있는가 — **양쪽을 같은 함수로** 정규화해서 비교한다.

    ``evidence()`` 와 갈라 둔 이유: 소비 지점마다 정규형이 다르다. 이 모듈의 :func:`compact` 는
    공백만 지우고, ``analytical_intent._compact`` 는 문장부호(``[\\s.,!?·_-/]``)까지 지운다.
    한쪽만으로 비교하면 문장부호를 포함한 스팬이 조용히 증발한다 — 실패가 아니라 침묵이라 눈에
    띄지 않는다. 그래서 normalize 를 호출자가 넘기게 하고 여기서 양쪽에 같이 적용한다.
    """
    haystack = normalize(text)
    return next((span for span in spans if normalize(span) in haystack), None)


def signal_choices(
    namespace: str, text: str, *, normalize: Callable[[str], str] = compact
) -> tuple[tuple[str, str], ...]:
    """열린 스코프에서 이 네임스페이스가 고른 ``(key, 근거스팬)`` 전부. 근거가 긴 것부터.

    최장 근거 우선 정렬은 결정론 최장일치(longest-match)와 같은 서열이다 — 두 경로의 순위가
    갈라지면 '규칙이 읽을 때와 LLM 이 읽을 때 답이 다른' 상태가 되므로 서열을 맞춰 둔다.
    """
    prefix = f"{namespace}{NAMESPACE_SEP}"
    picked: list[tuple[str, str]] = []
    for concept_id, spans in current_signals().items():
        if not concept_id.startswith(prefix):
            continue
        span = _evidence_with(spans, text, normalize)
        if span is not None:
            picked.append((concept_id[len(prefix):], span))
    picked.sort(key=lambda item: len(item[1]), reverse=True)
    return tuple(picked)


def signal_choice(
    namespace: str, text: str, *, normalize: Callable[[str], str] = compact
) -> str | None:
    """이 네임스페이스에서 고른 항목 하나. **최장 근거가 유일하게 이길 때만** 채택한다.

    동점이면 None(fail-close). 결정론 ``max()`` 는 임의로 하나를 고르지만, LLM 결과가 애매할 때
    임의로 고르면 그 임의성이 곧 SQL 이 된다 — 애매하면 백스톱에 맡기는 편이 낫다.
    """
    choices = signal_choices(namespace, text, normalize=normalize)
    if not choices:
        return None
    if len(choices) > 1 and len(choices[0][1]) == len(choices[1][1]):
        return None
    return choices[0][0]


# ── Tier N: 이름 리졸버 ───────────────────────────────────────────────────────────────────
# 위(Tier Q)의 haystack 은 사용자 원문이다. 그런데 같은 '표면어 → canonical' 문제인데 haystack 이
# 원문이 아니라 **식별자 문자열**인 자리가 있다: resolve_missing_field("comparison_month_1"),
# _aggregate_metric_id_for_canonical("구매금액") 같은 것들. 여기에 질의 스팬 규약을 적용하면
# 'comparison_month_1' 이 한국어 질의에 없으므로 언제나 빈 결과다. 그래서 haystack 을 이름으로
# 바꾼 같은 검증기를 쓴다 — validate() 의 query 자리에 이름을 넣을 뿐, 판정 규약은 동일하다.
_NAME_CACHE: dict[tuple[str, tuple[str, ...]], str | None] = {}
_NAME_CACHE_LIMIT = 512


def resolve_name(
    value: str,
    candidates: tuple[tuple[str, str], ...],
    extract: Callable[[str, tuple[tuple[str, str], ...]], Any],
    *,
    reject_evidence: frozenset[str] = frozenset(),
) -> str | None:
    """식별자 하나를 닫힌 후보 집합의 id 하나로 맞춘다. 못 맞추면 None(= 호출자는 오늘 동작 유지).

    ``candidates`` 는 ``(id, 설명)`` 튜플이라 해시 가능하다 — 그래서 캐시 키에 그대로 들어가고,
    레지스트리가 바뀌면 후보 튜플이 바뀌므로 캐시도 자동으로 갈린다(별도 버전 키가 필요 없다).

    ``reject_evidence`` 는 구조 접두/접미어를 근거로 인정하지 않는다. 이게 없으면 'customer'
    한 조각이 ``customer_*`` 로 시작하는 **모든** 후보를 정당화한다 — 근거 검사가 통과하는데도
    아무것도 검증하지 못하는 상태가 된다.

    음성 결과(None)도 캐시한다. 안 그러면 매칭이 안 되는 같은 이름을 요청마다 다시 묻게 되어
    실패 경로가 곧 상시 호출 경로가 된다.
    """
    if not enabled() or not (value or "").strip() or not candidates:
        return None
    cache_key = (value, tuple(cid for cid, _ in candidates))
    if cache_key in _NAME_CACHE:
        return _NAME_CACHE[cache_key]
    if len(_NAME_CACHE) >= _NAME_CACHE_LIMIT:
        # 상한을 넘으면 신규 호출을 멈춘다(캐시된 답만 쓴다). 축출 정책을 두는 대신 멈추는 이유:
        # 이 캐시가 그만큼 커졌다면 입력 공간이 닫혀 있다는 전제가 깨진 것이고, 그때 계속 부르면
        # 질의당 호출이 무한정 늘어난다.
        return None
    try:
        payload = extract(value, candidates)
    except Exception:  # noqa: BLE001 - 이름 해석 실패가 파싱을 막아서는 안 된다.
        payload = None
    picked: str | None = None
    if payload is not None:
        signals = validate(payload, [cid for cid, _ in candidates], value)
        scored: list[tuple[str, str]] = []
        for concept_id, spans in signals.items():
            usable = [span for span in spans if span not in reject_evidence]
            if usable:
                scored.append((concept_id, max(usable, key=len)))
        scored.sort(key=lambda item: len(item[1]), reverse=True)
        if scored and not (len(scored) > 1 and len(scored[0][1]) == len(scored[1][1])):
            picked = scored[0][0]
    _NAME_CACHE[cache_key] = picked
    return picked


def invalidate_names() -> None:
    """이름 캐시를 비운다. 레지스트리 로더 캐시 무효화·테스트 fixture 와 반드시 함께 부른다."""
    _NAME_CACHE.clear()


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
