"""제공자 strict 함수호출 계약 — 스키마가 **전송 가능한 모양**인지 오프라인에서 고정한다.

이 파일이 있는 이유: V4 오디언스 스키마가 `$ref`/`$defs` 압축형으로 바뀌면서
`minLength`·`pattern`·`format`·`minItems`·`minimum` 같은 검증 키워드가 섞여 들어갔는데,
"제공자 strict 모드가 이걸 받아주는가"를 **오프라인으로는 확인할 수 없다**는 것이 미해결
위험으로 남아 있었다. 2026-08-02 라이브 스모크로 현재 스키마가 수락됨을 확인했다
(`현재 등급이 VIP인 회원` → status=success, SQL 출고).

그래서 여기서 하는 일은 "제공자 규격을 안다고 주장하는 것"이 아니라 **드리프트를 보이게
하는 것**이다:

  1. strict 모드의 **구조** 불변식(루트 object, 모든 property 가 required, 열린 object 금지,
     `$ref` 대상 실재)은 하드 실패로 강제한다 — 이건 규격이 아니라 이 코드가 스스로 만드는
     모양이고, `_strictify` 가 보장한다고 선언한 바로 그것이다.
  2. 검증 키워드는 **선언된 목록**과 대조한다. 새 키워드가 스키마에 들어오면 목록에 한 줄
     추가해야 하고, 그 한 줄이 "라이브에서 수락되는지 확인했는가"를 묻는 마찰이 된다.
     오프라인 테스트는 제공자 수락을 증명할 수 없으므로, 증명 대신 **의식적 결정**을 강제한다.

`tests/test_audience_schema_compactness.py` 는 크기·enum 기수를, 이 파일은 전송 적합성을 본다.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from query_structurer.campaign_plan_v4 import (  # noqa: E402
    CAMPAIGN_QUERY_PLAN_V4_LLM_JSON_SCHEMA as SCHEMA,
    CAMPAIGN_QUERY_PLAN_V4_TOOL as TOOL,
)

# 라이브에서 수락이 확인된 검증 키워드(2026-08-02 스모크). 새 키워드를 쓰려면 여기 추가하고,
# 추가 커밋에 라이브 확인 근거를 남겨라 — 오프라인으로는 수락 여부를 알 수 없다.
#
# `maximum`: semantic_plan 노드의 confidence(0~1) 가 들여온다. 2026-08-02 라이브 확인 —
# 좁힌 semantic_plan 노출면을 붙인 스키마로 /target-sql 5건 호출, 전부 200 + strict tool
# 인자 정상 반환(거부 코드 없음). 로그: logs/rag_llm/2026-08-02/ campaign_query_plan_v4_response.
LIVE_VERIFIED_VALIDATION_KEYWORDS = frozenset({
    "minimum", "maximum", "minLength", "maxLength", "pattern", "format", "minItems",
})

# 구조·의미 키워드(제공자 strict 규격의 뼈대). 이건 드리프트 관리 대상이 아니다.
STRUCTURAL_KEYWORDS = frozenset({
    "type", "properties", "required", "additionalProperties", "items", "enum",
    "anyOf", "$ref", "$defs", "description", "const", "title",
})


def _walk(node: Any, path: str = "$"):
    """(경로, dict 노드) 를 모두 훑는다."""
    if isinstance(node, dict):
        yield path, node
        for key, value in node.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _walk(value, f"{path}[{index}]")


def _object_nodes():
    """실제 object 스키마 노드만(=properties 를 선언한 노드)."""
    for path, node in _walk(SCHEMA):
        if isinstance(node.get("properties"), dict):
            yield path, node


def test_tool_declares_strict_mode() -> None:
    assert TOOL["function"]["strict"] is True
    assert TOOL["function"]["parameters"] is SCHEMA


def test_root_is_a_closed_object() -> None:
    assert SCHEMA["type"] == "object"
    assert SCHEMA["additionalProperties"] is False
    # strict 모드는 루트에 anyOf 를 허용하지 않는다.
    assert "anyOf" not in SCHEMA


def test_every_object_is_closed() -> None:
    open_objects = [
        path for path, node in _object_nodes() if node.get("additionalProperties") is not False
    ]
    assert not open_objects, (
        "열린 object 는 strict 모드에서 거부된다(additionalProperties:false 누락): "
        f"{open_objects[:8]}"
    )


def test_every_property_is_required() -> None:
    """strict 모드는 선택 필드를 허용하지 않는다 — 무표현은 required + nullable 로 낸다."""
    violations = []
    for path, node in _object_nodes():
        declared = set(node.get("required") or [])
        present = set(node["properties"])
        if declared != present:
            violations.append((path, sorted(present - declared), sorted(declared - present)))
    assert not violations, (
        "required 가 properties 와 일치하지 않는다(누락, 유령). "
        f"_strictify 가 보장해야 하는 불변식이다: {violations[:5]}"
    )


def test_optional_fields_became_nullable_instead_of_optional() -> None:
    """모든 필드가 required 가 됐으므로, 원래 선택이던 것은 null 을 받을 수 있어야 한다.

    이게 깨지면 모델이 '해당 없음'을 표현할 방법을 잃고 값을 지어내기 시작한다 —
    이 저장소가 반복해서 겪은 실패 유형(환각 방출)의 스키마 쪽 원인이다.
    """
    def _accepts_null(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        if node.get("type") == "null":
            return True
        if isinstance(node.get("type"), list) and "null" in node["type"]:
            return True
        return any(_accepts_null(branch) for branch in node.get("anyOf") or [])

    expression = SCHEMA["properties"]["audience_requirement"]["properties"]["expression"]
    assert _accepts_null(expression), (
        "audience_requirement.expression 이 null 을 못 받으면 '오디언스 조건 없음'을 표현할 수 없다."
    )


def test_all_refs_resolve_to_declared_defs() -> None:
    defs = set(SCHEMA.get("$defs") or {})
    assert defs, "$defs 가 비었다 — 압축형 스키마의 전제가 깨졌다."
    dangling = []
    for path, node in _walk(SCHEMA):
        ref = node.get("$ref")
        if not isinstance(ref, str):
            continue
        if not ref.startswith("#/$defs/") or ref.rpartition("/")[2] not in defs:
            dangling.append((path, ref))
    assert not dangling, (
        "제공자는 함수 parameters 문서 루트에서만 $ref 를 푼다 — 미해결 참조는 전송 즉시 거부다: "
        f"{dangling[:5]}"
    )


def test_no_unhoisted_nested_schema_identity() -> None:
    """중첩 `$id` 는 제공자가 존재하지 않는 컴포넌트를 찾게 만든다(hoist 되어야 한다)."""
    stray = [path for path, node in _walk(SCHEMA) if "$id" in node]
    assert not stray, f"루트로 끌어올리지 않은 $id 가 남았다: {stray}"


def test_validation_keywords_are_declared_and_live_verified() -> None:
    """검증 키워드는 선언 목록 안에만 있어야 한다.

    오프라인 테스트는 제공자 수락을 증명하지 못한다. 그래서 증명 대신 **한 줄의 마찰**을 둔다 —
    새 키워드를 쓰려면 목록에 추가해야 하고, 그때 라이브로 확인하게 된다.
    """
    seen: set[str] = set()
    for _path, node in _walk(SCHEMA):
        seen.update(node)
    undeclared = seen - STRUCTURAL_KEYWORDS - LIVE_VERIFIED_VALIDATION_KEYWORDS
    # `properties`/`$defs` 아래의 이름은 키워드가 아니라 **데이터**다(필드명·정의명).
    # 그 값 노드들은 _walk 가 이미 개별 노드로 방문하므로, 여기서는 이름 자체를 뺀다.
    names: set[str] = set(SCHEMA.get("$defs") or {})
    for _path, node in _object_nodes():
        names.update(node["properties"])
    undeclared -= names
    assert not undeclared, (
        f"선언되지 않은 스키마 키워드: {sorted(undeclared)}\n"
        "제공자 strict 모드가 이 키워드를 받는지 라이브로 확인한 뒤 "
        "LIVE_VERIFIED_VALIDATION_KEYWORDS 에 추가하라(확인 근거를 커밋 메시지에)."
    )


def test_schema_stays_small_enough_to_send() -> None:
    """압축형 스키마의 목적은 카탈로그가 커져도 전송면이 고정되는 것이다."""
    import json

    size = len(json.dumps(SCHEMA))
    assert size < 60_000, (
        f"LLM 전송 스키마가 {size} 바이트로 커졌다 — $ref 압축이 풀렸는지 확인하라."
    )
