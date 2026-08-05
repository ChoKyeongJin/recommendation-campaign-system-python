"""범용 Temporal Semantic Framework — 시간 의미 · 이력 형태 · 시간 연산의 분리.

계층::

    자연어(도메인 계층 소유)
      → Canonical Temporal Semantic IR      temporal_ir.semantic_ir
      → metric / temporal binding 해석       temporal_ir.catalog
      → 시간 의미와 데이터 역량 검증          temporal_ir.registry
      → Temporal Operator Registry           temporal_ir.operators
      → Execution IR 로 lowering              temporal_ir.lowering  → event_ir
      → SQL                                   event_compiler
      → Lowering Receipt / Coverage 판정      temporal_ir.lowering

SQL 컴파일러는 :class:`~temporal_ir.semantic_ir.TemporalCondition` 을 보지 않는다. 실행 IR
(:mod:`event_ir`)만 받는다 — 그 경계가 있어야 "시간 의미가 어디서 사라졌는가"를 물을 수 있다.

새 시간 속성을 추가할 때 이 패키지의 Python 코드는 바뀌지 않는다. 바뀌는 것은
``docs/data/runtime/semantics/temporal_bindings.json`` 한 항목이다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import event_ir
import resolved_semantic_catalog
from temporal_ir import calendar, catalog, lowering, operators, registry, semantic_ir
from temporal_ir.catalog import (
    CoverageSpec,
    CoverageStatus,
    MetricSpec,
    ObservationCapabilities,
    Representation,
    TemporalBindingSpec,
    TemporalCatalog,
    TemporalCatalogError,
)
from temporal_ir.lowering import (
    AudienceComposition,
    LoweringBlocked,
    LoweringInvalid,
    LoweringOutcome,
    LoweringSuccess,
    LoweringUnsupported,
    TemporalLoweringReceipt,
)
from temporal_ir.registry import (
    TemporalOperatorDefinition,
    TemporalOperatorRegistry,
    resolve_operator_name,
)
from temporal_ir.semantic_ir import TemporalCondition, TemporalRequestContext


@dataclass(frozen=True, slots=True)
class TemporalRuntime:
    """구성된 카탈로그 + 레지스트리. 애플리케이션 초기화에서 **한 번** 만들어 전달한다.

    전역 레지스트리를 두지 않는 이유는 테스트 격리와 import 부작용이다(CLAUDE.md §30·§58).
    """

    temporal_catalog: TemporalCatalog
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog
    registry: TemporalOperatorRegistry

    def lower(
        self, condition: TemporalCondition, context: TemporalRequestContext
    ) -> LoweringOutcome:
        return lowering.lower_temporal_condition(
            condition,
            temporal_catalog=self.temporal_catalog,
            semantic_catalog=self.semantic_catalog,
            context=context,
            registry=self.registry,
        )

    def compose(
        self,
        conditions: Sequence[TemporalCondition],
        context: TemporalRequestContext,
        *,
        additional: Sequence[event_ir.Condition] = (),
    ) -> AudienceComposition:
        return lowering.compose_audience(
            conditions,
            temporal_catalog=self.temporal_catalog,
            semantic_catalog=self.semantic_catalog,
            context=context,
            registry=self.registry,
            additional=additional,
        )


def create_temporal_runtime(
    semantic_catalog: resolved_semantic_catalog.ResolvedSemanticCatalog,
    *,
    path: str | Path = catalog.DEFAULT_TEMPORAL_CATALOG_PATH,
    payload: Mapping[str, Any] | None = None,
) -> TemporalRuntime:
    """명시적 구성 진입점 — import 부작용으로 카탈로그를 읽지 않는다."""
    operator_registry = operators.create_default_temporal_operator_registry()
    temporal_catalog = catalog.load_temporal_catalog(
        semantic_catalog,
        path=path,
        payload=payload,
        known_operators=operator_registry.names(),
    )
    return TemporalRuntime(
        temporal_catalog=temporal_catalog,
        semantic_catalog=semantic_catalog,
        registry=operator_registry,
    )


__all__ = [
    "AudienceComposition",
    "CoverageSpec",
    "CoverageStatus",
    "LoweringBlocked",
    "LoweringInvalid",
    "LoweringOutcome",
    "LoweringSuccess",
    "LoweringUnsupported",
    "MetricSpec",
    "ObservationCapabilities",
    "Representation",
    "TemporalBindingSpec",
    "TemporalCatalog",
    "TemporalCatalogError",
    "TemporalCondition",
    "TemporalLoweringReceipt",
    "TemporalOperatorDefinition",
    "TemporalOperatorRegistry",
    "TemporalRequestContext",
    "TemporalRuntime",
    "calendar",
    "catalog",
    "create_temporal_runtime",
    "lowering",
    "operators",
    "registry",
    "resolve_operator_name",
    "semantic_ir",
]
