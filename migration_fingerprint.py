"""이행 지문(fingerprint) — "무엇이 같고 무엇이 달라졌는가"를 세 축으로 나눠 답한다.

지문 하나로는 답할 수 없는 질문이 셋이다. 하나로 합치면 세 질문이 서로를 가린다:

    source_fingerprint     원본 legacy payload 가 바뀌었는가  → 변환이 stale 인가
    semantic_fingerprint   변환된 **의미**가 바뀌었는가        → 변환이 멱등인가
    binding_fingerprint    물리 바인딩/버전이 바뀌었는가       → 같은 의미가 다른 SQL 이 되는가

예: 카탈로그가 주문 테이블 이름을 바꾸면 **의미는 그대로**인데 SQL 은 달라진다. 지문이 하나면
"의미가 바뀌었다"로 보고돼 cut-over 가 막히거나, 반대로 아무 신호도 못 낸다. 세 축이면
"semantic 동일 · binding 변경"이라는 정확한 문장이 나오고, 그것이 divergence 분류
(EXPECTED_BINDING_CHANGE)의 입력이 된다.

정규화 규칙(세 지문 공통):

* 키 순서에 영향받지 않는다(``sort_keys``).
* 실행별 값(timestamp, converted_at, 노드 id)은 들어가지 않는다.
* 비의미 metadata(라벨·표시 문자열·렌더 파생값)는 호출자가 선언한 목록으로 뺀다.
* 같은 입력을 반복 변환하면 같은 지문이 나온다.

순수 모듈 규약: graph_rag 도 도메인 모듈도 import 하지 않는다. IR 노드는 ``to_dict()`` 산출물로만
받는다 — 지문 계층이 IR 타입을 알면 IR 이 바뀔 때마다 지문 규칙이 따라 바뀐다.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

# Event IR 의 유일한 출처(provenance) 필드. 의미 지문에서 제외한다 — 같은 뜻을 다른 문장에서 읽으면
# 반드시 달라지는 값이고, 그 차이는 "조건이 달라졌다"가 아니다.
IR_PROVENANCE_KEYS: frozenset[str] = frozenset({"evidence"})

# ``AbsoluteInterval.to_dict`` 가 기존 달력 슬롯 소비자를 위해 함께 싣는 **파생 표기**.
# 캐노니컬은 start/end_exclusive 뿐이고 from/to 는 그 함수다 — 지문에 두 표기를 다 넣으면
# 같은 사실을 두 번 세게 된다.
_DERIVED_INTERVAL_KEYS: frozenset[str] = frozenset({"from", "to"})


def canonical_json(value: Any) -> str:
    """정규 직렬화. 키 정렬 + 공백 없음 + 유니코드 보존(한글 값이 이스케이프로 갈리지 않게)."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    """정규 직렬화의 SHA-256(모든 지문의 공통 마지막 단계)."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def is_empty(value: Any) -> bool:
    """조건을 담고 있지 않은 값(스켈레톤 기본값). ``0``·``False`` 는 의미이므로 비지 않았다."""
    return value is None or (isinstance(value, (str, list, tuple, dict, set)) and len(value) == 0)


def canonical_legacy_payload(
    payload: Any,
    *,
    non_semantic_keys: Iterable[str] = (),
    drop_empty: bool = True,
) -> Any:
    """legacy payload 의 **의미 정규형**. 비의미 키와 빈 슬롯을 걷어낸다(입력은 변형하지 않는다).

    빈 슬롯을 빼는 이유: 이 저장소의 플랜 스켈레톤은 모든 슬롯을 ``None``/``[]`` 로 미리 깔아 둔다
    (`_empty_query_plan`). 그 기본값이 지문에 들어가면 "슬롯 하나가 스켈레톤에 추가됐다"는 사실만으로
    **모든 자산이 한꺼번에 stale** 이 되고, 그때 stale 은 신호가 아니라 잡음이 된다.
    """
    excluded = {str(key) for key in non_semantic_keys}
    return _canonical(payload, excluded, drop_empty)


def _canonical(value: Any, excluded: set[str], drop_empty: bool) -> Any:
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for key, child in value.items():
            name = str(key)
            if name in excluded or name.startswith("_"):
                continue
            if drop_empty and is_empty(child):
                continue
            out[name] = _canonical(child, excluded, drop_empty)
        return out
    if isinstance(value, (list, tuple)):
        return [_canonical(item, excluded, drop_empty) for item in value]
    return value


