"""등급 서열은 문자열 크기가 아니다 — 순서 있는 값 도메인 계약.

`EMART_GRADE_CD` 의 **저장값 자체가** `'MEM_GRADE_CD.WELCOME'` … 형태라 사전식 순서가
``FAMILY < GOLD < SILVER < VIP < WELCOME`` 이다. 그래서 부등호를 그대로 렌더하면:

  · "골드 이상"  → GOLD, **SILVER**, VIP, **WELCOME**  (오탐 2)
  · "실버 이상"  → SILVER, VIP, **WELCOME**            (골드가 빠짐 — 미탐)
  · "VIP 이상"   → VIP, **WELCOME**                    (최하위 등급이 최상위 조건에 섞임)

경고 0·예외 0 이고 결과가 DB collation 에도 종속된다. 그래서 부등호를 **물리값 IN 목록**으로
컴파일한다. 서열(rank)의 단일 소유자는 `member_target_filters.json` 의 eq_filters 이고,
카탈로그는 "이 도메인에는 순서가 있다"만 선언한다(이중 소유 금지).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402
import event_ir  # noqa: E402
import member_filters_config  # noqa: E402

WELCOME = "MEM_GRADE_CD.WELCOME"
FAMILY = "MEM_GRADE_CD.FAMILY"
SILVER = "MEM_GRADE_CD.SILVER"
GOLD = "MEM_GRADE_CD.GOLD"
VIP = "MEM_GRADE_CD.VIP"


def _sql(operator: str, canonical: str, field: str = "subject.grade") -> str:
    catalog = audience_runtime.resolve_audience_catalog()
    condition = event_ir.Comparison(
        operator=operator,
        left=event_ir.FieldRef(name=field),
        right=event_ir.Literal(value=canonical),
    )
    return event_compiler.compile_condition(condition, catalog.compile_context(literals=True)).sql


@pytest.mark.parametrize(
    ("operator", "canonical", "expected"),
    [
        # 사전식이면 SILVER·WELCOME 이 섞이던 자리.
        (">=", "gold_grade", [GOLD, VIP]),
        # 사전식이면 GOLD 가 빠지던 자리.
        (">=", "silver_grade", [SILVER, GOLD, VIP]),
        # 사전식이면 WELCOME 이 섞이던 자리.
        (">=", "vip", [VIP]),
        ("<=", "silver_grade", [WELCOME, FAMILY, SILVER]),
        (">", "silver_grade", [GOLD, VIP]),
        ("<", "family_grade", [WELCOME]),
    ],
)
def test_ordinal_comparison_expands_to_ranked_values(operator: str, canonical: str, expected: list[str]) -> None:
    sql = _sql(operator, canonical)
    assert sql == "B.EMART_GRADE_CD IN (" + ", ".join(f"'{value}'" for value in expected) + ")"


def test_no_string_inequality_survives_in_the_sql() -> None:
    """부등호가 하나라도 남으면 collation 종속 오답이 되살아난다."""
    sql = _sql(">=", "gold_grade")
    assert ">=" not in sql and ">" not in sql and "<" not in sql


def test_equality_is_untouched() -> None:
    """서열 확장은 부등호 전용이다 — 같음/다름은 원래 코드 변환만으로 옳다."""
    assert _sql("=", "gold_grade") == f"B.EMART_GRADE_CD = '{GOLD}'"


def test_unordered_domain_rejects_size_comparison() -> None:
    """순서가 선언되지 않은 도메인(성별·상태)에 '이상'이 오면 근사하지 않고 막는다."""
    with pytest.raises(event_compiler.SqlCompileError) as excinfo:
        _sql(">=", "female", field="subject.gender")
    assert "순서가 선언되지 않은" in str(excinfo.value)


def test_unknown_canonical_still_fails_closed() -> None:
    with pytest.raises(event_compiler.SqlCompileError):
        _sql(">=", "platinum_grade")


def test_rank_has_a_single_owner() -> None:
    """카탈로그는 순서가 '있다'만 말하고 순서 자체는 eq_filters 가 소유한다."""
    raw = audience_runtime.load_audience_catalog_config()
    declaration = raw["value_domains"]["grade"]
    assert declaration.get("ordered") is True
    assert declaration.get("source_category") == "grade"
    # 값·서열이 카탈로그에 복제되면 두 파일이 곧 어긋난다.
    assert "values" not in declaration and "order" not in declaration

    ranks = {
        canonical: entry["rank"]
        for canonical, entry in member_filters_config.eq_filter_values("grade").items()
    }
    assert ranks == {
        "welcome_grade": 1, "family_grade": 2, "silver_grade": 3, "gold_grade": 4, "vip": 5,
    }


def test_ordered_declaration_requires_every_value_to_have_a_rank() -> None:
    """rank 가 빠진 값이 하나라도 있으면 조용히 순서를 지어내지 않는다."""
    raw = {"value_domains": {"state": {"source_category": "state", "ordered": True}}}
    with pytest.raises(audience_runtime.AudienceCatalogLoadError) as excinfo:
        audience_runtime.materialize_value_domains(raw)
    assert "rank" in str(excinfo.value)


def test_state_domain_is_unordered_by_declaration() -> None:
    """정상/휴면/탈퇴에는 rank 가 없다 — 그것이 '무순서'라는 선언이다."""
    entries = member_filters_config.eq_filter_values("state")
    assert entries, "state 범주가 사라졌다면 이 계약의 전제가 바뀐 것이다."
    assert all(entry.get("rank") is None for entry in entries.values())
