"""실행 자산 **총체성 검사** — 프롬프트를 돌리기 전에 배선의 구멍을 찾는다.

배경(실측 2026-08-06)
---------------------
카탈로그가 심볼을 선언해 놓았는데 그 심볼을 낮출 lowering 이나 물리 바인딩이 없으면, 그
사실은 **사용자 프롬프트가 그 심볼을 건드리는 순간에야** 드러난다. 그때의 증상은
``semantic_registry_gap`` 이고, 그 코드는 "설정이 비어 있다"까지만 말한다. 어느 심볼이
어느 고리에서 끊겼는지는 로그를 파야 나온다.

이 모듈은 그 판정을 **시작 시점**으로 당긴다. 심볼마다 다섯 고리를 확인한다::

    catalog symbol → field/operator mapping → validator → lowering → compiler → physical binding

끊긴 고리가 있으면 :class:`CapabilityCheck` 한 줄로 남고, 배포 모드에 따라 기동을 실패시키거나
(프로덕션) 진단으로 남긴다(개발). **선언은 있는데 구현이 없는 상태를 조용히 두지 않는다** —
그 상태는 곧 "있다고 광고한 능력"이다.

이 모듈은 카탈로그를 고치지 않는다. 읽고 대조만 한다.

실행: python -m pytest tests/test_compile_outcome_totality.py -q  (이 모듈의 계약이 그 파일에 있다)
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

# 배포 모드. 프로덕션에서는 치명적 gap 이 기동을 막는다.
STRICT_ENV = "CAPABILITY_AUDIT_STRICT"

REGISTRY_GAP_CODE = "semantic_registry_gap"


@dataclass(frozen=True, slots=True)
class CapabilityCheck:
    """심볼 하나의 배선 상태. ``False`` 인 고리가 곧 gap 이다."""

    symbol: str
    kind: str
    field_mapping: bool = False
    operator_mapping: bool = False
    validator: bool = False
    lowering: bool = False
    compiler: bool = False
    physical_binding: bool = False

    @property
    def missing_links(self) -> tuple[str, ...]:
        return tuple(
            name
            for name in (
                "field_mapping",
                "operator_mapping",
                "validator",
                "lowering",
                "compiler",
                "physical_binding",
            )
            if not getattr(self, name)
        )

    @property
    def is_complete(self) -> bool:
        return not self.missing_links

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "kind": self.kind,
            "field_mapping": self.field_mapping,
            "operator_mapping": self.operator_mapping,
            "validator": self.validator,
            "lowering": self.lowering,
            "compiler": self.compiler,
            "physical_binding": self.physical_binding,
            "missing_links": list(self.missing_links),
        }


@dataclass(frozen=True, slots=True)
class AuditReport:
    checks: tuple[CapabilityCheck, ...]
    catalog_error: str | None = None

    @property
    def gaps(self) -> tuple[CapabilityCheck, ...]:
        return tuple(check for check in self.checks if not check.is_complete)

    @property
    def is_clean(self) -> bool:
        return self.catalog_error is None and not self.gaps

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog_error": self.catalog_error,
            "symbol_count": len(self.checks),
            "gap_count": len(self.gaps),
            "gaps": [check.to_dict() for check in self.gaps],
        }

    def summary(self) -> str:
        if self.catalog_error:
            return f"{REGISTRY_GAP_CODE}: catalog unavailable ({self.catalog_error})"
        if not self.gaps:
            return f"capability audit clean ({len(self.checks)} symbols)"
        listed = ", ".join(
            f"{check.symbol}[{'/'.join(check.missing_links)}]" for check in self.gaps[:8]
        )
        return f"{REGISTRY_GAP_CODE}: {len(self.gaps)} symbol(s) unwired — {listed}"


def _declared_sources(catalog: Any) -> dict[str, Any]:
    events = getattr(catalog, "compiler_events", None)
    return dict(events) if isinstance(events, Mapping) else {}


def _declared_fields(catalog: Any) -> dict[str, Any]:
    fields = getattr(catalog, "compiler_fields", None)
    return dict(fields) if isinstance(fields, Mapping) else {}


def audit_event_sources() -> AuditReport:
    """오디언스 카탈로그의 이벤트 소스 심볼을 전수 검사한다.

    검사 대상은 **컴파일러가 실제로 읽는 사양**이다(선언 파일이 아니라). 그래야 "파일에는
    있는데 컴파일러가 못 읽는" 상태가 gap 으로 잡힌다.
    """
    try:
        import audience_runtime
        import event_compiler

        catalog = audience_runtime.resolve_audience_catalog()
    except Exception as exc:  # noqa: BLE001 — 카탈로그 로딩 실패 자체가 최상위 gap 이다
        return AuditReport((), f"{exc.__class__.__name__}: {exc}"[:160])

    sources = _declared_sources(catalog)
    fields = _declared_fields(catalog)
    checks: list[CapabilityCheck] = []
    for symbol, spec in sorted(sources.items()):
        # 고리의 이름은 :class:`event_compiler.EventSpec` 이 소유한다 — 여기서 두 번째 사양을
        # 만들지 않는다. 없는 속성을 물으면 그 자체가 gap 으로 잡히므로 오탐이 조용히 남는다.
        table = str(getattr(spec, "table", "") or "")
        from_sql = str(getattr(spec, "from_sql", "") or "")
        subject_key = str(getattr(spec, "subject_key", "") or "")
        event_subject_key = str(getattr(spec, "event_subject_key", "") or "")
        correlation_sql = str(getattr(spec, "correlation_sql", "") or "")
        time_column = str(getattr(spec, "time_column", "") or "")
        time_expression = str(getattr(spec, "time_expression", "") or "")
        binding = str(getattr(spec, "binding", "") or "")
        exposed_fields = [
            field_symbol
            for field_symbol, field_spec in fields.items()
            if getattr(field_spec, "source", None) == symbol
        ]
        checks.append(
            CapabilityCheck(
                symbol=symbol,
                kind="event_source",
                # 필드 사상: 이 소스가 노출하는 필드가 하나라도 선언돼 있는가.
                field_mapping=bool(exposed_fields),
                # 연산 사상: 시간 연산자가 성립하려면 시각 바인딩(컬럼 또는 식)이 있어야 한다.
                operator_mapping=bool(time_column or time_expression),
                # 검증기: 주체 결속(직접 키 또는 상관식)이 있어야 조건을 검증할 수 있다.
                validator=bool((subject_key and event_subject_key) or correlation_sql),
                # lowering: 바인딩 종류가 컴파일러가 아는 어휘여야 한다.
                lowering=binding in {"fact_table", "subject_column"},
                compiler=hasattr(event_compiler, "compile_relation")
                and hasattr(event_compiler, "compile_condition"),
                # 물리 바인딩: 테이블이거나 검증된 조인 relation.
                physical_binding=bool(table or from_sql),
            )
        )
    return AuditReport(tuple(checks))


def strict_mode() -> bool:
    return os.getenv(STRICT_ENV, "").strip().casefold() in {"1", "true", "yes", "on"}


class CapabilityAuditError(RuntimeError):
    """프로덕션 모드에서 치명적 gap 이 발견됐다(기동 실패)."""


def enforce(report: AuditReport, *, strict: bool | None = None) -> AuditReport:
    """gap 을 정책대로 처리한다. strict 면 기동을 실패시킨다."""
    if (strict if strict is not None else strict_mode()) and not report.is_clean:
        raise CapabilityAuditError(report.summary())
    return report


def unwired_symbols(report: AuditReport) -> tuple[str, ...]:
    return tuple(check.symbol for check in report.gaps)


def as_unsupported_registrations(report: AuditReport) -> tuple[dict[str, Any], ...]:
    """gap 심볼을 **명시적 미지원**으로 등록할 목록.

    프로덕션이 아닌 배포에서는 기동을 막는 대신 이 목록을 남긴다 — 그 심볼을 건드리는 요청은
    '조용한 실패'가 아니라 이름을 가진 미지원으로 끝나야 한다.
    """
    return tuple(
        {
            "symbol": check.symbol,
            "code": REGISTRY_GAP_CODE,
            "missing_links": list(check.missing_links),
        }
        for check in report.gaps
    )


__all__ = [
    "REGISTRY_GAP_CODE",
    "STRICT_ENV",
    "AuditReport",
    "CapabilityAuditError",
    "CapabilityCheck",
    "audit_event_sources",
    "as_unsupported_registrations",
    "enforce",
    "strict_mode",
    "unwired_symbols",
]
