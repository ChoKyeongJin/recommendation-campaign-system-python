"""레지스트리에서 '고를 수 있는 것'을 런타임에 파생한다 — 표면어를 손으로 쓰지 않기 위해.

배경
----
``surface_concepts.json`` 은 사람이 '뜻'을 손으로 선언하고 LLM 이 그 뜻의 낱말을 읽는 구조다
(:mod:`lexicon_llm`). 그 방식은 뜻이 코드에 박힌 판정("이 문장이 발송 얘기인가")에는 맞지만,
**닫힌 집합이 이미 레지스트리 안에 있는** 축에는 맞지 않는다. 등급·지표·requirement 가 그렇다:
``member_target_filters.json`` 에 등급을 하나 추가하면 그 canonical 이 곧 고를 대상인데, 사람이
``surface_concepts.json`` 에 같은 항목을 또 써야 한다면 같은 사실을 두 파일이 소유하게 된다.

이 모듈은 그 축들을 **런타임에 파생**한다. 빌드 스크립트를 두지 않은 이유가 핵심이다 — 생성 파일은
'재실행'이라는 새로운 사람 손을 만들고, 안 돌리면 아무 오류 없이 조용히 옛 목록으로 동작한다.
파생이면 JSON 한 줄이 곧 카탈로그다(재시작으로 충분하다는 이 저장소의 배포 규약과도 같다).

경계
----
- 이 모듈은 **레지스트리 로더만** 안다. LLM 호출도, 프롬프트도, SQL 도 모른다.
- 파생하는 것은 **고를 대상(id)과 그것을 사람이 부르는 이름(label) 하나**뿐이다. 동의어 목록을
  여기서 다시 만들지 않는다 — 그러면 이 작업이 없애려던 낱말 나열이 이름만 바꿔 되돌아온다.
- 물리 스키마(컬럼·테이블)는 hint 에 넣지 않는다. 프롬프트에 물리 이름이 새면 DB 스왑 때
  프롬프트까지 따라 고쳐야 한다.

파생 실패는 오답이 아니라 **미검출**이다: 레지스트리가 깨지거나 상한을 넘으면 그 축은 빈 튜플로
강등되고, 소비 지점은 동결 백스톱만으로 오늘과 똑같이 동작한다.
"""

from __future__ import annotations

import functools
import os
import re
from dataclasses import dataclass
from typing import Any, Callable

import lexicon_llm
import member_filters_config
import semantic_resolution

# 축 하나가 프롬프트에 넣을 수 있는 선택지 상한. 레지스트리가 커질 때 프롬프트가 조용히 비대해지는
# 것을 막는다 — 넘으면 그 축을 통째로 강등해서 '일부만 보이는' 애매한 상태를 만들지 않는다.
DEFAULT_MAX_CHOICES = 40

_CODE_TAIL_RE = re.compile(r"^[A-Z0-9_]+\.(?P<tail>[A-Z0-9_]+)$")
_IDENTIFIER_WORD_RE = re.compile(r"[_.]+")


@dataclass(frozen=True)
class Choice:
    """고를 수 있는 항목 하나. 낱말이 아니라 **이름 하나 + 경계 설명**만 담는다."""

    namespace: str
    choice_id: str
    label: str
    hint: str = ""

    @property
    def qualified_id(self) -> str:
        return lexicon_llm.namespaced(self.namespace, self.choice_id)

    def description(self) -> str:
        """LLM 프롬프트에 들어갈 설명. 낱말 나열이 아니라 뜻과 반례."""
        return f"{self.label}{f' ({self.hint})' if self.hint else ''}"


@dataclass(frozen=True)
class NamespaceSpec:
    namespace: str
    substrate: str  # "query" | "name"
    derive: Callable[[], tuple[Choice, ...]]
    max_choices: int = DEFAULT_MAX_CHOICES


# ── 켜고 끄기 ──────────────────────────────────────────────────────────────────────────────
def enabled(namespace: str) -> bool:
    """축 단위 kill switch.

    ``SURFACE_CHOICE_LLM=off`` 로 전부 끄고, ``only=analytics_scope,member_grade`` 로 일부만 켠다.
    축 단위인 이유: 사고가 나면 개념 계층 전체(``SURFACE_LEXICON_LLM``)를 끄는 것이 아니라 문제가
    난 축만 꺼야 한다. 특히 SQL 술어를 직접 바꾸는 ``analytics_scope`` 를 마지막에 켜기 위해 필요하다.
    """
    raw = (os.getenv("SURFACE_CHOICE_LLM") or "").strip()
    if not raw:
        return True
    value = raw.casefold()
    if value in {"0", "false", "no", "off"}:
        return False
    if value.startswith("only="):
        allowed = {item.strip() for item in raw[len("only="):].split(",") if item.strip()}
        return namespace in allowed
    return value not in {"", "none"}


# ── 이름·힌트 파생 ─────────────────────────────────────────────────────────────────────────
def _humanize(identifier: str) -> str:
    """canonical 을 사람이 읽는 이름으로. 아무 이름도 없을 때의 마지막 폴백."""
    return _IDENTIFIER_WORD_RE.sub(" ", identifier).strip()


