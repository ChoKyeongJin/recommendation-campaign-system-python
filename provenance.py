"""요구 → IR 노드 → 컴파일 영수증 → SQL artifact 의 **provenance graph**.

왜 개수 비교가 아닌가
---------------------
"IR 원자 수 == 영수증 수 == SQL 술어 수" 는 처음부터 성립하지 않는다. 하나의 의미 요구가
여러 술어·조인·집계로 내려가고(``최근 30일 구매`` → EXISTS + 시간 술어), 여러 요구가 하나의
식으로 합쳐지기도 한다(같은 컬럼의 두 조건 → 하나의 BETWEEN). 개수를 세는 가드는 그래서
정상 SQL 을 막거나 결손 SQL 을 통과시킨다 — 둘 다 실측됐다.

그래서 이 모듈은 **연결**을 본다. 그래프의 간선은 셋이다::

    SemanticRequirement --owns--> IrNode --lowered_by--> CompileReceipt --emits--> SqlArtifact

출고 전 검사는 "모든 compiled 요구가 artifact 까지 도달하는가"와 "출처 없는 의미 artifact 가
있는가" 두 방향이다. 방향이 하나면 한쪽 결손을 놓친다.

artifact 추출은 sqlglot AST 로 한다 — 정규식으로 SQL 을 읽으면 별칭·줄바꿈·중첩에서 조용히
틀린다. 파싱에 실패하면 **추출 실패를 그대로 보고**하고 빈 목록을 정상으로 위장하지 않는다.

실행: python -m pytest tests/test_provenance.py -q
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import expressions as exp

# SQL artifact 종류(닫힌 집합).
ARTIFACT_PREDICATE = "predicate"
ARTIFACT_JOIN = "join"
ARTIFACT_PROJECTION = "projection"
ARTIFACT_AGGREGATION = "aggregation"
ARTIFACT_GROUPING = "grouping"
ARTIFACT_ORDERING = "ordering"
ARTIFACT_LIMIT = "limit"
ARTIFACT_WINDOW = "window"
ARTIFACT_CTE = "cte"

ARTIFACT_KINDS: frozenset[str] = frozenset({
    ARTIFACT_PREDICATE,
    ARTIFACT_JOIN,
    ARTIFACT_PROJECTION,
    ARTIFACT_AGGREGATION,
    ARTIFACT_GROUPING,
    ARTIFACT_ORDERING,
    ARTIFACT_LIMIT,
    ARTIFACT_WINDOW,
    ARTIFACT_CTE,
})

PROVENANCE_KEY = "provenance"


class ProvenanceError(ValueError):
    """그래프 계약 위반(알 수 없는 artifact 종류 등)."""


def _digest(*parts: Any) -> str:
    payload = " ".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True, slots=True)
class IrNode:
    """canonical IR 의 노드 하나. ``symbols`` 는 이 노드가 지목한 소스·필드 심볼이다."""

    node_id: str
    node_type: str
    symbols: tuple[str, ...] = ()
    negated: bool = False
    evidence: tuple[int, int] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "symbols": list(self.symbols),
            "negated": self.negated,
            "evidence": list(self.evidence) if self.evidence else None,
        }


@dataclass(frozen=True, slots=True)
class CompileReceipt:
    """IR 노드를 물리로 낮춘 단계 하나의 영수증."""

    receipt_id: str
    stage: str
    ir_node_ids: tuple[str, ...] = ()
    requirement_ids: tuple[str, ...] = ()
    artifact_ids: tuple[str, ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "stage": self.stage,
            "ir_node_ids": list(self.ir_node_ids),
            "requirement_ids": list(self.requirement_ids),
            "artifact_ids": list(self.artifact_ids),
            "detail": dict(self.detail),
        }


@dataclass(frozen=True, slots=True)
class SqlArtifact:
    """출고 SQL 의 한 조각과 그 출처."""

    artifact_id: str
    kind: str
    sql_fragment: str
    source_ir_node_ids: tuple[str, ...] = ()
    source_requirement_ids: tuple[str, ...] = ()
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in ARTIFACT_KINDS:
            raise ProvenanceError(f"unknown sql artifact kind: {self.kind!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "sql_fragment": self.sql_fragment,
            "source_ir_node_ids": list(self.source_ir_node_ids),
            "source_requirement_ids": list(self.source_requirement_ids),
            "symbols": list(self.symbols),
        }


# ── IR → 노드 ────────────────────────────────────────────────────────────────────


def ir_nodes_from_event_expression(expression: Any) -> tuple[IrNode, ...]:
    """canonical Event IR 표현의 원자 노드들. 극성(Not)을 잃지 않는다.

    ``event_ir`` 은 지연 import 한다 — 이 모듈은 진단 계층에서도 쓰이므로 IR 스키마 로딩을
    import 부작용으로 만들지 않는다.
    """
    import event_ir

    nodes: list[IrNode] = []
    for index, (atom, negated) in enumerate(event_ir.iter_signed_atoms(expression)):
        symbols = sorted(
            {*event_ir.sources(atom), *event_ir.field_names(atom)}
        )
        evidence = getattr(atom, "evidence", None)
        span = (
            (evidence.start, evidence.end)
            if evidence is not None and hasattr(evidence, "start")
            else None
        )
        node_type = str(getattr(atom, "type", atom.__class__.__name__))
        nodes.append(
            IrNode(
                node_id=f"ir_{index}_{_digest(node_type, tuple(symbols), negated, span)}",
                node_type=node_type,
                symbols=tuple(symbols),
                negated=negated,
                evidence=span,
            )
        )
    return tuple(nodes)


# ── SQL → artifact ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ArtifactExtraction:
    """추출 결과. ``parsed=False`` 면 artifact 목록의 공백은 '없음'이 아니라 '못 읽음'이다."""

    artifacts: tuple[SqlArtifact, ...]
    parsed: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "parsed": self.parsed,
            "error": self.error,
            "artifacts": [item.to_dict() for item in self.artifacts],
        }

    def by_kind(self, kind: str) -> tuple[SqlArtifact, ...]:
        return tuple(item for item in self.artifacts if item.kind == kind)


def _symbols_of(node: exp.Expression) -> tuple[str, ...]:
    names: set[str] = set()
    for column in node.find_all(exp.Column):
        if column.name:
            names.add(str(column.name).upper())
    for table in node.find_all(exp.Table):
        if table.name:
            names.add(str(table.name).upper())
    return tuple(sorted(names))


def _conjuncts(node: exp.Expression) -> list[exp.Expression]:
    """AND 로 이어진 술어를 원자 단위로 편다(OR 는 통째로 하나의 술어다)."""
    if isinstance(node, exp.And):
        return [*_conjuncts(node.this), *_conjuncts(node.expression)]
    if isinstance(node, exp.Paren):
        return _conjuncts(node.this)
    return [node]


def extract_sql_artifacts(sql: str, *, dialect: str = "tsql") -> ArtifactExtraction:
    """출고 SQL 을 종류별 artifact 로 쪼갠다(파싱 실패는 실패로 보고한다)."""
    if not isinstance(sql, str) or not sql.strip():
        return ArtifactExtraction((), False, "empty_sql")
    try:
        statements = [node for node in sqlglot.parse(sql, read=dialect) if node is not None]
    except Exception as exc:  # noqa: BLE001 — sqlglot 은 파싱 실패 예외 종류를 좁혀 주지 않는다
        return ArtifactExtraction((), False, f"{exc.__class__.__name__}: {exc}"[:200])
    if len(statements) != 1:
        return ArtifactExtraction((), False, f"expected one statement, got {len(statements)}")

    root = statements[0]
    artifacts: list[SqlArtifact] = []

    def add(kind: str, node: exp.Expression) -> None:
        fragment = node.sql(dialect=dialect)
        if not fragment.strip():
            return
        artifacts.append(
            SqlArtifact(
                artifact_id=f"sa_{_digest(kind, fragment, len(artifacts))}",
                kind=kind,
                sql_fragment=fragment,
                symbols=_symbols_of(node),
            )
        )

    for cte in root.find_all(exp.CTE):
        add(ARTIFACT_CTE, cte)
    for select in root.find_all(exp.Select):
        for projection in select.expressions:
            aggregates = list(projection.find_all(exp.AggFunc))
            add(ARTIFACT_AGGREGATION if aggregates else ARTIFACT_PROJECTION, projection)
        where = select.args.get("where")
        if isinstance(where, exp.Where) and where.this is not None:
            for predicate in _conjuncts(where.this):
                add(ARTIFACT_PREDICATE, predicate)
        for join in select.args.get("joins") or ():
            add(ARTIFACT_JOIN, join)
        group = select.args.get("group")
        if isinstance(group, exp.Group):
            for item in group.expressions:
                add(ARTIFACT_GROUPING, item)
        having = select.args.get("having")
        if isinstance(having, exp.Having) and having.this is not None:
            for predicate in _conjuncts(having.this):
                add(ARTIFACT_PREDICATE, predicate)
    for ordered in root.find_all(exp.Ordered):
        add(ARTIFACT_ORDERING, ordered)
    for limit in root.find_all(exp.Limit):
        add(ARTIFACT_LIMIT, limit)
    for top in root.find_all(exp.Select):
        limit_arg = top.args.get("limit")
        if isinstance(limit_arg, exp.Limit):
            continue
    for window in root.find_all(exp.Window):
        add(ARTIFACT_WINDOW, window)
    return ArtifactExtraction(tuple(artifacts), True)


# ── 그래프 ───────────────────────────────────────────────────────────────────────


@dataclass
class ProvenanceGraph:
    """요구 · IR 노드 · 영수증 · artifact 와 그 간선."""

    ir_nodes: dict[str, IrNode] = field(default_factory=dict)
    receipts: dict[str, CompileReceipt] = field(default_factory=dict)
    artifacts: dict[str, SqlArtifact] = field(default_factory=dict)
    extraction_error: str | None = None

    def add_ir_nodes(self, nodes: Iterable[IrNode]) -> None:
        for node in nodes:
            self.ir_nodes.setdefault(node.node_id, node)

    def add_artifacts(self, extraction: ArtifactExtraction) -> None:
        if not extraction.parsed:
            self.extraction_error = extraction.error
        for artifact in extraction.artifacts:
            self.artifacts.setdefault(artifact.artifact_id, artifact)

    def add_receipt(self, receipt: CompileReceipt) -> CompileReceipt:
        return self.receipts.setdefault(receipt.receipt_id, receipt)

    def link_by_symbols(self, *, stage: str) -> list[CompileReceipt]:
        """IR 노드 ↔ artifact 를 **심볼 일치**로 잇고 그 연결마다 영수증을 만든다.

        심볼(소스·필드 이름)은 IR 이 지목한 것이고 artifact 는 실제 SQL 이 참조한 것이다.
        둘이 겹치면 그 artifact 는 그 노드의 물리 구현이다. 물리 컬럼 **이름 문자열**을
        원장이 미리 알고 있을 필요가 없다 — 이름은 카탈로그가 정하고 여기서는 대조만 한다.
        """
        created: list[CompileReceipt] = []
        for node in self.ir_nodes.values():
            wanted = {symbol.upper() for symbol in node.symbols}
            if not wanted:
                continue
            matched = tuple(
                artifact.artifact_id
                for artifact in self.artifacts.values()
                if wanted & set(artifact.symbols)
            )
            if not matched:
                continue
            receipt = CompileReceipt(
                receipt_id=f"cr_{_digest(stage, node.node_id)}",
                stage=stage,
                ir_node_ids=(node.node_id,),
                artifact_ids=matched,
            )
            created.append(self.add_receipt(receipt))
        return created

    def receipts_for_node(self, node_id: str) -> tuple[CompileReceipt, ...]:
        return tuple(
            receipt for receipt in self.receipts.values() if node_id in receipt.ir_node_ids
        )

    def nodes_covering_span(self, start: int, end: int) -> tuple[IrNode, ...]:
        """원문 구간과 근거가 겹치는 IR 노드들(요구 ↔ 노드 결속의 단일 규칙)."""
        found: list[IrNode] = []
        for node in self.ir_nodes.values():
            span = node.evidence
            if span is None:
                continue
            if not (end <= span[0] or start >= span[1]):
                found.append(node)
        return tuple(found)

    def orphan_artifacts(self) -> tuple[SqlArtifact, ...]:
        """어느 IR 노드로도 역추적되지 않는 **의미** artifact.

        투영·정렬·제한은 형태(shape)의 소유이고 IR 원자와 무관할 수 있으므로 제외한다.
        의미를 바꾸는 것은 술어·조인이다.
        """
        linked = {
            artifact_id
            for receipt in self.receipts.values()
            for artifact_id in receipt.artifact_ids
        }
        return tuple(
            artifact
            for artifact in self.artifacts.values()
            if artifact.kind in {ARTIFACT_PREDICATE, ARTIFACT_JOIN}
            and artifact.artifact_id not in linked
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "ir_nodes": [node.to_dict() for node in self.ir_nodes.values()],
            "compile_receipts": [item.to_dict() for item in self.receipts.values()],
            "sql_artifacts": [item.to_dict() for item in self.artifacts.values()],
            "extraction_error": self.extraction_error,
            "counts": {
                "ir_nodes": len(self.ir_nodes),
                "compile_receipts": len(self.receipts),
                "sql_artifacts": len(self.artifacts),
            },
        }


def build_graph(
    *,
    event_expression: Any,
    sql: str,
    dialect: str = "tsql",
    extra_artifacts: Sequence[Mapping[str, Any]] = (),
) -> ProvenanceGraph:
    """표현 + 출고 SQL → provenance graph(연결까지 완료된 상태)."""
    graph = ProvenanceGraph()
    if event_expression is not None:
        graph.add_ir_nodes(ir_nodes_from_event_expression(event_expression))
    extraction = extract_sql_artifacts(sql, dialect=dialect)
    graph.add_artifacts(extraction)
    for raw in extra_artifacts:
        kind = str(raw.get("kind") or "")
        fragment = str(raw.get("sql_fragment") or "")
        if kind in ARTIFACT_KINDS and fragment:
            artifact = SqlArtifact(
                artifact_id=f"sa_{_digest(kind, fragment, 'extra')}",
                kind=kind,
                sql_fragment=fragment,
            )
            graph.artifacts.setdefault(artifact.artifact_id, artifact)
    graph.link_by_symbols(stage="audience_lowering")
    return graph


__all__ = [
    "ARTIFACT_AGGREGATION",
    "ARTIFACT_CTE",
    "ARTIFACT_GROUPING",
    "ARTIFACT_JOIN",
    "ARTIFACT_KINDS",
    "ARTIFACT_LIMIT",
    "ARTIFACT_ORDERING",
    "ARTIFACT_PREDICATE",
    "ARTIFACT_PROJECTION",
    "ARTIFACT_WINDOW",
    "PROVENANCE_KEY",
    "ArtifactExtraction",
    "CompileReceipt",
    "IrNode",
    "ProvenanceError",
    "ProvenanceGraph",
    "SqlArtifact",
    "build_graph",
    "extract_sql_artifacts",
    "ir_nodes_from_event_expression",
]
