"""Fail-close source-claim coverage for canonical audience Event IR.

The structuring model proposes a fixed algebra tree; it does not own literal
values or permission to omit a source operator.  This module compares the
application-extracted literals and immutable semantic obligations with that
tree before it can become executable SQL.  It contains no campaign/query
template and recognizes business vocabulary only through the semantic catalog.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Iterable, Mapping, MutableMapping, Sequence
from datetime import date, timedelta
from typing import Any

import consent_cardinality
import event_ir
import lexicon_patterns
import open_text_scope_claims
import ordered_catalog_claims
import rolling_absence_claims
import semantic_domain_binding
import semantic_outcome
import semantic_requirements
import temporal_clause


_DISJUNCTION_CONNECTOR_RE = re.compile(
    r"^\s*(?:[,·/]\s*)?(?:또는|혹은|OR)(?:\s*[,·/])?\s*$",
    re.IGNORECASE,
)


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
            elif node_type == "limit" and isinstance(value.get("percent"), (int, float)):
                tokens.append((path, "percentage", value["percent"]))
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
    if kind == "percentage":
        if isinstance(normalized, Mapping):
            return "percentage", normalized.get("value")
        return "percentage", normalized
    if kind in {"number", "number_with_unit", "duration"}:
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


def _node_at_path(root: Any, path: tuple[Any, ...]) -> Mapping[str, Any] | None:
    value = root
    for part in path:
        addressable = (
            isinstance(part, int) and isinstance(value, list) and part < len(value)
        ) or (
            isinstance(part, str) and isinstance(value, Mapping) and part in value
        )
        if not addressable:
            return None
        value = value[part]
    return value if isinstance(value, Mapping) else None


def _unscoped_duration_token_matches(
    binding: Mapping[str, Any], atom: event_ir.Condition, path: tuple[Any, ...]
) -> bool:
    """Join a duration to one structurally exact nested window.

    Aggregate evidence is often the comparison phrase (``3회 이상``), while
    its time window is nested under the same atom and has no evidence field of
    its own.  Application-owned value, unit, and temporal kind can still make
    that join deterministic; callers additionally require a unique candidate.
    """
    if binding.get("kind") != "duration":
        return False
    normalized = binding.get("normalized")
    if not isinstance(normalized, Mapping):
        return False
    node = _node_at_path(atom.to_dict(), path)
    if not isinstance(node, Mapping) or node.get("type") not in {"rolling", "relative"}:
        return False
    expected_type = event_ir.CALENDAR_KIND_WINDOW_TYPES.get(
        str(normalized.get("temporal_kind") or "")
    )
    return bool(
        expected_type == node.get("type")
        and event_ir.canonical_unit(normalized.get("semantic_unit"))
        == event_ir.canonical_unit(node.get("unit"))
        and normalized.get("value") == node.get("value")
    )


def _anchored_interval_duration_candidates(
    binding: Mapping[str, Any],
    atoms: list[tuple[event_ir.Condition, bool]],
    token_rows: list[tuple[int, tuple[Any, ...], str, Any]],
    bindings: list[Mapping[str, Any]],
) -> list[tuple[int, tuple[Any, ...]]]:
    """Match one app-owned ``as_of_date + rolling duration`` interval receipt."""

    if binding.get("kind") != "duration":
        return []
    normalized = binding.get("normalized")
    if not isinstance(normalized, Mapping) or normalized.get("temporal_kind") != "rolling_duration":
        return []
    unit = event_ir.canonical_unit(normalized.get("semantic_unit"))
    value = normalized.get("value")
    if (
        unit not in event_ir.WINDOW_UNITS
        or not isinstance(value, int)
        or isinstance(value, bool)
    ):
        return []
    anchors = [
        item.get("normalized")
        for item in bindings
        if isinstance(item, Mapping) and item.get("kind") == "as_of_date"
    ]
    if len(anchors) != 1 or not isinstance(anchors[0], Mapping):
        return []
    try:
        anchor = date.fromisoformat(str(anchors[0].get("date")))
    except ValueError:
        return []
    expected_end = anchor + timedelta(days=1)
    expected_start = expected_end - timedelta(days=value * event_ir.UNIT_DAYS[unit])
    binding_start, binding_end = binding.get("start"), binding.get("end")
    scoped: list[tuple[int, tuple[Any, ...]]] = []
    structural: list[tuple[int, tuple[Any, ...]]] = []
    for atom_index, path, token_kind, _token_value in token_rows:
        if token_kind != "date_window":
            continue
        atom = atoms[atom_index][0]
        node = _node_at_path(atom.to_dict(), path)
        if not isinstance(node, Mapping) or node.get("type") != "interval":
            continue
        try:
            start = date.fromisoformat(str(node.get("start")))
            end_exclusive = date.fromisoformat(str(node.get("end_exclusive")))
        except ValueError:
            continue
        if (start, end_exclusive) != (expected_start, expected_end):
            continue
        candidate = (atom_index, path)
        evidence_covers = any(
            evidence_start <= binding_start and binding_end <= evidence_end
            for evidence_start, evidence_end in _evidence_spans(atom)
        ) if isinstance(binding_start, int) and isinstance(binding_end, int) else False
        (scoped if evidence_covers else structural).append(candidate)
    return scoped or structural


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
    binding_rows = [item for item in bindings if isinstance(item, Mapping)]
    token_rows: list[tuple[int, tuple[Any, ...], str, Any]] = []
    for atom_index, (atom, _negated) in enumerate(atoms):
        token_rows.extend(
            (atom_index, path, kind, value)
            for path, kind, value in _semantic_tokens(atom)
        )
    consumed: set[tuple[int, tuple[Any, ...]]] = set()
    issues: list[dict[str, Any]] = []
    for index, binding in enumerate(binding_rows):
        target = _binding_target(binding)
        if target is None:
            continue
        target_kind, target_value = target
        start, end = binding.get("start"), binding.get("end")
        scoped_candidates: list[tuple[int, tuple[Any, ...]]] = []
        structural_candidates: list[tuple[int, tuple[Any, ...]]] = []
        for atom_index, path, token_kind, token_value in token_rows:
            atom = atoms[atom_index][0]
            evidence_covers = any(
                evidence_start <= start and end <= evidence_end
                for evidence_start, evidence_end in _evidence_spans(atom)
            ) if isinstance(start, int) and isinstance(end, int) else False
            if token_kind != target_kind or token_value != target_value:
                continue
            candidate = (atom_index, path)
            if target_kind == "date_window" or evidence_covers:
                scoped_candidates.append(candidate)
            elif _unscoped_duration_token_matches(binding, atom, path):
                structural_candidates.append(candidate)
        available = next(
            (candidate for candidate in scoped_candidates if candidate not in consumed),
            None,
        )
        if available is None:
            structural_available = [
                candidate
                for candidate in structural_candidates
                if candidate not in consumed
            ]
            if len(structural_available) == 1:
                available = structural_available[0]
        if available is None:
            anchored_available = [
                candidate
                for candidate in _anchored_interval_duration_candidates(
                    binding, atoms, token_rows, binding_rows
                )
                if candidate not in consumed
            ]
            if len(anchored_available) == 1:
                available = anchored_available[0]
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

    인자 이름 → 리터럴 종류의 손 매핑은 만들지 않는다(모델의 `argument` 는 닫힌 어휘가
    아니라 그런 표가 곧 낡는다). 대신 **근거 구간**을 조인 키로 쓴다:

      1. 자리표시자('특정 브랜드')면 어떤 추출값으로도 못 채운다 → `user_omission`.
         판정 기계는 이미 있다(`semantic_domain_binding.user_omission_reason`).
      2. 주장된 근거 구간 안에 애플리케이션이 추출한 리터럴이 있으면 → `model_omission`.
         구간을 지목해 놓고 그 안의 값을 못 봤다는 뜻이므로 되묻지 말고 재방출한다.
      3. 기간(`missing_argument(period)`)은 구간 **하나**로 끝나지 않는다. '최근 30일' 은
         표면 구간 둘('최근'·'30일')이 시간 절 하나를 이루므로, 모델이 표지만 지목하면
         수량 구간이 신고 구간 밖에 있어 2번이 실패한다. 그래서 포함 관계가 실패했을 때
         절 판정자(:func:`temporal_clause.stated_period_for_issue`)에게 한 번 더 묻는다.
         수량화된 절이 나오면 그 신고는 **원문과 모순**이므로 → `model_omission`.
      4. 그 밖 → `user_omission`. 맨 '최근'처럼 원문에 정말 값이 없는 경우다.

    구간 안팎을 따지는 것이 핵심이다. 구간을 보지 않으면 **다른 절의 값** 때문에 진짜 결핍이
    재방출로 새고(실측: '최근 3개월 … 최근 구매가 없는' 의 뒤쪽 맨 '최근'), 그러면 재시도만
    소모하고 사용자는 답할 기회를 잃는다. 3번이 그 보호를 깨지 않는 이유는 절 판정자가
    **절 단위**로 답하기 때문이다 — 뒤쪽 '최근' 은 수량을 얻지 못한 별개의 절이라
    `is_quantified=False` 이고, 앞 절의 '3개월' 을 빌려오지 않는다.

    3번은 프롬프트별 예외가 아니다. 판정 재료는 recency 표지 어휘와 애플리케이션이 뽑은
    duration 리터럴뿐이라, 단위(일/주/개월/년)나 문장이 늘어도 여기 분기가 늘지 않는다.
    """
    # `bindings` 는 Iterable 계약이다. 아래에서 두 번 읽으므로(구간 색인 + 시간 절 판정)
    # 제너레이터가 들어오면 두 번째 소비가 조용히 빈 목록이 된다 — 한 번만 소비해 고정한다.
    binding_rows = [item for item in bindings or () if isinstance(item, Mapping)]
    literal_spans: list[tuple[int, int]] = []
    for binding in binding_rows:
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
            "cause": semantic_outcome.CAUSE_USER_OMISSION,
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
            if not covered:
                # 시간 절은 떨어진 구간 둘이 하나의 의미다. 신고 구간이 그 절의 **일부**면
                # 원문은 이미 기간을 말한 것이므로 되묻지 말고 재방출한다(실측 2026-08-06:
                # '최근 30일 구매한 회원 수를 알려줘' 가 '최근'[0,2] 만 지목당해 닫혔다).
                # 명시 기간의 단일 판정자는 temporal_clause 다 — 여기에 두 번째 판정 로직을
                # 만들지 않는다. 근거 구간은 리터럴 구간과 같은 좌표계·같은 모양으로 남겨
                # 하류(재방출 트리거·감사 로그)가 한 가지 형태만 읽게 한다.
                clause = temporal_clause.stated_period_for_issue(query, issue, binding_rows)
                if clause is not None:
                    covered = [(span.start, span.end) for span in clause.source_spans]
            if covered:
                record["cause"] = semantic_outcome.CAUSE_MODEL_OMISSION
                record["literal_spans"] = covered
        records.append(record)
    return records


