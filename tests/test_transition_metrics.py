"""전이 지표 계약과 lowering 의 고정 계약.

이 파일이 재는 것은 **선언에서 SQL 이 나오는가**이지 실데이터가 무엇을 돌려주는가가 아니다.
데이터베이스에 연결하지 않고, 실제 회원 행·전이 이력·최신 월 적재를 만들지 않는다. 값의
유효성은 카탈로그 값 사전 하나로 판정한다 — 사전 밖 문자열이 SQL 로 새지 않게 하는 것이 그
검증의 목적이고, "실DB 에 그 값이 있는가"는 이 계층의 질문이 아니다.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import resolved_semantic_catalog  # noqa: E402
import transition_metrics  # noqa: E402
from resolved_semantic_catalog import CatalogError  # noqa: E402

GRADE_TRANSITION = "member_grade_transition"
WORTH_TRANSITION = "member_worth_grade_transition"

# 합성 카탈로그의 값 사전. 물리 코드가 canonical 과 다른 것은 의도다 — 변환이 실제로
# 일어나는지 재려면 두 값이 달라야 한다.
_RANKED_DOMAIN = {
    "label": "서열 있는 축",
    "ordered": True,
    "order": ["low", "mid", "high"],
    "values": {
        "low": {"physical": "L", "aliases": ["로우"]},
        "mid": {"physical": "M", "aliases": ["미드"]},
        "high": {"physical": "H", "aliases": ["하이"]},
    },
}
_FLAT_DOMAIN = {
    "label": "서열 없는 축",
    "values": {
        "alpha": {"physical": "A", "aliases": ["알파"]},
        "beta": {"physical": "B", "aliases": ["베타"]},
    },
}


@pytest.fixture(scope="module")
def catalog():
    return audience_runtime.resolve_audience_catalog()


def _declaration(**overrides):
    declaration = {
        "source": "snapshot",
        "kind": "transition",
        "expression_field": "snapshot.current",
        "prev_expression_field": "snapshot.previous",
        "strategy": "current_previous_columns",
        "data_type": "string",
        "grain": "subject",
        "allowed_operators": ["=", "!="],
        "required_capabilities": ["metric.transition"],
    }
    declaration.update(overrides)
    return {key: value for key, value in declaration.items() if value is not None}


def _synthetic_catalog(metrics=None, **declaration_overrides):
    return resolved_semantic_catalog.resolve_semantic_catalog(
        registry={},
        runtime_config={
            "value_domains": {"ranked": _RANKED_DOMAIN, "flat": _FLAT_DOMAIN},
            "sources": {
                "snapshot": {
                    "table": "SNAPSHOT",
                    "alias": "S",
                    "subject_key": "MEMBER_NO",
                    "event_subject_key": "MEMBER_NO",
                    "time_column": "YYYYMM",
                    "time_format": "char6",
                    "binding": "fact_table",
                }
            },
            "fields": {
                "snapshot.current": {
                    "source": "snapshot",
                    "column": "CURRENT_CD",
                    "data_type": "string",
                    "value_domain": "ranked",
                },
                "snapshot.previous": {
                    "source": "snapshot",
                    "column": "PREVIOUS_CD",
                    "data_type": "string",
                    "value_domain": "ranked",
                },
                "snapshot.unranked": {
                    "source": "snapshot",
                    "column": "FLAT_CD",
                    "data_type": "string",
                    "value_domain": "flat",
                },
                "snapshot.prev_unranked": {
                    "source": "snapshot",
                    "column": "PREV_FLAT_CD",
                    "data_type": "string",
                    "value_domain": "flat",
                },
            },
            "metrics": metrics or {"probe": _declaration(**declaration_overrides)},
        },
    )


# ── 1. 선언 수집 ────────────────────────────────────────────────────────────────

def test_transition_metric_ids_collects_every_declared_axis(catalog) -> None:
    assert transition_metrics.transition_metric_ids(catalog) == (
        GRADE_TRANSITION,
        WORTH_TRANSITION,
    )


def test_declared_axes_expose_their_value_domain_from_the_declaration(catalog) -> None:
    """축 이름이 아니라 값 도메인이 지표를 고른다."""

    assert dict(transition_metrics.transition_metric_declarations(catalog)) == {
        GRADE_TRANSITION: "grade",
        WORTH_TRANSITION: "worth_grade",
    }


# ── 2~5. 선언 검증 ──────────────────────────────────────────────────────────────

def test_transition_without_a_declared_strategy_fails_to_load() -> None:
    """전략 기본값을 두지 않는다 — 없는 선언을 추측하면 이력 축이 한 행 비교로 낮아진다."""

    with pytest.raises(CatalogError) as error:
        _synthetic_catalog(strategy=None)
    assert error.value.code == "transition_strategy_missing"
    assert "current_previous_columns" in str(error.value)


def test_unknown_strategy_name_is_a_catalog_syntax_error() -> None:
    with pytest.raises(CatalogError) as error:
        _synthetic_catalog(strategy="grade_pair")
    assert error.value.code == "invalid_catalog_declaration"
    assert "unknown strategy" in str(error.value)


@pytest.mark.parametrize("strategy", sorted(
    transition_metrics.DECLARED_STRATEGIES - transition_metrics.SUPPORTED_STRATEGIES
))
def test_reserved_but_unimplemented_strategy_fails_closed(strategy: str) -> None:
    """예약 어휘는 문법 오류가 아니라 **미지원**이다 — 두 결말을 구분한다."""

    resolved = _synthetic_catalog(strategy=strategy)
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.validate_transition_contract(resolved, "probe")
    assert error.value.code == "transition_strategy_unsupported"
    assert strategy in str(error.value)


def test_a_single_value_metric_cannot_be_used_as_a_transition(catalog) -> None:
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.validate_transition_contract(catalog, "member_grade")
    assert error.value.code == "catalog_contract_mismatch"


def test_two_sides_reading_different_value_domains_fail_closed() -> None:
    resolved = _synthetic_catalog(prev_expression_field="snapshot.prev_unranked")
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.validate_transition_contract(resolved, "probe")
    assert error.value.code == "transition_domain_mismatch"


def test_a_transition_needs_an_equality_operator() -> None:
    resolved = _synthetic_catalog(allowed_operators=["!="])
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.validate_transition_contract(resolved, "probe")
    assert error.value.code == "catalog_contract_mismatch"


def test_strategy_on_a_non_transition_metric_fails_to_load() -> None:
    with pytest.raises(CatalogError) as error:
        _synthetic_catalog(metrics={
            "probe": {
                "source": "snapshot",
                "kind": "field",
                "expression_field": "snapshot.current",
                "strategy": "current_previous_columns",
            }
        })
    assert "only 'transition' metrics carry a strategy" in str(error.value)


# ── 6~7. 값 검증 ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("from_value", "to_value"),
    [("gold_grade", "platinum_grade"), ("platinum_grade", "vip"), ("gold_grade", "~")],
)
def test_values_outside_the_declared_dictionary_fail_closed(
    catalog, from_value: str, to_value: str
) -> None:
    """판정 기준은 실DB 가 아니라 카탈로그 값 사전이다."""

    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.lower_transition_metric(
            catalog, GRADE_TRANSITION, from_value=from_value, to_value=to_value
        )
    assert error.value.code == "transition_value_unknown"


def test_the_same_value_on_both_sides_is_not_a_change(catalog) -> None:
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.lower_transition_metric(
            catalog, GRADE_TRANSITION, from_value="vip", to_value="vip"
        )
    assert error.value.code == "transition_values_identical"


# ── 8~10. 방향 검증 ─────────────────────────────────────────────────────────────

def test_a_direction_cannot_be_checked_without_a_declared_ranking() -> None:
    resolved = _synthetic_catalog(
        expression_field="snapshot.unranked",
        prev_expression_field="snapshot.prev_unranked",
    )
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.lower_transition_metric(
            resolved,
            "probe",
            from_value="alpha",
            to_value="beta",
            direction=transition_metrics.ASCENDING,
        )
    assert error.value.code == "transition_direction_unverifiable"


def test_an_unranked_domain_still_lowers_without_a_direction_cue() -> None:
    """방향어가 없으면 서열이 없어도 두 값 비교는 표현할 수 있다."""

    resolved = _synthetic_catalog(
        expression_field="snapshot.unranked",
        prev_expression_field="snapshot.prev_unranked",
    )
    lowered = transition_metrics.lower_transition_metric(
        resolved, "probe", from_value="alpha", to_value="beta"
    )
    assert lowered.receipt["value_order"] == []


@pytest.mark.parametrize(
    ("direction", "from_value", "to_value"),
    [
        (transition_metrics.ASCENDING, "vip", "gold_grade"),
        (transition_metrics.DESCENDING, "gold_grade", "vip"),
    ],
)
def test_a_direction_that_contradicts_the_declared_ranking_fails_closed(
    catalog, direction: str, from_value: str, to_value: str
) -> None:
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.lower_transition_metric(
            catalog,
            GRADE_TRANSITION,
            from_value=from_value,
            to_value=to_value,
            direction=direction,
        )
    assert error.value.code == "transition_direction_contradicted"


@pytest.mark.parametrize(
    ("direction", "from_value", "to_value"),
    [
        (transition_metrics.ASCENDING, "gold_grade", "vip"),
        (transition_metrics.DESCENDING, "vip", "gold_grade"),
    ],
)
def test_a_direction_that_agrees_with_the_declared_ranking_lowers(
    catalog, direction: str, from_value: str, to_value: str
) -> None:
    lowered = transition_metrics.lower_transition_metric(
        catalog,
        GRADE_TRANSITION,
        from_value=from_value,
        to_value=to_value,
        direction=direction,
    )
    assert lowered.receipt["direction"] == direction


def test_an_unknown_direction_name_fails_closed(catalog) -> None:
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.lower_transition_metric(
            catalog, GRADE_TRANSITION, from_value="gold_grade", to_value="vip",
            direction="up",
        )
    assert error.value.code == "catalog_contract_mismatch"


# ── 11~14. 낮춘 결과 ────────────────────────────────────────────────────────────

def _comparisons(expression: event_ir.Condition) -> dict[str, object]:
    payload = expression.to_dict()
    assert payload["type"] == "exists"
    where = payload["relation"]["where"]
    assert where["type"] == "and"
    return {
        item["left"]["name"]: item["right"]["value"] for item in where["operands"]
    }


def test_the_lowered_shape_reads_current_and_previous_in_one_row(catalog) -> None:
    """두 비교가 한 Filter 안에 있어야 '같은 행의 변화'가 된다."""

    lowered = transition_metrics.lower_transition_metric(
        catalog, GRADE_TRANSITION, from_value="gold_grade", to_value="vip"
    )
    payload = lowered.expression.to_dict()
    assert payload["relation"]["type"] == "filter"
    assert payload["relation"]["relation"] == {
        "type": "source",
        "name": "member_month_snapshot",
    }
    assert _comparisons(lowered.expression) == {
        "member_month_snapshot.grade": "vip",
        "member_month_snapshot.prev_grade": "gold_grade",
    }


def test_the_source_comes_from_the_metric_declaration() -> None:
    resolved = _synthetic_catalog()
    lowered = transition_metrics.lower_transition_metric(
        resolved, "probe", from_value="low", to_value="high"
    )
    assert lowered.expression.to_dict()["relation"]["relation"]["name"] == "snapshot"
    assert lowered.receipt["source"] == "snapshot"


def test_the_ir_literal_stays_canonical_and_the_compiler_owns_the_physical_code(
    catalog,
) -> None:
    """canonical → 물리 변환의 소유자는 컴파일러다. 여기서 물리 코드를 만들면 표가 둘이 된다."""

    lowered = transition_metrics.lower_transition_metric(
        catalog, GRADE_TRANSITION, from_value="gold_grade", to_value="vip"
    )
    assert set(_comparisons(lowered.expression).values()) == {"vip", "gold_grade"}
    sql = event_compiler.compile_expression(
        lowered.expression, context=catalog.compile_context(literals=True)
    ).sql
    assert "MS.ZTS_GRADE = 'MEM_GRADE_CD.VIP'" in sql
    assert "MS.PREV_ZTS_GRADE = 'MEM_GRADE_CD.GOLD'" in sql


def test_the_receipt_explains_the_declaration_without_running_anything(catalog) -> None:
    lowered = transition_metrics.lower_transition_metric(
        catalog, GRADE_TRANSITION, from_value="gold_grade", to_value="vip",
        direction=transition_metrics.ASCENDING,
    )
    assert lowered.receipt["metric_id"] == GRADE_TRANSITION
    assert lowered.receipt["strategy"] == "current_previous_columns"
    assert lowered.receipt["value_domain"] == "grade"
    assert lowered.receipt["from_physical_value"] == "MEM_GRADE_CD.GOLD"
    assert lowered.receipt["to_physical_value"] == "MEM_GRADE_CD.VIP"
    assert lowered.receipt["conditions"] == {
        "current": "member_month_snapshot.grade = vip",
        "previous": "member_month_snapshot.prev_grade = gold_grade",
    }


def test_both_declared_axes_lower_through_the_same_function(catalog) -> None:
    """회원등급과 가치등급은 선언만 다르다 — 코드 경로는 하나다."""

    grade = transition_metrics.lower_transition_metric(
        catalog, GRADE_TRANSITION, from_value="gold_grade", to_value="vip"
    )
    worth = transition_metrics.lower_transition_metric(
        catalog, WORTH_TRANSITION, from_value="gold_worth_grade", to_value="vip_worth_grade"
    )
    context = catalog.compile_context(literals=True)
    grade_sql = event_compiler.compile_expression(grade.expression, context=context).sql
    worth_sql = event_compiler.compile_expression(
        worth.expression, context=catalog.compile_context(literals=True)
    ).sql
    # 접두어가 있는 코드 도메인과 없는 코드 도메인이 같은 lowering 에서 나온다.
    assert "MS.WORTH_GRADE = 'VIP'" in worth_sql
    assert "MS.PREV_WORTH_GRADE = 'GOLD'" in worth_sql
    assert "MEM_GRADE_CD." in grade_sql and "MEM_GRADE_CD." not in worth_sql
    assert grade.expression.to_dict()["relation"]["type"] == (
        worth.expression.to_dict()["relation"]["type"]
    )


def test_transition_lowering_invents_no_snapshot_predicate(catalog) -> None:
    """최신 월 고정은 source lowering 의 책임이다 — 전이라는 이유로 술어를 지어내지 않는다."""

    lowered = transition_metrics.lower_transition_metric(
        catalog, GRADE_TRANSITION, from_value="gold_grade", to_value="vip"
    )
    where = lowered.expression.to_dict()["relation"]["where"]
    assert len(where["operands"]) == 2
    sql = event_compiler.compile_expression(
        lowered.expression, context=catalog.compile_context(literals=True)
    ).sql
    assert "MAX(YYYYMM)" not in sql


# ── 도메인 → 지표 선택 ──────────────────────────────────────────────────────────

def test_a_value_domain_selects_its_transition_metric(catalog) -> None:
    assert transition_metrics.transition_metric_for_domain(catalog, "grade").id == (
        GRADE_TRANSITION
    )
    assert transition_metrics.transition_metric_for_domain(catalog, "worth_grade").id == (
        WORTH_TRANSITION
    )


def test_a_domain_without_a_transition_metric_fails_closed(catalog) -> None:
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.transition_metric_for_domain(catalog, "gender")
    assert error.value.code == "transition_metric_not_declared"


def test_two_metrics_claiming_one_domain_are_ambiguous() -> None:
    resolved = _synthetic_catalog(metrics={
        "probe": _declaration(),
        "probe_copy": _declaration(),
    })
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.transition_metric_for_domain(resolved, "ranked")
    assert error.value.code == "transition_metric_ambiguous"


# ── capability ──────────────────────────────────────────────────────────────────

def test_the_transition_capability_is_declared_by_the_ir_vocabulary() -> None:
    assert "metric.transition" in event_ir.CAPABILITIES
    assert transition_metrics.SUPPORTED_CAPABILITIES <= event_ir.CAPABILITIES


def test_a_capability_the_compiler_provides_passes_the_contract() -> None:
    resolved = _synthetic_catalog(
        required_capabilities=["metric.transition", "relation.ranked_limit"]
    )
    assert transition_metrics.validate_transition_contract(resolved, "probe").id == "probe"


def test_a_capability_this_path_cannot_provide_fails_closed() -> None:
    """다른 계약이 제공하는 capability 를 선언하면 lowering 전에 막힌다."""

    resolved = _synthetic_catalog(
        required_capabilities=["metric.transition", "metric.member_scalar"]
    )
    with pytest.raises(transition_metrics.TransitionContractError) as error:
        transition_metrics.validate_transition_contract(resolved, "probe")
    assert error.value.code == "catalog_capability_unsupported"
    assert "metric.member_scalar" in str(error.value)


# ── 15. 데이터베이스에 닿지 않는다 ──────────────────────────────────────────────

def test_lowering_pulls_in_no_database_module() -> None:
    """SQL 생성 경로가 DB 모듈을 import 조차 하지 않는다(연결이 아니라 의존성으로 잰다)."""

    probe = (
        "import sys; import transition_metrics, transition_claims; "
        "print([name for name in sys.modules "
        "if 'pyodbc' in name or 'psycopg' in name or name == 'db_connections'])"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=True,
    )
    assert result.stdout.strip() == "[]", result.stdout


_FORBIDDEN_AXIS_NAMES = (
    "prev_grade",
    "worth_grade",
    "ZTS_GRADE",
    "MEM_GRADE_CD",
    "member_month_snapshot",
    "회원등급",
    "가치등급",
)


@pytest.mark.parametrize("module_name", ["transition_metrics", "transition_claims"])
def test_common_transition_code_never_names_an_axis(module_name: str) -> None:
    """축 이름은 선언에서 온다. 코드가 한 축의 이름을 알면 다음 축은 코드 변경이 된다.

    산문(docstring·주석)은 축을 예로 들 수 있다 — 재는 것은 **실행되는 이름**이다.
    """

    tree = ast.parse((REPO_ROOT / f"{module_name}.py").read_text(encoding="utf-8"))
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    executable_text: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                executable_text.append(node.value)
        elif isinstance(node, ast.Name):
            executable_text.append(node.id)
        elif isinstance(node, ast.Attribute):
            executable_text.append(node.attr)
    joined = "\n".join(executable_text)
    leaked = [name for name in _FORBIDDEN_AXIS_NAMES if name in joined]
    assert not leaked, f"{module_name} names catalog-owned axes: {leaked}"
