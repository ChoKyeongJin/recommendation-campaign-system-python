"""집계 파서 설정의 계약 — 잘못된 설정은 조용히 기본값으로 돌지 않고 시작 단계에서 터진다.

설정을 데이터로 뺀 이상, '설정이 틀렸는데 코드 기본값으로 계속 달리는' 상태가 가장 위험하다
(운영에서 설정 파일과 실제 동작이 갈린다). 그래서 여기 있는 테스트 대부분은 **실패해야 실패하는지**를
확인한다. 표면어 목록 자체는 검증하지 않는다 — 그건 데이터의 자유다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import aggregate_parser_config as config

DATA_DIR = Path(__file__).resolve().parents[1] / "docs" / "data" / "runtime" / "language"


def _rules_payload() -> dict:
    return json.loads((DATA_DIR / "aggregate_parser_rules.json").read_text(encoding="utf-8"))


def _messages_payload() -> dict:
    return json.loads((DATA_DIR / "clarification_messages.ko.json").read_text(encoding="utf-8"))


def _write(tmp_path: Path, name: str, payload: dict) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _load(tmp_path: Path, payload: dict, messages: dict | None = None) -> config.AggregateParserRules:
    message_path = _write(tmp_path, "messages.json", messages if messages is not None else _messages_payload())
    return config.load_rules(
        path=_write(tmp_path, "rules.json", payload),
        messages=config.load_messages(path=message_path),
    )


# ── 1. 정상 로드 ────────────────────────────────────────────────────────────────────────
def test_shipped_config_loads_and_validates() -> None:
    rules = config.rules()
    assert rules.version == config.SUPPORTED_VERSION
    assert rules.unit_tokens
    assert rules.aggregate_threshold_unit_kinds <= rules.unit_kinds
    # 배수어는 긴 표면형이 먼저 온다 — '천만'이 '천'보다 앞이어야 '3천만'이 3000 이 되지 않는다.
    surfaces = [surface for surface, _ in rules.number_multipliers]
    assert surfaces.index("천만") < surfaces.index("천")


def test_rules_are_cached_and_immutable() -> None:
    assert config.rules() is config.rules()
    with pytest.raises(Exception):
        config.rules().span_binding.max_unit_whitespace = 5  # type: ignore[misc]


def test_use_rules_injects_and_restores(tmp_path: Path) -> None:
    original = config.rules()
    payload = _rules_payload()
    payload["span_binding"]["max_unit_whitespace"] = 0
    injected = _load(tmp_path, payload)
    with config.use_rules(injected):
        assert config.rules() is injected
        assert config.rules().span_binding.max_unit_whitespace == 0
    assert config.rules() is original


# ── 2. fail-fast ───────────────────────────────────────────────────────────────────────
def test_wrong_version_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["version"] = 99
    with pytest.raises(config.AggregateParserConfigError, match="버전"):
        _load(tmp_path, payload)


def test_duplicate_unit_surface_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["unit_tokens"].append({"surface": "개", "kind": "count", "canonical": "dup", "priority": 10})
    with pytest.raises(config.AggregateParserConfigError, match="단위 토큰 표면어"):
        _load(tmp_path, payload)


def test_unknown_unit_kind_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["unit_tokens"].append({"surface": "말", "kind": "volume", "canonical": "mal", "priority": 10})
    with pytest.raises(config.AggregateParserConfigError, match="선언되지 않은 단위 종류"):
        _load(tmp_path, payload)


def test_unknown_threshold_unit_kind_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["aggregate_threshold_unit_kinds"].append("volume")
    with pytest.raises(config.AggregateParserConfigError, match="aggregate_threshold_unit_kinds"):
        _load(tmp_path, payload)


def test_duplicate_attribute_alias_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["unsupported_attribute_hints"][1]["aliases"].append("인구")
    with pytest.raises(config.AggregateParserConfigError, match="미지원 속성 별칭"):
        _load(tmp_path, payload)


def test_missing_message_key_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["unsupported_attribute_hints"][0]["message_key"] = "no_such_message"
    with pytest.raises(config.AggregateParserConfigError, match="message_key"):
        _load(tmp_path, payload)


def test_negative_search_distance_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["span_binding"]["attribute_search_left_tokens"] = -1
    with pytest.raises(config.AggregateParserConfigError, match="스키마를 위반"):
        _load(tmp_path, payload)


def test_excessive_unit_whitespace_fails(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["span_binding"]["max_unit_whitespace"] = 99
    with pytest.raises(config.AggregateParserConfigError, match="스키마를 위반"):
        _load(tmp_path, payload)


def test_overlapping_domain_node_kinds_fail(tmp_path: Path) -> None:
    payload = _rules_payload()
    payload["semantic_domains"]["purchase"]["absence_node_kinds"].append("purchase_aggregate")
    with pytest.raises(config.AggregateParserConfigError, match="존재/부재 노드 종류가 겹친다"):
        _load(tmp_path, payload)


def test_unknown_top_level_key_fails(tmp_path: Path) -> None:
    """오타난 키가 조용히 무시되면 '설정했는데 안 먹는' 죽은 설정이 된다."""
    payload = _rules_payload()
    payload["unit_token"] = []
    with pytest.raises(config.AggregateParserConfigError, match="스키마를 위반"):
        _load(tmp_path, payload)


def test_broken_json_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "rules.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(config.AggregateParserConfigError, match="올바른 JSON"):
        config.load_rules(path=path)


def test_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(config.AggregateParserConfigError, match="읽을 수 없다"):
        config.load_rules(path=tmp_path / "absent.json")


# ── 3. 안내 문구 ────────────────────────────────────────────────────────────────────────
def test_messages_render_with_fields() -> None:
    messages = config.messages()
    rendered = messages.render("unsupported_attribute_only", attribute="인구", hint=" 힌트")
    assert "인구" in rendered and "힌트" in rendered


def test_unknown_message_key_raises() -> None:
    with pytest.raises(config.AggregateParserConfigError, match="등록되지 않은 안내 문구 키"):
        config.messages().render("no_such_key")


def test_user_messages_do_not_leak_internal_terms() -> None:
    """사용자 문구에 내부 노드명·사유 코드가 새면 그 자체가 오답 신호다."""
    leaked = ("AGG", "NOT EXISTS", "unsupported_attribute", "metric unresolved", "semantic conflict")
    for key in config.messages().keys():
        template = config.messages().render(
            key, attribute="X", hint="", remaining_conditions="Y", value="1",
        )
        for term in leaked:
            assert term not in template, f"'{key}' 문구에 내부 표현 '{term}' 이 노출됐다"