def _continues_ascii_word(char: str) -> bool:
    """ASCII 낱말이 이 글자로 **이어지는가**.

    한글은 이어지지 않는다 — 'VIP로'의 '로'는 조사이지 낱말의 일부가 아니다. 예전에는
    ``isalnum()`` 만 봤는데 한글도 alnum 이라 ``VIP로``·``VIP인`` 이 통째로 히트에서 빠졌고,
    그래서 값 사전에 선언된 ``VIP`` 가 원문에 있어도 카탈로그 값 청구가 서지 않았다(실측).
    라틴 낱말 안의 부분 일치(``gold`` in ``goldman``)를 막는 원래 목적은 그대로다.
    """

    return bool(char) and char.isascii() and char.isalnum()


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
                (not needle[0].isascii() or not _continues_ascii_word(before))
                and (not needle[-1].isascii() or not _continues_ascii_word(after))
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


# `catalog_issue_owned_by_relation_node`(카탈로그 값 issue 의 relation 노드 귀속)는
# 2026-08-05 삭제됐다 — 축1(등급/상태 이력·전이) 폐기로 relation 노드의 생산자가 사라졌다.


def _reconcile_axis_scoped_claims(
    query: str, claims: Iterable[Mapping[str, Any]], catalog: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Attribute an unqualified value to a nearby catalog-declared axis.

    Korean particles commonly appear between an axis alias and its values
    (``가치등급이 골드에서 VIP``), while the value dictionary deliberately
    stores the reusable aliases ``가치등급 골드`` / ``가치등급 VIP``.  A
    longest, nearby axis cue selects the domain; value identities are still
    derived exclusively from that domain's declared aliases.

    2026-08-05 `semantic_relation_ownership` 에서 이 파일로 옮겼다. relation 노드를 읽지
    않는 **순수 카탈로그 판정**이고, canonical 레인의 `catalog_claim_issues` 가 유일한
    소비자다(옮기지 않고 지우면 '가치등급 VIP' 류가 일반 등급 청구로 잘못 갈린다).
    """
    fields = catalog.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    domains = catalog.get("value_domains")
    domains = domains if isinstance(domains, Mapping) else {}
    domain_fields: dict[str, list[str]] = {}
    domain_axis_terms: dict[str, set[str]] = {}
    cues: list[tuple[str, int, int]] = []
    for field_id, declaration in fields.items():
        if not isinstance(declaration, Mapping):
            continue
        domain_id = declaration.get("value_domain")
        if not isinstance(domain_id, str) or domain_id not in domains:
            continue
        domain_fields.setdefault(domain_id, []).append(str(field_id))
        aliases = declaration.get("aliases")
        terms = [declaration.get("label"), *(aliases if isinstance(aliases, list) else [])]
        for term in terms:
            if not isinstance(term, str) or len(term.strip()) < 2:
                continue
            domain_axis_terms.setdefault(domain_id, set()).add(
                "".join(term.split()).casefold()
            )
            cues.extend((domain_id, start, end) for start, end in _term_hits(query, [term]))
    # A specific cue (가치등급) owns a contained generic cue (등급).
    cues = [
        cue for cue in cues
        if not any(
            other[0] != cue[0] and other[1] <= cue[1] and cue[2] <= other[2]
            and other[2] - other[1] > cue[2] - cue[1]
            for other in cues
        )
    ]

    def clause_bounds(position: int) -> tuple[int, int]:
        start = max(query.rfind(token, 0, position) for token in (",", ".", ";")) + 1
        ends = [index for token in (",", ".", ";") if (index := query.find(token, position)) >= 0]
        return start, min(ends) if ends else len(query)

    def target_canonical(domain_id: str, text: str) -> str | None:
        domain = domains.get(domain_id)
        values = domain.get("values") if isinstance(domain, Mapping) else None
        if not isinstance(values, Mapping):
            return None
        surface = "".join(text.split()).casefold()
        axis_terms = domain_axis_terms.get(domain_id, set())
        composed: set[str] = set()
        matches: set[str] = set()
        for canonical, declaration in values.items():
            aliases = declaration.get("aliases") if isinstance(declaration, Mapping) else []
            terms = [canonical, *(aliases if isinstance(aliases, list) else [])]
            normalized = ["".join(str(term).split()).casefold() for term in terms]
            # 축 표면어 + 값 표면어의 **정확한 합성**('가치등급'+'vip'). 접미 일치만 보면
            # '가치등급vvip' 도 'vip' 로 끝나므로 VIP 요청이 VVIP 와 함께 모호해진다.
            if any(
                term in {axis + surface, surface + axis}
                for term in normalized
                for axis in axis_terms
            ):
                composed.add(str(canonical))
            if any(
                term == surface or term.startswith(surface) or term.endswith(surface)
                for term in normalized
            ):
                matches.add(str(canonical))
        if len(composed) == 1:
            return next(iter(composed))
        return next(iter(matches)) if len(matches) == 1 else None

    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in claims:
        claim = dict(raw)
        for start, end in claim.get("hits") or ():
            clause_start, clause_end = clause_bounds(start)
            nearby = [
                cue for cue in cues
                if clause_start <= cue[1] < clause_end
                and min(abs(start - cue[2]), abs(cue[1] - end)) <= 32
            ]
            cue = min(
                nearby,
                key=lambda item: (
                    min(abs(start - item[2]), abs(item[1] - end)),
                    -(item[2] - item[1]),
                ),
                default=None,
            )
            domain_id, canonical = str(claim.get("domain")), str(claim.get("canonical"))
            if cue is not None and cue[0] != domain_id:
                redirected = target_canonical(cue[0], query[start:end])
                if redirected is not None:
                    domain_id, canonical = cue[0], redirected
            key = domain_id, canonical
            row = merged.setdefault(key, {
                "domain": domain_id,
                "canonical": canonical,
                "fields": domain_fields.get(domain_id, list(claim.get("fields") or ())),
                "hits": [],
            })
            if (start, end) not in row["hits"]:
                row["hits"].append((start, end))
    return list(merged.values())


def _normalized_open_text(value: str) -> str:
    """Return the conservative normalization allowed for source grounding."""
    return unicodedata.normalize("NFKC", value).casefold()


def _open_text_literal_issues(
    query: str,
    expression: event_ir.Condition,
    fields: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Ground catalog-declared ``contains`` literals in their source evidence.

    Open text has no closed value domain that can correct a hallucinated model
    value.  The catalog therefore opts a field into this check with
    ``match_mode=contains``; no product vocabulary is inferred here.  A literal
    must occur in the comparison's exact source span after only Unicode
    compatibility normalization and case folding.
    """
    contains_fields = {
        str(field_id)
        for field_id, declaration in fields.items()
        if isinstance(declaration, Mapping)
        and str(declaration.get("match_mode") or "").casefold() == "contains"
    }
    if not contains_fields:
        return []

    issues: list[dict[str, Any]] = []
    for comparison in _nodes(expression.to_dict(), "comparison"):
        left, right = comparison.get("left"), comparison.get("right")
        for field, literal in ((left, right), (right, left)):
            field_name = _field_name(field)
            if (
                field_name not in contains_fields
                or not isinstance(literal, Mapping)
                or literal.get("type") != "literal"
                or not isinstance(literal.get("value"), str)
            ):
                continue

            value = str(literal["value"])
            evidence = comparison.get("evidence")
            evidence = evidence if isinstance(evidence, Mapping) else {}
            start, end = evidence.get("start"), evidence.get("end")
            valid_span = (
                isinstance(start, int)
                and not isinstance(start, bool)
                and isinstance(end, int)
                and not isinstance(end, bool)
                and 0 <= start < end <= len(query)
            )
            source_span = query[start:end] if valid_span else query
            evidence_matches = valid_span and evidence.get("text") == source_span
            grounded = (
                bool(value)
                and evidence_matches
                and _normalized_open_text(value) in _normalized_open_text(source_span)
            )
            if grounded:
                continue
            issues.append({
                "code": "validation_mismatch",
                "argument": f"catalog_literal.{field_name}",
                "message": (
                    "부분 문자열 검색 리터럴이 원문의 해당 evidence 구간에 "
                    "근거하지 않습니다. Semantic Catalog의 match_mode=contains "
                    "필드는 원문에 명시된 검색어만 사용할 수 있습니다."
                ),
                "evidence": {
                    "text": source_span,
                    "start": start if valid_span else 0,
                    "end": end if valid_span else len(query),
                },
            })
    return issues


def catalog_value_claims(
    query: str,
    catalog: Mapping[str, Any],
    *,
    excluded_spans: Iterable[tuple[int, int]] = (),
) -> list[dict[str, Any]]:
    """원문에 등장한 카탈로그 값 청구 ``(domain, canonical, fields, hits)``.

    "이 문장이 어떤 선언된 값을 말했는가"의 단일 소유자다. 값 사전(별칭)에서만 정체를 얻고,
    조사가 끼어든 축 한정('가치등급이 골드에서')은 :func:`_reconcile_axis_scoped_claims` 가
    가까운 축 표지로 도메인을 고른다. 두 소비자가 각자 이 조립을 다시 쓰면 같은 문장이 서로
    다른 값 집합으로 읽히므로 함수 하나로 둔다.

    ``excluded_spans`` 는 **이미 다른 근거로 소비된** 구간이다(부재 조건의 동어반복 등).
    """

    fields = catalog.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    domains = catalog.get("value_domains")
    domains = domains if isinstance(domains, Mapping) else {}
    excluded = set(excluded_spans)

    domain_fields: dict[str, list[str]] = {}
    domain_values: dict[str, Mapping[str, Any]] = {}
    for field_id, field_declaration in fields.items():
        if not isinstance(field_declaration, Mapping):
            continue
        domain_id = field_declaration.get("value_domain")
        domain = domains.get(domain_id) if isinstance(domain_id, str) else None
        values = domain.get("values") if isinstance(domain, Mapping) else None
        if not isinstance(values, Mapping):
            continue
        domain_fields.setdefault(domain_id, []).append(str(field_id))
        domain_values[domain_id] = values

    claims: list[dict[str, Any]] = []
    for domain_id, values in domain_values.items():
        for canonical, value_declaration in values.items():
            aliases = (
                value_declaration.get("aliases")
                if isinstance(value_declaration, Mapping) else []
            )
            hits = _term_hits(query, [canonical, *(aliases if isinstance(aliases, list) else [])])
            hits = [hit for hit in hits if hit not in excluded]
            if not hits:
                continue
            claims.append({
                "domain": domain_id,
                "canonical": str(canonical),
                "fields": domain_fields[domain_id],
                "hits": hits,
            })
    return _reconcile_axis_scoped_claims(query, claims, catalog)


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
    binding_rows = list(bindings)
    issues: list[dict[str, Any]] = []
    fields = catalog.get("fields")
    fields = fields if isinstance(fields, Mapping) else {}
    domains = catalog.get("value_domains")
    domains = domains if isinstance(domains, Mapping) else {}

    issues.extend(_open_text_literal_issues(query, expression, fields))
    issues.extend(
        open_text_scope_claims.omitted_open_text_scope_issues(
            query, expression, fields
        )
    )

    # A unit-bearing source literal must be consumed by an atom that references
    # a field with the same declared unit.  This prevents money from being
    # attached to age/count merely because the numeric value happens to match.
    for index, binding in enumerate(binding_rows):
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
    absence_restatements = rolling_absence_claims.absence_restatement_spans(
        query, expression, catalog
    )
    claims = catalog_value_claims(
        query, catalog, excluded_spans=absence_restatements
    )
    issues.extend(_catalog_disjunction_issues(query, expression, claims, negative_terms))

    # A specific cross-domain alias owns an overlapping generic value hit.  For
    # example, ``가치등급 VIP`` is a worth-grade claim; the shorter ``VIP``
    # substring must not independently create a current member-grade claim.
    # Equal spans remain ambiguous and therefore both fail closed.
    all_hits = [
        (claim["domain"], start, end)
        for claim in claims
        for start, end in claim["hits"]
    ]
    for claim in claims:
        hits = [
            (start, end)
            for start, end in claim["hits"]
            if not any(
                other_domain != claim["domain"]
                and other_start < end
                and start < other_end
                and other_end - other_start > end - start
                for other_domain, other_start, other_end in all_hits
            )
        ]
        if not hits:
            continue
        # Longest alias gives the most useful evidence while still keeping
        # the comparison value claim singular.
        start, end = max(hits, key=lambda hit: (hit[1] - hit[0], -hit[0]))
        local = query[max(0, start - 8):min(len(query), end + 18)].casefold()
        expected_negative = any(term.casefold() in local for term in negative_terms)
        matched = False
        field_ids = frozenset(claim["fields"])
        domain = domains.get(claim["domain"])
        domain = domain if isinstance(domain, Mapping) else {}
        for atom, negated in atoms:
            evidence = atom.evidence
            if evidence is None or not (evidence.start <= start and end <= evidence.end):
                continue
            # A coordinated exclusion commonly places one negation marker at
            # the end of the whole list (``블랙리스트, 휴면, 임직원은 제외``).
            # The fixed +/- character window above cannot reach that marker for
            # early items.  Exact atom evidence is already source-validated, so
            # a marker inside that evidence is legitimate shared list polarity.
            evidence_text = evidence.text.casefold()
            shared_list_polarity = bool(
                evidence.end - evidence.start <= 64
                and end - start <= 32
                and not re.search(r"[.!?。\n]", evidence_text)
                and any(
                    (
                        marker := evidence_text.find(term.casefold())
                    ) >= 0
                    and marker >= end - evidence.start
                    and marker - (end - evidence.start) <= 48
                    for term in negative_terms
                )
            )
            atom_expected_negative = expected_negative or any(
                term.casefold() in evidence_text for term in negative_terms
            ) and shared_list_polarity
            for comparison in _nodes(atom.to_dict(), "comparison"):
                left, right = comparison.get("left"), comparison.get("right")
                pairs = ((left, right), (right, left))
                equality_value_matches = any(
                    _field_name(field) in field_ids
                    and isinstance(literal, Mapping)
                    and literal.get("type") == "literal"
                    and literal.get("value") == claim["canonical"]
                    for field, literal in pairs
                )
                comparison_operator = comparison.get("operator")
                equality_matches = (
                    comparison_operator in {"=", "!="}
                    and equality_value_matches
                    and (negated ^ (comparison_operator == "!="))
                    == atom_expected_negative
                )
                ordered_matches = ordered_catalog_claims.ordered_comparison_consumes_claim(
                    query,
                    comparison,
                    binding_rows,
                    field_ids=field_ids,
                    canonical=claim["canonical"],
                    domain=domain,
                    evidence_start=evidence.start,
                    evidence_end=evidence.end,
                    negated=negated,
                    expected_negative=atom_expected_negative,
                )
                if equality_matches or ordered_matches:
                    matched = True
                    break
            if matched:
                break
        if not matched:
            issues.append({
                "code": "validation_mismatch",
                "argument": f"catalog_value.{claim['fields'][0]}",
                "message": "원문의 카탈로그 값과 포함/제외 극성이 canonical expression에 보존되지 않았습니다.",
                "evidence": {"text": query[start:end], "start": start, "end": end},
            })
    return issues


def _catalog_disjunction_issues(
    query: str,
    expression: event_ir.Condition,
    claims: list[dict[str, Any]],
    negative_terms: Iterable[str],
) -> list[dict[str, Any]]:
    """Prove that catalog values joined by an explicit OR share an IR Or ancestor."""

    atom_rows: list[
        tuple[event_ir.Condition, bool, tuple[tuple[int, str], ...]]
    ] = []

    def visit(
        node: event_ir.Condition,
        negated: bool = False,
        path: tuple[tuple[int, str], ...] = (),
    ) -> None:
        if isinstance(node, event_ir.Not):
            visit(node.operand, not negated, path)
            return
        if isinstance(node, (event_ir.And, event_ir.Or)):
            branch_path = (*path, (id(node), node.type))
            for operand in node.operands:
                visit(operand, negated, branch_path)
            return
        atom_rows.append((node, negated, path))

    visit(expression)

    def paths_for(claim: Mapping[str, Any], hit: tuple[int, int]) -> list[tuple[tuple[int, str], ...]]:
        field_ids = frozenset(str(item) for item in claim.get("fields", []))
        canonical = claim.get("canonical")
        start, end = hit
        paths: list[tuple[tuple[int, str], ...]] = []
        for atom, negated, path in atom_rows:
            if not isinstance(atom, event_ir.Comparison) or atom.evidence is None:
                continue
            if not (atom.evidence.start <= start and end <= atom.evidence.end):
                continue
            pairs = ((atom.left, atom.right), (atom.right, atom.left))
            if not any(
                isinstance(field, event_ir.FieldRef)
                and field.name in field_ids
                and isinstance(literal, event_ir.Literal)
                and literal.value == canonical
                for field, literal in pairs
            ):
                continue
            if negated ^ (atom.operator == "!="):
                continue
            paths.append(path)
        return paths

    def common_boolean(paths: tuple[tuple[int, str], ...], other: tuple[tuple[int, str], ...]) -> str | None:
        shared: str | None = None
        for left, right in zip(paths, other):
            if left[0] != right[0]:
                break
            shared = left[1]
        return shared

    issues: list[dict[str, Any]] = []
    for index, left_claim in enumerate(claims):
        for right_claim in claims[index + 1 :]:
            if left_claim.get("domain") != right_claim.get("domain"):
                continue
            for left_hit in left_claim.get("hits", []):
                for right_hit in right_claim.get("hits", []):
                    first_claim, first_hit, second_claim, second_hit = (
                        (left_claim, left_hit, right_claim, right_hit)
                        if left_hit[0] <= right_hit[0]
                        else (right_claim, right_hit, left_claim, left_hit)
                    )
                    between = query[first_hit[1] : second_hit[0]]
                    if not _DISJUNCTION_CONNECTOR_RE.fullmatch(between):
                        continue
                    clause = query[first_hit[0] : second_hit[1]]
                    if any(term.casefold() in clause.casefold() for term in negative_terms):
                        continue
                    left_paths = paths_for(first_claim, first_hit)
                    right_paths = paths_for(second_claim, second_hit)
                    if any(
                        common_boolean(left_path, right_path) == "or"
                        for left_path in left_paths
                        for right_path in right_paths
                    ):
                        continue
                    issues.append({
                        "code": "validation_mismatch",
                        "argument": f"catalog_boolean.{left_claim['domain']}",
                        "message": "원문의 '또는' 값 연결이 canonical Event IR의 Or로 보존되지 않았습니다.",
                        "evidence": {
                            "text": clause,
                            "start": first_hit[0],
                            "end": second_hit[1],
                        },
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


# canonical Event IR 컴파일러가 **영수증으로 방면할 수 있는** 의무 종류. 이 집합이 곧
# "애플리케이션이 이 의미를 낼 수 있다"의 정의다 — 영수증 발행(refresh_canonical_unresolved)과
# 미지원 신고 반박(query_structurer.structurer)이 같은 답을 써야 하므로 선언은 하나뿐이다.
#
# 여기 **없는** 의무(주기 반복·외부 지정 집합·스냅샷 선택·등급 이력)는 감지는 되지만 그 뜻을
# 낼 표현이 없다. 그것을 '지원됨'으로 읽으면 없는 능력을 광고하게 되고, 모델의 옳은 미지원
# 신고까지 반박해 재시도만 반복한다.
CANONICAL_COMPILED_OBLIGATION_KINDS: frozenset[str] = frozenset({
    "ranked_entity_set",
    "member_metric_ranking",
})


def supported_obligations_for_query(
    query: str,
) -> tuple[semantic_requirements.SourceRequirement, ...]:
    """원문의 의무 중 **canonical 컴파일러가 방면할 수 있는 종류**만.

    의무는 지원 여부와 무관하게 감지의 기록으로 만들어진다. 그러므로 '애플리케이션이 이
    의미를 낼 수 있다'의 답은 의무의 존재가 아니라 그 종류다.
    """
    if not isinstance(query, str) or not query.strip():
        return ()
    return tuple(
        requirement
        for requirement in semantic_requirements.capture_source_semantic_obligations(query)
        if semantic_requirements.obligation_kind(requirement)
        in CANONICAL_COMPILED_OBLIGATION_KINDS
    )


def obligation_conflicting_with_claim(
    claim: Mapping[str, Any],
    obligations: Sequence[semantic_requirements.SourceRequirement],
) -> semantic_requirements.SourceRequirement | None:
    """'표현할 수 없다'는 신고와 **같은 원문 자리**를 차지한 지원 의무. 없으면 None.

    판정 축은 이름이 아니라 좌표다 — 모델이 그 계산을 뭐라고 부르든, 애플리케이션이 이미
    실행 계약으로 계산해 둔 구간이라면 그 주장은 스스로 반박된다. 근거 구간이 없으면 겹침을
    판정할 수 없으므로 개입하지 않는다(fail-safe).
    """
    evidence = claim.get("evidence")
    if not isinstance(evidence, Mapping):
        return None
    return next(
        (
            obligation
            for obligation in obligations
            if semantic_requirements.spans_overlap(evidence, obligation.source_span)
        ),
        None,
    )


def _ranked_membership_relation_matches(
    join: Mapping[str, Any], value: Mapping[str, Any]
) -> bool:
    """semi Join 하나가 랭킹 계약(소스·엔터티·측정·방향·개수·기간)을 만족하는가.

    **관계** 판정만 한다 — 교집합의 크기(카디널리티)는 이 함수의 소관이 아니다. 둘을 한
    함수에 섞으면 '관계는 맞지만 개수 임계가 빠졌다'를 구별할 수 없다.
    """
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
    # 회원 행동 기간은 랭킹 기간과 **다른 자리**에 걸린다. 여기서는 있어야 할 것이 있는지만
    # 본다 — 없어야 할 것이 없는지(왼쪽에 창이 붙어 요청보다 좁아졌는지)는 기준일을 가진
    # 청구 계층(:func:`ranked_window_scope_issues`)이 판정한다. 이 원장은 기준일이 없어
    # 상대 연도('올해')로 결속된 창을 값으로 갖지 못하므로, 여기서 배제까지 하면 옳은
    # 표현을 반려한다.
    expected_membership_window = value.get("membership_time_window")
    expected_membership_window = (
        expected_membership_window if isinstance(expected_membership_window, Mapping) else None
    )

    if join.get("kind", "inner") != "semi":
        return False
    left, right = join.get("left"), join.get("right")
    if not _has_source(left, expected_source, "subject"):
        return False
    if not _window_matches(left, expected_membership_window):
        return False
    if not _has_source(right, expected_source, "none"):
        return False
    if expected_entity:
        on = join.get("on")
        if not (
            isinstance(on, Mapping)
            and on.get("type") == "comparison"
            and on.get("operator") == "="
            and _field_name(on.get("left")) == expected_entity
            and _field_name(on.get("right")) == expected_entity
        ):
            return False
    for limit in _nodes(right, "limit"):
        if isinstance(expected_limit, Mapping):
            limit_type = expected_limit.get("type")
            if limit_type not in {"count", "percent"}:
                continue
            if limit.get(str(limit_type)) != expected_limit.get("value"):
                continue
        elif limit.get("count") != expected_limit:
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
                order_keys = [
                    key for key in order.get("keys") or [] if isinstance(key, Mapping)
                ]
                if not any(
                    key.get("name") == measure_name
                    and key.get("direction", "asc") == expected_direction
                    for key in order_keys
                ):
                    continue
                entity_key_names = [
                    str(key.get("name"))
                    for key in summary.get("keys") or []
                    if isinstance(key, Mapping)
                    and isinstance(key.get("name"), str)
                    and (
                        not expected_entity
                        or _field_name(key.get("expression")) == expected_entity
                    )
                ]
                if expected_entity and not entity_key_names:
                    continue
                if value.get("tie_policy") == "exact_count":
                    if len(order_keys) < 2:
                        continue
                    if not (
                        order_keys[0].get("name") == measure_name
                        and order_keys[0].get("direction", "asc") == expected_direction
                        and order_keys[1].get("name") in entity_key_names
                        and order_keys[1].get("direction", "asc") == "asc"
                    ):
                        continue
                if not _window_matches(summary, expected_window):
                    continue
                return True
    return False


def _ranked_cardinality_matches(
    payload: Mapping[str, Any],
    value: Mapping[str, Any],
    cardinality: Mapping[str, Any],
    joins: Sequence[Mapping[str, Any]],
) -> bool:
    """'상위 N개 중 M개 이상' 이 실제로 **교집합의 distinct 엔터티 수** 비교로 서 있는가.

    허용하는 형상은 하나다::

        Comparison(Aggregate(count, distinct, <entity_field>, relation=<랭킹 semi Join>),
                   <operator>, M)

    ``Exists(<랭킹 semi Join>)`` 는 이 의무를 방면하지 못한다 — 그것은 '1개 이상'이라서
    요청보다 넓은 집합을 낸다(같은 SQL 이 조용히 다른 오디언스를 만든다).
    """
    expected_operator = cardinality.get("operator")
    expected_value = cardinality.get("value")
    expected_distinct = bool(cardinality.get("distinct", True))
    expected_entity = (
        value.get("entity_field") if isinstance(value.get("entity_field"), str) else None
    )
    if not isinstance(expected_value, int) or isinstance(expected_value, bool):
        return False

    for comparison in _nodes(payload, "comparison"):
        if comparison.get("operator") != expected_operator:
            continue
        threshold = comparison.get("right")
        if not (
            isinstance(threshold, Mapping)
            and threshold.get("type") == "literal"
            and isinstance(threshold.get("value"), int)
            and not isinstance(threshold.get("value"), bool)
            and threshold["value"] == expected_value
        ):
            continue
        counted = comparison.get("left")
        if not (
            isinstance(counted, Mapping)
            and counted.get("type") == "aggregate"
            and counted.get("function") == "count"
            and bool(counted.get("distinct", False)) == expected_distinct
        ):
            continue
        if expected_entity and _field_name(counted.get("expression")) != expected_entity:
            continue
        # 세는 대상이 **그 랭킹 집합과의 교집합**이어야 한다. 다른 관계 위의 같은 모양 집계는
        # 우연히 같은 숫자를 셀 뿐 이 의무를 방면하지 않는다.
        counted_joins = _nodes(counted.get("relation"), "join")
        if any(candidate is join for candidate in counted_joins for join in joins):
            return True
    return False


def _ranked_membership_matches(expression: event_ir.Condition, value: Mapping[str, Any]) -> bool:
    """랭킹 의무 하나를 이 표현이 방면하는가(관계 + 선택적 개수 임계)."""
    payload = expression.to_dict()
    joins = [
        join
        for join in _nodes(payload, "join")
        if _ranked_membership_relation_matches(join, value)
    ]
    if not joins:
        return False
    cardinality = value.get("cardinality")
    if not isinstance(cardinality, Mapping):
        return True
    return _ranked_cardinality_matches(payload, value, cardinality, joins)


def _span_covered(
    source_span: Any, owned: Sequence[tuple[int, int]]
) -> bool:
    """의무의 원문 구간이 컴파일된 구간 안에 들어 있는가."""

    if not isinstance(source_span, Mapping):
        return False
    start, end = source_span.get("start"), source_span.get("end")
    if not (isinstance(start, int) and isinstance(end, int)):
        return False
    return any(
        owned_start <= start and end <= owned_end for owned_start, owned_end in owned
    )


def _issue_span_covered(
    issue: Mapping[str, Any], owned: Sequence[tuple[int, int]]
) -> bool:
    """청구 issue 의 근거 구간이 컴파일된 시간 조건 구간 안에 들어 있는가."""

    evidence = issue.get("evidence")
    return _span_covered(evidence, owned)


def temporal_obligation_compiled_spans(
    query: str,
    expression: event_ir.Condition | None,
    *,
    today: date | None = None,
) -> tuple[tuple[int, int], ...]:
    """시간 의무를 방면할 수 있는 구간 — 이 표현에서 시간 조건이 실제로 컴파일된 자리.

    판정의 소유자는 :mod:`temporal_claims` 다(순환을 피해 지연 import 한다). 선언을 읽지
    못하면 **빈 튜플**을 돌려준다 — 근거 부재는 통과가 아니라 fail-close 다.

    ``today`` 는 낮춤이 쓴 기준일이다. 주지 않으면 실행 시점으로 떨어지는데, 그때 상대
    시점 표현('지난달 말 기준')이 다른 달로 다시 읽혀 **컴파일된 조건이 컴파일되지 않은
    것으로 보인다**. 호출자가 이미 들고 있는 기준일을 그대로 넘긴다.
    """

    if expression is None:
        return ()
    try:
        import audience_runtime  # noqa: PLC0415 - 지연 import(순환 방지)
        import temporal_claims  # noqa: PLC0415
        import temporal_ir  # noqa: PLC0415

        catalog = audience_runtime.resolve_audience_catalog()
        return temporal_claims.compiled_obligation_spans(
            query,
            expression,
            snapshot=audience_runtime.catalog_snapshot(),
            catalog=catalog,
            runtime=temporal_ir.create_temporal_runtime(catalog),
            context=temporal_claims.request_context_for(today),
            today=today,
        )
    except (ImportError, ValueError, KeyError, TypeError):
        return ()


def semantic_obligation_issues(
    query: str,
    expression: event_ir.Condition,
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    temporal_spans = temporal_obligation_compiled_spans(query, expression, today=today)
    for requirement in semantic_requirements.capture_source_semantic_obligations(query):
        kind = semantic_requirements.obligation_kind(requirement)
        value = requirement.value if isinstance(requirement.value, Mapping) else {}
        if kind in CANONICAL_COMPILED_OBLIGATION_KINDS and _ranked_membership_matches(
            expression, value
        ):
            continue
        if kind == semantic_requirements.TEMPORAL_QUALIFIER_KIND and _span_covered(
            requirement.source_span, temporal_spans
        ):
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
        if kind in CANONICAL_COMPILED_OBLIGATION_KINDS:
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


def _is_ranked_semi_join(join: Mapping[str, Any]) -> bool:
    """``semi Join(…, Limit(Order(Summarize(…))))`` — 랭킹 집합과의 교집합 관계인가."""
    if join.get("kind", "inner") != "semi":
        return False
    return any(
        _nodes(limit.get("relation"), "order")
        and _nodes(limit.get("relation"), "summarize")
        for limit in _nodes(join.get("right"), "limit")
    )


def _scope_window_pair(window: Mapping[str, Any]) -> tuple[Any, Any]:
    return window.get("from"), window.get("to")


def ranked_window_scope_issues(
    query: str,
    expression: event_ir.Condition,
    literal_bindings: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """랭킹 요청의 각 달력 창이 **결속된 절**에 걸렸는가(양방향).

    한 문장의 두 절은 기간도 둘이다. 랭킹 창은 모집단을, 회원 행동 창은 그 회원이 언제 샀는지를
    좁힌다 — 자리를 바꾸면 SQL 은 유효한 채 다른 오디언스를 낸다. 그래서 두 방향을 다 본다:

      · 결속된 창이 그 절에 **없으면** 조건이 사라진 것이다(요청보다 넓다).
      · 결속되지 않았는데 회원 쪽에 창이 **있으면** 랭킹 기간을 회원 행동에 잘못 옮긴 것이다
        (요청보다 좁다). 기본 결속이 랭킹 전용이므로 이 방향이 훨씬 흔하다.

    기준일이 필요한 상대 연도('작년'·'올해')까지 보려면 창의 값을 가진 계층에서 호출해야 한다 —
    그래서 입력이 리터럴 바인딩이다.
    """
    windows = [
        (binding["normalized"], binding["start"], binding["end"])
        for binding in literal_bindings
        if binding.get("kind") == "date_window"
        and isinstance(binding.get("normalized"), Mapping)
        and isinstance(binding.get("start"), int)
        and isinstance(binding.get("end"), int)
    ]
    if not windows:
        return []
    payload = expression.to_dict()
    joins = [join for join in _nodes(payload, "join") if _is_ranked_semi_join(join)]
    scoped = semantic_requirements.ranked_window_scope_bindings(query, windows)
    # 랭킹 관계가 둘 이상이면 어느 창이 어느 관계의 것인지 이 계층에서 확정할 수 없다.
    # 억지 귀속은 멀쩡한 플랜을 반려하므로 판정하지 않는다(의무 원장의 대조는 그대로 돈다).
    if len(joins) != 1 or not scoped:
        return []
    join = joins[0]
    by_scope: dict[str, list[Any]] = {}
    for binding in scoped:
        by_scope.setdefault(binding.scope, []).append(binding)
    if any(len(items) > 1 for items in by_scope.values()):
        return []
    # 어느 절에도 결속되지 않은 창이 남아 있으면 **배제 판정을 하지 않는다**. 그 창의 소속을
    # 모르는 채 '회원 쪽에 창이 있으면 틀렸다'고 하면, 결속 문법이 아직 읽지 못하는 어순
    # ('올해 들어서 처음 구매한')에서 옳은 표현을 반려한다. 있어야 할 창이 있는지(양의 방향)는
    # 그대로 본다 — 그쪽은 결속이 확정된 창만 근거로 삼기 때문이다.
    all_windows_bound = len(scoped) == len(windows)

    issues: list[dict[str, Any]] = []
    sides = {
        temporal_clause.SCOPE_RANKING: ("right", "랭킹 모집단"),
        temporal_clause.SCOPE_MEMBERSHIP: ("left", "회원 행동"),
    }
    for scope, (side, label) in sides.items():
        binding = (by_scope.get(scope) or [None])[0]
        intervals = [
            _scope_window_pair(node) for node in _nodes(join.get(side), "interval")
        ]
        if binding is not None:
            if _scope_window_pair(binding.window) in intervals:
                continue
            message = (
                f"'{query[binding.window_span[0]:binding.window_span[1]]}' 는 {label}의 기간입니다. "
                f"그 창({binding.window.get('label') or binding.window.get('from')})을 semi Join 의 "
                f"{side} 쪽 TimeFilter 로 두세요."
            )
            evidence = {
                "text": query[binding.window_span[0]:binding.window_span[1]],
                "start": binding.window_span[0],
                "end": binding.window_span[1],
            }
        elif scope == temporal_clause.SCOPE_MEMBERSHIP and intervals and all_windows_bound:
            other = by_scope.get(temporal_clause.SCOPE_RANKING) or []
            message = (
                "원문은 회원이 언제 그 행동을 했는지 말하지 않았습니다. "
                f"{label} 쪽(semi Join 의 {side})에는 TimeFilter 를 두지 마세요"
                + (
                    f" — '{query[other[0].window_span[0]:other[0].window_span[1]]}' 는 "
                    "랭킹 모집단의 기간입니다."
                    if other else "."
                )
            )
            evidence = (
                {
                    "text": query[other[0].window_span[0]:other[0].window_span[1]],
                    "start": other[0].window_span[0],
                    "end": other[0].window_span[1],
                }
                if other else {"text": query, "start": 0, "end": len(query)}
            )
        else:
            continue
        issues.append({
            "code": "validation_mismatch",
            "argument": f"ranked_window_scope.{scope}",
            "message": message,
            "evidence": evidence,
        })
    return issues


def canonical_claim_issues(
    query: str,
    expression: event_ir.Condition,
    literal_bindings: Iterable[Mapping[str, Any]],
    catalog: Mapping[str, Any] | None = None,
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    bindings = list(literal_bindings)
    literal_issues = literal_claim_issues(query, expression, bindings)
    catalog_issues = (
        catalog_claim_issues(query, expression, bindings, catalog)
        if catalog is not None else []
    )
    rolling_absence_consumed = (
        rolling_absence_claims.consumed_literal_binding_indices(
            query, expression, bindings, catalog
        )
        if catalog is not None else frozenset()
    )
    if rolling_absence_consumed:
        consumed = {
            f"literal_bindings[{index}]" for index in rolling_absence_consumed
        }
        literal_issues = [
            issue for issue in literal_issues
            if issue.get("argument") not in consumed
        ]
    # 시간 조건이 컴파일한 구간의 리터럴·값은 **이미 증명된** 소비다. temporal 영수증은
    # 낮춘 트리를 되읽어 소스·시간 조건·값 비교가 전부 있는지 확인한 뒤에만 발급되므로
    # (temporal_ir.lowering._composition_gaps) 일반 청구 검사보다 강하다. 그 구간까지
    # 미소비로 세면 '6개월'이 절대 구간으로 확정된 사실과 '내내'가 전칭으로 낮아진 사실이
    # 리터럴 표면에 남지 않았다는 이유만으로 SQL 이 막힌다.
    temporal_spans = temporal_obligation_compiled_spans(query, expression)
    if temporal_spans:
        literal_issues = [
            issue for issue in literal_issues
            if not _issue_span_covered(issue, temporal_spans)
        ]
        catalog_issues = [
            issue for issue in catalog_issues
            if not _issue_span_covered(issue, temporal_spans)
        ]
    cardinality = (
        consent_cardinality.validate_consent_cardinality(
            query, expression, bindings, catalog
        )
        if catalog is not None else None
    )
    if cardinality is not None and cardinality.equivalent:
        consumed = {
            f"literal_bindings[{index}]"
            for index in cardinality.consumed_binding_indices
        }
        consent_arguments = {
            f"catalog_value.{field_id}"
            for field_id in cardinality.consent_field_ids
        }
        literal_issues = [
            issue for issue in literal_issues
            if issue.get("argument") not in consumed
        ]
        catalog_issues = [
            issue for issue in catalog_issues
            if issue.get("argument") not in consent_arguments
        ]
    issues = [
        *literal_issues,
        *window_kind_issues(query, expression, bindings),
        *ranked_window_scope_issues(query, expression, bindings),
        *catalog_issues,
        *semantic_obligation_issues(query, expression, today=today),
    ]
    if cardinality is not None and not cardinality.equivalent:
        issues.append({
            "code": "validation_mismatch",
            "argument": "consent_cardinality",
            "message": (
                "Canonical consent Boolean expression이 원문에서 요청한 채널 수 조건과 "
                "모든 진리값 조합에서 동치가 아닙니다."
            ),
            "evidence": {
                "text": cardinality.quantifier_text,
                "start": cardinality.quantifier_start,
                "end": cardinality.quantifier_end,
            },
        })
    return issues


def ranked_obligation_is_compiled(
    expression: event_ir.Condition,
    requirement_value: Mapping[str, Any],
) -> bool:
    """Public receipt predicate used by the graph-level immutable ledger."""
    return _ranked_membership_matches(expression, requirement_value)


# `_issue_is_superseded_by_another_compiler`(다른 컴파일러가 이미 컴파일한 구절의 미지원
# 신고 회수)는 2026-08-05 삭제됐다. 회수의 근거는 둘뿐이었고 — lowered relation 영수증과
# 컴파일된 relational_operation — 축1 폐기로 둘 다 생산자가 사라졌다. 남겨 두면 항상
# False 를 돌려주는 분기가 되어, 없는 두 번째 컴파일 경로를 광고하게 된다.


def refresh_canonical_unresolved(
    query: str,
    plan: dict[str, Any],
    expression: event_ir.Condition | None,
    catalog: Mapping[str, Any],
    *,
    today: date | None = None,
) -> list[dict[str, Any]]:
    """Refresh graph-level canonical coverage and immutable receipts.

    ``today`` 는 시간 의무 재판정의 기준일이다 — 낮춤이 쓴 것과 같아야 한다.
    """
    requirement = plan.get("audience_requirement")
    issues: list[dict[str, Any]] = []
    if expression is not None:
        # Normal ingress seals this ledger before compilation.  Programmatic
        # canonical callers (migration/tests) may enter directly at this graph
        # boundary; create the immutable source-only ledger once, before issuing
        # any receipt.  An existing—even empty—ledger is never rewritten.
        if semantic_requirements.SOURCE_REQUIREMENTS_KEY not in plan:
            semantic_requirements.attach_source_requirements(
                plan,
                semantic_requirements.capture_source_semantic_obligations(query),
            )
        semantic_requirements.discharge_source_semantic_obligations(
            plan,
            query,
            kinds=set(CANONICAL_COMPILED_OBLIGATION_KINDS),
            status="compiled",
            compiler="canonical_event_ir",
            evidence=expression.to_dict(),
            value_filter=lambda _kind, value: (
                isinstance(value, Mapping)
                and ranked_obligation_is_compiled(expression, value)
            ),
        )
        # 시간·이력 의무는 **그 구간이 실제로 컴파일됐을 때만** 방면한다. 종류만 보고
        # 통째로 면제하면 as_of·직전값·유지·변경횟수 마커가 만든 의무까지 함께 풀려,
        # 낮춰지지 않은 표현이 검증을 통과한다.
        temporal_spans = temporal_obligation_compiled_spans(
            query, expression, today=today
        )
        if temporal_spans:
            semantic_requirements.discharge_source_semantic_obligations(
                plan,
                query,
                kinds={semantic_requirements.TEMPORAL_QUALIFIER_KIND},
                status="compiled",
                compiler="temporal_ir",
                evidence=expression.to_dict(),
                requirement_filter=lambda requirement: _span_covered(
                    requirement.source_span, temporal_spans
                ),
            )
        bindings = plan.get("literal_bindings")
        if isinstance(bindings, list):
            issues.extend(
                issue
                for issue in canonical_claim_issues(
                    query, expression, bindings, catalog, today=today
                )
            )
    elif isinstance(requirement, Mapping):
        issues.extend(
            issue for issue in (requirement.get("issues") or [])
            if isinstance(issue, dict)
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
    # 영수증 없는 의미 노드를 미해결로 남기던 게이트(`semantic_receipts.unreceipted_nodes`)는
    # 2026-08-05 삭제됐다 — 노드를 만드는 축이 전부 폐기돼 게이트에 들어올 노드가 없다.
    # "절이 조용히 사라진 성공은 없다"는 계약은 그대로다: 그 절의 카탈로그 값·리터럴이
    # 위 `canonical_claim_issues` 커버리지 검사에서 미소비로 잡혀 SQL 출고를 막는다(실측 재현).
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
    "catalog_value_claims",
    "discharge_legacy_ranked_obligations",
    "literal_claim_issues",
    "ranked_obligation_is_compiled",
    "ranked_window_scope_issues",
    "refresh_canonical_unresolved",
    "semantic_obligation_issues",
]
