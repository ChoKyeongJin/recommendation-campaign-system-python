"""Hard safety boundary for the offline capability-discovery subsystem.

This module deliberately has no dependency on the runtime semantic planner or
SQL compiler.  Discovery output is evidence for a review workflow, never an
execution contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class DiscoveryBoundaryError(ValueError):
    """Raised when a caller attempts to weaken the offline boundary."""


@dataclass(frozen=True)
class DiscoveryPolicy:
    """Non-configurable G0 policy represented in reports and candidates."""

    discovery_only: bool = True
    executable: bool = False
    mutation_allowed: bool = False
    runtime_failure_codes_reused: bool = False
    data_availability_policy: str = "advise"

    def __post_init__(self) -> None:
        unsafe = (
            not self.discovery_only
            or self.executable
            or self.mutation_allowed
            or self.runtime_failure_codes_reused
        )
        if unsafe:
            raise DiscoveryBoundaryError(
                "offline discovery cannot execute SQL, mutate approved sources, "
                "or reuse runtime failure codes"
            )
        if self.data_availability_policy != "advise":
            raise DiscoveryBoundaryError(
                "G0 must preserve semantic_capabilities.data_availability_policy='advise'"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "discovery_only": self.discovery_only,
            "executable": self.executable,
            "mutation_allowed": self.mutation_allowed,
            "runtime_failure_codes_reused": self.runtime_failure_codes_reused,
            "data_availability_policy": self.data_availability_policy,
        }


DEFAULT_DISCOVERY_POLICY = DiscoveryPolicy()


def validate_offline_payload(payload: Mapping[str, Any]) -> None:
    """Reject a serialized report/candidate that crosses the G0 boundary."""

    policy = payload.get("policy")
    if not isinstance(policy, Mapping):
        raise DiscoveryBoundaryError("payload.policy is required")
    expected = DEFAULT_DISCOVERY_POLICY.to_dict()
    mismatches = {
        key: (policy.get(key), value)
        for key, value in expected.items()
        if policy.get(key) != value
    }
    if mismatches:
        raise DiscoveryBoundaryError(f"unsafe discovery policy: {mismatches}")

    if payload.get("executable", False) is not False:
        raise DiscoveryBoundaryError("discovery payload must declare executable=false")
    if payload.get("mutation_allowed", False) is not False:
        raise DiscoveryBoundaryError(
            "discovery payload must declare mutation_allowed=false"
        )
