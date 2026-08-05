"""canonical 실행 경로의 소유권 계약 — 카탈로그가 원본, 코드 레지스트리는 최소 폴백.

이 저장소에는 SQL 로 가는 길이 둘 있다:

  canonical  원문 → **Event IR** → event_compiler → SQL
             바인딩은 `docs/data/runtime/semantics/audience_catalog.json` 이 선언한다.
             새 사건·필드·지표는 **JSON 한 항목**이고 컴파일 코드는 손대지 않는다.
             (SemanticPlanV2 중간표현과 `semantic_plan_event_lowering` 은 2026-08-05 폐기)
  legacy     query_plan 슬롯 → 20개 빌더 레지스트리 → SQL
             빌더 몸통이 SQL 모양을 코드로 들고 있다.

범용화의 방향은 "legacy 빌더를 선언적으로 다시 쓰는 것"이 아니라 **canonical 이 덮는 범위를
넓혀 legacy 로 내려가지 않게 하는 것**이다. 그래서 이 파일이 지키는 것은 하나다:
**canonical 경로의 물리 지식은 카탈로그가 소유한다.**

`event_compiler.EVENT_REGISTRY` / `FIELD_REGISTRY` 는 카탈로그 파일이 통째로 없을 때 기동만
되게 하는 최소 폴백이다(member_filters_config.CODE_DEFAULTS 와 같은 지위). 그 미러가 카탈로그가
모르는 사건·필드를 갖게 되면 런타임과 폴백이 서로 다른 스키마를 말하게 되므로 여기서 막는다.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import audience_runtime  # noqa: E402
import event_compiler  # noqa: E402

CATALOG_PATH = REPO_ROOT / "docs" / "data" / "runtime" / "semantics" / "audience_catalog.json"


def _raw_catalog() -> dict:
    return json.loads(CATALOG_PATH.read_text(encoding="utf-8"))


def test_catalog_file_exists_and_declares_bindings() -> None:
    catalog = _raw_catalog()
    assert catalog.get("sources"), "카탈로그에 사건 소스 선언이 없다."
    assert catalog.get("fields"), "카탈로그에 필드 선언이 없다."
    assert catalog.get("subject"), "카탈로그에 주체(회원) 바인딩이 없다."


def test_every_code_event_is_declared_in_the_catalog() -> None:
    """미러에만 있는 사건 = 카탈로그가 모르는 스키마. 런타임에는 존재하지 않는다."""
    catalog_sources = set(_raw_catalog()["sources"])
    missing = sorted(set(event_compiler.EVENT_REGISTRY) - catalog_sources)
    assert not missing, (
        f"코드 폴백에만 있는 사건: {missing} — 카탈로그에 선언하거나 폴백에서 지워라. "
        "둘이 갈리면 설정 파일 유무에 따라 다른 스키마로 컴파일된다."
    )


def test_every_code_field_is_declared_in_the_catalog() -> None:
    catalog_fields = set(_raw_catalog()["fields"])
    missing = sorted(set(event_compiler.FIELD_REGISTRY) - catalog_fields)
    assert not missing, f"코드 폴백에만 있는 필드: {missing}"


def test_catalog_is_richer_than_the_code_fallback() -> None:
    """폴백이 카탈로그를 따라잡으면 '최소 폴백'이라는 전제가 깨진다(이중 소유 부활)."""
    catalog = _raw_catalog()
    assert len(catalog["sources"]) >= len(event_compiler.EVENT_REGISTRY)
    assert len(catalog["fields"]) >= len(event_compiler.FIELD_REGISTRY)


def test_resolved_catalog_shadows_the_code_fallback_at_runtime() -> None:
    """런타임 해석본이 폴백을 **덮는지** — 덮지 않으면 JSON 수정이 반영되지 않는다."""
    resolved = audience_runtime.resolve_audience_catalog()
    for name in event_compiler.EVENT_REGISTRY:
        assert name in resolved.sources, f"해석된 카탈로그에 사건 {name} 이 없다."
    for name in event_compiler.FIELD_REGISTRY:
        assert name in resolved.fields, f"해석된 카탈로그에 필드 {name} 이 없다."


def test_guidance_renders_inline_and_referenced_value_domains_once() -> None:
    """모델 안내도 실행 카탈로그와 같은 materialized 값 사전을 읽어야 한다.

    consent_flag 는 audience_catalog 안에 값이 있고, 나머지는
    member_target_filters.eq_filters 를 source_category 로 참조한다. raw JSON을 그대로
    렌더하면 후자만 조용히 빠져 앱 로그인 채널 같은 지원 필드를 모델이 미지원으로 신고한다.
    """
    guidance = audience_runtime.audience_catalog_guidance()

    expected = {
        "consent_flag": "agreed",
        "gender": "female",
        "grade": "vip",
        "login_channel": "app_user",
    }
    for domain, canonical in expected.items():
        marker = f"\n- {domain}:"
        assert guidance.count(marker) == 1
        line = next(
            line
            for line in guidance.splitlines()
            if line.startswith(f"- {domain}:")
        )
        assert canonical in line


@pytest.mark.parametrize("source", sorted(_raw_catalog()["sources"]))
def test_catalog_sources_bind_to_real_schema(source: str) -> None:
    """카탈로그가 가리키는 테이블이 schema_catalog 에 실재하는가(유령 바인딩 차단)."""
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import physical_binding_inventory as inventory

    tables, _columns = inventory._catalog_names()
    declared = _raw_catalog()["sources"][source]
    table = declared.get("table")
    if not table:
        pytest.skip(f"{source} 는 테이블 바인딩이 없다(파생 소스)")
    assert table in tables, (
        f"카탈로그 사건 {source} 가 스키마에 없는 테이블 {table} 를 가리킨다 — "
        "DB 스왑 후 조용히 0명이 되는 형태다."
    )


@pytest.mark.parametrize("field", sorted(_raw_catalog()["fields"]))
def test_catalog_fields_bind_to_real_columns(field: str) -> None:
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import physical_binding_inventory as inventory

    _tables, columns = inventory._catalog_names()
    declared = _raw_catalog()["fields"][field]
    column = declared.get("column")
    if not column:
        pytest.skip(f"{field} 는 컬럼 바인딩이 없다(표현식 필드)")
    assert column in columns, (
        f"카탈로그 필드 {field} 가 스키마에 없는 컬럼 {column} 를 가리킨다."
    )


# `test_lowering_has_no_business_event_branches` 는 2026-08-05 삭제됐다. 검사 대상이던
# `semantic_plan_event_lowering.py` 는 SemanticPlanV2 중간표현과 함께 폐기되며, 그 모듈의
# "사건별 분기 없음" 계약도 모듈과 함께 사라진다(현재 canonical 경로에는 그 파일이 없다).
# 사건 지식이 카탈로그 소유라는 계약 자체는 위의 미러 대조 3종과 아래 확장 경계 테스트가
# 계속 지킨다 — 대상 없는 소스 스캔만 없앴다.


def test_adding_a_source_is_json_only(tmp_path: Path) -> None:
    """확장 경계의 기계 검증: 사건 하나를 JSON 에 더해도 코드 변경 없이 해석된다.

    이것이 "새 데이터는 선언, 새 의미는 코드"라는 경계의 실측본이다 — 이 테스트가 깨지면
    canonical 경로의 확장이 더 이상 선언만으로 되지 않는다는 뜻이다.
    """
    probe = json.loads(json.dumps(_raw_catalog()))
    template = next(iter(probe["sources"].values()))
    probe["sources"]["__probe_event__"] = dict(template, label="probe")

    probe_path = tmp_path / "audience_catalog.json"
    probe_path.write_text(json.dumps(probe, ensure_ascii=False), encoding="utf-8")

    resolved = audience_runtime.resolve_audience_catalog(probe_path)
    assert "__probe_event__" in resolved.sources, (
        "JSON 에 사건을 추가했는데 해석본에 나타나지 않는다 — 확장이 선언만으로 되지 않는다."
    )
    # 파생 시각 필드도 자동으로 따라와야 한다(사건만 등록하고 기간 조건을 못 거는 상태 방지).
    assert any(
        name.startswith("__probe_event__.") for name in resolved.fields
    ), "새 사건의 발생 시각 필드가 파생되지 않았다 — 기간 조건을 걸 수 없는 사건이 된다."
