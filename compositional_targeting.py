"""속성 카탈로그 로더(등급/상태 값 사전 + 물리 바인딩 선언의 런타임 조인).

2026-08-05 이행에서 이 모듈의 **실행 절반**(슬롯 리졸버 `resolve_operation` /
`resolve_slot_to_operations`, 스냅샷·창 SQL 컴파일러 `compile_sql`, 검증 어휘
`validation_terms`, 연산자 집합, 슬롯 어휘 파생 `slot_vocab`)이 삭제됐다 — 등급/상태
이력·전이 축(축1)이 폐기돼 그 슬롯을 만드는 생산자도, SQL 을 내는 소비자도 없다.

**남는 것은 카탈로그 로더뿐이고, 이것은 이력 축의 자산이 아니다.**
`targeting_domain.attribute_catalog()` 이 지연 import 로 이 로더를 호출해 등급·상태의
**축 표면형과 값 표면형**을 파생하고(`attribute_axis_terms` / `attribute_value_terms`),
그 어휘를 시간 한정어 감지가 쓴다. 그 호출은 `except (OSError, ValueError)` 로 빈 카탈로그를
돌려주므로, 로더를 함께 지우면 조건이 **조용히** 사라진다(예외조차 보이지 않는다).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

DEFAULT_ATTRIBUTE_CATALOG_PATH = (
    Path(__file__).resolve().parent
    / "docs" / "data" / "runtime" / "semantics" / "attribute_catalog.json"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── 속성 카탈로그: 물리 바인딩은 JSON, 값 사전은 eq_filters 참조 조인(이중 소유 금지) ──────────

# 값 사전(eq_filters synonyms)에 없는 결정론 감지 전용 표면형. 값 소유권은 여전히 eq_filters 이며,
# 여기는 "정상인/휴면이던"처럼 수식형으로 등장하는 낱말만 보탠다(파싱 규칙은 JSON 아닌 소스 소유).
_DETECTOR_EXTRA_VALUE_TOKENS: dict[str, list[str]] = {
    "normal_member": ["정상"],
    "dormant": ["휴면"],
}


def load_attribute_catalog(
    eq_filter_entries: list[Mapping[str, Any]],
    path: Path | str = DEFAULT_ATTRIBUTE_CATALOG_PATH,
) -> dict[str, Any]:
    """attribute_catalog.json 과 eq_filters 값 사전을 조인한 런타임 카탈로그를 만든다.

    반환: {"attributes": {attribute_id: {label, binding, snapshot_months_available,
    history_unsupported_reason?, values: {canonical: {value, rank, synonyms}}, surface_terms}}}
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    attributes_raw = raw.get("attributes")
    if not isinstance(attributes_raw, Mapping) or not attributes_raw:
        raise ValueError("attribute catalog must declare a non-empty attributes mapping")
    by_category: dict[str, dict[str, dict[str, Any]]] = {}
    for entry in eq_filter_entries or []:
        if not isinstance(entry, Mapping):
            continue
        category = str(entry.get("category") or "")
        canonical = str(entry.get("canonical") or "")
        value = str(entry.get("value") or "")
        if not category or not canonical or not value:
            continue
        by_category.setdefault(category, {})[canonical] = {
            "value": value,
            "rank": entry.get("rank"),
            "synonyms": [str(s) for s in entry.get("synonyms") or [] if str(s).strip()],
        }
    attributes: dict[str, Any] = {}
    for attribute_id, spec in attributes_raw.items():
        if not isinstance(spec, Mapping):
            continue
        binding = spec.get("binding") if isinstance(spec.get("binding"), Mapping) else None
        if binding is not None:
            for key in ("table", "entity_key", "time_column", "value_column"):
                if not _IDENTIFIER_RE.fullmatch(str(binding.get(key) or "")):
                    raise ValueError(
                        f"attribute {attribute_id!r} binding.{key} is not a safe identifier"
                    )
            prev_column = binding.get("prev_value_column")
            if prev_column is not None and not _IDENTIFIER_RE.fullmatch(str(prev_column)):
                raise ValueError(
                    f"attribute {attribute_id!r} binding.prev_value_column is not a safe identifier"
                )
        attributes[str(attribute_id)] = {
            "id": str(attribute_id),
            "label": str(spec.get("label") or attribute_id),
            "binding": dict(binding) if binding is not None else None,
            "snapshot_months_available": int(spec.get("snapshot_months_available") or 0),
            "history_unsupported_reason": spec.get("history_unsupported_reason"),
            "values": by_category.get(str(spec.get("value_category") or ""), {}),
            "surface_terms": [str(t) for t in spec.get("surface_terms") or []],
        }
    return {"attributes": attributes}