def derive_label(entry: dict[str, Any], identifier: str) -> str:
    """이름 하나를 고른다: llm_label → ko_label → label → synonyms[0] → canonical 인간화.

    사슬을 두는 이유는 **새 항목이 낱말 없이도 인식되게** 하기 위해서다. 등급을 하나 추가할 때
    synonyms 를 안 써도 ko_label 이나 canonical 만으로 이름이 나오면 LLM 이 고를 수 있다.
    100% 를 보장하지는 않는다 — 실패하면 오답이 아니라 미검출(백스톱만)이다.
    """
    for key in ("llm_label", "ko_label", "label"):
        value = entry.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    synonyms = entry.get("synonyms")
    if isinstance(synonyms, list):
        first = next((s for s in synonyms if isinstance(s, str) and s.strip()), None)
        if first:
            return first.strip()
    return _humanize(identifier)


def _code_tail(value: Any) -> str:
    """'MEM_GRADE_CD.GOLD' → 'GOLD'. 코드값 꼬리는 힌트로 쓰되 컬럼명은 절대 쓰지 않는다."""
    match = _CODE_TAIL_RE.match(str(value or ""))
    return match.group("tail") if match else ""


# ── 축별 파생 ──────────────────────────────────────────────────────────────────────────────
def _member_filters() -> dict[str, Any]:
    return member_filters_config._load()


def _derive_member_grade() -> tuple[Choice, ...]:
    rows = _member_filters().get("eq_filters")
    if not isinstance(rows, list):
        return ()
    choices: list[Choice] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("category") != "grade":
            continue
        canonical = str(row.get("canonical") or "")
        if not canonical:
            continue
        tail = _code_tail(row.get("value"))
        rank = row.get("rank")
        bits = [bit for bit in (f"등급 코드 {tail}" if tail else "", f"서열 {rank}" if rank else "") if bit]
        choices.append(
            Choice(
                "member_grade",
                canonical,
                derive_label(row, canonical),
                "; ".join([*bits, "회원 등급 이름을 말한 경우만. 나이·지역·성별은 아니다"]),
            )
        )
    return tuple(choices)


def _derive_analytics_filter() -> tuple[Choice, ...]:
    filters = _analytics_registry().get("filters")
    if not isinstance(filters, dict):
        return ()
    return tuple(
        Choice(
            "analytics_filter",
            str(filter_id),
            derive_label(spec, str(filter_id)),
            "이 회원 속성을 가리키는 경우",
        )
        for filter_id, spec in filters.items()
        if isinstance(spec, dict)
    )


def _derive_analytics_scope(negated: bool = False) -> tuple[Choice, ...]:
    scopes = _analytics_registry().get("memberScopes")
    if not isinstance(scopes, list):
        return ()
    namespace = "analytics_scope_negated" if negated else "analytics_scope"
    choices: list[Choice] = []
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        scope_id = str(scope.get("id") or "")
        if not scope_id:
            continue
        label = derive_label(scope, scope_id)
        # 긍정/부정이 서로를 반례로 지목한다. 이 상호 지목이 극성 반전 방어의 프롬프트 쪽 절반이다
        # — '구매 경험이 전무한' 에서 근거 '구매' 로 긍정을 켜면 모집단이 통째로 뒤집히는데,
        # 그 근거는 원문에 글자 그대로 있어서 근거 검사만으로는 걸러지지 않는다.
        if negated:
            hint = (
                f"'{label}' 이 **없다**는 뜻. 하지 않았다·이력이 없다·한 번도 없다가 해당한다. "
                f"있다는 뜻이면 이것이 아니라 analytics_scope:{scope_id} 다"
            )
        else:
            hint = (
                f"'{label}' 이 **있다**는 뜻. 없다·하지 않았다는 뜻이면 이것이 아니라 "
                f"analytics_scope_negated:{scope_id} 다"
            )
        choices.append(Choice(namespace, scope_id, label, hint))
    return tuple(choices)


def _derive_aggregate_metric() -> tuple[Choice, ...]:
    metrics = (_member_filters().get("aggregate_targets") or {}).get("metrics")
    if not isinstance(metrics, dict):
        return ()
    return tuple(
        Choice("aggregate_metric", str(metric_id), derive_label(spec, str(metric_id)), "집계 지표")
        for metric_id, spec in metrics.items()
        if isinstance(spec, dict)
    )


