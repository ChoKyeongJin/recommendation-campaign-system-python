from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ExternalCondition, ResolutionContext, ResolverResult


class ExternalConditionResolver(ABC):
    provider: str

    @abstractmethod
    def supports(self, condition: ExternalCondition) -> bool:
        """Return whether this resolver owns the normalized condition."""

    @abstractmethod
    def resolve(
        self, condition: ExternalCondition, context: ResolutionContext
    ) -> ResolverResult:
        """Resolve the external condition without generating SQL."""
