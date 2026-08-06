"""의미 요구사항 **원장** — 모든 요구가 terminal disposition 을 갖게 하는 회계 계층.

배경(실측 2026-08-06)
---------------------
``직전 등급이 골드였던 회원`` 이 SQL 을 냈는데 '직전 기준 기간' 의미가 빠져 있었다. 러너는
코퍼스가 선언한 물리 컬럼(``PREV_ZTS_GRADE``)이 SQL 에 없다는 이유로 그것을 잡았지만, 그건
**코퍼스가 물리 필드 이름을 정답으로 들고 있어서** 가능했던 것이다. 코퍼스가 그 줄을 안 적은
의미는 아무도 못 잡는다.

이 모듈은 그 안전망을 코퍼스에서 런타임으로 옮긴다. 규칙은 하나다:

    **모든 SemanticRequirement 가 terminal disposition 을 갖기 전에는 SQL 을 출고하지 않는다.**

terminal 은 넷뿐이다 — ``compiled`` · ``explicit_unsupported`` · ``user_clarification`` ·
``policy_rejected``. 'detected' 로 남은 요구가 하나라도 있으면 그것은 **조용히 사라진 의미**이고,
그 상태의 SQL 은 성공이 아니다.

원문 구간 하나가 요구 하나라고 가정하지 않는다. ``최근`` 과 ``30일`` 처럼 떨어진 구간 여럿이
하나의 typed requirement 를 이룰 수 있으므로 :attr:`SemanticRequirement.source_spans` 는 복수다.

이 모듈은 **회계만** 한다 — 추출도, 컴파일도, SQL 도 하지 않는다. 재료는 호출자가 준다.

실행: python -m pytest tests/test_semantic_ledger.py -q
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

# ── terminal disposition(닫힌 집합) ───────────────────────────────────────────────
DISPOSITION_COMPILED = "compiled"
DISPOSITION_UNSUPPORTED = "explicit_unsupported"
DISPOSITION_CLARIFICATION = "user_clarification"
DISPOSITION_POLICY_REJECTED = "policy_rejected"

TERMINAL_DISPOSITIONS: frozenset[str] = frozenset({
    DISPOSITION_COMPILED,
    DISPOSITION_UNSUPPORTED,
    DISPOSITION_CLARIFICATION,
    DISPOSITION_POLICY_REJECTED,
})

# 소유자(누가 이 요구를 처리해야 하는가). 진단에서 "고칠 곳"을 가리킨다.
OWNERSHIP_AUDIENCE = "audience_compiler"
OWNERSHIP_PROJECTION = "projection_compiler"
OWNERSHIP_POLICY = "policy"
OWNERSHIP_STRUCTURER = "structurer"

LEDGER_KEY = "semantic_requirements_ledger"


class LedgerContractError(ValueError):
    """원장 계약 위반(알 수 없는 disposition 등)."""


@dataclass(frozen=True, slots=True)
class EvidenceSpan:
    start: int
    end: int
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"start": self.start, "end": self.end, "text": self.text}

    def overlaps(self, other: "EvidenceSpan") -> bool:
        return not (self.end <= other.start or self.start >= other.end)


@dataclass(frozen=True, slots=True)
class SemanticRequirement:
    """원문이 요구한 의미 하나와 그 귀결.

    ``canonical_kind`` 는 **도메인 의미**다(``temporal_window`` · ``result_shape`` ·
    ``grade_reference`` …). 물리 컬럼 이름이 아니다 — 물리 구현은 lowering 이 카탈로그를 보고
    고르고, 그 선택은 :mod:`provenance` 의 artifact 로 추적한다(§5).
    """

    requirement_id: str
    canonical_kind: str
    typed_value: Any = None
    source_spans: tuple[EvidenceSpan, ...] = ()
    scope: str | None = None
    polarity: str | None = None
    ownership: str = OWNERSHIP_AUDIENCE
    disposition: str | None = None
    reason_code: str | None = None
    ir_node_ids: tuple[str, ...] = ()
    compile_receipt_ids: tuple[str, ...] = ()
    sql_artifact_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.disposition is not None and self.disposition not in TERMINAL_DISPOSITIONS:
            raise LedgerContractError(f"unknown disposition: {self.disposition!r}")

    @property
    def is_terminal(self) -> bool:
        return self.disposition in TERMINAL_DISPOSITIONS

    @property
    def span(self) -> tuple[int, int] | None:
        if not self.source_spans:
            return None
        return (
            min(item.start for item in self.source_spans),
            max(item.end for item in self.source_spans),
        )

    def traceability_gaps(self) -> tuple[str, ...]:
        """``compiled`` 인데 끊긴 추적 고리(compiled 가 아니면 빈 튜플)."""
        if self.disposition != DISPOSITION_COMPILED:
            return ()
        gaps: list[str] = []
        if not self.ir_node_ids:
            gaps.append("ir_node")
        if not self.compile_receipt_ids:
            gaps.append("compile_receipt")
        if not self.sql_artifact_ids:
            gaps.append("sql_artifact")
        return tuple(gaps)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "canonical_kind": self.canonical_kind,
            "typed_value": self.typed_value,
            "source_spans": [span.to_dict() for span in self.source_spans],
            "scope": self.scope,
            "polarity": self.polarity,
            "ownership": self.ownership,
            "disposition": self.disposition,
            "reason_code": self.reason_code,
            "ir_node_ids": list(self.ir_node_ids),
            "compile_receipt_ids": list(self.compile_receipt_ids),
            "sql_artifact_ids": list(self.sql_artifact_ids),
        }


def requirement_id(
    *, canonical_kind: str, spans: Sequence[EvidenceSpan], typed_value: Any
) -> str:
    """내용 기반 안정 id. 같은 의미는 실행이 달라도 같은 id 를 갖는다(지문 재료)."""
    payload = json.dumps(
        {
            "kind": canonical_kind,
            "spans": sorted((span.start, span.end) for span in spans),
            "value": typed_value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return "req_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass
class RequirementLedger:
    """요구 목록과 그 귀결. 순서는 등록 순(재현 가능)."""

    _items: dict[str, SemanticRequirement] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[SemanticRequirement]:
        return iter(self._items.values())

    def add(self, requirement: SemanticRequirement) -> SemanticRequirement:
        """요구를 등록한다. 같은 id 가 이미 있으면 **덮어쓰지 않는다** —
        먼저 기록된 것이 원본이고, 귀결은 :meth:`settle` 로만 바뀐다."""
        return self._items.setdefault(requirement.requirement_id, requirement)

    def get(self, key: str) -> SemanticRequirement | None:
        return self._items.get(key)

    def settle(
        self,
        key: str,
        disposition: str,
        *,
        reason_code: str | None = None,
        ir_node_ids: Iterable[str] = (),
        compile_receipt_ids: Iterable[str] = (),
        sql_artifact_ids: Iterable[str] = (),
    ) -> SemanticRequirement | None:
        """귀결을 확정한다. 이미 terminal 이면 **바꾸지 않는다**(첫 귀결이 사실이다)."""
        current = self._items.get(key)
        if current is None or current.is_terminal:
            return current
        settled = replace(
            current,
            disposition=disposition,
            reason_code=reason_code or current.reason_code,
            ir_node_ids=tuple(dict.fromkeys([*current.ir_node_ids, *ir_node_ids])),
            compile_receipt_ids=tuple(
                dict.fromkeys([*current.compile_receipt_ids, *compile_receipt_ids])
            ),
            sql_artifact_ids=tuple(
                dict.fromkeys([*current.sql_artifact_ids, *sql_artifact_ids])
            ),
        )
        self._items[key] = settled
        return settled

    def settle_open(
        self, disposition: str, *, reason_code: str | None = None
    ) -> list[SemanticRequirement]:
        """아직 귀결이 없는 요구 전부를 한 귀결로 닫는다(미지원·되묻기 응답의 마무리)."""
        settled: list[SemanticRequirement] = []
        for key in list(self._items):
            if not self._items[key].is_terminal:
                item = self.settle(key, disposition, reason_code=reason_code)
                if item is not None:
                    settled.append(item)
        return settled

    def open_requirements(self) -> list[SemanticRequirement]:
        return [item for item in self._items.values() if not item.is_terminal]

    def compiled(self) -> list[SemanticRequirement]:
        return [
            item for item in self._items.values() if item.disposition == DISPOSITION_COMPILED
        ]

    def to_dict(self) -> dict[str, Any]:
        return {
            "requirements": [item.to_dict() for item in self._items.values()],
            "counts": self.counts(),
        }

    def counts(self) -> dict[str, int]:
        counts = {name: 0 for name in sorted(TERMINAL_DISPOSITIONS)}
        counts["open"] = 0
        for item in self._items.values():
            counts[item.disposition if item.is_terminal else "open"] += 1
        return counts


@dataclass(frozen=True, slots=True)
class CoverageVerdict:
    """출고 자격 판정. ``is_shippable=False`` 면 **성공 응답이 아니다**."""

    is_shippable: bool
    reason_code: str | None
    missing_requirement_ids: tuple[str, ...] = ()
    missing_canonical_kinds: tuple[str, ...] = ()
    traceability_gaps: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_shippable": self.is_shippable,
            "reason_code": self.reason_code,
            "missing_requirement_ids": list(self.missing_requirement_ids),
            "missing_canonical_kinds": list(self.missing_canonical_kinds),
            "traceability_gaps": [dict(item) for item in self.traceability_gaps],
        }


SHIPPABLE = CoverageVerdict(is_shippable=True, reason_code=None)

REASON_OPEN_REQUIREMENTS = "partial_sql"
REASON_TRACEABILITY = "untraceable_compiled_requirement"


def verdict(
    ledger: RequirementLedger, *, require_traceability: bool = False
) -> CoverageVerdict:
    """SQL 출고 전 커버리지 판정.

    1. terminal 이 아닌 요구가 하나라도 있으면 ``partial_sql`` 이다 — 일부 의미가 누락된 SQL 을
       HTTP 성공으로 돌려주지 않는다.
    2. ``require_traceability`` 면 compiled 요구가 IR 노드 → 영수증 → artifact 까지
       이어지는지도 본다. 개수 비교가 아니라 **연결**을 본다(하나의 요구가 여러 술어로,
       여러 요구가 하나의 식으로 갈 수 있으므로 개수는 애초에 같을 이유가 없다).
    """

    open_items = ledger.open_requirements()
    if open_items:
        return CoverageVerdict(
            is_shippable=False,
            reason_code=REASON_OPEN_REQUIREMENTS,
            missing_requirement_ids=tuple(item.requirement_id for item in open_items),
            missing_canonical_kinds=tuple(
                dict.fromkeys(item.canonical_kind for item in open_items)
            ),
        )
    if require_traceability:
        gaps = [
            {
                "requirement_id": item.requirement_id,
                "canonical_kind": item.canonical_kind,
                "missing_links": list(item.traceability_gaps()),
            }
            for item in ledger.compiled()
            if item.traceability_gaps()
        ]
        if gaps:
            return CoverageVerdict(
                is_shippable=False,
                reason_code=REASON_TRACEABILITY,
                missing_requirement_ids=tuple(item["requirement_id"] for item in gaps),
                missing_canonical_kinds=tuple(
                    dict.fromkeys(str(item["canonical_kind"]) for item in gaps)
                ),
                traceability_gaps=tuple(gaps),
            )
    return SHIPPABLE


def write_ledger(plan: dict[str, Any], ledger: RequirementLedger) -> RequirementLedger:
    plan[LEDGER_KEY] = ledger.to_dict()
    return ledger


def read_ledger(plan: Mapping[str, Any]) -> RequirementLedger:
    """플랜에 기록된 원장을 되읽는다(응답 조립·러너 진단용)."""
    payload = plan.get(LEDGER_KEY)
    ledger = RequirementLedger()
    if not isinstance(payload, Mapping):
        return ledger
    for raw in payload.get("requirements") or ():
        if not isinstance(raw, Mapping):
            continue
        spans = tuple(
            EvidenceSpan(
                start=int(item.get("start", 0)),
                end=int(item.get("end", 0)),
                text=str(item.get("text") or ""),
            )
            for item in raw.get("source_spans") or ()
            if isinstance(item, Mapping)
        )
        ledger.add(
            SemanticRequirement(
                requirement_id=str(raw.get("requirement_id")),
                canonical_kind=str(raw.get("canonical_kind")),
                typed_value=raw.get("typed_value"),
                source_spans=spans,
                scope=raw.get("scope"),
                polarity=raw.get("polarity"),
                ownership=str(raw.get("ownership") or OWNERSHIP_AUDIENCE),
                disposition=raw.get("disposition"),
                reason_code=raw.get("reason_code"),
                ir_node_ids=tuple(str(item) for item in raw.get("ir_node_ids") or ()),
                compile_receipt_ids=tuple(
                    str(item) for item in raw.get("compile_receipt_ids") or ()
                ),
                sql_artifact_ids=tuple(str(item) for item in raw.get("sql_artifact_ids") or ()),
            )
        )
    return ledger


__all__ = [
    "DISPOSITION_CLARIFICATION",
    "DISPOSITION_COMPILED",
    "DISPOSITION_POLICY_REJECTED",
    "DISPOSITION_UNSUPPORTED",
    "LEDGER_KEY",
    "OWNERSHIP_AUDIENCE",
    "OWNERSHIP_POLICY",
    "OWNERSHIP_PROJECTION",
    "OWNERSHIP_STRUCTURER",
    "REASON_OPEN_REQUIREMENTS",
    "REASON_TRACEABILITY",
    "SHIPPABLE",
    "TERMINAL_DISPOSITIONS",
    "CoverageVerdict",
    "EvidenceSpan",
    "LedgerContractError",
    "RequirementLedger",
    "SemanticRequirement",
    "read_ledger",
    "requirement_id",
    "verdict",
    "write_ledger",
]
