"""데이터 카탈로그 — "어떤 데이터 개념이 존재하고 실행 가능한가"의 단일 조회 지점.

LLM 의미 해석 계층은 여기서 검색된 후보 중에서만 개념을 고를 수 있다(요청 6·7번). 카탈로그에
없는 개념은 아무리 의미가 그럴듯해도 unresolved 다 — 새 concept_id 를 지어내는 길이 없다.

개념 소스 3종(이중 소유 금지 — 카탈로그는 기존 스펙의 **파생 뷰** + 특수 슬롯 선언만 갖는다):
  1. 공통 조건(집계 지표): member_target_filters.json aggregate_targets.metrics 파생.
     지표 id·동의어·집계 방식은 그 설정이 계속 소유한다(member_filters_config 경유).
  2. 회원 프로필 지표: metric_registry(docs/data/runtime/sql/metrics/*.json) 파생 — targeting.enabled 만.
  3. 특수 슬롯 개념(미구매/미접속/가입/카트보관 등):
     docs/data/runtime/semantics/concept_catalog.json 선언.
     별칭 확장(alias_extensions)도 이 파일이 갖는다 — 원 스펙의 synonyms 를 복제하지 않는다.

실행 가능성(is_executable)은 "활성 + 선언된 compiler 가 compiler_registry 에 실재"다. 컴파일러
이름이 선언만 되고 등록이 없으면 executable=False → 정규화 결과가 compiler_not_registered 로
끝난다(요청 16번). 검색(search_concepts)은 결정론이다: 별칭 정규화 포함 매칭 + 길이 가중,
동점은 concept_id 사전순 — LLM 재량이 아니라 재현 가능한 후보 목록을 만든다.

순수 모듈 불변식: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping

import compiler_registry as _compiler_registry
import member_filters_config
import metric_registry as _metric_registry
from common_utils import normalize_entity_term
from condition_normalizers import NORMALIZERS

_REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONCEPT_CATALOG_PATH = (
    _REPO_ROOT / "docs" / "data" / "runtime" / "semantics" / "concept_catalog.json"
)

_DEFAULT_OPERATORS: tuple[str, ...] = (">", ">=", "<", "<=")
CONCEPT_KINDS: frozenset[str] = frozenset({"special_slot", "generic_condition"})


class ConceptCatalogError(ValueError):
    """카탈로그 선언 계약 위반(중복 id·미등록 normalizer·형식 오류). 조용한 강등 대신 즉시 실패."""


@dataclass(frozen=True)
class ConceptSpec:
    """개념 하나의 카탈로그 선언(요청 6번 최소 필드)."""

    concept_id: str
    label: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    kind: str = "generic_condition"
    entity_type: str = "metric"
    data_type: str = "number"
    normalizer: str = "generic_condition"
    compiler: str | None = None
    allowed_operators: tuple[str, ...] = _DEFAULT_OPERATORS
    supported_windows: bool = False
    supported_aggregations: tuple[str, ...] = ()
    default_aggregation: str | None = None
    allowed_units: tuple[str, ...] = ()
    min_value: int | None = None
    data_source: str = ""
    active: bool = True

    def __post_init__(self) -> None:
        if self.kind not in CONCEPT_KINDS:
            raise ConceptCatalogError(f"{self.concept_id}: 미지원 kind {self.kind!r}")
        if self.normalizer not in NORMALIZERS:
            raise ConceptCatalogError(
                f"{self.concept_id}: 미등록 normalizer {self.normalizer!r} — condition_normalizers.NORMALIZERS 참조")


@lru_cache(maxsize=1)
def _default_compiler_names() -> frozenset[str]:
    return _compiler_registry.default_registry().names()


def _default_compiler_available(name: str | None) -> bool:
    return bool(name) and name in _default_compiler_names()


# ── 소스별 개념 파생 ───────────────────────────────────────────────────────────────────────
def _declared(path: Path) -> tuple[dict[str, ConceptSpec], dict[str, tuple[str, ...]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, {}
    if not isinstance(payload, dict):
        return {}, {}
    specs: dict[str, ConceptSpec] = {}
    for concept_id, raw in (payload.get("concepts") or {}).items():
        if not isinstance(raw, dict):
            continue
        specs[concept_id] = ConceptSpec(
            concept_id=concept_id,
            label=str(raw.get("label") or concept_id),
            description=str(raw.get("description") or ""),
            aliases=tuple(str(a) for a in raw.get("aliases") or ()),
            kind=str(raw.get("kind") or "special_slot"),
            entity_type=str(raw.get("entity_type") or "slot"),
            data_type=str(raw.get("data_type") or "number"),
            normalizer=str(raw.get("normalizer") or "duration"),
            compiler=str(raw["compiler"]) if raw.get("compiler") else None,
            allowed_operators=tuple(str(op) for op in raw.get("allowed_operators") or _DEFAULT_OPERATORS),
            supported_windows=bool(raw.get("supported_windows", False)),
            supported_aggregations=tuple(str(a).lower() for a in raw.get("supported_aggregations") or ()),
            default_aggregation=(str(raw["default_aggregation"]).lower() if raw.get("default_aggregation") else None),
            allowed_units=tuple(str(u) for u in raw.get("allowed_units") or ()),
            min_value=raw.get("min_value") if isinstance(raw.get("min_value"), int) else None,
            data_source=str(raw.get("data_source") or ""),
            active=bool(raw.get("active", True)),
        )
    extensions = {
        str(concept_id): tuple(str(a) for a in aliases)
        for concept_id, aliases in (payload.get("alias_extensions") or {}).items()
        if isinstance(aliases, list)
    }
    return specs, extensions


def _derived_aggregate(extensions: Mapping[str, tuple[str, ...]]) -> dict[str, ConceptSpec]:
    """공통 조건 개념: 집계 지표 설정의 파생 뷰. 동의어·집계 방식은 설정이 소유한다."""
    specs: dict[str, ConceptSpec] = {}
    for metric_id, raw in member_filters_config.aggregate_metrics().items():
        aliases = tuple(str(s) for s in raw.get("synonyms") or ())
        label = str(raw.get("ko_label") or metric_id)
        agg = str(raw.get("agg") or "").lower()
        semantic_type = str(raw.get("semantic_type") or "")
        is_date = "date" in metric_id or "date" in semantic_type
        specs[metric_id] = ConceptSpec(
            concept_id=metric_id,
            label=label,
            description=f"주문 팩트 회원별 집계 지표({semantic_type or 'sum'})",
            aliases=aliases + tuple(extensions.get(metric_id) or ()),
            kind="generic_condition",
            entity_type="metric",
            data_type="date" if is_date else "number",
            normalizer="generic_condition",
            compiler="purchase_aggregate",
            allowed_operators=_DEFAULT_OPERATORS,
            supported_windows=True,
            supported_aggregations=(agg,) if agg else (),
            default_aggregation=agg or None,
            data_source="order",
        )
    return specs


def _derived_profile(extensions: Mapping[str, tuple[str, ...]]) -> dict[str, ConceptSpec]:
    """회원 프로필 수치 지표: metric_registry 파생 뷰(targeting.enabled 스펙만 실행 가능 어휘에 든다)."""
    metrics, _date_states = _metric_registry.profile_slot_vocab()
    if not isinstance(metrics, Mapping):
        return {}
    try:
        registry = _metric_registry.MetricRegistry.load()
    except Exception:  # 스펙 파일 하나의 오류가 카탈로그 전체를 죽이지 않는다(profile_slot_vocab 과 동일 정책)
        registry = None
    specs: dict[str, ConceptSpec] = {}
    for metric_id, entry in metrics.items():
        if not isinstance(entry, Mapping):
            continue
        aliases: tuple[str, ...] = ()
        if registry is not None:
            spec = registry.by_id(metric_id)
            raw = getattr(spec, "raw", None) if spec is not None else None
            if isinstance(raw, Mapping):
                nouns = (raw.get("aliases") or {}).get("nouns") if isinstance(raw.get("aliases"), Mapping) else None
                if isinstance(nouns, list):
                    aliases = tuple(str(n) for n in nouns)
        operators = entry.get("operators")
        specs[metric_id] = ConceptSpec(
            concept_id=metric_id,
            label=str(entry.get("label") or metric_id),
            description="회원 프로필 수치 지표(월 스냅샷/기본 컬럼)",
            aliases=aliases + tuple(extensions.get(metric_id) or ()),
            kind="generic_condition",
            entity_type="metric",
            data_type="number",
            normalizer="generic_condition",
            compiler="member_profile_metric",
            allowed_operators=tuple(sorted(operators)) if isinstance(operators, (set, frozenset, list, tuple)) and operators else _DEFAULT_OPERATORS,
            supported_windows=False,
            data_source="member_profile",
        )
    return specs


# ── 카탈로그 ──────────────────────────────────────────────────────────────────────────────
class ConceptCatalog:
    """search_concepts / get_concept / is_executable / get_compiler (요청 7·18번)."""

    def __init__(
        self,
        specs: Mapping[str, ConceptSpec],
        *,
        compiler_available: Callable[[str | None], bool] | None = None,
    ) -> None:
        self._specs = dict(specs)
        self._compiler_available = compiler_available or _default_compiler_available

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        compiler_available: Callable[[str | None], bool] | None = None,
    ) -> "ConceptCatalog":
        catalog_path = Path(path) if path is not None else Path(
            os.getenv("GRAPH_RAG_CONCEPT_CATALOG") or DEFAULT_CONCEPT_CATALOG_PATH)
        declared, extensions = _declared(catalog_path)
        specs: dict[str, ConceptSpec] = {}
        for source in (_derived_aggregate(extensions), _derived_profile(extensions), declared):
            for concept_id, spec in source.items():
                if concept_id in specs:
                    raise ConceptCatalogError(
                        f"개념 id 충돌: {concept_id!r} — 파생 소스와 선언이 같은 id 를 갖는다(이중 소유 금지).")
                specs[concept_id] = spec
        return cls(specs, compiler_available=compiler_available)

    # ── 조회 ──────────────────────────────────────────────────────────────────────────────
    def get_concept(self, concept_id: str) -> ConceptSpec | None:
        return self._specs.get(concept_id) if isinstance(concept_id, str) else None

    def get_compiler(self, concept_id: str) -> str | None:
        spec = self.get_concept(concept_id)
        return spec.compiler if spec is not None else None

    def is_executable(self, concept_id: str) -> bool:
        spec = self.get_concept(concept_id)
        return bool(spec is not None and spec.active and self._compiler_available(spec.compiler))

    def concept_ids(self) -> frozenset[str]:
        return frozenset(self._specs)

    # ── 검색(결정론) ──────────────────────────────────────────────────────────────────────
    def search_concepts(self, query: str, context: str | None = None, limit: int = 5) -> list[dict[str, Any]]:
        """정규화 포함 매칭 후보 목록. LLM 은 이 결과의 canonical_id 중 하나만 선택할 수 있다.

        점수: 정확 일치 1.0 > 별칭이 질의에 포함 0.9+길이가중 > 질의가 별칭에 포함 0.75+길이가중.
        동점은 concept_id 사전순 — 같은 질의는 항상 같은 후보 순서를 만든다."""
        needle = normalize_entity_term(query)
        if not needle:
            return []
        scored: list[tuple[float, str, ConceptSpec]] = []
        for concept_id, spec in self._specs.items():
            best = 0.0
            for surface in (spec.label, *spec.aliases):
                token = normalize_entity_term(surface)
                if not token:
                    continue
                if token == needle:
                    score = 1.0
                elif token in needle:
                    score = 0.9 + min(len(token), 20) * 0.004
                elif needle in token:
                    score = 0.75 + min(len(needle), 20) * 0.004
                else:
                    continue
                best = max(best, min(score, 0.99) if score < 1.0 else 1.0)
            if best > 0.0:
                scored.append((best, concept_id, spec))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [
            {
                "canonical_id": concept_id,
                "label": spec.label,
                "entity_type": spec.entity_type,
                "score": round(score, 4),
                "aliases": list(spec.aliases),
                "executable": self.is_executable(concept_id),
                "compiler": spec.compiler,
            }
            for score, concept_id, spec in scored[: max(1, int(limit))]
        ]


# ── 기본 카탈로그(캐시) ────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=2)
def _default_catalog_cached(path_text: str) -> ConceptCatalog:
    return ConceptCatalog.load(Path(path_text))


def default_catalog() -> ConceptCatalog:
    path = os.getenv("GRAPH_RAG_CONCEPT_CATALOG") or str(DEFAULT_CONCEPT_CATALOG_PATH)
    return _default_catalog_cached(str(Path(path)))


def clear_cache() -> None:
    """카탈로그/설정 파일을 바꿔 끼우는 테스트용."""
    _default_catalog_cached.cache_clear()
    _default_compiler_names.cache_clear()


__all__ = [
    "CONCEPT_KINDS",
    "DEFAULT_CONCEPT_CATALOG_PATH",
    "ConceptCatalog",
    "ConceptCatalogError",
    "ConceptSpec",
    "clear_cache",
    "default_catalog",
]
