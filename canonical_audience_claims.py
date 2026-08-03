"""Fail-close source-claim coverage for canonical audience Event IR.

The structuring model proposes a fixed algebra tree; it does not own literal
values or permission to omit a source operator.  This module compares the
application-extracted literals and immutable semantic obligations with that
tree before it can become executable SQL.  It contains no campaign/query
template and recognizes business vocabulary only through the semantic catalog.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

import event_ir
import lexicon_patterns
import semantic_domain_binding
import semantic_plan
import semantic_receipts
import semantic_requirements


def _walk(value: Any) -> Iterable[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk(child)


def _semantic_tokens(atom: event_ir.Condition) -> list[tuple[tuple[Any, ...], str, Any]]:
    """Return uniquely-addressed literals/windows/operators from one atom."""
    root = atom.to_dict()
    tokens: list[tuple[tuple[Any, ...], str, Any]] = []

    def visit(value: Any, path: tuple[Any, ...] = ()) -> None:
        if isinstance(value, Mapping):
            node_type = value.get("type")
            if node_type == "literal" and isinstance(value.get("value"), (int, float)):
                tokens.append((path, "number", value["value"]))
            elif node_type == "limit" and isinstance(value.get("count"), int):
                tokens.append((path, "number", value["count"]))
            elif node_type in {"rolling", "relative", "duration"} and isinstance(
                value.get("value"), int
            ):
                tokens.append((path, "number", value["value"]))
            elif node_type == "interval":
                tokens.append((path, "date_window", (value.get("from"), value.get("to"))))
            elif node_type == "comparison" and isinstance(value.get("operator"), str):
                tokens.append((path, "comparison_operator", value["operator"]))
            for key, child in value.items():
                if key != "evidence":
                    visit(child, (*path, key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                visit(child, (*path, index))

    visit(root)
    return tokens


def _binding_target(binding: Mapping[str, Any]) -> tuple[str, Any] | None:
    kind = binding.get("kind")
    normalized = binding.get("normalized")
    if kind == "date_window" and isinstance(normalized, Mapping):
        return "date_window", (normalized.get("from"), normalized.get("to"))
    if kind == "comparison_operator" and isinstance(normalized, str):
        return "comparison_operator", normalized
    if kind == "money" and isinstance(normalized, Mapping):
        return "number", normalized.get("amount")
    if kind in {"number", "number_with_unit", "duration", "percentage"}:
        if isinstance(normalized, Mapping):
            return "number", normalized.get("value")
        return "number", normalized
    return None


def _binding_evidence(binding: Mapping[str, Any], query: str) -> dict[str, Any]:
    start, end = binding.get("start"), binding.get("end")
    if (
        isinstance(start, int) and not isinstance(start, bool)
        and isinstance(end, int) and not isinstance(end, bool)
        and 0 <= start < end <= len(query)
    ):
        return {"text": query[start:end], "start": start, "end": end}
    return {"text": query, "start": 0, "end": len(query)}


def _evidence_spans(atom: event_ir.Condition) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    for node in _walk(atom.to_dict()):
        evidence = node.get("evidence")
        if not isinstance(evidence, Mapping):
            continue
        start, end = evidence.get("start"), evidence.get("end")
        if (
            isinstance(start, int) and not isinstance(start, bool)
            and isinstance(end, int) and not isinstance(end, bool)
            and start < end
        ):
            spans.append((start, end))
    return spans


def literal_claim_issues(
    query: str,
    expression: event_ir.Condition,
    bindings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Require each application-owned literal/operator to have one IR consumer.

    Matching is evidence-scoped and one-to-one.  Thus two equal source numbers
    require two semantic occurrences; evidence offsets themselves are never
    mistaken for business numbers.
    """
    atoms = list(event_ir.iter_signed_atoms(expression))
    token_rows: list[tuple[int, tuple[Any, ...], str, Any]] = []
    for atom_index, (atom, _negated) in enumerate(atoms):
        token_rows.extend(
            (atom_index, path, kind, value)
            for path, kind, value in _semantic_tokens(atom)
        )
    consumed: set[tuple[int, tuple[Any, ...]]] = set()
    issues: list[dict[str, Any]] = []
    for index, binding in enumerate(bindings):
        target = _binding_target(binding)
        if target is None:
            continue
        target_kind, target_value = target
        start, end = binding.get("start"), binding.get("end")
        candidates: list[tuple[int, tuple[Any, ...]]] = []
        for atom_index, path, token_kind, token_value in token_rows:
            atom = atoms[atom_index][0]
            evidence_covers = any(
                evidence_start <= start and end <= evidence_end
                for evidence_start, evidence_end in _evidence_spans(atom)
            ) if isinstance(start, int) and isinstance(end, int) else False
            if (
                target_kind != "date_window"
                and not evidence_covers
            ):
                continue
            if token_kind == target_kind and token_value == target_value:
                candidates.append((atom_index, path))
        available = next((candidate for candidate in candidates if candidate not in consumed), None)
        if available is not None:
            consumed.add(available)
            continue
        evidence = _binding_evidence(binding, query)
        issues.append({
            "code": "validation_mismatch",
            "argument": f"literal_bindings[{index}]",
            "message": "원문에서 확정한 리터럴 또는 비교 연산자가 canonical expression에서 소비되지 않았습니다.",
            "evidence": evidence,
        })
    return issues


