from __future__ import annotations

import copy
import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


DEFAULT_CATALOG_PATH = (
    Path(__file__).resolve().parent.parent
    / "docs/data/runtime/external/external_condition_catalog.json"
)


@lru_cache(maxsize=8)
def load_catalog(path_text: str) -> dict[str, Any]:
    path = Path(path_text)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"conditions": []}
    return payload if isinstance(payload, dict) else {"conditions": []}


def configured_catalog() -> dict[str, Any]:
    path = os.getenv("EXTERNAL_CONDITION_CATALOG_PATH", str(DEFAULT_CATALOG_PATH))
    return load_catalog(path)


def _contains_any(text: str, terms: list[Any]) -> bool:
    return any(isinstance(term, str) and term and term in text for term in terms)


def _condition_span(query: str, start: int, end: int, catalog: dict[str, Any]) -> tuple[int, int]:
    tail = query[end: min(len(query), end + 24)]
    markers = [str(term) for term in catalog.get("target_context_terms", []) if isinstance(term, str)]
    marker_match = next(
        (
            match
            for marker in sorted(markers, key=len, reverse=True)
            if (match := re.search(r"\s*" + re.escape(marker) + r"(?:에|의|인|은|는|이|가)?", tail))
            and match.start() <= 10
        ),
        None,
    )
    return start, end + marker_match.end() if marker_match else end


def classify_external_conditions(
    query: str,
    *,
    catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Classify current/future external dependencies without resolving their values.

    Aliases are catalog data, while contextual gates prevent a historical
    campaign name or a product name from becoming a live dependency.  The
    classifier never emits regions or measurements.
    """

    catalog = catalog or configured_catalog()
    historical_terms = list(catalog.get("historical_context_terms", []))
    product_context_terms = list(catalog.get("product_name_context_terms", []))
    target_terms = list(catalog.get("target_context_terms", []))
    current_terms = list(catalog.get("current_context_terms", []))
    whole_audience_terms = list(catalog.get("whole_audience_terms", []))
    results: list[dict[str, Any]] = []

    for spec in catalog.get("conditions", []):
        if not isinstance(spec, dict):
            continue
        aliases = sorted(
            (alias for alias in spec.get("aliases", []) if isinstance(alias, str) and alias),
            key=len,
            reverse=True,
        )
        for alias in aliases:
            for match in re.finditer(re.escape(alias), query, flags=re.IGNORECASE):
                before = query[max(0, match.start() - 16):match.start()]
                after = query[match.end():min(len(query), match.end() + 28)]
                local = before + query[match.start():match.end()] + after
                if _contains_any(before, historical_terms):
                    continue
                if _contains_any(before, product_context_terms):
                    continue
                if re.search(r"(?:지난해|작년|과거|이전|지난)\s*$", before):
                    continue
                explicit_target = _contains_any(after[:20], target_terms)
                current_context = _contains_any(local, current_terms)
                if _contains_any(query, whole_audience_terms) and not explicit_target:
                    continue
                if not explicit_target and not current_context:
                    continue
                # "폭염 대비 상품" 같은 소재/상품 수식은 live target이 아니다.
                if re.match(r"\s*(?:대비|예방|대응)", after) and not explicit_target:
                    continue
                start, end = _condition_span(query, match.start(), match.end(), catalog)
                source_text = query[start:end]
                condition = {
                    "id": f"external-condition-{len(results) + 1}",
                    "domain": str(spec.get("domain") or "external"),
                    "condition_type": str(spec.get("condition_type") or "state"),
                    "condition_code": str(spec.get("condition_code") or "unknown"),
                    "state": str(spec.get("default_state") or "active"),
                    "target_basis": copy.deepcopy(
                        spec.get("default_target_basis")
                        or {"entity": "member", "attribute": "residence"}
                    ),
                    "resolution_status": "pending",
                    # The default common-sense resolver is intentionally
                    # non-realtime.  Preserve whether the source explicitly
                    # requested a current/provider-backed snapshot so it can
                    # fail closed instead of silently weakening that meaning.
                    "freshness_requirement": (
                        "live"
                        if current_context
                        else "general_knowledge_non_realtime"
                    ),
                    "source_text": source_text,
                    "source_span": {"start": start, "end": end},
                }
                signature = (
                    condition["domain"],
                    condition["condition_type"],
                    condition["condition_code"],
                    condition["state"],
                )
                if not any(
                    (item["domain"], item["condition_type"], item["condition_code"], item["state"])
                    == signature
                    for item in results
                ):
                    results.append(condition)
                break
            else:
                continue
            break
    return results


# ``mask_external_condition_spans`` 는 2026-08-06 삭제됐다. 분류기가 소유한 구간을 공백으로
# 가려 다른 파서가 그 낱말을 다시 집지 않게 하려던 함수인데, 부르는 곳이 한 번도 없었다
# (외부 조건 산출은 LLM 플랜의 ``external_conditions`` 가 소유한다).
