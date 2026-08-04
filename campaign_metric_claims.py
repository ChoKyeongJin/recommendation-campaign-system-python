"""Closed campaign-average amount claim -> application-owned SemanticPlan.

The model occasionally reports that the money/operator literals in a fully
declared campaign purchase-response average cannot be represented.  This
module may contradict that report only when three independent contracts agree:

* the whole query is one closed ``per campaign / purchase-response amount /
  average / money / comparison`` member predicate;
* every model issue is owned by the exact application literal bindings (or one
  declared metric issue owns the entire predicate);
* the canonical audience catalog and SQL registry agree on member correlation,
  campaign denominator, amount column, target-group filter, and purchase-
  response filter.

No value, time window, response predicate, or physical name is inferred from
model prose.  Any incomplete declaration makes synthesis fail closed.
"""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import audience_frame
import audience_runtime
import member_filters_config

OWNER = "campaign_metric_claims.catalog_average_literal_operator"


@dataclass(frozen=True)
class CampaignMetricSynthesis:
    """One aggregate predicate plus its catalog/SQL/literal proof receipt."""

    node: dict[str, Any]
    receipt: dict[str, Any]


def _span(row: Mapping[str, Any], query: str) -> tuple[int, int] | None:
    start, end, text = row.get("start"), row.get("end"), row.get("text")
    if not (
        isinstance(start, int)
        and not isinstance(start, bool)
        and isinstance(end, int)
        and not isinstance(end, bool)
        and isinstance(text, str)
        and 0 <= start < end <= len(query)
        and query[start:end] == text
    ):
        return None
    return start, end


def _issue_span(issue: Mapping[str, Any], query: str) -> tuple[int, int] | None:
    evidence = issue.get("evidence")
    return _span(evidence, query) if isinstance(evidence, Mapping) else None


def _strings(value: Any) -> tuple[str, ...]:
    return tuple(
        item.strip()
        for item in value or []
        if isinstance(item, str) and item.strip()
    )


def _unique_term(
    query: str, terms: Sequence[str]
) -> tuple[str, int, int] | None:
    matches: set[tuple[str, int, int]] = set()
    for term in sorted(set(terms), key=lambda item: (-len(item), item)):
        cursor = 0
        while (start := query.find(term, cursor)) >= 0:
            end = start + len(term)
            matches.add((term, start, end))
            cursor = end
    if not matches:
        return None
    longest = max(end - start for _term, start, end in matches)
    winners = [item for item in matches if item[2] - item[1] == longest]
    spans = {(start, end) for _term, start, end in winners}
    if len(spans) != 1:
        return None
    return min(winners, key=lambda item: item[0])


def _equals_predicate(column: Any, value: Any) -> str | None:
    if not isinstance(column, str) or not column or not isinstance(value, str) or not value:
        return None
    return f"{column} = '{value.replace(chr(39), chr(39) * 2)}'"


def _campaign_assets() -> tuple[dict[str, Any], ...]:
    """Every metric that *declares* a per-campaign average claim, fully verified.

    The metric id used to be written here as a literal, which made a second
    declared axis unreachable without a code change.  The catalog owns which
    metrics carry a ``claim_synthesis.average_per_campaign`` contract; this
    module only checks that each such declaration is complete and agrees with
    the SQL asset.  An incomplete declaration is dropped, never guessed at.
    """

    catalog = audience_runtime.catalog_snapshot()
    subject = catalog.get("subject")
    metrics = catalog.get("metrics")
    fields = catalog.get("fields")
    sources = catalog.get("sources")
    if not all(
        isinstance(section, Mapping)
        for section in (subject, metrics, fields, sources)
    ):
        return ()
    assert isinstance(metrics, Mapping)

    assets: list[dict[str, Any]] = []
    for metric_id, metric in metrics.items():
        if not isinstance(metric, Mapping):
            continue
        claim = metric.get("claim_synthesis")
        if not isinstance(claim, Mapping):
            continue
        average = claim.get("average_per_campaign")
        sql_ref = claim.get("sql_asset")
        if not isinstance(average, Mapping) or not isinstance(sql_ref, Mapping):
            continue
        asset = _verified_asset(
            str(metric_id),
            metric,
            claim,
            average,
            sql_ref,
            subject=subject,
            fields=fields,
            sources=sources,
        )
        if asset is not None:
            assets.append(asset)
    return tuple(assets)


