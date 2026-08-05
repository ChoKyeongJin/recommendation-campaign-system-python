"""엔티티 해석 계층 — 사전에 없는 **데이터 값**을 카탈로그로 접지한다."""

from __future__ import annotations

from nl_event_ir.resolver.base import EntityCandidate, EntityRepository, EntityResolution
from nl_event_ir.resolver.entity import EntityResolver
from nl_event_ir.resolver.repository import InMemoryEntityRepository

__all__ = [
    "EntityCandidate",
    "EntityRepository",
    "EntityResolution",
    "EntityResolver",
    "InMemoryEntityRepository",
]
