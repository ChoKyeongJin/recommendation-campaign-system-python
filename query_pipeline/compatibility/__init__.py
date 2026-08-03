"""compatibility 계층 — 기존 저장 형식과 새 계층 사이의 한 방향 다리(deprecated)."""

from query_pipeline.compatibility.legacy_event_expression import (
    CANONICAL_SOURCES,
    DEPRECATION_MESSAGE,
    LegacyAdapterError,
    LegacyConversionFailed,
    LegacyConversionResult,
    LegacyEventExpressionAdapter,
    LegacyEventExpressionPayload,
    LegacySpecConversion,
    to_legacy_unsupported,
)

__all__ = [
    "CANONICAL_SOURCES",
    "DEPRECATION_MESSAGE",
    "LegacyAdapterError",
    "LegacyConversionFailed",
    "LegacyConversionResult",
    "LegacyEventExpressionAdapter",
    "LegacyEventExpressionPayload",
    "LegacySpecConversion",
    "to_legacy_unsupported",
]