def _verified_asset(
    semantic_metric_id: str,
    metric: Mapping[str, Any],
    claim: Mapping[str, Any],
    average: Mapping[str, Any],
    sql_ref: Mapping[str, Any],
    *,
    subject: Any,
    fields: Any,
    sources: Any,
) -> dict[str, Any] | None:
    """Cross-check one semantic declaration against the SQL asset."""

    source_id = metric.get("source")
    amount_field_id = metric.get("expression_field")
    denominator_field_id = average.get("denominator_field")
    source = sources.get(source_id) if isinstance(source_id, str) else None
    amount_field = fields.get(amount_field_id) if isinstance(amount_field_id, str) else None
    denominator_field = (
        fields.get(denominator_field_id)
        if isinstance(denominator_field_id, str)
        else None
    )
    if not all(
        isinstance(item, Mapping)
        for item in (source, amount_field, denominator_field)
    ):
        return None

    execution_metric = claim.get("semantic_plan_metric")
    scope = claim.get("semantic_plan_scope")
    aggregation = average.get("aggregation")
    unit = metric.get("unit")
    metric_aliases = _strings(metric.get("aliases"))
    metric_terms = (
        *_strings(amount_field.get("aliases")),
        str(amount_field.get("label") or ""),
    )
    grain_terms = _strings(average.get("grain_terms"))
    aggregation_terms = _strings(average.get("aggregation_terms"))
    if not (
        source_id == amount_field.get("source") == denominator_field.get("source")
        and metric.get("kind") == "aggregate"
        and str(metric.get("function") or "").casefold() == "sum"
        and metric.get("data_type") == "number"
        and isinstance(unit, str)
        and unit
        and unit == amount_field.get("unit")
        and scope == "campaign"
        and isinstance(execution_metric, str)
        and execution_metric in metric_aliases
        and aggregation == "avg"
        and average.get("denominator_distinct") is True
        and grain_terms
        and aggregation_terms
        and metric_terms
    ):
        return None

    sql_section = sql_ref.get("section")
    if sql_section != "campaign_response_targets":
        return None
    sql_asset = member_filters_config.campaign_response_targets()
    aggregate_metrics = sql_asset.get("aggregate_metrics")
    boolean_metrics = sql_asset.get("boolean_metrics")
    campaign_join = sql_asset.get("campaign_join")
    member_join = sql_asset.get("member_join")
    target_group = sql_asset.get("target_group_condition")
    valid_campaign = sql_asset.get("valid_campaign_condition")
    if not all(
        isinstance(item, Mapping)
        for item in (
            aggregate_metrics,
            boolean_metrics,
            campaign_join,
            member_join,
            target_group,
            valid_campaign,
        )
    ):
        return None
    aggregate_metric = aggregate_metrics.get(sql_ref.get("aggregate_metric"))
    response_metric = boolean_metrics.get(sql_ref.get("response_metric"))
    if not isinstance(aggregate_metric, Mapping) or not isinstance(response_metric, Mapping):
        return None

    alias = source.get("alias")
    amount_column = amount_field.get("column")
    denominator_expression = denominator_field.get("expression")
    if not all(
        isinstance(value, str) and value
        for value in (
            alias,
            source.get("table"),
            source.get("event_subject_key"),
            source.get("correlation_sql"),
            source.get("from_sql"),
            amount_column,
            denominator_expression,
            sql_asset.get("table"),
            sql_asset.get("alias"),
            sql_asset.get("member_column"),
            sql_asset.get("campaign_key_expression"),
            member_join.get("left"),
            member_join.get("right"),
            campaign_join.get("table"),
            valid_campaign.get("expression"),
        )
    ):
        return None

    target_predicate = _equals_predicate(
        target_group.get("column"), target_group.get("value")
    )
    response_predicate = _equals_predicate(
        response_metric.get("column"), response_metric.get("value")
    )
    if target_predicate is None or response_predicate is None:
        return None
    catalog_predicates = {
        str(item).replace("{alias}", str(alias))
        for item in source.get("extra_predicates") or []
        if isinstance(item, str)
    }
    valid_predicate = str(valid_campaign["expression"])
    qualified_amount = f"{alias}.{amount_column}"
    rendered_denominator = str(denominator_expression).replace("{alias}", str(alias))
    campaign_conditions = _strings(campaign_join.get("conditions"))
    rendered_from = str(source["from_sql"]).replace("{alias}", str(alias))
    rendered_correlation = (
        str(source["correlation_sql"])
        .replace("{alias}", str(alias))
        .replace("{subject_alias}", str(subject.get("alias")))
        .replace("{subject_key}", str(subject.get("key")))
    )
    sql_correlation = f"{member_join['left']} = {member_join['right']}"
    if not (
        sql_asset.get("table") == source.get("table")
        and sql_asset.get("alias") == alias
        and sql_asset.get("member_column") == source.get("event_subject_key")
        and source.get("subject_key") == subject.get("key")
        and rendered_correlation == sql_correlation
        and str(aggregate_metric.get("agg") or "").casefold() == "sum"
        and aggregate_metric.get("column") == qualified_amount
        and sql_asset.get("campaign_key_expression") == rendered_denominator
        and len(campaign_conditions) >= 2
        and all(condition in rendered_from for condition in campaign_conditions)
        and {target_predicate, response_predicate, valid_predicate} <= catalog_predicates
    ):
        return None

    return {
        "semantic_metric_id": semantic_metric_id,
        "execution_metric": execution_metric,
        "scope": scope,
        "aggregation": aggregation,
        "unit": unit,
        "allowed_operators": frozenset(_strings(metric.get("allowed_operators"))),
        "metric_terms": tuple(term for term in metric_terms if term),
        "grain_terms": grain_terms,
        "aggregation_terms": aggregation_terms,
        "generic_issue_arguments": frozenset(_strings(claim.get("issue_arguments"))),
        "source": {
            "id": source_id,
            "table": source.get("table"),
            "member_column": sql_asset.get("member_column"),
            "member_join": {
                "left": member_join.get("left"),
                "right": member_join.get("right"),
            },
            "campaign_join": {
                "table": campaign_join.get("table"),
                "conditions": list(campaign_conditions),
            },
        },
        "amount": {
            "field": amount_field_id,
            "column": qualified_amount,
            "base_aggregation": "SUM",
        },
        "per_campaign": {
            "denominator_field": denominator_field_id,
            "denominator_expression": rendered_denominator,
            "denominator_distinct": True,
        },
        "response_filters": {
            "target_group": target_predicate,
            "purchase_response": response_predicate,
            "valid_campaign": valid_predicate,
        },
    }


