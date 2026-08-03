"""Offline, non-executable canonical capability discovery.

Runtime planning and SQL compilation must not depend on this package.  The
public service is imported lazily by callers that explicitly run discovery.
"""

from .domain import (
    Evidence,
    GapRecord,
    GraphEdge,
    GraphNode,
    GraphSnapshot,
    RepositoryRevision,
)
from .graph_store import DiscoveryGraphStore
from .policy import DEFAULT_DISCOVERY_POLICY, DiscoveryBoundaryError, DiscoveryPolicy

__all__ = [
    "DEFAULT_DISCOVERY_POLICY",
    "DiscoveryBoundaryError",
    "DiscoveryGraphStore",
    "DiscoveryPolicy",
    "Evidence",
    "GapRecord",
    "GraphEdge",
    "GraphNode",
    "GraphSnapshot",
    "RepositoryRevision",
]
