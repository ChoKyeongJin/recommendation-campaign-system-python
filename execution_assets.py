"""선언된 실행 자산의 표면어 색인 — "이 뜻을 처리할 수 있는 것이 있는가"의 단일 판정자.

**"표현할 수 없다"는 원문을 읽은 모델이 아니라 실행 자산을 아는 애플리케이션이 판정한다.**
그 판정을 여기 한 곳에 두는 이유는 두 소비자가 있기 때문이다 — 미지원 선언의 강등 가드와
라우팅. 같은 질문을 두 곳에서 다르게 답하면 한쪽은 미지원으로 끝내고 다른 쪽은 계속 진행해
조용한 불일치가 생긴다.

자산 목록을 손으로 들지 않는다. 표면어는 **이미 선언된 레지스트리에서 파생**한다:

    canonical      audience_catalog 의 지표 별칭·심볼            (Event IR 경로)
    member_filter  member_target_filters.json eq_filters synonyms (회원 속성 축)
    metric         docs/data/runtime/sql/metrics/*.json aliases    (프로필 지표 축)
    concept        concept_catalog 의 실행 가능 개념              (개념 축)

``history``(attribute_catalog 의 PREV_* 스냅샷 바인딩) 계층은 2026-08-05 폐기됐다 — 등급/상태
이력·전이 축의 실행 경로가 통째로 사라져 "처리할 자산이 있다"가 더 이상 참이 아니다.

새 축이 선언되면 코드 변경 없이 여기 들어온다. 그것이 P4(커버리지 패리티)의 차집합이 줄면
이 모듈의 답이 저절로 정확해지는 방식이다.

이 모듈은 **판정만** 한다 — SQL 도, 슬롯도, 사용자 문구도 만들지 않는다.
"""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

CANONICAL = "canonical"
MEMBER_FILTER = "member_filter"
METRIC = "metric"
CONCEPT = "concept"

# 실행 계층 이름(닫힌 집합). canonical 은 Event IR 경로, 나머지는 그 밖의 선언된 실행 자산이다.
LAYERS: tuple[str, ...] = (CANONICAL, MEMBER_FILTER, METRIC, CONCEPT)

# 표면어 최소 길이. 한 글자 토큰은 아무 문장에나 걸려 판정을 무의미하게 만든다.
_MIN_SURFACE_LEN = 2
# 라틴 표기는 낱말 경계를 요구한다('app' 이 'happy' 안에서 걸리면 안 된다).
_LATIN_RE = re.compile(r"^[A-Za-z0-9_.]+$")
# 토큰 사이에 허용하는 여유 글자 수(조사·수식어 몇 글자). 이보다 벌어지면 우연 일치로 본다.
_TOKEN_GAP_BUDGET = 4


@dataclass(frozen=True)
class ExecutionAsset:
    """원문 구간을 처리할 수 있다고 **선언된** 자산 하나."""

    layer: str
    symbol: str
    surface: str

    def to_dict(self) -> dict[str, str]:
        return {"layer": self.layer, "symbol": self.symbol, "surface": self.surface}