def _whole_phrase_matches(
    query: str,
    *,
    grain: tuple[str, int, int],
    metric: tuple[str, int, int],
    aggregation: tuple[str, int, int],
    money_bounds: tuple[int, int],
    operator_bounds: tuple[int, int],
) -> bool:
    """Require one closed claim; do not synthesize across an unknown remainder.

    The five axes must appear in that order with nothing but whitespace and a
    subject particle between them — that ordering is what binds each literal to
    its role.  Everything *outside* the claim is judged by clause structure
    instead of by a member-noun suffix template: it has to be frame
    (:mod:`audience_frame`), which is the same answer for ``찾아줘`` /
    ``조회해 주세요`` / no request verb at all, and still rejects one more
    condition on either side.
    """

    _grain_text, grain_start, grain_end = grain
    _metric_text, metric_start, metric_end = metric
    _aggregation_text, aggregation_start, aggregation_end = aggregation
    money_start, money_end = money_bounds
    operator_start, operator_end = operator_bounds
    if not (
        grain_end <= metric_start < metric_end <= aggregation_start
        < aggregation_end <= money_start < money_end <= operator_start < operator_end
    ):
        return False
    return bool(
        re.fullmatch(r"\s+", query[grain_end:metric_start])
        and re.fullmatch(r"(?:이|가|은|는)?\s+", query[metric_end:aggregation_start])
        and re.fullmatch(r"\s+", query[aggregation_end:money_start])
        and re.fullmatch(r"\s+", query[money_end:operator_start])
        and audience_frame.is_frame_only(query, [(grain_start, operator_end)])
    )


