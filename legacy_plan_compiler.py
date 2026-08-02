"""SemanticPlanV2 → 기존 query_plan 컴파일러.

**이 모듈만** 실행 슬롯 이름을 안다:

    target_user.cart_aggregate
    target_user.aggregate_conditions
    target_user.campaign_response_frequency
    target_user.campaign_buy_amount
    target_user.balance_conditions
    target_user.profile_date_conditions
    target_user.metric_trend
    target_user.entity_set_condition
    target_user.relational_operation
    member_metric_ranking            (plan 컨테이너)

LLM(semantic_plan_llm), 정규화기(semantic_normalizers), coverage verifier(semantic_coverage)는
이 이름들을 모른다. 그것이 이 파일이 존재하는 이유다 — 슬롯 지식의 단일 소유자.

기존 실행기는 그대로 둔다. 실행 계약을 바꾸는 대신, 슬롯을 만드는 **유일한 경로**를 이
컴파일러로 좁힌다. 그래서 컴파일 결과는 같은 SemanticPlan 에 대해 항상 같다(순수 함수).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

import compile_contract
import semantic_domain_binding
import semantic_plan
from compile_contract import PLAN_ROOT, CompileContext
from semantic_normalizers import (
    AmountNormalizer,
    Money,
    NormalizationError,
    OperatorNormalizer,
    Period,
    PeriodNormalizer,
    RankLimitNormalizer,
    RelativeWindow,
)
from semantic_plan import SemanticNode, SemanticPlanV2

# 슬롯 이름 상수(문자열 리터럴이 코드에 흩어지지 않게).
SLOT_CART_AGGREGATE = "cart_aggregate"
SLOT_AGGREGATE_CONDITIONS = "aggregate_conditions"
SLOT_CAMPAIGN_FREQUENCY = "campaign_response_frequency"
SLOT_CAMPAIGN_BUY_AMOUNT = "campaign_buy_amount"
SLOT_BALANCE_CONDITIONS = "balance_conditions"
SLOT_PROFILE_DATE_CONDITIONS = "profile_date_conditions"
SLOT_METRIC_TREND = "metric_trend"
SLOT_ENTITY_SET = "entity_set_condition"
SLOT_RELATIONAL_OPERATION = "relational_operation"
SLOT_MEMBER_METRIC_RANKING = "member_metric_ranking"


def member_container() -> str:
    """회원 조건 슬롯이 들어갈 실행 플랜 컨테이너 이름(도메인 선언이 권위)."""
    return semantic_domain_binding.plan_container("member_condition") or "target_user"


def member_slot_path(slot: str) -> str:
    """'<컨테이너>.<슬롯>' 감사 경로. 컨테이너 이름이 코드에 흩어지지 않게 한 곳에서 만든다."""
    return f"{member_container()}.{slot}"


@dataclass
class CompileResult(compile_contract.CompileResult):
    """도메인 컴파일 산출물 — 코어 계약에 컨테이너 별칭만 얹는다.

    코어 파이프라인은 컨테이너 **이름**을 모르고 `containers` 를 그대로 옮겨 쓴다.
    `target_user` / `plan` 은 이 도메인 컴파일러 안에서만 쓰는 읽기 편의 별칭이다.
    """

    @property
    def target_user(self) -> dict[str, Any]:
        return self.container(member_container())

    @property
    def plan(self) -> dict[str, Any]:
        return self.container(PLAN_ROOT)

    def to_dict(self) -> dict[str, Any]:
        payload = super().to_dict()
        payload["target_user"] = copy.deepcopy(self.containers.get(member_container(), {}))
        payload["plan"] = copy.deepcopy(self.containers.get(PLAN_ROOT, {}))
        return payload


class LegacyQueryPlanCompiler:
    """SemanticPlanV2 를 기존 실행 슬롯으로 옮긴다. 순수 함수 — 같은 입력이면 같은 출력."""

    def compile(
        self,
        semantic_plan_v2: SemanticPlanV2,
        capabilities: Any = None,
        context: CompileContext | None = None,
    ) -> CompileResult:
        ctx = context or CompileContext()
        result = CompileResult()
        executable = self._executable_node_ids(semantic_plan_v2, capabilities)
        # 하위 노드는 부모가 통째로 컴파일한다(EntitySetMembership 의 ranked_set 등) —
        # 단독으로도 돌리면 '회원 집합과 연결되지 않은 상품 랭킹'으로 오판돼 요청이 막힌다.
        owned_by_parent = {
            child.id for node in semantic_plan_v2.walk()
            if node.type != semantic_plan.LogicalExpression.TYPE
            for child in node.children()
        }
        for node in semantic_plan_v2.walk():
            if node.type == semantic_plan.LogicalExpression.TYPE:
                continue
            if node.id in owned_by_parent:
                continue
            if node.missing_fields():
                # 결핍 노드는 컴파일하지 않는다 — 반쯤 채운 슬롯이 가짜 성공의 원천이다.
                continue
            if node.id not in executable:
                continue
            handler = self._HANDLERS.get(node.type)
            if handler is None:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                    "reason": f"'{node.type}' 컴파일러가 없다",
                })
                continue
            try:
                handler(self, node, ctx, result)
            except NormalizationError as exc:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.VALIDATION_MISMATCH,
                    "reason": f"값 정규화 실패({exc.code}): {exc}",
                })
            except (KeyError, TypeError, ValueError) as exc:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.INTERNAL_FAULT,
                    "reason": f"{exc.__class__.__name__}: {exc}",
                })
        return result

    # ── capability 게이트 ──
    @staticmethod
    def _executable_node_ids(plan: SemanticPlanV2, capabilities: Any) -> frozenset[str]:
        if capabilities is None:
            return frozenset(node.id for node in plan.walk())
        verdicts = capabilities.judge_plan(plan) if hasattr(capabilities, "judge_plan") else []
        return frozenset(
            verdict.node_id for verdict in verdicts if getattr(verdict, "executable", False)
        )

    # ── 공통 값 처리 ──
    @staticmethod
    def _threshold(node: SemanticNode) -> tuple[int | float, str | None]:
        value = AmountNormalizer.normalize(node.values.get("value"), unit_hint=node.values.get("unit"))
        if isinstance(value, Money):
            return value.amount, "currency"
        return value.value, value.unit

    @staticmethod
    def _operator(node: SemanticNode, field_name: str = "operator") -> str:
        return OperatorNormalizer.normalize(node.values.get(field_name))

    @staticmethod
    def _period(node: SemanticNode, field_name: str, ctx: CompileContext) -> Period | RelativeWindow | None:
        """상대 창은 상대인 채로 돌려준다 — 어떤 슬롯이 롤링 창을 그대로 받는지(카트/파생 집합)와
        절대 구간을 요구하는지(기간 대 기간)는 슬롯마다 다르고, 그 판단은 각 핸들러 소관이다."""
        raw = node.values.get(field_name)
        if raw in (None, "", {}, []):
            return None
        return PeriodNormalizer.normalize(raw, today=None)

    @classmethod
    def _absolute_period(cls, node: SemanticNode, field_name: str, ctx: CompileContext) -> Period | None:
        """절대 구간을 요구하는 슬롯용 — 상대 창은 주입된 기준일로 확정한다."""
        window = cls._period(node, field_name, ctx)
        if isinstance(window, RelativeWindow):
            return window.resolve(ctx.today) if ctx.today is not None else None
        return window

    @staticmethod
    def _coerce(ctx: CompileContext, slot_name: str, raw: Any, allowed_key: str | None) -> Any:
        shape = ctx.slot_shapes.get(slot_name)
        if shape is None:
            return raw
        allowed = ctx.allowed.get(allowed_key) if allowed_key else None
        return shape.coerce(raw, allowed=allowed)

    @staticmethod
    def _append_list_slot(result: CompileResult, node: SemanticNode, slot: str, items: list[Any]) -> None:
        bucket = result.target_user.setdefault(slot, [])
        bucket.extend(items)
        result.node_slots[node.id] = member_slot_path(slot)

    # ── 노드별 컴파일 ────────────────────────────────────────────────────────────
    def _compile_aggregate_predicate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        scope = str(node.values.get("scope") or "")
        slot = ctx.scope_slots.get(scope) or self._DEFAULT_SCOPE_SLOTS.get(scope)
        if slot is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"집계 도메인 '{scope}' 의 실행 슬롯이 없다",
            })
            return
        if slot == SLOT_CART_AGGREGATE:
            self._compile_cart_aggregate(node, ctx, result)
        elif slot == SLOT_AGGREGATE_CONDITIONS:
            self._compile_order_aggregate(node, ctx, result)
        elif slot == SLOT_CAMPAIGN_FREQUENCY:
            self._compile_campaign_aggregate(node, ctx, result)
        elif slot == SLOT_BALANCE_CONDITIONS:
            self._compile_profile_aggregate(node, ctx, result)

    def _compile_cart_aggregate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        metric = ctx.resolve_metric("cart", node.values.get("metric"))
        threshold, _unit = self._threshold(node)
        item = {"metric": metric, "operator": self._operator(node), "threshold": threshold}
        coerced = self._coerce(ctx, SLOT_CART_AGGREGATE, [item], "cart_aggregate_metrics")
        if not coerced:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"장바구니 지표 '{metric}' 는 실행 어휘에 없다",
            })
            return
        self._append_list_slot(result, node, SLOT_CART_AGGREGATE, list(coerced))

    def _compile_order_aggregate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        metric = ctx.resolve_metric("aggregate", node.values.get("metric"))
        threshold, _unit = self._threshold(node)
        item: dict[str, Any] = {
            "metric_id": metric,
            "operator": self._operator(node),
            "threshold": threshold,
        }
        window = self._period(node, "period", ctx)
        if isinstance(window, RelativeWindow):
            item["window"] = {"value": window.value, "unit": window.unit}
        elif isinstance(window, Period):
            item["window_days"] = None
            item.pop("window_days")
        grain = node.values.get("grain")
        if isinstance(grain, str) and grain:
            item["aggregation_scope"] = grain
        qualifier = node.values.get("qualifier")
        if isinstance(qualifier, str) and qualifier.strip():
            item["scope"] = {"brand": qualifier.strip()}
        coerced = self._coerce(ctx, SLOT_AGGREGATE_CONDITIONS, [item], "aggregate_metrics")
        if not coerced:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"집계 지표 '{metric}' 는 실행 어휘에 없다",
            })
            return
        self._append_list_slot(result, node, SLOT_AGGREGATE_CONDITIONS, list(coerced))

    def _compile_campaign_aggregate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        """캠페인 도메인은 지표에 따라 두 슬롯으로 갈린다(횟수 vs 귀속 금액)."""
        value = AmountNormalizer.normalize(node.values.get("value"), unit_hint=node.values.get("unit"))
        operator = self._operator(node)
        if isinstance(value, Money):
            slot: dict[str, Any] = {"operator": operator, "amount": value.amount}
            if str(node.values.get("aggregation") or "").lower() == "avg":
                slot["agg"] = "AVG"
            coerced = self._coerce(ctx, SLOT_CAMPAIGN_BUY_AMOUNT, slot, None)
            if coerced is None:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.VALIDATION_MISMATCH,
                    "reason": "캠페인 귀속 금액 슬롯 검증 실패",
                })
                return
            result.target_user[SLOT_CAMPAIGN_BUY_AMOUNT] = coerced
            result.node_slots[node.id] = member_slot_path(SLOT_CAMPAIGN_BUY_AMOUNT)
            return
        event = node.values.get("event") or ctx.resolve_metric("campaign_event", node.values.get("metric"))
        slot = {"event": event, "operator": operator, "count": int(value.value)}
        coerced = self._coerce(ctx, SLOT_CAMPAIGN_FREQUENCY, slot, "campaign_frequency_events")
        if coerced is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"캠페인 이벤트 '{event}' 는 실행 어휘에 없다",
            })
            return
        result.target_user[SLOT_CAMPAIGN_FREQUENCY] = coerced
        result.node_slots[node.id] = member_slot_path(SLOT_CAMPAIGN_FREQUENCY)

    def _compile_profile_aggregate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        metric = ctx.resolve_metric("profile", node.values.get("metric"))
        threshold, _unit = self._threshold(node)
        item = {"metric_id": metric, "operator": self._operator(node), "threshold": threshold}
        coerced = self._coerce(ctx, SLOT_BALANCE_CONDITIONS, [item], "profile_metrics")
        if not coerced:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"프로필 지표 '{metric}' 는 실행 어휘에 없다",
            })
            return
        self._append_list_slot(result, node, SLOT_BALANCE_CONDITIONS, list(coerced))

    def _compile_predicate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        """단일 술어는 프로필 지표 임계(잔액/주기)와 날짜 상태 두 슬롯으로 간다."""
        metric = ctx.resolve_metric("profile", node.values.get("metric"))
        state = node.values.get("state")
        if isinstance(state, str) and state.strip():
            item = {"metric_id": metric, "state": state.strip()}
            coerced = self._coerce(ctx, SLOT_PROFILE_DATE_CONDITIONS, [item], "profile_date_states")
            if not coerced:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                    "reason": f"프로필 날짜 상태 '{state}' 는 실행 어휘에 없다",
                })
                return
            self._append_list_slot(result, node, SLOT_PROFILE_DATE_CONDITIONS, list(coerced))
            return
        threshold, _unit = self._threshold(node)
        item = {"metric_id": metric, "operator": self._operator(node), "threshold": threshold}
        coerced = self._coerce(ctx, SLOT_BALANCE_CONDITIONS, [item], "profile_metrics")
        if not coerced:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"프로필 지표 '{metric}' 는 실행 어휘에 없다",
            })
            return
        self._append_list_slot(result, node, SLOT_BALANCE_CONDITIONS, list(coerced))

    def _compile_metric_comparison(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        baseline = self._absolute_period(node, "baseline", ctx)
        current = self._absolute_period(node, "current", ctx)
        if not isinstance(baseline, Period) or not isinstance(current, Period):
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.MISSING_ARGUMENT,
                "reason": "기간 대 기간 비교에는 확정된 두 기간이 필요하다",
            })
            return
        metric = ctx.resolve_metric("aggregate", node.values.get("metric"))
        slot: dict[str, Any] = {
            "metric_id": metric,
            "direction": str(node.values.get("relation")),
            "baseline": baseline.to_window(),
            "current": current.to_window(),
        }
        threshold = node.values.get("threshold")
        if threshold not in (None, "", {}, []):
            value = AmountNormalizer.normalize(threshold)
            operator = OperatorNormalizer.normalize_or_none(node.values.get("threshold_operator")) or ">="
            slot["relative_change"] = {
                "unit": "percent",
                "comparisons": [{"operator": operator,
                                 "value": value.amount if isinstance(value, Money) else value.value}],
            }
        coerced = self._coerce(ctx, SLOT_METRIC_TREND, slot, "aggregate_metrics")
        if coerced is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"증감 지표 '{metric}' 는 실행 어휘에 없다",
            })
            return
        result.target_user[SLOT_METRIC_TREND] = coerced
        result.node_slots[node.id] = member_slot_path(SLOT_METRIC_TREND)

    def _compile_ranked_set(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        entity = str(node.values.get("entity") or "")
        if entity != "member":
            # 회원이 아닌 엔터티 랭킹은 단독 타겟이 아니라 entity_set_membership 의 피연산자다.
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"'{entity}' 랭킹은 단독 타겟 조건이 아니다(회원 집합과 연결해야 한다)",
            })
            return
        limit = RankLimitNormalizer.normalize(node.values.get("limit"))
        slot: dict[str, Any] = {
            "metric_id": ctx.resolve_metric("member_metric", node.values.get("metric")),
            "direction": "high" if str(node.values.get("direction")) == "descending" else "low",
        }
        if limit.type == "percent":
            slot.update({"limit_type": "percent", "percent": limit.value})
        else:
            slot.update({"limit_type": "count", "top_n": int(limit.value)})
        coerced = self._coerce(ctx, SLOT_MEMBER_METRIC_RANKING, slot, "member_metrics")
        if coerced is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"회원 지표 '{slot['metric_id']}' 는 랭킹 어휘에 없다",
            })
            return
        result.plan[SLOT_MEMBER_METRIC_RANKING] = coerced
        result.node_slots[node.id] = SLOT_MEMBER_METRIC_RANKING

    def _compile_entity_set_membership(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        ranked = node.children()
        if not ranked:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.MISSING_ARGUMENT,
                "reason": "파생 집합 조건에 랭킹 집합이 없다",
            })
            return
        ranking = ranked[0]
        limit = RankLimitNormalizer.normalize(ranking.values.get("limit"))
        if limit.type != "count":
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": "파생 집합의 랭킹 제한은 개수만 지원한다",
            })
            return
        slot: dict[str, Any] = {
            "entity": str(ranking.values.get("entity")),
            "measure": ctx.resolve_metric("entity_set_measure", ranking.values.get("metric")),
            "direction": "top" if str(ranking.values.get("direction")) == "descending" else "bottom",
            "limit": int(limit.value),
            "relation": str(node.values.get("relation")),
            "negated": bool(node.values.get("negated")),
        }
        window = self._period(ranking, "period", ctx)
        if isinstance(window, Period):
            slot["window"] = window.to_window()
        elif isinstance(window, RelativeWindow):
            slot["window"] = {"days": window.days}
        cardinality = node.values.get("cardinality")
        if cardinality not in (None, "", {}, []):
            quantity = AmountNormalizer.normalize(cardinality)
            slot["cardinality"] = {
                "operator": OperatorNormalizer.normalize_or_none(node.values.get("cardinality_operator")) or ">=",
                "value": int(quantity.amount if isinstance(quantity, Money) else quantity.value),
            }
        if node.source_span:
            slot["surface"] = node.source_span
        result.target_user[SLOT_ENTITY_SET] = slot
        result.node_slots[node.id] = member_slot_path(SLOT_ENTITY_SET)

    def _compile_co_purchase(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        """동시구매 관계 → condition_evaluations 실행 IR(조건 판정 grain 이 분리된 계약).

        슬롯이 아니라 IR 이지만 소유 규칙은 같다 — 이 컴파일러만 쓴다. IR 본체는 기존
        검증된 빌더가 만들고(capability 서명 고정), 여기서는 노드가 소유한 창과 근거
        스팬만 넘긴다.
        """
        import condition_evaluation_ir  # 순환 없음(IR 모듈은 컴파일러를 모른다)

        window = self._period(node, "period", ctx)
        purchase_date = window.to_window() if isinstance(window, Period) else None
        evaluation = condition_evaluation_ir.build_same_product_co_purchase_evaluation(
            node.source_span or "", purchase_date
        )
        if node.source_start is not None and node.source_end is not None:
            evaluation["source_span"] = {"start": node.source_start, "end": node.source_end}
        else:
            evaluation.pop("source_span", None)
        issues = condition_evaluation_ir.validate_evaluations([evaluation])
        if issues:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.VALIDATION_MISMATCH,
                "reason": "동시구매 판정 IR 이 capability 서명 검증에 실패했다",
                "issues": [issue.to_dict() for issue in issues],
            })
            return
        result.plan.setdefault(condition_evaluation_ir.PLAN_KEY, []).append(evaluation)
        result.node_slots[node.id] = condition_evaluation_ir.PLAN_KEY

    def _compile_relation_predicate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        """속성 시점·전이 조건 → relational_operation 슬롯(리졸버가 실행 IR 로 귀결).

        관계명으로 분기하지 않는다: 관계를 **범용 시간 연산자**로 정규화한 뒤, 도메인 선언의
        `temporal.execution_operators` 표로 실행 연산자를 얻는다. 새 표현·새 실행 연산자
        이름은 선언 한 줄로 열리고, 시간 축이 아닌 관계(동시구매 등)는 자연히 여기서 빠진다.
        """
        relation = str(node.values.get("relation") or "")
        operator_id = semantic_domain_binding.temporal_operator_of(node)
        if operator_id is None:
            # 시간 축이 아닌 관계 — 전용 컴파일러로 넘긴다.
            handler = self._RELATION_HANDLERS.get(relation)
            if handler is None:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                    "reason": f"'{relation}' 관계의 컴파일러가 없다",
                })
                return
            handler(self, node, ctx, result)
            return
        window = self._period(node, "period", ctx)
        # 시점 앵커의 유무로 실행 연산자가 갈리는 연산자가 있다(최신 스냅샷 vs 지정 시점) —
        # 의미 노드는 그 구분을 알 필요가 없고, 앵커 유무로 여기서 결정한다.
        operator = semantic_domain_binding.execution_operator(
            operator_id, anchored=isinstance(window, Period)
        )
        if operator is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"'{operator_id}' 시간 연산의 실행 컴파일러가 선언되어 있지 않다",
            })
            return
        slot: dict[str, Any] = {
            "attribute_id": ctx.resolve_metric("history_attribute", node.values.get("attribute")),
            "operator": operator,
        }
        for source in ("value", "from_value", "to_value", "value_comparison"):
            raw = node.values.get(source)
            if isinstance(raw, str) and raw.strip():
                slot[source] = raw.strip()
        if isinstance(window, Period):
            slot["month"] = window.start[:6]
        elif isinstance(window, RelativeWindow):
            slot["months"] = max(1, window.value if window.unit == "months" else window.days // 30)
        months = node.values.get("months")
        if isinstance(months, (int, float)) and not isinstance(months, bool):
            slot["months"] = int(months)
        count = node.values.get("count")
        if count not in (None, "", {}, []):
            quantity = AmountNormalizer.normalize(count)
            slot["change_count"] = int(quantity.amount if isinstance(quantity, Money) else quantity.value)
            operator = OperatorNormalizer.normalize_or_none(node.values.get("count_operator"))
            if operator:
                slot["change_count_operator"] = {">=": "gte", ">": "gt", "<=": "lte", "<": "lt", "=": "eq"}.get(
                    operator, "gte"
                )
        coerced = self._coerce(ctx, SLOT_RELATIONAL_OPERATION, slot, "history_attributes")
        if coerced is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"속성 이력 '{slot['attribute_id']}' 는 카탈로그에 없다",
            })
            return
        result.target_user[SLOT_RELATIONAL_OPERATION] = coerced
        result.node_slots[node.id] = member_slot_path(SLOT_RELATIONAL_OPERATION)

    _DEFAULT_SCOPE_SLOTS: dict[str, str] = {
        "cart": SLOT_CART_AGGREGATE,
        "order": SLOT_AGGREGATE_CONDITIONS,
        "campaign": SLOT_CAMPAIGN_FREQUENCY,
        "profile": SLOT_BALANCE_CONDITIONS,
    }

    # 시간 축이 아닌 관계의 전용 컴파일러(관계명 → 핸들러). 시간 축 관계는 여기 없다 —
    # 그쪽은 범용 시간 연산자 + 도메인 실행 연산자 표가 처리한다.
    _RELATION_HANDLERS: dict[str, Callable[..., None]] = {
        "co_purchase": _compile_co_purchase,
    }

    _HANDLERS: dict[str, Callable[..., None]] = {
        semantic_plan.AggregatePredicate.TYPE: _compile_aggregate_predicate,
        semantic_plan.Predicate.TYPE: _compile_predicate,
        semantic_plan.MetricComparison.TYPE: _compile_metric_comparison,
        semantic_plan.RankedSet.TYPE: _compile_ranked_set,
        semantic_plan.EntitySetMembership.TYPE: _compile_entity_set_membership,
        semantic_plan.RelationPredicate.TYPE: _compile_relation_predicate,
    }


# SemanticPlan 노드 → 실행 슬롯 매핑표(문서·테스트가 읽는 파생 표).
NODE_SLOT_MAP: dict[str, tuple[str, ...]] = {
    semantic_plan.Predicate.TYPE: (
        member_slot_path(SLOT_BALANCE_CONDITIONS),
        member_slot_path(SLOT_PROFILE_DATE_CONDITIONS),
    ),
    semantic_plan.AggregatePredicate.TYPE: (
        member_slot_path(SLOT_CART_AGGREGATE),
        member_slot_path(SLOT_AGGREGATE_CONDITIONS),
        member_slot_path(SLOT_CAMPAIGN_FREQUENCY),
        member_slot_path(SLOT_CAMPAIGN_BUY_AMOUNT),
        member_slot_path(SLOT_BALANCE_CONDITIONS),
    ),
    semantic_plan.MetricComparison.TYPE: (member_slot_path(SLOT_METRIC_TREND),),
    semantic_plan.RankedSet.TYPE: (SLOT_MEMBER_METRIC_RANKING,),
    semantic_plan.EntitySetMembership.TYPE: (member_slot_path(SLOT_ENTITY_SET),),
    semantic_plan.RelationPredicate.TYPE: (
        member_slot_path(SLOT_RELATIONAL_OPERATION),
        "condition_evaluations",
    ),
    semantic_plan.LogicalExpression.TYPE: (),
}

# 이 컴파일러가 소유하는 슬롯 전체 — 다른 생산자가 쓰면 드리프트 가드가 잡는다.
COMPILER_OWNED_SLOTS: frozenset[str] = frozenset(
    slot for slots in NODE_SLOT_MAP.values() for slot in slots
)


__all__ = [
    "COMPILER_OWNED_SLOTS",
    "PLAN_ROOT",
    "CompileContext",
    "CompileResult",
    "LegacyQueryPlanCompiler",
    "NODE_SLOT_MAP",
    "member_container",
    "member_slot_path",
    "SLOT_AGGREGATE_CONDITIONS",
    "SLOT_BALANCE_CONDITIONS",
    "SLOT_CAMPAIGN_BUY_AMOUNT",
    "SLOT_CAMPAIGN_FREQUENCY",
    "SLOT_CART_AGGREGATE",
    "SLOT_ENTITY_SET",
    "SLOT_MEMBER_METRIC_RANKING",
    "SLOT_METRIC_TREND",
    "SLOT_PROFILE_DATE_CONDITIONS",
    "SLOT_RELATIONAL_OPERATION",
]
