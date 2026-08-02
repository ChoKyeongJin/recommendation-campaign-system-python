"""회원 타겟 레지스트리 — 코드 미러 ↔ 배포 설정의 드리프트 가드.

물리 이름은 예전에 **세 곳**에 살았다:
  ① `docs/data/runtime/sql/member_target_filters.json` (단일 출처)
  ② graph_rag 안의 코드 기본값 미러 (파일 부재 시 폴백)
  ③ 접근자 안의 인라인 기본값 `config.get("table", "ODS_MALL_OMS_CART")`

③ 은 ② 가 키를 선언하는 한 죽은 코드였다. 하지만 ② 가 키를 빠뜨리면 조용히 되살아나
**구DB 이름으로 컴파일**된다 — SQL 은 성공하는데 0명이 나오는, 이 저장소가 가장 무서워하는 형태다.
실제로 `base_entity.age_column` 이 정확히 그 상태였다(미러에 없음 → 인라인 "AGE" 가 살아 있었음).

2026-08-02 에 ② 를 순수 설정 모듈(`member_filters_config.CODE_DEFAULTS`)로 옮기고 ③ 을 64건
걷어냈다. 이제 층은 둘이고, 둘이 어긋나면 값이 `None` 이 되어 **즉시 시끄럽게** 깨진다.
이 파일은 그 어긋남을 배포 전에 잡는다.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import member_filters_config  # noqa: E402

CONFIG_PATH = REPO_ROOT / "docs" / "data" / "runtime" / "sql" / "member_target_filters.json"

# 값이 물리 바인딩인 섹션 — 미러와 배포 설정이 **같은 키 집합**을 선언해야 한다.
# 값 사전(eq_filters 등 리스트 섹션)은 항목이 늘어나는 것이 정상이라 대상이 아니다.
BINDING_SECTIONS: tuple[str, ...] = (
    "base_entity",
    "active_state",
    "birthday_target",
    "signup_target",
    "recent_login_target",
    "order_count_targets",
    "aggregate_targets",
    "cart_targets",
    "campaign_response_targets",
    "cell_rate_targets",
    "region_target",
    "purchase_product_target",
    "entity_set_targets",
    "region_density",
    "member_metric_ranking",
)


def _shipped() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def test_mirror_is_not_empty() -> None:
    assert member_filters_config.CODE_DEFAULTS, "코드 기본값 미러가 비었다 — 폴백이 사라졌다."


def test_mirror_lives_outside_graph_rag() -> None:
    """미러가 graph_rag 로 되돌아오면 하위 계층(confidence 등)이 다시 순환 없이 못 읽는다."""
    graph_rag_source = (REPO_ROOT / "graph_rag.py").read_text(encoding="utf-8")
    assert "_DEFAULT_MEMBER_TARGET_FILTERS: dict[str, Any] = {" not in graph_rag_source, (
        "물리 이름 미러가 graph_rag 안으로 되돌아왔다 — member_filters_config.CODE_DEFAULTS 가 소유자다."
    )


@pytest.mark.parametrize("section", BINDING_SECTIONS)
def test_shipped_config_declares_every_mirrored_binding_key(section: str) -> None:
    """미러에 있는데 배포 설정에 없는 키 = 인라인 기본값을 걷어낸 지금은 조용한 ``None``.

    로더는 최상위 키 단위로 병합하므로, JSON 의 섹션이 미러의 섹션을 **통째로** 대체한다.
    따라서 JSON 섹션에 키가 빠지면 미러의 값이 살아나는 게 아니라 그냥 사라진다.
    """
    mirrored = member_filters_config.CODE_DEFAULTS.get(section)
    shipped = _shipped().get(section)
    if not isinstance(mirrored, dict) or not isinstance(shipped, dict):
        pytest.skip(f"{section} 은 dict 섹션이 아니다")
    missing = sorted(set(mirrored) - set(shipped))
    assert not missing, (
        f"배포 설정 {section} 에 {missing} 가 없다. 로더는 섹션을 통째로 대체하므로 "
        f"미러 값은 폴백되지 않고 None 이 된다 — JSON 에 키를 추가하라."
    )


def test_mirror_is_a_minimal_fallback_not_a_second_copy() -> None:
    """미러는 **최소 폴백**이지 JSON 의 사본이 아니다 — 이건 의도이므로 계약으로 적어 둔다.

    배포 JSON 이 훨씬 풍부하다(동의어·단위·힌트 어휘 등). 미러를 JSON 과 같게 만들려는 시도는
    방금 걷어낸 이중 소유를 원래 크기로 되살리는 일이다. 미러의 임무는 하나뿐이다:
    설정 파일이 통째로 없을 때 **기동은 되게** 하는 것.

    따라서 이 방향(JSON ⊇ 미러)만 계약이고, 역방향(미러 ⊇ JSON)은 계약이 아니다.
    """
    shipped = _shipped()
    mirrored = member_filters_config.CODE_DEFAULTS
    assert set(mirrored) - set(shipped) == set(), (
        "미러에만 있는 섹션이 있다 — 배포 설정이 이기므로 그 섹션은 런타임에 존재하지 않는다."
    )
    assert len(json.dumps(shipped)) > len(json.dumps(mirrored)), (
        "미러가 배포 설정보다 커졌다 — 최소 폴백이라는 전제가 깨졌다(이중 소유 부활)."
    )


# 인라인 기본값을 걷어낸 자리에서 코드가 이제 `.get(key)` 로 읽는 키들.
# 값이 None 이 되면 SQL 이 'None' 이라는 이름의 테이블/컬럼을 참조하거나 조용히 빠진다.
SWEPT_BINDING_KEYS: tuple[str, ...] = (
    "table", "column", "join_column", "member_column", "member_table",
    "date_column", "order_date_column", "order_id_column", "value_table",
    "group_column", "campaign_date_column", "contact_success_column",
)


@pytest.mark.parametrize("section", BINDING_SECTIONS)
def test_swept_keys_resolve_under_the_shipped_config(section: str) -> None:
    """인라인 기본값을 지운 키가 배포 설정에서 실제로 값을 갖는지.

    미러가 아니라 **병합된 설정**을 본다 — 로더는 섹션을 통째로 대체하므로 런타임 값의
    출처는 JSON 이다. 여기서 None 이 나오면 그것이 곧 'SQL 은 성공하는데 0명'의 씨앗이다.
    """
    import graph_rag

    merged = graph_rag._MEMBER_TARGET_FILTERS.get(section)
    if not isinstance(merged, dict):
        pytest.skip(f"{section} 은 dict 섹션이 아니다")
    empty = [
        key for key in SWEPT_BINDING_KEYS
        if key in merged and not str(merged[key] or "").strip()
    ]
    assert not empty, f"{section} 의 {empty} 가 비어 있다 — 물리 바인딩이 소실됐다."


def test_physical_inline_defaults_do_not_come_back_in_graph_rag() -> None:
    """`config.get("key", "PHYSICAL_NAME")` 의 부활을 막는다(세 번째 층 재생성 금지).

    판정은 **카탈로그에 실재하는 이름**으로만 한다 — `agg="SUM"` 이나 별칭 `"CELL"` 처럼
    스키마 이름이 아닌 리터럴은 물리 바인딩이 아니라 SQL 어휘라 대상이 아니다.
    """
    sys.path.insert(0, str(REPO_ROOT / "tools"))
    import physical_binding_inventory as inventory

    tables, columns = inventory._catalog_names()
    physical = tables | columns
    pattern = re.compile(r'\.get\(\s*"[a-z_]+"\s*,\s*"([A-Z][A-Z0-9_]{2,})"\s*\)')
    offenders: list[str] = []
    source = (REPO_ROOT / "graph_rag.py").read_text(encoding="utf-8").splitlines()
    for index, line in enumerate(source, start=1):
        for match in pattern.finditer(line):
            if match.group(1) in physical:
                offenders.append(f"graph_rag.py:{index}: {line.strip()[:90]}")
    assert not offenders, (
        "물리 이름을 인라인 기본값으로 되살렸다 — 설정에서 키가 빠져도 조용히 구DB 이름으로 "
        "컴파일된다('SQL 은 성공하는데 0명'):\n  " + "\n  ".join(offenders[:10])
    )


def test_base_entity_bindings_resolve() -> None:
    """가장 많이 쓰이는 회원 기준 바인딩이 실제로 값을 낸다(None 회귀 즉시 감지)."""
    import graph_rag

    assert graph_rag._member_table()
    assert graph_rag._member_alias()
    assert graph_rag._member_key_column()
    assert graph_rag._member_login_id_column()
