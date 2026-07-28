"""플랜 후보의 유일한 병합 지점.

파서와 보강기는 최종 플랜을 서로 수정하지 않고 :class:`PlanCandidate` 만 만든다.
서로 다른 출처가 같은 슬롯에 값을 제안하면 이 모듈이 우선순위와 병합 정책을 한 번만
적용한다. 따라서 새 후보 출처가 추가돼도 충돌 규칙이 파서 구현 안으로 흩어지지 않는다.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Iterable


RESOLUTION_KEY = "plan_resolution"
_CONTROL_KEYS = frozenset({"_candidate_resolves_unsupported"})
_SNAPSHOT_KEYS = frozenset({"source_requirements", "source_requirements_digest"})


@dataclass(frozen=True)
class PlanCandidate:
    """한 출처가 제안한 희소 또는 완전 플랜."""

    source: str
    payload: dict[str, Any]
    priority: int


def resolve_plan_candidates(candidates: Iterable[PlanCandidate]) -> dict[str, Any]:
    """후보들을 하나의 실행 플랜으로 해석한다.

    높은 priority가 스칼라 충돌에서 이긴다. dict는 슬롯별로 재귀 해석하고 list는 순서를
    보존한 합집합으로 결합한다. ``intent=unknown`` 은 미결정 값이라 낮은 우선순위의 구체적
    intent가 채울 수 있다. 입력 후보는 절대로 변경하지 않는다.
    """

    ordered = sorted(list(candidates), key=lambda item: item.priority, reverse=True)
    if not ordered:
        raise ValueError("at least one plan candidate is required")
    if len({candidate.source for candidate in ordered}) != len(ordered):
        raise ValueError("plan candidate sources must be unique")

    resolved: dict[str, Any] = {}
    owners: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    resolutions: list[dict[str, Any]] = []
    unsupported_resolvers: list[dict[str, str]] = []

    for candidate in ordered:
        payload = candidate.payload
        if not isinstance(payload, dict):
            raise TypeError(f"plan candidate '{candidate.source}' payload must be a dict")
        raw_resolvers = payload.get("_candidate_resolves_unsupported")
        if isinstance(raw_resolvers, list):
            unsupported_resolvers.extend(
                item for item in raw_resolvers
                if isinstance(item, dict)
                and isinstance(item.get("reason"), str)
                and isinstance(item.get("path"), str)
            )
        _merge_mapping(
            resolved,
            payload,
            source=candidate.source,
            owners=owners,
            conflicts=conflicts,
            resolutions=resolutions,
        )

    unsupported = resolved.get("unsupported")
    if isinstance(unsupported, dict):
        reason = unsupported.get("reason")
        match = next(
            (
                item for item in unsupported_resolvers
                if item["reason"] == reason and not _is_missing(item["path"], _read_path(resolved, item["path"]))
            ),
            None,
        )
        if match is not None:
            resolved.pop("unsupported", None)
            resolutions.append({
                "path": "unsupported",
                "action": "clear_resolved_gate",
                "reason": reason,
                "resolved_by": match["path"],
            })

    resolved[RESOLUTION_KEY] = {
        "resolver": "single_pass_v1",
        "candidates": [
            {"source": candidate.source, "priority": candidate.priority}
            for candidate in ordered
        ],
        "conflicts": conflicts,
        "resolutions": resolutions,
    }
    return resolved


def _merge_mapping(
    target: dict[str, Any],
    incoming: dict[str, Any],
    *,
    source: str,
    owners: dict[str, str],
    conflicts: list[dict[str, Any]],
    resolutions: list[dict[str, Any]],
    prefix: str = "",
) -> None:
    for key, value in incoming.items():
        if key in _CONTROL_KEYS or key in _SNAPSHOT_KEYS:
            continue
        path = f"{prefix}.{key}" if prefix else str(key)
        if key not in target or _is_missing(path, target.get(key)):
            if _is_missing(path, value):
                target.setdefault(key, copy.deepcopy(value))
                owners.setdefault(path, source)
                continue
            target[key] = copy.deepcopy(value)
            _mark_owners(value, path, source, owners)
            resolutions.append({"path": path, "action": "select", "source": source})
            continue

        current = target[key]
        if _is_missing(path, value):
            continue
        if isinstance(current, dict) and isinstance(value, dict):
            _merge_mapping(
                current,
                value,
                source=source,
                owners=owners,
                conflicts=conflicts,
                resolutions=resolutions,
                prefix=path,
            )
            continue
        if isinstance(current, list) and isinstance(value, list):
            additions = [copy.deepcopy(item) for item in value if item not in current]
            if additions:
                current.extend(additions)
                resolutions.append({"path": path, "action": "union", "source": source, "added": additions})
            continue
        if current == value:
            continue

        conflicts.append({
            "path": path,
            "winner": owners.get(path, "higher_priority_candidate"),
            "winner_value": copy.deepcopy(current),
            "rejected": source,
            "rejected_value": copy.deepcopy(value),
            "policy": "higher_priority_wins",
        })


def _is_missing(path: str, value: Any) -> bool:
    if path == "intent" and value == "unknown":
        return True
    return value is None or value == "" or value == [] or value == {}


def _read_path(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def _mark_owners(value: Any, path: str, source: str, owners: dict[str, str]) -> None:
    owners[path] = source
    if isinstance(value, dict):
        for key, item in value.items():
            _mark_owners(item, f"{path}.{key}", source, owners)
