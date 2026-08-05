"""자연어 → Canonical Event IR 파이프라인.

이 패키지의 설계 전제 한 줄: **사전은 '같은 뜻의 다른 말'만 담고, 그 밖의 모든 것은 구조가
담당한다.** 활용형은 정규화기가, 데이터 값은 entity resolver 가, 문맥 판정은 extractor 와
composer 의 조합이 처리한다.

빠른 시작::

    from nl_event_ir import EntityCandidate, EntityType, InMemoryEntityRepository
    from nl_event_ir.parser import build_default_parser

    repository = InMemoryEntityRepository(
        [
            EntityCandidate(
                entity_type=EntityType.BRAND,
                entity_id="brand:nike",
                canonical_name="나이키",
                score=1.0,
            )
        ]
    )
    parser = build_default_parser(repository)
    result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")

기존 :mod:`event_ir` (조건 대수 IR)와는 다른 계층이다. 이 패키지는 **자연어 프런트엔드**이고,
그쪽은 조건을 SQL 로 낮추는 **백엔드 IR** 이다. 이름이 비슷하니 import 를 헷갈리지 않도록
주의한다.
"""

from __future__ import annotations

from nl_event_ir.aliases import AliasRegistry, AliasSection, load_alias_registry
from nl_event_ir.composer import EventIRComposer
from nl_event_ir.enums import (
    ComparisonOperator,
    EntityType,
    EventType,
    IssueSeverity,
    LogicOperator,
    MetricType,
    Polarity,
    SortDirection,
    TimeUnit,
)
from nl_event_ir.fallback import FallbackReason, NullSemanticFallback, SemanticFallback
from nl_event_ir.models import (
    AbsoluteDateRange,
    ConditionGroup,
    ConditionNode,
    EventCondition,
    EventIR,
    MetricCondition,
    NormalizedText,
    ParseResult,
    RelativeTimeWindow,
    ScopeFilter,
    ValidationIssue,
)
from nl_event_ir.normalizer import RuleBasedKoreanNormalizer, TextNormalizer
from nl_event_ir.parser import EventParser, build_default_parser
from nl_event_ir.resolver import (
    EntityCandidate,
    EntityRepository,
    EntityResolution,
    EntityResolver,
    InMemoryEntityRepository,
)
from nl_event_ir.tokenizer import SemanticToken, TokenKind
from nl_event_ir.validator import EventIRValidator

__all__ = [
    "AbsoluteDateRange",
    "AliasRegistry",
    "AliasSection",
    "ComparisonOperator",
    "ConditionGroup",
    "ConditionNode",
    "EntityCandidate",
    "EntityRepository",
    "EntityResolution",
    "EntityResolver",
    "EntityType",
    "EventCondition",
    "EventIR",
    "EventIRComposer",
    "EventIRValidator",
    "EventParser",
    "EventType",
    "FallbackReason",
    "InMemoryEntityRepository",
    "IssueSeverity",
    "LogicOperator",
    "MetricCondition",
    "MetricType",
    "NormalizedText",
    "NullSemanticFallback",
    "ParseResult",
    "Polarity",
    "RelativeTimeWindow",
    "RuleBasedKoreanNormalizer",
    "ScopeFilter",
    "SemanticFallback",
    "SemanticToken",
    "SortDirection",
    "TextNormalizer",
    "TimeUnit",
    "TokenKind",
    "ValidationIssue",
    "build_default_parser",
    "load_alias_registry",
]
