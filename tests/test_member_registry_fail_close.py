"""회원 물리 바인딩 설정의 구조·소비자·배포 게이트 fail-close 계약."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import db_swap_preflight  # noqa: E402
import member_filters_config  # noqa: E402
import member_policy  # noqa: E402

CONFIG_PATH = (
    REPO_ROOT / "docs" / "data" / "runtime" / "sql" / "member_target_filters.json"
)

REQUIRED_MAPPING_SECTIONS: tuple[str, ...] = (
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
    "group_ranking_axes",
    "member_metric_ranking",
    "validation",
)
REQUIRED_LIST_SECTIONS: tuple[str, ...] = (
    "eq_filters",
    "activity_filters",
    "numeric_filters",
    "purchase_product_match_columns",
)


def _shipped_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _write_config(tmp_path: Path, payload: dict[str, Any]) -> Path:
    path = tmp_path / "member_target_filters.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.mark.parametrize("section", REQUIRED_MAPPING_SECTIONS)
def test_required_mapping_sections_must_not_be_empty(
    tmp_path: Path,
    section: str,
) -> None:
    payload = _shipped_config()
    payload[section] = {}

    with pytest.raises(
        member_filters_config.MemberFiltersConfigError,
        match=section,
    ):
        member_filters_config.load_config(_write_config(tmp_path, payload))


@pytest.mark.parametrize("section", REQUIRED_LIST_SECTIONS)
def test_required_list_sections_must_not_be_empty(
    tmp_path: Path,
    section: str,
) -> None:
    payload = _shipped_config()
    payload[section] = []

    with pytest.raises(
        member_filters_config.MemberFiltersConfigError,
        match=section,
    ):
        member_filters_config.load_config(_write_config(tmp_path, payload))


@pytest.mark.parametrize(
    ("path", "empty_value"),
    (
        (("order_count_targets", "behaviors"), {}),
        (("aggregate_targets", "metrics"), {}),
        (("cart_targets", "behaviors"), {}),
        (("campaign_response_targets", "contact_member_list", "member_join"), {}),
        (
            (
                "campaign_response_targets",
                "contact_member_list",
                "campaign_join",
                "conditions",
            ),
            [],
        ),
        (("member_metric_ranking", "supported_metrics"), []),
    ),
)
def test_required_nested_registry_structures_must_not_be_empty(
    tmp_path: Path,
    path: tuple[str, ...],
    empty_value: dict[str, Any] | list[Any],
) -> None:
    payload = _shipped_config()
    node: dict[str, Any] = payload
    for part in path[:-1]:
        child = node[part]
        assert isinstance(child, dict)
        node = child
    node[path[-1]] = empty_value

    dotted_path = ".".join(path)
    with pytest.raises(
        member_filters_config.MemberFiltersConfigError,
        match=dotted_path,
    ):
        member_filters_config.load_config(_write_config(tmp_path, payload))


@pytest.mark.parametrize(
    "path",
    (
        ("signup_target", "default_days"),
        ("group_ranking_axes", "gender", "group_expr"),
        ("cart_targets", "table"),
        ("cart_targets", "join", "left"),
        ("cart_targets", "active_condition", "column"),
        ("campaign_response_targets", "table"),
        ("campaign_response_targets", "member_join", "right"),
        ("cell_rate_targets", "member_join", "right"),
        ("purchase_product_target", "product", "join"),
        ("aggregate_targets", "metrics", "purchase_amount", "agg"),
    ),
)
def test_required_physical_scalars_fail_closed(
    tmp_path: Path,
    path: tuple[str, ...],
) -> None:
    payload = _shipped_config()
    node: dict[str, Any] = payload
    for part in path[:-1]:
        node = node[part]
    del node[path[-1]]

    with pytest.raises(
        member_filters_config.MemberFiltersConfigError,
        match=re.escape(".".join(path[:-1])),
    ):
        member_filters_config.load_config(_write_config(tmp_path, payload))


def test_member_policy_propagates_missing_config_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing-member-target-filters.json"

    with pytest.raises(member_filters_config.MemberFiltersConfigError):
        member_policy.load_member_policy(str(missing))
    with pytest.raises(member_filters_config.MemberFiltersConfigError):
        member_policy.active_member_predicate(path=missing)


def test_preflight_fails_when_required_registry_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(REPO_ROOT)
    missing = tmp_path / "member_target_filters.json"
    registry_paths = [
        missing if path.name == missing.name else path
        for path in db_swap_preflight.REGISTRY_PATHS
    ]
    monkeypatch.setattr(db_swap_preflight, "REGISTRY_PATHS", registry_paths)

    result = db_swap_preflight.run_preflight()

    assert result["ok"] is False
    assert any(
        str(missing) in problem and "필수 레지스트리 파일 없음" in problem
        for problem in result["problems"]
    )
    assert not any(str(missing) in warning for warning in result["warnings"])