def _clean(values: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if len(text) >= _MIN_SURFACE_LEN:
            out.append(text)
    return out


def _canonical_surfaces() -> list[tuple[str, str]]:
    import audience_runtime

    try:
        catalog = audience_runtime.resolve_audience_catalog()
    except Exception:  # noqa: BLE001 — 카탈로그가 못 뜨면 자산이 없다고 답한다(추측하지 않는다).
        return []
    return [
        (symbol, surface)
        for symbol, surface in catalog.surface_terms()
        if len(surface) >= _MIN_SURFACE_LEN
    ]


def _member_filter_surfaces() -> list[tuple[str, str]]:
    import member_filters_config

    try:
        filters = member_filters_config.eq_filters()
    except Exception:  # noqa: BLE001
        return []
    pairs: list[tuple[str, str]] = []
    for entry in filters:
        if not isinstance(entry, Mapping):
            continue
        symbol = str(entry.get("canonical") or entry.get("column") or "").strip()
        if not symbol:
            continue
        surfaces = [*(entry.get("synonyms") or ()), *(entry.get("surface_terms") or ())]
        for surface in _clean(surfaces):
            pairs.append((symbol, surface))
    return pairs


def _metric_surfaces() -> list[tuple[str, str]]:
    import metric_registry

    try:
        registry = metric_registry.MetricRegistry.load()
    except Exception:  # noqa: BLE001
        return []
    pairs: list[tuple[str, str]] = []
    for spec in registry.all():
        for surface in _clean([*spec.aliases_nouns, *spec.aliases_actions]):
            pairs.append((spec.metric_id, surface))
    return pairs


def _concept_surfaces() -> list[tuple[str, str]]:
    import concept_catalog

    try:
        catalog = concept_catalog.default_catalog()
    except Exception:  # noqa: BLE001
        return []
    pairs: list[tuple[str, str]] = []
    for concept_id in sorted(catalog.concept_ids()):
        if not catalog.is_executable(concept_id):
            continue
        spec = catalog.get_concept(concept_id)
        if spec is None:
            continue
        for surface in _clean([*(spec.aliases or ()), spec.label or ""]):
            pairs.append((concept_id, surface))
    return pairs


# `_history_surfaces`(PREV_* 스냅샷 바인딩 색인)는 2026-08-05 삭제됐다 — 그 계층이 폐기됐다.

_PROVIDERS: tuple[tuple[str, Any], ...] = (
    (CANONICAL, _canonical_surfaces),
    (MEMBER_FILTER, _member_filter_surfaces),
    (METRIC, _metric_surfaces),
    (CONCEPT, _concept_surfaces),
)

_HISTORY_ARGUMENT_RE = re.compile(
    r"(?:transition|history|previous|prior|prev(?:ious)?|as[_-]?of)", re.IGNORECASE
)
_HISTORY_EVIDENCE_RE = re.compile(
    r"(?:승급|강등|직전|이력|(?:에서|부터).{0,20}(?:으로|로).{0,12}(?:바뀌|변경|전환))"
)


@functools.lru_cache(maxsize=1)
def declared_surfaces() -> tuple[ExecutionAsset, ...]:
    """선언된 실행 자산의 (계층, 심볼, 표면어) 전부. 긴 표면어가 먼저 걸리도록 정렬한다."""
    assets = [
        ExecutionAsset(layer=layer, symbol=symbol, surface=surface)
        for layer, provider in _PROVIDERS
        for symbol, surface in provider()
    ]
    assets.sort(key=lambda asset: (-len(asset.surface), asset.layer, asset.symbol, asset.surface))
    return tuple(assets)


def clear_cache() -> None:
    declared_surfaces.cache_clear()
    _capability_claim_surfaces.cache_clear()


def _matches(surface: str, haystack: str) -> bool:
    """표면어가 원문 구간에 나타나는가 — **조사 내성** 순서보존 토큰 매칭.

    부분문자열 하나로는 걸리지 않는다: 카탈로그 표면어는 '이메일 수신 동의' 인데 실제 원문은
    '이메일 수신에 동의한' 처럼 조사가 낀다(실측: 이 방식으로 수신동의 축 히트가 0이었다).
    그래서 표면어를 토큰으로 쪼개 **순서를 지키며** 찾되, 흩어진 우연 일치를 막으려고
    매칭 구간 길이를 표면어 길이의 몇 배 이내로 제한한다.
    """
    if _LATIN_RE.match(surface):
        return re.search(rf"(?<![A-Za-z0-9_]){re.escape(surface)}(?![A-Za-z0-9_])", haystack) is not None
    tokens = [token for token in surface.split() if token]
    if len(tokens) <= 1:
        return surface.replace(" ", "") in haystack.replace(" ", "")
    start = 0
    first: int | None = None
    for token in tokens:
        found = haystack.find(token, start)
        if found < 0:
            return False
        if first is None:
            first = found
        start = found + len(token)
    # 흩어진 낱말이 우연히 순서만 맞는 경우를 배제한다(조사 몇 글자는 허용, 문장 하나는 불허).
    return first is not None and (start - first) <= len(surface) + _TOKEN_GAP_BUDGET * len(tokens)


def assets_for_text(text: Any, *, layers: Iterable[str] | None = None) -> tuple[ExecutionAsset, ...]:
    """원문 구간을 처리할 수 있다고 선언된 자산들. 없으면 빈 튜플.

    같은 심볼이 여러 표면어로 걸려도 한 번만 돌려준다 — 세는 것은 자산이지 낱말이 아니다.
    """
    if not isinstance(text, str) or not text.strip():
        return ()
    haystack = text.strip().casefold()
    wanted = frozenset(layers) if layers is not None else None
    seen: set[tuple[str, str]] = set()
    found: list[ExecutionAsset] = []
    for asset in declared_surfaces():
        if wanted is not None and asset.layer not in wanted:
            continue
        key = (asset.layer, asset.symbol)
        if key in seen:
            continue
        if _matches(asset.surface.casefold(), haystack):
            seen.add(key)
            found.append(asset)
    return tuple(found)


def has_execution_asset(text: Any, *, layers: Iterable[str] | None = None) -> bool:
    return bool(assets_for_text(text, layers=layers))


def non_canonical_assets_for_text(text: Any) -> tuple[ExecutionAsset, ...]:
    """canonical **밖의** 자산만. 미지원 선언의 강등 판정이 쓰는 형태다.

    canonical 경로는 이미 자기 차례에 실패했다. 그러므로 '아직 시도되지 않은 실행 자산이
    있는가'가 강등의 조건이고, 그 답은 canonical 을 뺀 나머지 계층에 있다.
    """
    return assets_for_text(text, layers=[layer for layer in LAYERS if layer != CANONICAL])


# ── 모델의 '이 계산은 불가능하다' 주장 대조 ──────────────────────────────────────────────
#
# 위의 표면어 색인은 **원문 구절**을 카탈로그 자산과 맞춘다. 여기서 재는 것은 다른 축이다 —
# 모델이 "이런 **종류의 계산**을 표현할 수 없다"고 말할 때, 그 계산이 IR 이 이미 선언한
# capability 인가. 실측(2026-08-06)의 거짓 신고 둘이 이 축에 걸린다::
#
#     argument='distinct_products'            message="… 중복 제거한 상품 수(count distinct) … 표현 불가"
#     argument='distinct_count_of_cart.product_id'  message="… (제품별 distinct count) … 표현할 수 없습니다"
#
# `aggregate.count_distinct` 는 event_ir.CAPABILITIES 에 선언돼 있고 컴파일러가 그 자리에서
# SQL 을 만든다. 선언된 것을 못 한다는 주장은 스스로 반박된다.
#
# 표는 **닫혀** 있다: 키는 반드시 선언된 capability 이름이어야 하고(아니면 로드 시 실패),
# 새 capability 가 생기면 여기 한 줄이 그 표면어를 얻는다. 낱말을 늘려 의미를 만드는 표가
# 아니라, 이미 있는 의미를 사람이 부르는 방식을 적는 표다.
_CAPABILITY_CLAIM_SURFACES: dict[str, tuple[str, ...]] = {
    "aggregate.count_distinct": (
        "count distinct",
        "count_distinct",
        "countdistinct",
        "distinct count",
        "distinct_count",
        "중복 제거",
        "중복제거",
        "가짓수",
        "종류 수",
        "종류수",
    ),
    "relation.ranked_limit": (
        "top n",
        "top_n",
        "상위 n",
    ),
}


@functools.lru_cache(maxsize=1)
def _capability_claim_surfaces() -> tuple[tuple[str, str], ...]:
    """(capability, 표면어) 목록. 선언되지 않은 capability 를 키로 쓰면 즉시 실패한다."""
    import event_ir  # 지연 import — 판정자는 IR 스키마 로딩을 import 부작용으로 만들지 않는다

    undeclared = sorted(set(_CAPABILITY_CLAIM_SURFACES) - event_ir.CAPABILITIES)
    if undeclared:
        raise ValueError(
            f"capability claim surfaces name undeclared capabilities: {undeclared}"
        )
    return tuple(
        (capability, surface.casefold())
        for capability, surfaces in _CAPABILITY_CLAIM_SURFACES.items()
        for surface in surfaces
    )


def declared_capability_in_claim(*texts: Any) -> str | None:
    """주장이 지목한 **선언된** capability 이름. 없으면 None.

    argument 와 message 를 함께 받는다 — 모델은 둘 중 아무 쪽에나 계산 종류를 적는다
    (실측: 하나는 argument 에, 하나는 message 괄호 안에 있었다).
    """
    haystack = " ".join(
        text.strip().casefold() for text in texts if isinstance(text, str) and text.strip()
    )
    if not haystack:
        return None
    for capability, surface in _capability_claim_surfaces():
        if surface in haystack:
            return capability
    return None


def non_canonical_assets_for_issue(issue: Mapping[str, Any]) -> tuple[ExecutionAsset, ...]:
    """Return only assets capable of the issue's semantic relation.

    A current-value asset for ``휴면`` does not implement ``정상→휴면`` history.
    이력·전이 축은 2026-08-05 폐기돼 그 의미를 처리하는 자산이 **하나도 없다** — 그러므로
    이력 신고는 자산 없음(빈 튜플)으로 답한다. 이 판정을 빼면 현재값 자산('휴면' 필터)이
    이력 미지원 신고를 반박해 조용히 '지금 휴면인 회원'으로 뜻이 바뀐다(실측된 의미 반전).
    """

    evidence = issue.get("evidence")
    text = evidence.get("text") if isinstance(evidence, Mapping) else ""
    argument = str(issue.get("argument") or "")
    requires_history = bool(
        _HISTORY_ARGUMENT_RE.search(argument)
        or (isinstance(text, str) and _HISTORY_EVIDENCE_RE.search(text))
    )
    if requires_history:
        return ()
    return assets_for_text(text, layers=[layer for layer in LAYERS if layer != CANONICAL])


__all__ = [
    "CANONICAL",
    "CONCEPT",
    "LAYERS",
    "MEMBER_FILTER",
    "METRIC",
    "ExecutionAsset",
    "assets_for_text",
    "clear_cache",
    "declared_capability_in_claim",
    "declared_surfaces",
    "has_execution_asset",
    "non_canonical_assets_for_text",
    "non_canonical_assets_for_issue",
]