def compute_source_fingerprint(
    payload: Mapping[str, Any], *, non_semantic_keys: Iterable[str] = ()
) -> str:
    """정규화된 원본 legacy payload 의 해시. 이 값이 변하면 기존 변환은 stale 이다."""
    return digest(canonical_legacy_payload(payload, non_semantic_keys=non_semantic_keys))


def canonical_expression_payload(expression: Any) -> Any:
    """Event IR 직렬화의 **의미 정규형**.

    범용 ``strip_provenance`` 를 쓰지 않는 이유가 있다. 그 함수는 이름이 ``source`` 인 키를
    전부 출처로 보고 지우는데, Event IR 의 ``EventReference`` 는 **어느 사건인가**를 바로 그
    이름의 키로 싣는다(``{"type":"event_reference","source":"purchase",...}``). 그래서 그 정규형을
    쓰면 서로 다른 사건 사이의 시간 관계가 같은 지문을 갖는다 — 의미 지문이 의미를 못 가른다.
    여기서는 IR 이 실제로 갖는 출처 필드(``evidence``)만 뺀다.
    """
    return _canonical_expression(expression)


def _canonical_expression(value: Any) -> Any:
    if isinstance(value, Mapping):
        is_interval = value.get("type") == "interval"
        return {
            str(key): _canonical_expression(child)
            for key, child in value.items()
            if str(key) not in IR_PROVENANCE_KEYS
            and not (is_interval and str(key) in _DERIVED_INTERVAL_KEYS)
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_expression(item) for item in value]
    return value


def compute_semantic_fingerprint(expression: Any) -> str:
    """Event IR 의미 구조의 해시. 근거 구간·표면 문자열이 달라도 뜻이 같으면 같다."""
    return digest(canonical_expression_payload(expression))


def compute_binding_fingerprint(
    expression: Any,
    *,
    catalog_version: str,
    compiler_version: str,
    bindings: Mapping[str, Any] | None = None,
) -> str:
    """의미 + **그 의미가 닿는 물리 바인딩과 버전**의 해시.

    ``bindings`` 는 이 식이 실제로 참조하는 심볼의 물리 선언만 담는다(테이블·조인키·시각 컬럼·
    저장 포맷 등). 카탈로그 전체를 넣으면 무관한 선언 하나가 바뀔 때마다 모든 자산의 바인딩
    지문이 흔들려, 이 지문이 cut-over 차단 신호로서의 값을 잃는다.
    """
    return digest({
        "semantic": canonical_expression_payload(expression),
        "catalog_version": str(catalog_version),
        "compiler_version": str(compiler_version),
        "bindings": bindings or {},
    })


def compute_schema_checksum(payload: Any, *, non_semantic_keys: Iterable[str] = ()) -> str:
    """payload 의 **모양**(키 경로 + 값 타입)만의 해시 — 값이 아니라 스키마가 바뀌었는지 본다.

    값 지문(source_fingerprint)과 함께 저장하는 이유: 저장 스키마에 키가 하나 늘어나면 그 자산의
    의미가 그대로여도 어댑터의 경로 회계(path accounting)가 더 이상 완전하지 않다. 값만 비교하면
    그 사실이 보이지 않는다.
    """
    excluded = {str(key) for key in non_semantic_keys}
    return digest(sorted(_schema_paths(payload, "", excluded)))


def _schema_paths(value: Any, prefix: str, excluded: set[str]) -> list[str]:
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key, child in value.items():
            name = str(key)
            if name in excluded or name.startswith("_"):
                continue
            paths.extend(_schema_paths(child, f"{prefix}.{name}" if prefix else name, excluded))
        return paths or [f"{prefix}:object"]
    if isinstance(value, (list, tuple)):
        paths = []
        for item in value:
            # 배열 인덱스는 모양이 아니다 — 같은 모양의 항목이 몇 개인지로 스키마가 갈리면
            # 조건 하나를 더 담았다는 이유만으로 '스키마 변경'이 보고된다.
            paths.extend(_schema_paths(item, f"{prefix}[]", excluded))
        return paths or [f"{prefix}:array"]
    return [f"{prefix}:{type(value).__name__}"]


__all__ = [
    "IR_PROVENANCE_KEYS",
    "canonical_expression_payload",
    "canonical_json",
    "canonical_legacy_payload",
    "compute_binding_fingerprint",
    "compute_schema_checksum",
    "compute_semantic_fingerprint",
    "compute_source_fingerprint",
    "digest",
    "is_empty",
]
