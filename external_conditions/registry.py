from __future__ import annotations

from collections.abc import Iterable

from .models import ExternalCondition
from .resolvers.base import ExternalConditionResolver


class ExternalConditionResolverRegistry:
    def __init__(self, resolvers: Iterable[ExternalConditionResolver] = ()) -> None:
        self._resolvers = list(resolvers)

    def register(self, resolver: ExternalConditionResolver) -> None:
        self._resolvers.append(resolver)

    def find_resolver(
        self, condition: ExternalCondition
    ) -> ExternalConditionResolver | None:
        return next(
            (resolver for resolver in self._resolvers if resolver.supports(condition)),
            None,
        )
