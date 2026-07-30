"""Resolve volatile external facts into grounded CRM targeting filters."""

from .models import (
    AdministrativeRegion,
    ExternalCondition,
    ResolutionContext,
    ResolverResult,
)
from .service import ExternalConditionService, build_default_service

__all__ = [
    "AdministrativeRegion",
    "ExternalCondition",
    "ExternalConditionService",
    "ResolutionContext",
    "ResolverResult",
    "build_default_service",
]
