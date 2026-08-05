"""심볼 결속 양방향 패리티 — 스키마가 제시한 어휘를 실행 계층이 거부하면 red (P1).

실측된 결함(2026-08-03): `relation_predicate.value_comparison` 은 strict function-calling
스키마가 모델에게 `eq|gte|lte` 를 **허용값으로 제시**하는데, 그 값을 받은
`ResolvedSemanticCatalog.resolve_operator` 는 `= != > >= < <=` 만 등록해 두어
세 값 전부를 `catalog_operator_unregistered` 로 거부했다. **모델이 스키마를 지켰는데
lowering 이 자기 스키마의 어휘를 거부한다** — 이 모순 하나가 라이브 #13·#19·#24 를 함께 막았다.

이 파일이 지키는 계약은 하나다:

    노드 스키마가 선언한 모든 어휘값은 실행 계층에서 해석돼야 한다.

방향이 둘인 이유: 어휘를 넓히면(스키마→실행) 실행이 못 따라오고, 실행 어휘를 바꾸면
(실행→스키마) 스키마가 낡는다. 어느 쪽이 앞서가도 조용히 실패하므로 양쪽을 함께 본다.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import audience_schema  # noqa: E402
import event_ir  # noqa: E402
import resolved_semantic_catalog  # noqa: E402
import semantic_normalizers  # noqa: E402

# **값 비교** 의미를 가진 스키마 정의만 카탈로그 연산자 레지스트리로 검사한다. 정의 이름이
# 곧 의미다 — 같은 `operator` 필드라도 `arithmetic`(사칙연산)·`temporal_relation`(시간 관계)은
# 비교 연산자 레지스트리가 아니라 각자의 실행 계층이 해석한다.
_COMPARISON_DEFINITIONS = frozenset({"comparison"})
_OPERATOR_FIELDS = frozenset({"operator"})


def _catalog() -> resolved_semantic_catalog.ResolvedSemanticCatalog:
    return audience_runtime.resolve_audience_catalog()


def _declared_operator_values() -> list[tuple[str, str, str]]:
    """(정의 이름, 필드, 값) — 오디언스 스키마가 모델에게 제시하는 연산자 어휘 전부.

    파생원은 2026-08-05 SemanticPlanV2 노드 스키마에서 canonical Event IR 의 오디언스
    표현 스키마(`audience_schema`)로 옮겼다. SemanticPlan 이 폐기되면서 모델에게 연산자
    어휘를 제시하는 표면이 이 스키마 하나만 남았기 때문이다 — 계약(제시한 어휘는 실행
    계층이 해석한다)은 그대로다.
    """
    declared: list[tuple[str, str, str]] = []
    definitions = audience_schema.audience_expression_json_schema().get("$defs", {})
    for name, definition in sorted(definitions.items()):
        if name not in _COMPARISON_DEFINITIONS:
            continue
        properties = definition.get("properties")
        if not isinstance(properties, dict):
            continue
        for field, spec in sorted(properties.items()):
            if field not in _OPERATOR_FIELDS or not isinstance(spec, dict):
                continue
            for value in spec.get("enum") or ():
                declared.append((name, field, str(value)))
    return declared


def test_the_declaration_is_not_empty() -> None:
    """공허한 통과 방지 — 검사 대상이 없으면 green 이 아무것도 뜻하지 않는다."""
    assert _declared_operator_values(), (
        "연산자 어휘를 선언한 스키마 필드가 하나도 없다. "
        f"_COMPARISON_DEFINITIONS={sorted(_COMPARISON_DEFINITIONS)} / "
        f"_OPERATOR_FIELDS={sorted(_OPERATOR_FIELDS)} 가 낡았는지 확인하라."
    )


def test_every_declared_operator_value_resolves_in_the_catalog() -> None:
    """스키마 → 실행. 오늘의 `eq` 모순을 red 로 만드는 방향이다."""
    catalog = _catalog()
    unresolved: list[str] = []
    for node_type, field, value in _declared_operator_values():
        try:
            catalog.resolve_operator(value)
        except resolved_semantic_catalog.CatalogError as exc:
            unresolved.append(f"{node_type}.{field}={value!r} → {exc}")
    assert not unresolved, (
        "노드 스키마가 모델에게 제시한 연산자를 실행 계층이 거부한다. "
        "모델은 스키마를 지켰는데 lowering 이 실패하므로 그 조건은 영원히 컴파일되지 않는다:\n"
        + "\n".join(unresolved)
    )


def test_alias_table_only_maps_into_the_closed_symbol_set() -> None:
    """실행 → 스키마. 별칭이 IR 이 모르는 기호로 새면 lowering 이 조용히 깨진다."""
    strays = {
        alias: symbol
        for alias, symbol in event_ir.COMPARISON_OPERATOR_ALIASES.items()
        if symbol not in event_ir.COMPARISON_OPERATORS
    }
    assert not strays, f"별칭이 Event IR 기호 집합 밖을 가리킨다: {strays}"
    # 별칭이 기호를 덮어쓰면 '=' 같은 정본 표기가 다른 뜻이 된다.
    collisions = event_ir.COMPARISON_OPERATORS & set(event_ir.COMPARISON_OPERATOR_ALIASES)
    assert not collisions, f"별칭 키가 정본 기호와 충돌한다: {sorted(collisions)}"


@pytest.mark.parametrize("value", sorted({"eq", "gte", "lte", "gt", "lt", "=", ">=", "<="}))
def test_word_and_symbol_forms_resolve_to_the_same_operator(value: str) -> None:
    """두 표기가 같은 연산자로 귀결되는지 — 표가 하나라는 사실의 관측 가능한 형태."""
    catalog = _catalog()
    canonical = event_ir.canonical_comparison_operator(value)
    assert canonical is not None, f"{value!r} 가 정본 기호로 환원되지 않는다."
    assert catalog.resolve_operator(value).symbol == catalog.resolve_operator(canonical).symbol


def test_declared_metric_aliases_are_actually_wired() -> None:
    """선언은 맞았는데 배선이 없던 결함을 고정한다.

    `member_grade.aliases` 에는 라이브 모델이 실제로 내는 `grade` 가 **처음부터** 들어 있었다.
    그런데 로더가 그 필드를 읽지 않아 `attribute='grade'` 는 `catalog_metric_unregistered` 로
    죽었다 — 손 목록을 새로 만드는 대신 죽은 선언을 살리는 것이 이 항목의 수리였다.
    """
    catalog = _catalog()
    assert catalog.metric("grade").id == "member_grade"
    assert catalog.metric("등급").id == "member_grade"
    # 정본 id 는 별칭보다 언제나 우선한다.
    assert catalog.metric("member_grade").id == "member_grade"


def test_no_alias_shadows_an_existing_symbol() -> None:
    """별칭은 이미 있는 심볼의 **뜻을 바꿀 수 없다**.

    소스 id 는 존재(EXISTS) 지표로 자동 등록된다. 같은 이름을 집계 지표의 별칭으로 주장하면
    그 심볼이 '있었나'에서 '몇 번'으로 조용히 바뀐다 — 런타임은 정본 id 를 이기게 두므로
    동작은 안전하지만, 선언 자체가 오해를 부르므로 여기서 이름을 대고 막는다.
    """
    shadowed = resolved_semantic_catalog.shadowed_metric_aliases(_catalog().metrics)
    assert not shadowed, (
        f"이미 존재하는 심볼을 가리는 별칭 선언: {shadowed}. "
        "그 이름은 이미 임자가 있다 — 별칭에서 지우거나 다른 표기를 써라."
    )


def test_no_second_word_to_symbol_table_survives() -> None:
    """같은 사상이 두 벌 있으면 곧 어긋난다 — 네 벌이 있었고 실제로 어긋났다.

    소비자는 표를 **소유하지 않고 파생**하거나, 소유하더라도 그 표의 부분집합이어야 한다.
    """
    source = event_ir.COMPARISON_OPERATOR_ALIASES

    # `compositional_targeting._SQL_COMPARISONS`(파생 사본)과 targeting_ir 의
    # `_RELATIONAL_VALUE_COMPARISONS` / `_RELATIONAL_COUNT_OPERATORS`(부분집합 선택)는
    # 2026-08-05 삭제됐다 — 축1(등급/상태 이력·전이) 폐기로 그 표들의 소비자가 사라졌다.
    # 남은 두 번째 표는 정규화기의 별칭 확장 하나뿐이고, 계약(부분집합)은 그대로다.
    for name, chosen in (
        ("OperatorNormalizer._EXTRA_ALIASES", set(semantic_normalizers.OperatorNormalizer._EXTRA_ALIASES)),
    ):
        unknown = set(chosen) - set(source) - event_ir.COMPARISON_OPERATORS
        assert not unknown, (
            f"{name} 가 별칭 표에 없는 표기를 선언한다: {sorted(unknown)}. "
            "새 표기는 event_ir.COMPARISON_OPERATOR_ALIASES 에 한 줄로 추가하라."
        )
        conflicting = {
            token: (source[token], semantic_normalizers.OperatorNormalizer._EXTRA_ALIASES[token])
            for token in set(chosen) & set(source)
            if name.endswith("_EXTRA_ALIASES")
            and semantic_normalizers.OperatorNormalizer._EXTRA_ALIASES[token] != source[token]
        }
        assert not conflicting, f"{name} 가 같은 표기를 다른 기호로 사상한다: {conflicting}"
