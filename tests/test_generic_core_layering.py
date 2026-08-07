"""범용 코어 ↔ 도메인 계층의 경계 계약.

이 파일이 지키는 약속은 하나다: **코어는 회원 타기팅을 모른다.**

경계가 흐려지는 방식은 늘 같다 — "여기 한 줄만"이 쌓여서 코어가 도메인 낱말을 알게 되고,
그 다음 도메인이 바뀔 때 코어를 고쳐야 한다. 여기 있는 가드는 그 한 줄을 즉시 빨갛게 만든다.

계약 5종:
1. 코어 모듈은 도메인 이름(실행 컨테이너·슬롯·속성 값 어휘)을 **코드에도 문서에도** 쓰지 않는다.
2. 코어 모듈은 도메인 모듈을 import 하지 않는다(플러그인 바인딩을 통해서만 만난다).
3. 도메인 어휘를 빼면 코어는 죽지 않고 '제약 없음'으로 강등된다.
4. 도메인 선언(JSON)만 바꿔도 새 값이 노드 스키마·검증에 반영된다.
5. 시간 한정어 표면형·속성 값 어휘는 코어가 아니라 카탈로그 파생이다.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import semantic_domain_binding  # noqa: E402
import targeting_domain  # noqa: E402
import temporal_semantics  # noqa: E402

# 범용 코어 — 도메인 지식이 있으면 안 되는 모듈들.
# 2026-08-05 SemanticPlanV2 스택(semantic_plan / semantic_capability / semantic_coverage /
# semantic_candidates / semantic_pipeline / semantic_reemission / semantic_retype /
# requirement_ledger / compile_contract)이 폐기되면서 목록에서 빠졌다. 남은 셋은 오디언스
# 언어가 canonical Event IR 하나가 된 뒤에도 살아 있는 범용 코어다.
CORE_MODULES: tuple[str, ...] = (
    "semantic_normalizers.py",
    "temporal_semantics.py",
    "semantic_domain_binding.py",
)

# 도메인 계층 — 여기서는 이 이름들을 알아도 된다(오히려 알아야 한다).
DOMAIN_MODULES: tuple[str, ...] = (
    "targeting_domain.py",
)


def _module_text(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


def _split_code_and_docs(name: str) -> tuple[str, str]:
    """모듈 소스를 (실행 식별자·리터럴, 문서 문자열)로 나눈다."""
    tree = ast.parse(_module_text(name))
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    code_literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value not in docstrings
    ]
    identifiers = [node.id for node in ast.walk(tree) if isinstance(node, ast.Name)]
    attributes = [node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)]
    return (
        " ".join([*code_literals, *identifiers, *attributes]),
        " ".join(text for text in docstrings if text),
    )


def _mentions(haystack: str, term: str) -> bool:
    """ASCII 낱말은 단어 경계로, 한글은 부분 문자열로 본다.

    부분 문자열만 쓰면 'order'가 'ordered'에, 'ever'가 'never'에 걸려 오탐이 난다 —
    가드가 오탐을 내면 사람이 가드를 끄게 되므로 정확도가 곧 가드의 수명이다.
    """
    if term.isascii():
        return re.search(rf"\b{re.escape(term)}\b", haystack) is not None
    return term in haystack


def _attribute_catalog_specs() -> list[dict]:
    """속성 카탈로그의 **원본** 선언들(런타임 조인 전이라 value_category 가 살아 있다)."""
    import compositional_targeting

    raw = json.loads(
        Path(compositional_targeting.DEFAULT_ATTRIBUTE_CATALOG_PATH).read_text(encoding="utf-8")
    )
    return [spec for spec in (raw.get("attributes") or {}).values() if isinstance(spec, dict)]


def _domain_terms() -> list[str]:
    """금지 낱말 목록 — **파생**이다(손 목록을 두면 새 슬롯·새 등급이 조용히 빠진다).

    파생원은 2026-08-05 SemanticPlan 컴파일러(`legacy_plan_compiler.member_container` /
    `COMPILER_OWNED_SLOTS`)에서 **살아 있는 선언 두 곳**으로 옮겼다: 실행 플랜 컨테이너 이름은
    도메인 카탈로그(`targeting_domain.plan_container`)가, 실행 슬롯 이름은 실행 IR
    (`targeting_ir.SLOT_SHAPES`)이 소유한다. 컴파일러는 그 둘의 소비자였을 뿐이라 파생 범위는
    좁아지지 않는다 — 오히려 컴파일러가 소유하지 않던 슬롯까지 포함되어 넓어진다.
    """
    import member_filters_config
    import targeting_ir

    terms = {targeting_domain.plan_container("member_condition") or ""}
    terms |= {slot.rpartition(".")[2] for slot in targeting_ir.SLOT_SHAPES}
    for entry in member_filters_config.eq_filters():
        if entry.get("category") in {"grade", "state"}:
            terms.update(str(term) for term in entry.get("synonyms") or [])
            terms.add(str(entry.get("canonical") or ""))
    terms |= set(targeting_domain.attribute_axis_terms())
    terms |= set(targeting_domain.attribute_value_terms())
    for values in targeting_domain.vocabularies().values():
        terms.update(values)
    # 너무 짧거나 코어의 일반 어휘와 겹치는 것은 뺀다(오탐 방지).
    generic = {"and", "or", "not", "count", "sum", "avg", "max", "min", "eq", "gte", "lte",
               "member", "product", "brand", "category", "parent"}
    return sorted(
        term for term in terms
        if term and len(term) >= 3 and term.casefold() not in generic
    )


def test_domain_term_list_is_not_empty() -> None:
    """공허한 통과 방지 — 금지 낱말이 하나도 파생되지 않으면 아래 가드가 무의미하다."""
    terms = _domain_terms()
    assert len(terms) >= 10, f"도메인 낱말 파생이 비었다: {terms}"


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_core_module_has_no_domain_vocabulary(module_name: str) -> None:
    """코어의 코드에도 문서에도 도메인 낱말이 없다."""
    code, docs = _split_code_and_docs(module_name)
    offenders = sorted({
        term for term in _domain_terms()
        if _mentions(code, term) or _mentions(docs, term)
    })
    assert not offenders, (
        f"{module_name} 이 도메인 낱말을 안다: {offenders}\n"
        "값 목록은 targeting_domain.json 선언으로, 표면형은 targeting_domain.py 로 옮겨라."
    )


@pytest.mark.parametrize("module_name", CORE_MODULES)
def test_core_module_does_not_import_domain_modules(module_name: str) -> None:
    """코어는 도메인 모듈을 최상위에서 import 하지 않는다(플러그인 바인딩으로만 만난다)."""
    tree = ast.parse(_module_text(module_name))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.col_offset == 0:
            imported.add(node.module.split(".")[0])
    domain_names = {name[: -len(".py")] for name in DOMAIN_MODULES}
    offenders = sorted(imported & domain_names)
    assert not offenders, (
        f"{module_name} 이 도메인 모듈을 최상위 import 한다: {offenders}\n"
        "필요한 값은 semantic_domain_binding 으로 주입받아라."
    )


def test_core_survives_without_a_domain_plugin(monkeypatch) -> None:
    """플러그인이 없으면 코어는 죽지 않고 '제약 없음'으로 강등된다.

    노드 스키마 생성(`semantic_plan.semantic_node_json_schema`)으로 강등을 관측하던 절은
    2026-08-05 빠졌다 — SemanticPlanV2 가 폐기되어 그 스키마가 존재하지 않는다. 강등 자체는
    바인딩 접근자로 그대로 관측한다.
    """
    monkeypatch.setenv(semantic_domain_binding.DOMAIN_PLUGIN_ENV, "no_such_domain_plugin")
    semantic_domain_binding.reset()
    try:
        assert semantic_domain_binding.plugin() is None
        assert semantic_domain_binding.vocabulary("aggregate_scope") == ()
        assert semantic_domain_binding.plan_container() is None
    finally:
        semantic_domain_binding.reset()


def test_new_vocabulary_value_opens_by_declaration_only(monkeypatch, tmp_path) -> None:
    """카탈로그 선언 한 줄로 새 값이 코어 어휘에 반영된다(코드 변경 없음)."""
    catalog = json.loads(targeting_domain.DEFAULT_DOMAIN_PATH.read_text(encoding="utf-8"))
    catalog["vocabularies"]["aggregate_scope"] = [
        *catalog["vocabularies"]["aggregate_scope"], "subscription",
    ]
    path = tmp_path / "domain.json"
    path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setenv("TARGETING_DOMAIN_PATH", str(path))
    targeting_domain.reset_cache()
    try:
        assert "subscription" in semantic_domain_binding.vocabulary("aggregate_scope")
    finally:
        targeting_domain.reset_cache()


def test_attribute_vocabulary_is_derived_not_listed() -> None:
    """속성 값 어휘는 eq_filters 파생이다 — 같은 낱말이 여러 모듈 정규식에 재등장하지 않는다."""
    import member_filters_config

    derived = set(targeting_domain.attribute_value_terms())
    assert derived, "속성 값 어휘 파생이 비었다"
    # 값 범주도 **선언에서** 읽는다. 손으로 적어 두면 속성 축이 하나 늘 때마다 이 테스트가
    # 파생을 미선언으로 잘못 신고한다(2026-08-08: 가치등급 축 추가에서 실제로 그랬다).
    categories = {
        str(spec.get("value_category"))
        for spec in _attribute_catalog_specs()
        if spec.get("value_category")
    }
    assert categories, "속성 카탈로그가 값 범주를 선언하지 않았다"
    declared = {
        term
        for entry in member_filters_config.eq_filters()
        if entry.get("category") in categories
        for term in entry.get("synonyms") or []
    }
    # 파생 집합의 모든 항목은 선언에서 왔거나(동의어), 감지 전용 보조 표면형이다.
    import compositional_targeting

    extra = {
        term
        for terms in compositional_targeting._DETECTOR_EXTRA_VALUE_TOKENS.values()
        for term in terms
    }
    unknown = {term for term in derived if term not in declared and term not in extra}
    assert not unknown, f"어디에서도 선언되지 않은 값 어휘가 파생됐다: {sorted(unknown)}"


def test_temporal_surface_forms_map_to_generic_operators() -> None:
    """'기준'·'내내'·'직전'은 낱말별 분기가 아니라 범용 연산자로 사상된다."""
    lexicon = targeting_domain.temporal_lexicon()
    assert lexicon.operators <= temporal_semantics.OPERATORS
    assert lexicon.operators, "시간 한정어 어휘가 비었다"
    # 어휘가 쓰는 모든 연산자는 실행 연산자 선언을 갖거나, 갖지 않음이 명시적이다.
    for operator in sorted(lexicon.operators):
        assert operator in temporal_semantics.OPERATOR_SPECS


def test_execution_operator_table_only_references_known_operators() -> None:
    """실행 연산자 표가 코어의 닫힌 집합 밖 연산자를 가리키면 즉시 실패한다."""
    table = targeting_domain.catalog()["temporal"]["execution_operators"]
    unknown = sorted(
        key for key in table
        if not key.startswith("_") and key not in temporal_semantics.OPERATORS
    )
    assert not unknown, f"선언이 알 수 없는 시간 연산자를 가리킨다: {unknown}"


def test_extension_boundary_is_documented() -> None:
    """JSON 만으로 여는 범위 vs 코드가 필요한 범위가 선언돼 있다."""
    boundary = targeting_domain.extension_boundary()
    assert boundary["declaration_only"] and boundary["code_required"]
    joined = " ".join(boundary["code_required"])
    assert "시간 연산" in joined, "새 연산 의미가 코드 변경 범위임이 명시돼야 한다"