def _issues_own_claim(
    issues: list[Mapping[str, Any]],
    query: str,
    *,
    money_index: int,
    operator_index: int,
    money_bounds: tuple[int, int],
    operator_bounds: tuple[int, int],
    claim_bounds: tuple[int, int],
    generic_arguments: frozenset[str],
) -> bool:
    if not issues or any(issue.get("code") != "unsupported_semantics" for issue in issues):
        return False
    bounds = [_issue_span(issue, query) for issue in issues]
    if any(item is None for item in bounds):
        return False

    if len(issues) == 1:
        issue_bounds = bounds[0]
        assert issue_bounds is not None
        return bool(
            str(issues[0].get("argument") or "") in generic_arguments
            and issue_bounds[0] <= claim_bounds[0]
            and claim_bounds[1] <= issue_bounds[1]
        )

    expected = Counter(
        {
            f"literal_bindings[{money_index}]": 1,
            f"literal_bindings[{money_index}].unit": 1,
            f"literal_bindings[{operator_index}]": 1,
        }
    )
    actual = Counter(str(issue.get("argument") or "") for issue in issues)
    if len(issues) != 3 or actual != expected:
        return False
    expected_span = {
        f"literal_bindings[{money_index}]": money_bounds,
        f"literal_bindings[{money_index}].unit": money_bounds,
        f"literal_bindings[{operator_index}]": operator_bounds,
    }
    for issue, issue_bounds in zip(issues, bounds, strict=True):
        assert issue_bounds is not None
        owned = expected_span[str(issue["argument"])]
        if not (issue_bounds[0] <= owned[0] and owned[1] <= issue_bounds[1]):
            return False
    return True


def _validated_claim(
    query: str,
    literal_bindings: list[Any],
    semantic_plan: Any,
) -> dict[str, Any] | None:
    """Return the catalog/literal proof shared by both model wire shapes."""

    nodes = semantic_plan.get("nodes") if isinstance(semantic_plan, Mapping) else None
    if nodes != [] or len(literal_bindings) != 2:
        return None
    bindings = [binding for binding in literal_bindings if isinstance(binding, Mapping)]
    if len(bindings) != 2:
        return None

    money_items = [
        (index, binding)
        for index, binding in enumerate(literal_bindings)
        if isinstance(binding, Mapping) and binding.get("kind") == "money"
    ]
    operator_items = [
        (index, binding)
        for index, binding in enumerate(literal_bindings)
        if isinstance(binding, Mapping) and binding.get("kind") == "comparison_operator"
    ]
    if len(money_items) != 1 or len(operator_items) != 1:
        return None
    money_index, money = money_items[0]
    operator_index, comparison = operator_items[0]
    money_bounds, operator_bounds = _span(money, query), _span(comparison, query)
    normalized_money = money.get("normalized")
    operator = comparison.get("normalized")
    if not (
        money_bounds is not None
        and operator_bounds is not None
        and isinstance(normalized_money, Mapping)
        and isinstance(normalized_money.get("amount"), (int, float))
        and not isinstance(normalized_money.get("amount"), bool)
        and normalized_money.get("amount") > 0
        and isinstance(normalized_money.get("currency"), str)
        and isinstance(operator, str)
    ):
        return None

    matched: list[dict[str, Any]] = []
    for asset in _campaign_assets():
        if (
            normalized_money["currency"].upper() != str(asset["unit"]).upper()
            or operator not in asset["allowed_operators"]
        ):
            continue
        grain = _unique_term(query, asset["grain_terms"])
        metric = _unique_term(query, asset["metric_terms"])
        aggregation = _unique_term(query, asset["aggregation_terms"])
        if grain is None or metric is None or aggregation is None:
            continue
        if not _whole_phrase_matches(
            query,
            grain=grain,
            metric=metric,
            aggregation=aggregation,
            money_bounds=money_bounds,
            operator_bounds=operator_bounds,
        ):
            continue
        matched.append({
            "asset": asset,
            "money_index": money_index,
            "operator_index": operator_index,
            "money": money,
            "comparison": comparison,
            "money_bounds": money_bounds,
            "operator_bounds": operator_bounds,
            "normalized_money": normalized_money,
            "operator": operator,
            "grain": grain,
            "metric": metric,
            "aggregation": aggregation,
            "claim_bounds": (grain[1], operator_bounds[1]),
        })
    # Two declared metrics claiming the same phrase means the phrase is
    # ambiguous; the catalog, not this module, has to resolve that.
    return matched[0] if len(matched) == 1 else None