def _derive_lifecycle_canonical() -> tuple[Choice, ...]:
    payload = _member_filters()
    choices: list[Choice] = []
    for row in payload.get("eq_filters") or []:
        if not isinstance(row, dict):
            continue
        canonical = str(row.get("canonical") or "")
        if canonical:
            choices.append(
                Choice("lifecycle_canonical", canonical, derive_label(row, canonical), "회원 상태·속성")
            )
    for row in payload.get("activity_filters") or []:
        if not isinstance(row, dict):
            continue
        canonical = str(row.get("canonical") or "")
        if not canonical:
            continue
        days = row.get("days")
        # 창(90일/180일)을 반드시 적는다. inactive_user → inactive_90d 는 언어가 아니라 **정책
        # 선택**이라 LLM 이 문장에서 추론할 수 없다 — 후보 설명에 없으면 90 과 180 중 아무거나 고른다.
        hint = f"최근 {days}일 이상 미접속" if days else "미접속 상태"
        choices.append(Choice("lifecycle_canonical", canonical, derive_label(row, canonical), hint))
    return tuple(choices)


def _derive_resolution_requirement() -> tuple[Choice, ...]:
    registry = semantic_resolution.load_registry()
    rows = registry.get("requirements")
    if not isinstance(rows, list):
        return ()
    return tuple(
        Choice(
            "resolution_requirement",
            str(row.get("id")),
            _humanize(str(row.get("id"))),
            str(row.get("reason") or ""),
        )
        for row in rows
        if isinstance(row, dict) and row.get("id")
    )


NAMESPACES: tuple[NamespaceSpec, ...] = (
    NamespaceSpec("analytics_filter", "query", _derive_analytics_filter),
    NamespaceSpec("analytics_scope", "query", _derive_analytics_scope),
    NamespaceSpec("analytics_scope_negated", "query", lambda: _derive_analytics_scope(negated=True)),
    NamespaceSpec("member_grade", "query", _derive_member_grade),
    NamespaceSpec("aggregate_metric", "name", _derive_aggregate_metric),
    NamespaceSpec("lifecycle_canonical", "name", _derive_lifecycle_canonical, max_choices=80),
    NamespaceSpec("resolution_requirement", "name", _derive_resolution_requirement),
)

_SPECS: dict[str, NamespaceSpec] = {spec.namespace: spec for spec in NAMESPACES}


@functools.lru_cache(maxsize=16)
def choice_set(namespace: str) -> tuple[Choice, ...]:
    """축 하나의 선택지. 축이 꺼졌거나·파생이 실패했거나·상한을 넘으면 빈 튜플(= 백스톱만)."""
    spec = _SPECS.get(namespace)
    if spec is None or not enabled(namespace):
        return ()
    try:
        choices = spec.derive()
    except Exception:  # noqa: BLE001 - 파생 실패가 파싱을 막아서는 안 된다.
        return ()
    if not choices or len(choices) > spec.max_choices:
        return ()
    return choices


def query_concepts() -> tuple[lexicon_llm.SurfaceConcept, ...]:
    """Tier Q 축 전부를 손 개념과 같은 모양(SurfaceConcept)으로 내보낸다."""
    return tuple(
        lexicon_llm.SurfaceConcept(choice.qualified_id, choice.label, choice.description())
        for spec in NAMESPACES
        if spec.substrate == "query"
        for choice in choice_set(spec.namespace)
    )


def name_candidates(namespace: str) -> tuple[tuple[str, str], ...]:
    """Tier N 축의 ``(id, 설명)`` 후보. :func:`lexicon_llm.resolve_name` 에 그대로 넘긴다."""
    return tuple((choice.choice_id, choice.description()) for choice in choice_set(namespace))


@functools.lru_cache(maxsize=1)
def structural_identifier_noise() -> frozenset[str]:
    """근거로 인정하지 않을 구조 접두/접미어(정규형).

    이게 없으면 'customer' 한 조각이 ``customer_*`` 로 시작하는 모든 후보를 정당화한다 — 근거
    검사가 통과하는데도 실제로는 아무것도 검증하지 못하는 상태가 된다.
    """
    registry = semantic_resolution.load_registry()
    parts = [
        *(registry.get("structural_prefixes") or []),
        *(registry.get("structural_suffixes") or []),
    ]
    return frozenset(lexicon_llm.compact(str(part)) for part in parts if str(part).strip())


def invalidate() -> None:
    """파생 캐시를 비운다. 레지스트리를 바꿔 끼우는 테스트·재적재와 함께 부른다."""
    choice_set.cache_clear()
    structural_identifier_noise.cache_clear()
    member_filters_config.clear_cache()
    _analytics_registry.cache_clear()
    lexicon_llm.invalidate_names()


@functools.lru_cache(maxsize=1)
def _analytics_registry() -> dict[str, Any]:
    # analytical_intent 를 여기서 import 하는 이유: analytics_registry.json 의 경로 규약을 그 모듈이
    # 소유한다. 여기서 경로를 다시 쓰면 같은 규약을 두 곳이 소유하게 된다(이 작업이 없애려는 것).
    # 지연 import 인 이유는 무게다 — Tier N 만 쓰는 호출자가 89KB 모듈을 끌어올 이유가 없다.
    import analytical_intent

    return analytical_intent.load_analytics_registry()


__all__ = [
    "Choice",
    "NAMESPACES",
    "NamespaceSpec",
    "choice_set",
    "derive_label",
    "enabled",
    "invalidate",
    "name_candidates",
    "query_concepts",
    "structural_identifier_noise",
]
