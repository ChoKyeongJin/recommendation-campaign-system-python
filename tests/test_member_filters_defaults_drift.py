"""회원 타겟 물리 바인딩의 JSON 단일 소스·fail-close 계약."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import graph_rag  # noqa: E402
import confidence  # noqa: E402
import member_filters_config  # noqa: E402

CONFIG_PATH = REPO_ROOT / "docs" / "data" / "runtime" / "sql" / "member_target_filters.json"


def test_shipped_config_is_the_only_runtime_source() -> None:
    assert member_filters_config.load_config(CONFIG_PATH) == json.loads(
        CONFIG_PATH.read_text(encoding="utf-8")
    )
    assert not hasattr(member_filters_config, "CODE_DEFAULTS")
    assert "_DEFAULT_MEMBER_TARGET_FILTERS" not in (
        REPO_ROOT / "graph_rag.py"
    ).read_text(encoding="utf-8")


def test_default_path_is_absolute_and_independent_of_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    assert member_filters_config.DEFAULT_PATH.is_absolute()
    assert confidence.DEFAULT_MEMBER_FILTERS_PATH == member_filters_config.DEFAULT_PATH
    assert member_filters_config.load_config()["base_entity"]["table"]


@pytest.mark.parametrize(
    "contents",
    [None, "{broken", "[]", "{}", '{"base_entity": {}}'],
)
def test_missing_or_incomplete_config_fails_closed(
    tmp_path: Path,
    contents: str | None,
) -> None:
    path = tmp_path / "member_target_filters.json"
    if contents is not None:
        path.write_text(contents, encoding="utf-8")

    with pytest.raises(member_filters_config.MemberFiltersConfigError):
        member_filters_config.load_config(path)


def test_graph_sql_boundary_reports_configuration_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(graph_rag, "_MEMBER_TARGET_FILTERS_ERROR", "broken test config")

    blocked = graph_rag._member_target_filters_blocking_sql_result()
    compiled = graph_rag.compile_member_target_conditions(
        {"target_user": {"gender": "female"}}
    )

    assert blocked is not None
    assert blocked["sql"] is None
    assert blocked["is_success"] is False
    assert blocked["failure_reason"] == "member_target_filters_unavailable"
    assert blocked["interpretation_status"] == "unsupported"
    assert blocked["clarification_questions"] == []
    assert compiled["predicates"] == []
    assert compiled["unsupported"] == ["system.member_target_filters"]


def test_physical_inline_defaults_do_not_come_back_in_graph_rag() -> None:
    """`config.get(key, "OLD_COLUMN")` 형태의 제3 바인딩 층을 금지한다."""

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
        "물리 이름 인라인 폴백이 부활했다:\n  " + "\n  ".join(offenders[:10])
    )


def test_device_channel_families_cover_the_same_code_values() -> None:
    """같은 코드 도메인(DEVICE_TYPE_CD)을 쓰는 두 채널 계열의 값 집합은 어긋나면 안 된다.

    가입 채널(REG_CHANNEL_CD)은 앱/모바일웹/PC 를 모두 알았는데 로그인 채널
    (LAST_LOGIN_CHANNEL)은 앱 하나만 알았다 — 실컬럼에 MW 10,610건·PC 6,824건이 있는데도
    '모바일웹으로 접속한 회원'을 표현할 수단이 없었다(2026-08-04 실측).
    """
    payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    families: dict[str, set[str]] = {}
    for entry in payload["eq_filters"]:
        category = str(entry.get("category") or "")
        if category in ("login_channel", "signup_channel"):
            families.setdefault(category, set()).add(str(entry.get("value") or ""))
    assert families["login_channel"] == families["signup_channel"], (
        "디바이스 채널 계열의 코드값이 어긋난다 — 한쪽에만 있는 값은 표현할 수 없다:\n"
        f"  login_channel  : {sorted(families['login_channel'])}\n"
        f"  signup_channel : {sorted(families['signup_channel'])}"
    )


def test_base_entity_bindings_resolve_from_the_shipped_config() -> None:
    assert graph_rag._MEMBER_TARGET_FILTERS_ERROR is None
    assert graph_rag._member_table()
    assert graph_rag._member_alias()
    assert graph_rag._member_key_column()
    assert graph_rag._member_login_id_column()