def _build_synthesis(
    query: str,
    claim: Mapping[str, Any],
    *,
    issues: Sequence[Mapping[str, Any]],
    model_expression_receipt: Mapping[str, Any] | None = None,
) -> CampaignMetricSynthesis:
    asset = claim["asset"]
    money = claim["money"]
    comparison = claim["comparison"]
    money_bounds = claim["money_bounds"]
    operator_bounds = claim["operator_bounds"]
    normalized_money = claim["normalized_money"]
    operator = claim["operator"]
    grain = claim["grain"]
    metric = claim["metric"]
    aggregation = claim["aggregation"]
    claim_bounds = claim["claim_bounds"]
    amount = normalized_money["amount"]
    digest_material = "\0".join(
        (
            query,
            asset["semantic_metric_id"],
            asset["execution_metric"],
            str(money.get("id") or ""),
            str(comparison.get("id") or ""),
            str(amount),
            operator,
        )
    )
    node_id = "campaign-metric-" + hashlib.sha256(
        digest_material.encode("utf-8")
    ).hexdigest()[:12]
    node = {
        "id": node_id,
        "type": "aggregate_predicate",
        "source_span": query[claim_bounds[0] : claim_bounds[1]],
        "source_start": claim_bounds[0],
        "source_end": claim_bounds[1],
        "scope": asset["scope"],
        "metric": asset["execution_metric"],
        "operator": operator,
        "value": amount,
        "aggregation": asset["aggregation"],
        "unit": asset["unit"],
        "producer": OWNER,
    }
    receipt = {
        "owner": OWNER,
        "node_id": node_id,
        "semantic_metric_id": asset["semantic_metric_id"],
        "execution_metric": asset["execution_metric"],
        "scope": asset["scope"],
        "metric_alias": {
            "text": metric[0],
            "start": metric[1],
            "end": metric[2],
        },
        "per_campaign": {
            "text": grain[0],
            "start": grain[1],
            "end": grain[2],
            **asset["per_campaign"],
        },
        "aggregation": {
            "text": aggregation[0],
            "start": aggregation[1],
            "end": aggregation[2],
            "function": asset["aggregation"],
            "base_amount_aggregation": asset["amount"]["base_aggregation"],
        },
        "threshold": {
            "binding_id": money.get("id"),
            "amount": amount,
            "currency": normalized_money["currency"],
            "start": money_bounds[0],
            "end": money_bounds[1],
        },
        "comparison": {
            "binding_id": comparison.get("id"),
            "operator": operator,
            "start": operator_bounds[0],
            "end": operator_bounds[1],
        },
        "source": asset["source"],
        "amount_asset": asset["amount"],
        "response_filters": asset["response_filters"],
        "consumed_literal_binding_ids": [money.get("id"), comparison.get("id")],
        "discharged_issues": [
            {
                "code": issue.get("code"),
                "argument": issue.get("argument"),
                "start": bounds[0],
                "end": bounds[1],
            }
            for issue in issues
            if (bounds := _issue_span(issue, query)) is not None
        ],
    }
    if model_expression_receipt is not None:
        receipt["model_expression"] = dict(model_expression_receipt)
    return CampaignMetricSynthesis(node=node, receipt=receipt)