def missing_field_cause_records(
    query: str,
    issues: Iterable[Mapping[str, Any]],
    bindings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """결핍 주장 → **원인**(사용자 누락인가, 모델 누락인가).

    아키텍처가 정의한 원인 축(`user_omission`=물어봐야 함 / `model_omission`=재방출)이
    라이브 경로에서 채워지지 않아 **모든 결핍이 되묻기로 귀결**됐다. 실측된 최악의 형태:

        '누적 구매금액 상위 10% 회원을 추출해줘'
        literal_bindings = [percentage_1 '10%' → {unit: percent, value: 10}]
        semantic_ir.missing_fields = ['audience.percentage']
        → 사용자에게 "몇 퍼센트인가요?" 되묻기

    **시스템이 이미 결정론으로 뽑아 정규화까지 마친 값을 사용자에게 되묻는다.**

    판정은 두 가지만 본다. 인자 이름 → 리터럴 종류의 손 매핑은 만들지 않는다(모델의
    `argument` 는 닫힌 어휘가 아니라 그런 표가 곧 낡는다). 대신 **근거 구간**을 조인 키로 쓴다:

      1. 자리표시자('특정 브랜드')면 어떤 추출값으로도 못 채운다 → `user_omission`.
         판정 기계는 이미 있다(`semantic_domain_binding.user_omission_reason`).
      2. 주장된 근거 구간 안에 애플리케이션이 추출한 리터럴이 있으면 → `model_omission`.
         구간을 지목해 놓고 그 안의 값을 못 봤다는 뜻이므로 되묻지 말고 재방출한다.
      3. 그 밖 → `user_omission`. 맨 '최근'처럼 원문에 정말 값이 없는 경우다.

    구간 안팎을 따지는 것이 핵심이다. 구간을 보지 않으면 **다른 절의 값** 때문에 진짜 결핍이
    재방출로 새고(실측: '최근 3개월 … 최근 구매가 없는' 의 맨 '최근'), 그러면 재시도만 소모하고
    사용자는 답할 기회를 잃는다.
    """
    literal_spans: list[tuple[int, int]] = []
    for binding in bindings or ():
        if not isinstance(binding, Mapping):
            continue
        start, end = binding.get("start"), binding.get("end")
        if isinstance(start, int) and isinstance(end, int) and start < end:
            literal_spans.append((start, end))

    records: list[dict[str, Any]] = []
    for issue in issues or ():
        if not isinstance(issue, Mapping):
            continue
        if issue.get("code") not in {"missing_argument", "ambiguous_requirement"}:
            continue
        evidence = issue.get("evidence") if isinstance(issue.get("evidence"), Mapping) else {}
        span_text = str(evidence.get("text") or "")
        start, end = evidence.get("start"), evidence.get("end")
        whole_query = start == 0 and end == len(query)
        record: dict[str, Any] = {
            "field": f"audience.{issue.get('argument')}",
            "path": f"audience_requirement.{issue.get('argument')}",
            "source_span": span_text,
            "node_type": None,
            "cause": semantic_plan.CAUSE_USER_OMISSION,
        }
        omission = semantic_domain_binding.user_omission_reason(span_text)
        if omission:
            record["question"] = omission.get("question")
            record["matched"] = omission.get("matched")
        elif isinstance(start, int) and isinstance(end, int):
            covered = [
                (literal_start, literal_end)
                for literal_start, literal_end in literal_spans
                # 원문 전체를 근거로 든 주장은 구간이 아니라 '어디든'이라는 뜻이다.
                if whole_query or (start <= literal_start and literal_end <= end)
            ]
            if covered:
                record["cause"] = semantic_plan.CAUSE_MODEL_OMISSION
                record["literal_spans"] = covered
        records.append(record)
    return records


def _term_hits(query: str, terms: Iterable[Any]) -> list[tuple[int, int]]:
    folded = query.casefold()
    hits: set[tuple[int, int]] = set()
    for raw_term in terms:
        term = str(raw_term or "").strip()
        if not term:
            continue
        needle = term.casefold()
        cursor = 0
        while (start := folded.find(needle, cursor)) >= 0:
            before = folded[start - 1] if start else ""
            after_index = start + len(needle)
            after = folded[after_index] if after_index < len(folded) else ""
            if (
                (not needle[0].isascii() or not before.isalnum())
                and (not needle[-1].isascii() or not after.isalnum())
            ):
                hits.add((start, after_index))
            cursor = start + max(1, len(needle))
    return sorted(hits)


def _atom_field_names(atom: event_ir.Condition) -> set[str]:
    return {
        str(node["name"])
        for node in _nodes(atom.to_dict(), "field")
        if isinstance(node.get("name"), str)
    }


def catalog_claim_issues(
    query: str,
    expression: event_ir.Condition,
    bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Validate catalog-owned value domains and literal units.

    Vocabulary and physical codes stay in the catalog.  The only language
    operation here is generic polarity detection via the shared lexicon.
    """
    atoms = list(event_ir.iter_signed_atoms(expression))
    issues: list[dict[str, Any]] = []
    fields = catalog.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    domains = catalog.get("value_domains")
    domains = domains if isinstance(domains, Mapping) else {}

    # A unit-bearing source literal must be consumed by an atom that references
    # a field with the same declared unit.  This prevents money from being
    # attached to age/count merely because the numeric value happens to match.
    for index, binding in enumerate(bindings):
        if binding.get("kind") != "money":
            continue
        normalized = binding.get("normalized")
        expected_unit = normalized.get("currency") if isinstance(normalized, Mapping) else None
        start, end = binding.get("start"), binding.get("end")
        covered = False
        for atom, _negated in atoms:
            evidence = atom.evidence
            if not (
                isinstance(start, int) and isinstance(end, int)
                and evidence is not None
                and evidence.start <= start and end <= evidence.end
            ):
                continue
            if any(
                isinstance(fields.get(field_name), Mapping)
                and fields[field_name].get("unit") == expected_unit
                for field_name in _atom_field_names(atom)
            ):
                covered = True
                break
        if not covered:
            issues.append({
                "code": "validation_mismatch",
                "argument": f"literal_bindings[{index}].unit",
                "message": "금액 리터럴이 같은 통화 단위를 선언한 canonical 필드에 연결되지 않았습니다.",
                "evidence": _binding_evidence(binding, query),
            })

    negative_terms = lexicon_patterns.vocabulary("generic_negation")
    seen_claims: set[tuple[str, str]] = set()
    for field_id, field_declaration in fields.items():
        if not isinstance(field_declaration, Mapping):
            continue
        domain_id = field_declaration.get("value_domain")
        domain = domains.get(domain_id) if isinstance(domain_id, str) else None
        values = domain.get("values") if isinstance(domain, Mapping) else None
        if not isinstance(values, Mapping):
            continue
        for canonical, value_declaration in values.items():
            aliases = (
                value_declaration.get("aliases")
                if isinstance(value_declaration, Mapping) else []
            )
            hits = _term_hits(query, [canonical, *(aliases if isinstance(aliases, list) else [])])
            if not hits or (str(field_id), str(canonical)) in seen_claims:
                continue
            seen_claims.add((str(field_id), str(canonical)))
            # Longest alias gives the most useful evidence while still keeping
            # the comparison value claim singular.
            start, end = max(hits, key=lambda hit: (hit[1] - hit[0], -hit[0]))
            local = query[max(0, start - 8):min(len(query), end + 18)].casefold()
            expected_negative = any(term.casefold() in local for term in negative_terms)
            matched = False
            for atom, negated in atoms:
                evidence = atom.evidence
                if evidence is None or not (evidence.start <= start and end <= evidence.end):
                    continue
                for comparison in _nodes(atom.to_dict(), "comparison"):
                    left, right = comparison.get("left"), comparison.get("right")
                    pairs = ((left, right), (right, left))
                    if any(
                        _field_name(field) == field_id
                        and isinstance(literal, Mapping)
                        and literal.get("type") == "literal"
                        and literal.get("value") == canonical
                        for field, literal in pairs
                    ) and comparison.get("operator") == "=" and negated == expected_negative:
                        matched = True
                        break
                if matched:
                    break
            if not matched:
                issues.append({
                    "code": "validation_mismatch",
                    "argument": f"catalog_value.{field_id}",
                    "message": "원문의 카탈로그 값과 포함/제외 극성이 canonical expression에 보존되지 않았습니다.",
                    "evidence": {"text": query[start:end], "start": start, "end": end},
                })
    return issues


def _nodes(value: Any, node_type: str) -> list[Mapping[str, Any]]:
    return [node for node in _walk(value) if node.get("type") == node_type]


def _has_source(value: Any, source: str | None, correlation: str) -> bool:
    if not source:
        return True
    return any(
        node.get("name") == source
        and str(node.get("correlation") or "subject") == correlation
        for node in _nodes(value, "source")
    )


def _field_name(value: Any) -> str | None:
    return (
        str(value.get("name"))
        if isinstance(value, Mapping)
        and value.get("type") == "field"
        and isinstance(value.get("name"), str)
        else None
    )


def _window_matches(value: Any, expected: Mapping[str, Any] | None) -> bool:
    if not expected:
        return True
    pair = (expected.get("from"), expected.get("to"))
    return any(
        (node.get("from"), node.get("to")) == pair
        for node in _nodes(value, "interval")
    )


def _ranked_membership_matches(expression: event_ir.Condition, value: Mapping[str, Any]) -> bool:
    payload = expression.to_dict()
    expected_limit = value.get("limit")
    expected_direction = "desc" if value.get("direction") == "top" else "asc"
    expected_source = value.get("source") if isinstance(value.get("source"), str) else None
    expected_entity = (
        value.get("entity_field") if isinstance(value.get("entity_field"), str) else None
    )
    expected_function = (
        value.get("measure_function")
        if isinstance(value.get("measure_function"), str) else None
    )
    expected_measure_field = (
        value.get("measure_field") if isinstance(value.get("measure_field"), str) else None
    )
    expected_distinct = bool(value.get("measure_distinct", False))
    expected_window = value.get("time_window")
    expected_window = expected_window if isinstance(expected_window, Mapping) else None

    for join in _nodes(payload, "join"):
        if join.get("kind", "inner") != "semi":
            continue
        left, right = join.get("left"), join.get("right")
        if not _has_source(left, expected_source, "subject"):
            continue
        if not _has_source(right, expected_source, "none"):
            continue
        if expected_entity:
            on = join.get("on")
            if not (
                isinstance(on, Mapping)
                and on.get("type") == "comparison"
                and on.get("operator") == "="
                and _field_name(on.get("left")) == expected_entity
                and _field_name(on.get("right")) == expected_entity
            ):
                continue
        for limit in _nodes(right, "limit"):
            if limit.get("count") != expected_limit:
                continue
            ranked_input = limit.get("relation")
            for order in _nodes(ranked_input, "order"):
                summarized_input = order.get("relation")
                for summary in _nodes(summarized_input, "summarize"):
                    measure_name: str | None = None
                    for measure in summary.get("measures") or []:
                        if not isinstance(measure, Mapping):
                            continue
                        if expected_function and measure.get("function") != expected_function:
                            continue
                        if bool(measure.get("distinct", False)) != expected_distinct:
                            continue
                        if expected_measure_field and _field_name(measure.get("expression")) != expected_measure_field:
                            continue
                        measure_name = str(measure.get("name") or "") or None
                        break
                    if measure_name is None:
                        continue
                    if not any(
                        isinstance(key, Mapping)
                        and key.get("name") == measure_name
                        and key.get("direction", "asc") == expected_direction
                        for key in order.get("keys") or []
                    ):
                        continue
                    if expected_entity and not any(
                        isinstance(key, Mapping)
                        and _field_name(key.get("expression")) == expected_entity
                        for key in summary.get("keys") or []
                    ):
                        continue
                    if not _window_matches(summary, expected_window):
                        continue
                    return True
    return False


def semantic_obligation_issues(
    query: str,
    expression: event_ir.Condition,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for requirement in semantic_requirements.capture_source_semantic_obligations(query):
        kind = str(requirement.base.get("name") or "")
        value = requirement.value if isinstance(requirement.value, Mapping) else {}
        if kind == "ranked_entity_set" and _ranked_membership_matches(expression, value):
            continue
        span = requirement.source_span
        start = span.get("start") if isinstance(span, Mapping) else None
        end = span.get("end") if isinstance(span, Mapping) else None
        if not isinstance(start, int) or not isinstance(end, int) or not (0 <= start < end <= len(query)):
            start, end = 0, len(query)
        expected = ", ".join(
            f"{key}={value.get(key)}"
            for key in (
                "source", "entity_field", "measure_function", "measure_field",
                "measure_distinct", "direction", "limit", "time_window",
            )
            if value.get(key) is not None
        )
        if kind == "ranked_entity_set":
            expected = (
                "expression=Exists(semi Join), member_source_correlation=subject"
                "(omit correlation key), rank_source_correlation=none, "
                + expected
            )
        issues.append({
            "code": "validation_mismatch",
            "argument": f"source_semantics.{kind or 'unknown'}",
            "message": (
                "원문의 조합 의미를 보존하는 canonical 연산 구조가 누락되었거나 검증되지 않았습니다."
                + (f" 기대 계약: {expected}" if expected else "")
            ),
            "evidence": {"text": query[start:end], "start": start, "end": end},
        })
    return issues


def _expected_window_types(
    bindings: Iterable[Mapping[str, Any]],
) -> dict[tuple[int, str], set[str]]:
    """앱이 판정한 기간 표현 → 그 뜻을 담는 창 타입 후보. 값·단위가 조인 키다.

    한 (값, 단위)에 종류가 둘 이상 모이면 귀속할 수 없다는 뜻이다 — 호출자가 그때 물러선다.
    """
    expected: dict[tuple[int, str], set[str]] = {}
    for binding in bindings:
        if not isinstance(binding, Mapping) or binding.get("kind") != "duration":
            continue
        normalized = binding.get("normalized")
        if not isinstance(normalized, Mapping):
            continue
        window_type = event_ir.CALENDAR_KIND_WINDOW_TYPES.get(
            str(normalized.get("temporal_kind") or "")
        )
        unit = event_ir.canonical_unit(normalized.get("semantic_unit"))
        value = normalized.get("value")
        if window_type is None or unit is None or not isinstance(value, int) or isinstance(value, bool):
            continue
        expected.setdefault((value, unit), set()).add(window_type)
    return expected


def _window_nodes(root: Any) -> Iterable[MutableMapping[str, Any]]:
    for node in _walk(root):
        if node.get("type") in {"rolling", "relative"} and isinstance(node, MutableMapping):
            yield node


def apply_window_kinds(
    raw_expression: Any, bindings: Iterable[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """앱이 판정한 종류로 창 타입을 **맞춰 넣고**, 무엇을 고쳤는지 돌려준다(반려보다 먼저).

    종류의 소유자가 애플리케이션이면, 모델이 다른 값을 냈을 때 할 일은 되묻는 것이 아니라 소유한
    값을 쓰는 것이다 — 값(30)과 단위(days→day)는 이미 그렇게 다룬다. 반려만 두면 요청마다 재시도
    라운드를 태우고, 예산(3회)을 넘기면 옳은 조건을 만들 수 있는데도 실패로 끝난다(실측
    2026-08-03: 반려만 켠 상태로 '최근 30일 …' 3회 실행 → 성공 0회, 되묻기·미지원 3회).

    귀속할 수 없는 경우(같은 값·단위의 기간 표현이 종류까지 갈릴 때)에는 고치지 않는다 —
    그 자리는 :func:`window_kind_issues` 가 지킨다. 고칠 수 있으면 고치고, 고칠 수 없으면 막는다.
    """
    expected = _expected_window_types(bindings)
    corrections: list[dict[str, Any]] = []
    for node in _window_nodes(raw_expression):
        unit = event_ir.canonical_unit(node.get("unit"))
        value = node.get("value")
        if unit is None or not isinstance(value, int) or isinstance(value, bool):
            continue
        wanted = expected.get((value, unit)) or set()
        if len(wanted) != 1:
            continue
        expected_type = next(iter(wanted))
        was = str(node.get("type"))
        if was == expected_type:
            continue
        node["type"] = expected_type
        corrections.append(
            {"value": value, "unit": unit, "from": was, "to": expected_type}
        )
    return corrections


def window_kind_issues(
    query: str,
    expression: event_ir.Condition,
    bindings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """창의 **종류**가 애플리케이션 판정과 어긋나면 반려한다(값만 맞는 것으로는 부족하다).

    '최근 30일'과 '30일 전'의 리터럴 원자는 같다 — value=30, unit=day. 종류를 모델이 고르게
    두면 rolling 이어야 할 창이 relative 로 와서 30일 전 **하루**만 보게 되고, 값 검증·근거
    구간 검증·SQL 가드가 모두 통과하므로 성공 응답으로 나간다(실측 2026-08-03).

    판정 자체는 표면 문법의 소유자(:mod:`calendar_window`)가 이미 했고 literal binding 에
    ``temporal_kind`` 로 실려 온다. 여기서는 대조만 한다 — 표면어를 다시 읽지 않는다.
    """
    bindings = list(bindings)
    expected = _expected_window_types(bindings)
    evidence_by_key: dict[tuple[int, str], dict[str, Any]] = {}
    for binding in bindings:
        if not isinstance(binding, Mapping) or binding.get("kind") != "duration":
            continue
        normalized = binding.get("normalized")
        if not isinstance(normalized, Mapping):
            continue
        unit = event_ir.canonical_unit(normalized.get("semantic_unit"))
        value = normalized.get("value")
        if unit is None or not isinstance(value, int) or isinstance(value, bool):
            continue
        evidence_by_key.setdefault((value, unit), _binding_evidence(binding, query))

    issues: list[dict[str, Any]] = []
    reported: set[tuple[int, str]] = set()
    for node in _walk(expression.to_dict()):
        node_type = node.get("type")
        if node_type not in {"rolling", "relative"}:
            continue
        unit = event_ir.canonical_unit(node.get("unit"))
        value = node.get("value")
        if unit is None or not isinstance(value, int) or isinstance(value, bool):
            continue
        wanted = expected.get((value, unit)) or set()
        # 종류가 하나로 확정된 경우에만 대조한다. 같은 값의 기간 표현이 둘 이상이고 서로 종류가
        # 다르면 어느 쪽이 이 창인지 여기서는 알 수 없다 — 억지 귀속은 멀쩡한 플랜을 반려한다.
        if len(wanted) != 1 or node_type in wanted or (value, unit) in reported:
            continue
        reported.add((value, unit))
        expected_type = next(iter(wanted))
        issues.append({
            "code": "validation_mismatch",
            "argument": "period",
            "message": (
                f"기간 표현의 종류는 애플리케이션이 판정합니다: {value}{unit} 창은 "
                f"'{expected_type}' 인데 expression 은 '{node_type}' 로 왔습니다. "
                "literal_bindings.normalized.temporal_kind 를 그대로 따르세요."
            ),
            "evidence": evidence_by_key.get((value, unit))
            or {"text": query, "start": 0, "end": len(query)},
        })
    return issues


def canonical_claim_issues(
    query: str,
    expression: event_ir.Condition,
    literal_bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    bindings = list(literal_bindings)
    return [
        *literal_claim_issues(query, expression, bindings),
        *window_kind_issues(query, expression, bindings),
        *(catalog_claim_issues(query, expression, bindings, catalog) if catalog is not None else []),
        *semantic_obligation_issues(query, expression),
    ]


def ranked_obligation_is_compiled(
    expression: event_ir.Condition,
    requirement_value: Mapping[str, Any],
) -> bool:
    """Public receipt predicate used by the graph-level immutable ledger."""
    return _ranked_membership_matches(expression, requirement_value)


def _issue_is_superseded_by_another_compiler(
    issue: Mapping[str, Any], plan: Mapping[str, Any], query: str
) -> bool:
    """다른 컴파일러가 이미 그 구절을 실행 IR 로 만들었으면 LLM 의 미지원 신고는 stale 이다.

    `audience_requirement.issues` 는 **Event IR 대수로 표현할 수 있는가**에 대한 LLM 의 보고다.
    Event IR 이 표현하지 못하는 축(등급/상태 시점·이력)은 semantic_plan 노드가 다른 컴파일러로
    가고, 그쪽이 성공하면 같은 구절에 대한 "표현할 수 없다"는 더 이상 참이 아니다.

    이 회수가 없으면 판정 계층에서 미지원을 강등해도 소용이 없다 — 같은 신고가
    `unresolved_source_conditions`(차단 채널)로 다시 들어와 SQL 생성 경로를 전부 닫는다
    (실측 2026-08-02: 이력 연산이 resolved 인데 query_plan_required_conditions_missing).

    귀속 판정은 **근거 스팬 겹침**으로만 한다 — 어휘 추정으로 걷으면 다른 절의 진짜 결핍까지
    삼킨다(동시구매 sweep 의 교훈).
    """
    import member_attribute_history  # 지연 import(순환 없음)

    evidence = issue.get("evidence")
    if not isinstance(evidence, Mapping):
        return False
    start, end = evidence.get("start"), evidence.get("end")
    if not (isinstance(start, int) and isinstance(end, int) and start <= end):
        return False
    return member_attribute_history.row_owned_by_compiled_operation(
        {"source_span": {"start": start, "end": end}}, plan
    )


def refresh_canonical_unresolved(
    query: str,
    plan: dict[str, Any],
    expression: event_ir.Condition | None,
    catalog: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Refresh graph-level canonical coverage and immutable receipts."""
    requirement = plan.get("audience_requirement")
    issues: list[dict[str, Any]] = []
    if expression is not None:
        semantic_requirements.discharge_source_semantic_obligations(
            plan,
            query,
            kinds={"ranked_entity_set"},
            status="compiled",
            compiler="canonical_event_ir",
            evidence=expression.to_dict(),
            value_filter=lambda _kind, value: (
                isinstance(value, Mapping)
                and ranked_obligation_is_compiled(expression, value)
            ),
        )
        bindings = plan.get("literal_bindings")
        if isinstance(bindings, list):
            issues.extend(canonical_claim_issues(query, expression, bindings, catalog))
    elif isinstance(requirement, Mapping):
        issues.extend(
            issue for issue in (requirement.get("issues") or [])
            if isinstance(issue, dict)
            and not _issue_is_superseded_by_another_compiler(issue, plan, query)
        )

    unresolved = [
        {
            "id": "usr_" + hashlib.sha256(
                f"{query}\0{issue.get('argument')}\0{issue.get('code')}".encode("utf-8")
            ).hexdigest()[:16],
            "path": f"source_coverage.{issue.get('argument') or 'canonical_audience'}",
            "label": str(
                (issue.get("evidence") or {}).get("text")
                if isinstance(issue.get("evidence"), dict)
                else issue.get("argument") or "canonical audience"
            ),
            "source_text": query,
            # 미지원 신고의 문장은 모델이 쓴 산문이다(실측 30/30, 그중 다수가 틀린 판정이었다).
            # 그것이 그대로 사용자 화면에 도달하던 경로를 여기서 닫는다 — 표현 가능성 판정은
            # 실행 자산을 아는 애플리케이션의 몫이므로, 모델 문장은 사유가 될 수 없다.
            "reason": (
                "요청한 조건이 canonical 실행 의미로 확정되지 않았습니다."
                if str(issue.get("code")) == "unsupported_semantics"
                else str(issue.get("message") or "원문 조건의 canonical 실행 의미가 검증되지 않았습니다.")
            ),
            "code": str(issue.get("code") or "validation_mismatch"),
            "status": "unresolved",
            "source": "canonical_audience_contract",
        }
        for issue in issues
    ]
    known = {str(item.get("id") or "") for item in unresolved}
    unresolved.extend(
        item
        for item in semantic_requirements.unresolved_semantic_obligations(plan, query)
        if str(item.get("id") or "") not in known
    )
    # canonical 표현이 섰다고 해서 **다른 축의 노드**까지 귀결된 것은 아니다. 영수증 없는 노드는
    # 여기서 미해결로 남아 SQL 출고를 막는다 — 그러지 않으면 그 절이 빠진 SQL 이 성공으로 나간다.
    known.update(str(item.get("id") or "") for item in unresolved)
    unresolved.extend(
        item
        for item in semantic_receipts.unreceipted_nodes(plan, query)
        if str(item.get("id") or "") not in known
    )
    plan["unresolved_source_conditions"] = unresolved
    return unresolved


def discharge_legacy_ranked_obligations(
    plan: dict[str, Any], query: str, node: Mapping[str, Any]
) -> None:
    """Issue a one-way receipt for a capability-validated persisted legacy slot."""

    def matches(_kind: str, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        pairs = {
            "direction": "direction",
            "limit": "limit",
            "entity_domain": "entity",
            "measure": "measure",
            "membership_relation": "relation",
        }
        if any(
            value.get(claim_key) is not None
            and node.get(slot_key) != value.get(claim_key)
            for claim_key, slot_key in pairs.items()
        ):
            return False
        expected_window = value.get("time_window")
        actual_window = node.get("window")
        return not isinstance(expected_window, Mapping) or (
            isinstance(actual_window, Mapping)
            and actual_window.get("from") == expected_window.get("from")
            and actual_window.get("to") == expected_window.get("to")
        )

    semantic_requirements.discharge_source_semantic_obligations(
        plan,
        query,
        kinds={"ranked_entity_set"},
        status="compiled",
        compiler="legacy_entity_set_adapter",
        evidence=dict(node),
        value_filter=matches,
    )


__all__ = [
    "canonical_claim_issues",
    "catalog_claim_issues",
    "discharge_legacy_ranked_obligations",
    "literal_claim_issues",
    "ranked_obligation_is_compiled",
    "refresh_canonical_unresolved",
    "semantic_obligation_issues",
]
