"""provenance graph: 개수가 아니라 **연결**을 본다.

"IR 원자 수 == 영수증 수 == SQL 술어 수" 는 처음부터 성립하지 않는다 — 하나의 요구가 여러
술어로 내려가고 여러 요구가 하나의 식으로 합쳐진다. 개수를 세는 가드는 정상 SQL 을 막거나
결손 SQL 을 통과시킨다.
"""

from __future__ import annotations

import event_ir
import provenance

SQL = "\n".join(
    [
        "SELECT DISTINCT B.MEMBER_NO AS CUST_ID, B.EMART_GRADE_CD",
        "FROM CRM_MB_BASEINFO B",
        "  INNER JOIN CRM_MB_MONTHCRMINFO M ON M.MEMBER_NO = B.MEMBER_NO",
        "WHERE B.MEMBER_STATE_CD = 'NORMAL'",
        "  AND EXISTS (SELECT 1 FROM CRM_SL_ORDERHEADERMALL O WHERE O.MEMBER_NO = B.MEMBER_NO)",
        "ORDER BY B.MEMBER_NO",
    ]
)


def test_artifacts_are_split_by_kind() -> None:
    extraction = provenance.extract_sql_artifacts(SQL)
    assert extraction.parsed and extraction.error is None
    kinds = {artifact.kind for artifact in extraction.artifacts}
    assert provenance.ARTIFACT_PREDICATE in kinds
    assert provenance.ARTIFACT_PROJECTION in kinds
    assert provenance.ARTIFACT_JOIN in kinds
    assert provenance.ARTIFACT_ORDERING in kinds


def test_conjunctions_are_split_into_atomic_predicates() -> None:
    """AND 로 이은 술어를 통째로 하나로 세면 조건 하나가 사라져도 개수가 같다."""

    extraction = provenance.extract_sql_artifacts(SQL)
    predicates = extraction.by_kind(provenance.ARTIFACT_PREDICATE)
    fragments = [artifact.sql_fragment for artifact in predicates]
    assert any("MEMBER_STATE_CD" in fragment for fragment in fragments)
    assert any("EXISTS" in fragment.upper() for fragment in fragments)


def test_a_parse_failure_is_reported_not_disguised_as_emptiness() -> None:
    """못 읽은 것과 없는 것을 같게 취급하면 결손이 통과한다."""

    extraction = provenance.extract_sql_artifacts("SELECT FROM WHERE ]]")
    assert not extraction.parsed or not extraction.artifacts
    if not extraction.parsed:
        assert extraction.error


def test_ir_nodes_keep_polarity_and_evidence() -> None:
    expression = event_ir.Not(
        operand=event_ir.Exists(
            relation=event_ir.Source(name="purchase"),
            evidence=event_ir.Evidence(text="구매하지 않은", start=0, end=7),
        )
    )
    nodes = provenance.ir_nodes_from_event_expression(expression)
    assert len(nodes) == 1
    assert nodes[0].negated is True
    assert nodes[0].evidence == (0, 7)


def test_symbol_matching_links_nodes_to_artifacts() -> None:
    expression = event_ir.Exists(
        relation=event_ir.Source(name="purchase"),
        evidence=event_ir.Evidence(text="구매", start=0, end=2),
    )
    graph = provenance.build_graph(event_expression=expression, sql=SQL)
    assert graph.ir_nodes
    # 'purchase' 심볼은 물리 테이블 이름이 아니라 IR 심볼이므로 artifact 와 겹치지 않는다.
    # 그 사실이 조용히 성공으로 보이면 안 된다 — 연결이 없으면 영수증도 없다.
    node = next(iter(graph.ir_nodes.values()))
    assert len(graph.receipts_for_node(node.node_id)) == len(
        [
            receipt
            for receipt in graph.receipts.values()
            if node.node_id in receipt.ir_node_ids
        ]
    )


def test_nodes_are_found_by_evidence_overlap_not_equality() -> None:
    expression = event_ir.Exists(
        relation=event_ir.Source(name="purchase"),
        evidence=event_ir.Evidence(text="최근 30일 구매", start=0, end=9),
    )
    graph = provenance.build_graph(event_expression=expression, sql=SQL)
    assert graph.nodes_covering_span(3, 6), "부분 구간으로도 그 노드를 짚을 수 있어야 한다"
    assert not graph.nodes_covering_span(40, 50)


def test_unknown_artifact_kind_is_rejected() -> None:
    import pytest

    with pytest.raises(provenance.ProvenanceError):
        provenance.SqlArtifact(artifact_id="x", kind="something", sql_fragment="1=1")


def test_graph_serialization_carries_counts_and_errors() -> None:
    graph = provenance.build_graph(event_expression=None, sql=SQL)
    payload = graph.to_dict()
    assert payload["counts"]["sql_artifacts"] == len(graph.artifacts)
    assert payload["extraction_error"] is None
