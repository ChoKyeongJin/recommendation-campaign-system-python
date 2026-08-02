"""Capability 판정 — 의미 노드가 **실제로 실행 가능한가**를 축별로 나눠 답한다.

`supported: true/false` 하나로 답하면 세 가지 다른 사건이 한 단어로 뭉개진다:

  ① 의미 연산자를 우리가 정의하지 않았다        → unsupported_semantics
  ② 연산은 있는데 데이터 그레인이 부족하다       → unsupported_data_grain
  ③ 그레인은 맞는데 요청 기간에 적재가 없다      → data_unavailable
  ④ 컴파일러/실행기가 터졌다                     → execution_failure / internal_fault

④ 를 사용자에게 '미지원'으로 표시하면 능력의 부재를 거짓 선언하는 것이다. 그래서 이
모듈은 판정을 축으로 분해하고, 실패 코드는 semantic_plan 의 닫힌 집합에서만 고른다.

권위: `docs/data/runtime/semantics/semantic_capabilities.json`. 새 지표·새 노드 종류의
지원 여부는 JSON 한 줄로 바뀐다(코드 변경 불필요).

순수 모듈 규약: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import semantic_plan

DEFAULT_CAPABILITY_PATH = (
    Path(__file__).resolve().parent / "docs" / "data" / "runtime" / "semantics" / "semantic_capabilities.json"
)


class CapabilityRegistryError(ValueError):
    """capability 선언 파일이 스키마를 위반했을 때."""


@dataclass(frozen=True)
class Coverage:
    start: str | None = None
    end: str | None = None
    complete: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"from": self.start, "to": self.end, "complete": self.complete}

    def covers(self, period_start: str | None, period_end: str | None) -> bool:
        """요청 기간이 적재 구간 안에 있는가. 경계가 선언되지 않았으면 제약 없음."""
        if self.start and period_start and _month_key(period_start) < _month_key(self.start):
            return False
        if self.end and period_end and _month_key(period_end) > _month_key(self.end):
            return False
        return True


def _month_key(value: str) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return digits[:6].ljust(6, "0")


@dataclass(frozen=True)
class CapabilityVerdict:
    """노드 하나의 실행 가능성 판정. 축이 전부 참일 때만 executable 이다."""

    node_id: str
    node_type: str
    metric: str | None
    semantic_supported: bool
    compiler_supported: bool
    executor_supported: bool
    required_grain: str | None
    available_grain: str | None
    coverage: Coverage
    failure_code: str | None = None
    message: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def executable(self) -> bool:
        return self.failure_code is None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "metric": self.metric,
            "semantic_supported": self.semantic_supported,
            "compiler_supported": self.compiler_supported,
            "executor_supported": self.executor_supported,
            "required_grain": self.required_grain,
            "available_grain": self.available_grain,
            "coverage": self.coverage.to_dict(),
            "executable": self.executable,
        }
        if self.failure_code:
            payload["failure_code"] = self.failure_code
        if self.message:
            payload["message"] = self.message
        if self.detail:
            payload["detail"] = dict(self.detail)
        return payload


class CapabilityRegistry:
    """선언 파일 하나에서 축별 판정을 만든다."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        if not isinstance(payload, Mapping):
            raise CapabilityRegistryError("capability 선언은 객체여야 한다")
        self._grains = payload.get("grains") if isinstance(payload.get("grains"), Mapping) else {}
        self._node_types = payload.get("node_types") if isinstance(payload.get("node_types"), Mapping) else {}
        self._metrics = payload.get("metrics") if isinstance(payload.get("metrics"), Mapping) else {}
        if not self._node_types:
            raise CapabilityRegistryError("capability 선언에 node_types 가 없다")
        missing = sorted(set(semantic_plan.NODE_CLASS_BY_TYPE) - set(self._node_types))
        if missing:
            raise CapabilityRegistryError(
                f"capability 선언에 없는 노드 타입: {', '.join(missing)} — 노드를 추가했으면 선언도 추가하라."
            )

    @classmethod
    def load(cls, path: str | Path | None = None) -> "CapabilityRegistry":
        target = Path(path or os.getenv("SEMANTIC_CAPABILITIES_PATH") or DEFAULT_CAPABILITY_PATH)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CapabilityRegistryError(f"capability 선언을 읽지 못했다: {exc}") from exc
        return cls(payload)

    # ── 축 조회 ──
    def _grain_coverage(self, grain: str | None) -> Coverage:
        spec = self._grains.get(str(grain)) if grain else None
        raw = spec.get("coverage") if isinstance(spec, Mapping) else None
        if not isinstance(raw, Mapping):
            return Coverage()
        return Coverage(
            start=raw.get("from") if isinstance(raw.get("from"), str) else None,
            end=raw.get("to") if isinstance(raw.get("to"), str) else None,
            complete=bool(raw.get("complete", True)),
        )

    def _node_spec(self, node: semantic_plan.SemanticNode) -> dict[str, Any]:
        """노드 타입 선언 + (scope/entity/relation) 하위 선언을 겹쳐 읽는다."""
        base = self._node_types.get(node.type)
        spec: dict[str, Any] = dict(base) if isinstance(base, Mapping) else {}
        for axis_key, value_field in (("scopes", "scope"), ("entities", "entity"), ("relations", "relation"),
                                      ("operators", "operator")):
            table = spec.get(axis_key)
            key = node.values.get(value_field)
            if isinstance(table, Mapping) and isinstance(key, str):
                override = table.get(key)
                if isinstance(override, Mapping):
                    spec.update(override)
        metric = node.values.get("metric") or node.values.get("attribute")
        metric_spec = self._metrics.get(str(metric)) if isinstance(metric, str) else None
        if isinstance(metric_spec, Mapping):
            if metric_spec.get("grain"):
                spec["required_grain"] = metric_spec["grain"]
            for key in ("semantic_supported", "compiler_supported", "executor_supported", "unsupported_message"):
                if key in metric_spec:
                    spec[key] = metric_spec[key]
        return spec

    def judge(
        self,
        node: semantic_plan.SemanticNode,
        *,
        available_months: Mapping[str, int] | None = None,
    ) -> CapabilityVerdict:
        """노드 하나의 축별 판정. `available_months` 는 그레인별 실적재 월 수(주입)."""
        spec = self._node_spec(node)
        metric = node.values.get("metric") or node.values.get("attribute")
        required_grain = spec.get("required_grain")
        required_grain = str(required_grain) if isinstance(required_grain, str) else None
        available_grain = required_grain if required_grain in self._grains else None
        coverage = self._grain_coverage(required_grain)

        semantic_ok = bool(spec.get("semantic_supported", False))
        compiler_ok = bool(spec.get("compiler_supported", False))
        executor_ok = bool(spec.get("executor_supported", False))
        message = spec.get("unsupported_message")
        message = str(message) if isinstance(message, str) and message.strip() else None

        def verdict(code: str | None, note: str | None = None, **detail: Any) -> CapabilityVerdict:
            return CapabilityVerdict(
                node_id=node.id,
                node_type=node.type,
                metric=str(metric) if isinstance(metric, str) else None,
                semantic_supported=semantic_ok,
                compiler_supported=compiler_ok,
                executor_supported=executor_ok,
                required_grain=required_grain,
                available_grain=available_grain,
                coverage=coverage,
                failure_code=code,
                message=note,
                detail=detail,
            )

        if not semantic_ok:
            return verdict(semantic_plan.UNSUPPORTED_SEMANTICS,
                           message or f"'{node.type}' 의미 연산을 아직 지원하지 않습니다.")
        if not compiler_ok:
            return verdict(semantic_plan.UNSUPPORTED_SEMANTICS,
                           message or f"'{node.type}' 조건을 실행 계획으로 옮기는 컴파일러가 없습니다.")
        if not executor_ok:
            return verdict(semantic_plan.UNSUPPORTED_SEMANTICS,
                           message or f"'{node.type}' 조건을 실행할 빌더가 없습니다.")
        if required_grain and available_grain is None:
            return verdict(semantic_plan.UNSUPPORTED_DATA_GRAIN,
                           message or f"이 조건에 필요한 데이터 그레인({required_grain})이 선언되어 있지 않습니다.",
                           required_grain=required_grain)

        # 그레인 깊이(월 스냅샷 개수 등) — 선언된 최소 요구를 실적재가 못 채우면 grain 부족이다.
        requires_months = spec.get("requires_months")
        if isinstance(requires_months, int) and requires_months > 1:
            loaded = int((available_months or {}).get(str(required_grain), 0) or 0)
            if loaded < requires_months:
                return verdict(
                    semantic_plan.UNSUPPORTED_DATA_GRAIN,
                    message or (
                        f"이 조건은 {requires_months}개월 이상의 이력이 필요하지만 현재 "
                        f"{loaded}개월만 적재되어 있습니다."
                    ),
                    required_months=requires_months,
                    available_months=loaded,
                )

        # 요청 기간이 적재 구간 밖이면 능력 부재가 아니라 데이터 부재다.
        for period_field in ("period", "baseline", "current"):
            window = node.values.get(period_field)
            if isinstance(window, Mapping) and (window.get("from") or window.get("to")):
                if not coverage.covers(window.get("from"), window.get("to")):
                    return verdict(
                        semantic_plan.DATA_UNAVAILABLE,
                        f"요청 기간의 데이터가 적재되어 있지 않습니다(적재 구간: "
                        f"{coverage.start or '제한 없음'}~{coverage.end or '제한 없음'}).",
                        requested={"field": period_field, **dict(window)},
                    )
        return verdict(None)

    def judge_plan(
        self,
        plan: semantic_plan.SemanticPlanV2,
        *,
        available_months: Mapping[str, int] | None = None,
    ) -> list[CapabilityVerdict]:
        return [
            self.judge(node, available_months=available_months)
            for node in plan.walk()
            if node.type != semantic_plan.LogicalExpression.TYPE
        ]


_REGISTRY: CapabilityRegistry | None = None


def registry() -> CapabilityRegistry:
    """프로세스 공유 레지스트리(선언 파일은 런타임에 바뀌지 않는다)."""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = CapabilityRegistry.load()
    return _REGISTRY


def reset_registry() -> None:
    """테스트에서 선언 파일을 갈아 끼울 때."""
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "CapabilityRegistry",
    "CapabilityRegistryError",
    "CapabilityVerdict",
    "Coverage",
    "DEFAULT_CAPABILITY_PATH",
    "registry",
    "reset_registry",
]