def synthesize_campaign_average_amount_predicate(
    query: str,
    issues: list[Any],
    literal_bindings: list[Any],
    semantic_plan: Any,
) -> CampaignMetricSynthesis | None:
    """Synthesize one declared per-campaign average amount predicate."""

    typed_issues = [issue for issue in issues if isinstance(issue, Mapping)]
    if len(typed_issues) != len(issues):
        return None
    claim = _validated_claim(query, literal_bindings, semantic_plan)
    if claim is None:
        return None
    if not _issues_own_claim(
        typed_issues,
        query,
        money_index=claim["money_index"],
        operator_index=claim["operator_index"],
        money_bounds=claim["money_bounds"],
        operator_bounds=claim["operator_bounds"],
        claim_bounds=claim["claim_bounds"],
        generic_arguments=claim["asset"]["generic_issue_arguments"],
    ):
        return None
    return _build_synthesis(query, claim, issues=typed_issues)


def synthesize_campaign_average_amount_expression(
    query: str,
    expression: Any,
    literal_bindings: list[Any],
    semantic_plan: Any,
) -> CampaignMetricSynthesis | None:
    """Retype the one exact model-owned aggregate wire shape.

    Event IR can carry a generic aggregate expression, but this campaign
    average has a more specific execution contract: base ``SUM(BUY_AMT)`` per
    distinct campaign execution, plus target/purchase-response/valid-campaign
    filters.  Only a byte-for-byte closed algebra shape that agrees with the
    application literals and those catalog assets may cross that boundary.
    """

    claim = _validated_claim(query, literal_bindings, semantic_plan)
    if claim is None or not isinstance(expression, Mapping):
        return None
    if set(expression) != {"type", "operator", "left", "right", "evidence"}:
        return None
    left, right, evidence = (
        expression.get("left"),
        expression.get("right"),
        expression.get("evidence"),
    )
    if not all(isinstance(item, Mapping) for item in (left, right, evidence)):
        return None
    assert isinstance(left, Mapping)
    assert isinstance(right, Mapping)
    assert isinstance(evidence, Mapping)
    if (
        set(left)
        != {"type", "function", "relation", "expression", "distinct"}
        or set(right) != {"type", "value"}
        or set(evidence) != {"text", "start", "end"}
    ):
        return None
    field = left.get("expression")
    relation = left.get("relation")
    if not isinstance(field, Mapping) or not isinstance(relation, Mapping):
        return None
    if set(field) != {"type", "name"}:
        return None

    source_id = claim["asset"]["source"]["id"]
    relation_wrapper = "source"
    source = relation
    if relation.get("type") == "filter":
        if set(relation) != {"type", "relation", "where"} or relation.get("where") is not None:
            return None
        source = relation.get("relation")
        relation_wrapper = "empty_filter"
    if not (
        isinstance(source, Mapping)
        and set(source) == {"type", "name"}
        and source.get("type") == "source"
        and source.get("name") == source_id
    ):
        return None

    amount = claim["normalized_money"]["amount"]
    operator = claim["operator"]
    evidence_bounds = _span(evidence, query)
    expected_evidence_bounds = (
        claim["money_bounds"][0],
        claim["operator_bounds"][1],
    )
    if not (
        expression.get("type") == "comparison"
        and expression.get("operator") == operator
        and left.get("type") == "aggregate"
        and left.get("function") == claim["asset"]["aggregation"]
        and left.get("distinct") is False
        and field.get("type") == "field"
        and field.get("name") == claim["asset"]["amount"]["field"]
        and right.get("type") == "literal"
        and isinstance(right.get("value"), (int, float))
        and not isinstance(right.get("value"), bool)
        and right.get("value") == amount
        and evidence_bounds == expected_evidence_bounds
    ):
        return None

    return _build_synthesis(
        query,
        claim,
        issues=(),
        model_expression_receipt={
            "type": "comparison.aggregate",
            "relation_wrapper": relation_wrapper,
            "source": source_id,
            "field": claim["asset"]["amount"]["field"],
            "function": claim["asset"]["aggregation"],
            "distinct": False,
            "operator": operator,
            "value": amount,
            "evidence_start": evidence_bounds[0],
            "evidence_end": evidence_bounds[1],
        },
    )


__all__ = [
    "OWNER",
    "CampaignMetricSynthesis",
    "synthesize_campaign_average_amount_predicate",
    "synthesize_campaign_average_amount_expression",
]
