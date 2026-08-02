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
from datetime import date
from typing import Any, Callable

import compile_contract
import semantic_domain_binding
import semantic_plan
from compile_contract import PLAN_ROOT, CompileContext
from semantic_normalizers import (
    COUNT_ABSENCE,
    COUNT_CONTRADICTION,
    COUNT_EXISTENCE,
    COUNT_TAUTOLOGY,
    COUNT_THRESHOLD,
    AmountNormalizer,
    CountThresholdNormalizer,
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
# 존재/부재 환원 착지 슬롯. 임계 슬롯과 달리 구조화기도 직접 채운다(존재·부재는 원문 표면이 곧
# 슬롯이라 노드를 거칠 이유가 없다) — 그래서 매핑표에는 싣되 소유는 주장하지 않는다(아래 참조).
SLOT_PURCHASE_MEMBERSHIP = "purchase_membership"
SLOT_PURCHASE_INACTIVITY = "purchase_inactivity"

# 캠페인 집계의 슬롯 분기 축 = **지표**. 실행 어휘 `campaign_frequency_events` 에 있으면 반응
# 횟수, 금액 지표 하나면 귀속 금액 — 이 둘의 합집합이 LLM 에 노출되는 campaign_metric_id 다.
# 금액 지표 id 가 슬롯 이름과 같은 것은 설정이 그렇게 선언했기 때문이고, 여기서 이름을 새로
# 짓지 않는다(두 번째 권위를 만들면 어휘와 컴파일러가 갈라진다).
CAMPAIGN_FREQUENCY_VOCAB = "campaign_frequency_events"
CAMPAIGN_AMOUNT_METRIC = SLOT_CAMPAIGN_BUY_AMOUNT


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

    @classmethod
    def _coerce_failure_detail(
        cls,
        ctx: CompileContext,
        slot_name: str,
        item: dict[str, Any],
        allowed_key: str | None,
        core_keys: frozenset[str],
    ) -> str:
        """슬롯 검증이 **왜** 실패했는지 — 슬롯이 스스로 답하게 한다.

        1순위는 슬롯 shape 이 선언한 거부 사유다(`SlotShape.reject`). 그것이 coerce 와 **같은**
        판정 함수를 쓰므로 사유와 실동작이 어긋날 수 없다. 이 계층이 없던 동안에는 값 도메인
        거부(임계값 0)까지 "지표가 실행 어휘에 없다"로 보고돼, 어휘에 멀쩡히 있는 지표를 두고
        운영자가 카탈로그를 뒤졌다(실측 2026-08-02: `order_count > 0`).

        사유 선언이 없는 슬롯만 이분 판정(핵심 항목만 남겨 다시 coerce)으로 넘어가 한정어(기간·
        브랜드·그레인) 탓인지를 가른다.
        """
        detail = cls._declared_rejection(ctx, slot_name, item, allowed_key)
        if detail:
            return detail
        core = {key: value for key, value in item.items() if key in core_keys}
        if core == item or not cls._coerce(ctx, slot_name, [core], allowed_key):
            return ""
        extra = ", ".join(sorted(set(item) - set(core)))
        return f"이 조건에는 한정어({extra})를 함께 적용할 수 없다"

    @staticmethod
    def _declared_rejection(
        ctx: CompileContext, slot_name: str, item: Any, allowed_key: str | None
    ) -> str:
        """슬롯이 선언한 거부 사유(없으면 빈 문자열). shape 는 주입된 것만 쓴다 — 레지스트리를 열지 않는다."""
        shape = ctx.slot_shapes.get(slot_name)
        reject = getattr(shape, "reject", None)
        if not callable(reject):
            return ""
        allowed = ctx.allowed.get(allowed_key) if allowed_key else None
        try:
            return str(reject(item, allowed) or "")
        except Exception:  # noqa: BLE001 — 진단이 컴파일을 죽이지 않는다.
            return ""

    @staticmethod
    def _append_list_slot(result: CompileResult, node: SemanticNode, slot: str, items: list[Any]) -> None:
        bucket = result.target_user.setdefault(slot, [])
        bucket.extend(items)
        result.node_slots[node.id] = member_slot_path(slot)

    @staticmethod
    def _set_scalar_slot(
        result: CompileResult,
        node: SemanticNode,
        slot: str,
        value: Any,
        *,
        bucket: dict[str, Any] | None = None,
        path: str | None = None,
    ) -> None:
        """스칼라 슬롯은 조건 **하나만** 담는다 — 두 번째 노드는 덮어쓰지 않고 충돌로 보고한다.

        덮어쓰면 노드 순서가 곧 대상이 되고(마지막 노드가 이긴다) 사라진 조건의 근거가 어디에도
        남지 않는다. 실측(2026-08-02): 캠페인 노드 둘이 같은 슬롯에 쓰여 앞 조건이 소실됐는데
        요구사항 원장은 `compiled: 2` 로 보고했다 — 조용히 좁아진 오디언스가 가짜 성공으로 나간다.
        같은 판정이 존재/부재 환원 경로에는 이미 있었고(:meth:`_compile_count_presence`),
        나머지 스칼라 슬롯에는 없었다. 여기서 그 규칙을 한 곳으로 모은다.

        값이 **같으면** 충돌이 아니다(같은 의미의 중복 방출) — 그때는 멱등하게 통과시킨다.
        여러 조건을 담을 수 있는 슬롯은 이 함수가 아니라 :meth:`_append_list_slot` 소관이다.
        """
        target = result.target_user if bucket is None else bucket
        slot_path = member_slot_path(slot) if path is None else path
        existing = target.get(slot)
        if slot in target and existing != value:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.VALIDATION_MISMATCH,
                "reason": (
                    f"'{slot_path}' 슬롯을 두 조건이 동시에 요구한다 — 이 슬롯은 조건 하나만 "
                    f"담는다({existing} vs {value})"
                ),
                # 슬롯 소유 소거의 대상이 아니다. 소거의 전제는 "같은 의미가 이미 실행 플랜에
                # 있다"인데, 충돌은 정반대다 — 슬롯을 **다른 조건**이 갖고 있어서 이 조건이
                # 표현되지 못했다. 걷어내면 조용히 좁아진 오디언스가 그대로 나간다.
                "supersedable": False,
            })
            return
        target[slot] = value
        result.node_slots[node.id] = slot_path

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
            detail = self._coerce_failure_detail(
                ctx, SLOT_CART_AGGREGATE, item, "cart_aggregate_metrics",
                frozenset({"metric", "operator", "threshold"}),
            )
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": detail or f"장바구니 지표 '{metric}' 는 실행 어휘에 없다",
            })
            return
        self._append_list_slot(result, node, SLOT_CART_AGGREGATE, list(coerced))

    @staticmethod
    def _rolling_window_days(
        window: Period | RelativeWindow | None, ctx: CompileContext
    ) -> tuple[int | None, str]:
        """집계 창을 롤링 일수로 확정한다. 반환: (일수|None, 확정 불가 사유|"").

        상대 창('최근 3개월')은 그대로 일수다. 절대 구간은 **오늘로 끝나는 후행 창일 때만**
        일수로 환산한다 — 달력 구간('2026년 5월')을 일수로 바꾸면 구간이 오늘 쪽으로 밀려
        원문에 없는 기간이 된다. 확정할 수 없으면 사유를 돌려주고 호출자가 fail-close 한다:
        조용히 창 없는(=평생) 집계로 내리면 '최근 3개월 5건'이 '평생 5건'이 되는 가짜 성공이다
        (실측 2026-08-02: 절대 구간 분기가 set-then-pop no-op 이라 창이 통째로 사라졌다).
        """
        if isinstance(window, RelativeWindow):
            return window.days, ""
        if not isinstance(window, Period):
            return None, ""
        if ctx.today is None:
            return None, "기준일이 주입되지 않아 기간을 확정할 수 없다"
        try:
            start = date(int(window.start[:4]), int(window.start[4:6]), int(window.start[6:8]))
            end = date(int(window.end[:4]), int(window.end[4:6]), int(window.end[6:8]))
        except (TypeError, ValueError):
            return None, f"기간 '{window.start}~{window.end}' 을 날짜로 해석할 수 없다"
        if end < ctx.today:
            label = window.label or f"{window.start}~{window.end}"
            return None, (
                f"'{label}' 처럼 과거에서 끝나는 달력 구간의 집계는 아직 지원하지 않는다"
                "(현재는 오늘로 끝나는 최근 N일 창만 컴파일된다)"
            )
        days = (ctx.today - start).days + 1
        if days <= 0:
            return None, f"기간 '{window.start}~{window.end}' 의 시작이 기준일보다 뒤에 있다"
        return days, ""

    # 집계 도메인 → (존재 슬롯, 부재 슬롯). 카운트 대수가 임계 비교를 존재/부재로 환원했을 때만 쓴다.
    _SCOPE_PRESENCE_SLOTS: dict[str, tuple[str, str]] = {
        "order": (SLOT_PURCHASE_MEMBERSHIP, SLOT_PURCHASE_INACTIVITY),
    }

    @staticmethod
    def _has_narrowing_qualifier(node: SemanticNode) -> bool:
        """이 노드가 존재/부재 슬롯이 담을 수 없는 **범위 한정어**를 들고 있는가.

        담을 수 없는 것을 환원하면 조건이 실패하는 게 아니라 **조용히 넓어진다** — 실패보다 나쁘다.
        """
        qualifier = node.values.get("qualifier")
        if isinstance(qualifier, str) and qualifier.strip():
            return True
        grain = node.values.get("grain")
        return isinstance(grain, str) and grain not in ("", "per_member")

    def _compile_count_presence(
        self,
        node: SemanticNode,
        ctx: CompileContext,
        result: CompileResult,
        *,
        scope: str,
        domain: str,
        metric: str | None,
        operator: str,
        threshold: int | float,
        window_days: int | None,
        window_issue: str,
    ) -> bool:
        """카운트 임계 비교를 존재/부재로 환원한다. 이 노드를 여기서 처리했으면 True.

        '주문은 있었지만'(COUNT>0)과 '구매가 없는'(COUNT=0)은 임계 비교가 아니라 **존재/부재**다.
        표현이 아니라 값으로 갈리므로 어구 목록이 아니라 대수로 판정한다 — 새 카운트 지표가
        늘어도 여기 손댈 것이 없고, 무엇이 카운트인지는 지표 스펙 선언이 소유한다(ctx.count_metrics).

        이 환원이 없던 동안 존재 표현은 갈 곳이 없어 임계 슬롯의 양수 도메인에 걸렸고, 요청 전체가
        "지표가 실행 어휘에 없다"로 막혔다(2026-08-02 실측 4/4). 항진식·모순도 여기서 정직하게
        끊는다 — '0건 이상'은 조건이 아니라 모든 회원이고, 조건인 척 SQL 에 들어가면 안 된다.
        """
        verdict = CountThresholdNormalizer.classify(operator, threshold)
        if verdict == COUNT_THRESHOLD:
            return False
        # 존재/부재 슬롯은 **회원 단위 구매 사실**만 표현한다 — 브랜드·카테고리 한정어도, 회원 외
        # grain(per_brand 등)도 담을 자리가 없다. 그런 노드를 환원하면 한정어가 조용히 사라져
        # 대상이 넓어진다('나이키를 1번 이상' → 그냥 '구매한 적 있음'). 한정어가 붙은 노드는
        # 임계 경로에 남긴다: '>=1' 은 양수라 그대로 컴파일되고, '>0' 은 정직하게 거부된다.
        if self._has_narrowing_qualifier(node):
            return False
        surface = f"{metric} {operator} {threshold}"
        if verdict == COUNT_TAUTOLOGY:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"'{surface}' 는 모든 대상이 만족하는 항진식이라 타겟 조건이 아니다",
            })
            return True
        if verdict == COUNT_CONTRADICTION:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": f"'{surface}' 를 만족하는 대상은 존재할 수 없다",
            })
            return True
        # 존재/부재 환원은 정수 카운트 위에서만 성립한다. 합계·비율 지표는 임계 경로 그대로 간다.
        if metric not in (ctx.count_metrics.get(domain) or frozenset()):
            return False
        slots = self._SCOPE_PRESENCE_SLOTS.get(scope)
        if slots is None:
            return False
        existence_slot, absence_slot = slots
        if window_issue:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": window_issue,
            })
            return True
        if verdict == COUNT_EXISTENCE:
            value: dict[str, Any] = {"operator": "exists", "window_days": window_days}
            slot_name = existence_slot
        else:  # COUNT_ABSENCE
            if window_days is None:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                    "reason": (
                        f"기간이 없는 '{surface}'(평생 부재)는 이 경로가 컴파일하지 않는다 — "
                        "기간을 붙이거나 무구매 행동으로 표현해야 한다"
                    ),
                })
                return True
            value = {"min_days": window_days}
            slot_name = absence_slot
        coerced = self._coerce(ctx, slot_name, value, None)
        if coerced is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.VALIDATION_MISMATCH,
                "reason": (
                    self._declared_rejection(ctx, slot_name, value, None)
                    or f"'{surface}' 의 존재/부재 환원 값이 슬롯 검증을 통과하지 못했다"
                ),
            })
            return True
        existing = result.target_user.get(slot_name)
        if existing is not None and existing != coerced:
            # 존재/부재 슬롯은 값이 하나뿐이라 두 번째 노드를 담을 자리가 없다. 덮어쓰면 **노드 순서가
            # 곧 대상**이 되고(마지막 노드가 이긴다) 그 사실이 어디에도 안 남는다. 조용히 하나를
            # 고르느니 무엇이 충돌했는지 말하고 막는다.
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": (
                    f"같은 조건 축에 서로 다른 {'존재' if verdict == COUNT_EXISTENCE else '부재'} 창이 "
                    f"둘 있다({existing} vs {coerced}) — 한 번에 하나만 표현할 수 있다"
                ),
            })
            return True
        result.target_user[slot_name] = coerced
        result.node_slots[node.id] = member_slot_path(slot_name)
        return True

    def _compile_order_aggregate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        metric = ctx.resolve_metric("aggregate", node.values.get("metric"))
        threshold, _unit = self._threshold(node)
        operator = self._operator(node)
        window = self._period(node, "period", ctx)
        window_days, window_issue = self._rolling_window_days(window, ctx)
        if self._compile_count_presence(
            node, ctx, result,
            scope="order", domain="aggregate", metric=metric, operator=operator,
            threshold=threshold, window_days=window_days, window_issue=window_issue,
        ):
            return
        item: dict[str, Any] = {
            "metric_id": metric,
            "operator": operator,
            "threshold": threshold,
        }
        if isinstance(window, RelativeWindow):
            item["window"] = {"value": window.value, "unit": window.unit}
        elif isinstance(window, Period):
            # 절대 구간은 오늘로 끝나는 후행 창일 때만 롤링 일수가 된다. 아니면 조용히 버리지 않고
            # 여기서 끊는다 — 창이 사라진 집계는 '평생'이 되어 원문과 다른 대상을 낸다.
            if window_issue:
                result.failures.append({
                    "node_id": node.id,
                    "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                    "reason": window_issue,
                })
                return
            item["window_days"] = window_days
        grain = node.values.get("grain")
        if isinstance(grain, str) and grain:
            item["aggregation_scope"] = grain
        qualifier = node.values.get("qualifier")
        if isinstance(qualifier, str) and qualifier.strip():
            item["scope"] = {"brand": qualifier.strip()}
        coerced = self._coerce(ctx, SLOT_AGGREGATE_CONDITIONS, [item], "aggregate_metrics")
        if not coerced:
            detail = self._coerce_failure_detail(
                ctx, SLOT_AGGREGATE_CONDITIONS, item, "aggregate_metrics",
                frozenset({"metric_id", "operator", "threshold"}),
            )
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": detail or f"집계 지표 '{metric}' 는 실행 어휘에 없다",
            })
            return
        self._append_list_slot(result, node, SLOT_AGGREGATE_CONDITIONS, list(coerced))

    def _compile_campaign_aggregate(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        """캠페인 도메인은 **지표**에 따라 두 슬롯으로 갈린다(반응 횟수 vs 귀속 금액).

        값의 모양으로 가르지 않는다. strict function calling 은 수량 객체의 키를 전부 채우므로
        '3회'가 `{"amount": 3, "currency": null, "value": 3, "unit": "회"}` 로 오고, 금액 판별이
        `amount` 키의 존재를 보는 순간 **횟수가 금액이 된다**. 실측(2026-08-02): '발송 성공 3회
        이상'과 '구매반응 없음'이 둘 다 금액 슬롯으로 가서 뒤 노드가 앞 노드를 덮었고, 정작
        반응 횟수 슬롯은 비어 있었다.

        지표에는 그 모호성이 없다 — 캠페인 어휘는 '반응 이벤트들'과 '귀속 금액 하나'의
        합집합(`campaign_metric_id`)이고, 어느 쪽인지는 지표 이름 하나로 결정된다.
        """
        event = node.values.get("event") or ctx.resolve_metric("campaign_event", node.values.get("metric"))
        frequency_events = ctx.allowed.get(CAMPAIGN_FREQUENCY_VOCAB) or ()
        if event == CAMPAIGN_AMOUNT_METRIC and event not in frequency_events:
            self._compile_campaign_buy_amount(node, ctx, result)
            return
        # 어휘 밖 지표도 횟수 쪽으로 보낸다 — 금액으로 넘겨 짐작하면 조용히 다른 조건이 되고,
        # 이쪽에서는 슬롯이 "'X' 는 실행 어휘에 없다"고 사유를 대며 정직하게 막는다.
        self._compile_campaign_frequency(node, event, ctx, result)

    def _compile_campaign_buy_amount(
        self, node: SemanticNode, ctx: CompileContext, result: CompileResult
    ) -> None:
        amount, _unit = self._threshold(node)
        slot: dict[str, Any] = {"operator": self._operator(node), "amount": amount}
        if str(node.values.get("aggregation") or "").lower() == "avg":
            slot["agg"] = "AVG"
        coerced = self._coerce(ctx, SLOT_CAMPAIGN_BUY_AMOUNT, slot, None)
        if coerced is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.VALIDATION_MISMATCH,
                "reason": (
                    self._declared_rejection(ctx, SLOT_CAMPAIGN_BUY_AMOUNT, slot, None)
                    or "캠페인 귀속 금액 슬롯 검증 실패"
                ),
            })
            return
        self._set_scalar_slot(result, node, SLOT_CAMPAIGN_BUY_AMOUNT, coerced)

    def _compile_campaign_frequency(
        self, node: SemanticNode, event: Any, ctx: CompileContext, result: CompileResult
    ) -> None:
        count, _unit = self._threshold(node)
        slot: dict[str, Any] = {"event": event, "operator": self._operator(node), "count": int(count)}
        coerced = self._coerce(ctx, SLOT_CAMPAIGN_FREQUENCY, slot, CAMPAIGN_FREQUENCY_VOCAB)
        if coerced is None:
            result.failures.append({
                "node_id": node.id,
                "failure_code": semantic_plan.UNSUPPORTED_SEMANTICS,
                "reason": (
                    self._declared_rejection(ctx, SLOT_CAMPAIGN_FREQUENCY, slot, CAMPAIGN_FREQUENCY_VOCAB)
                    or f"캠페인 이벤트 '{event}' 는 실행 어휘에 없다"
                ),
            })
            return
        self._set_scalar_slot(result, node, SLOT_CAMPAIGN_FREQUENCY, coerced)

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
                "reason": (
                    self._declared_rejection(ctx, SLOT_BALANCE_CONDITIONS, item, "profile_metrics")
                    or f"프로필 지표 '{metric}' 는 실행 어휘에 없다"
                ),
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
                    "reason": (
                        self._declared_rejection(ctx, SLOT_PROFILE_DATE_CONDITIONS, item, "profile_date_states")
                        or f"프로필 날짜 상태 '{state}' 는 실행 어휘에 없다"
                    ),
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
                "reason": (
                    self._declared_rejection(ctx, SLOT_BALANCE_CONDITIONS, item, "profile_metrics")
                    or f"프로필 지표 '{metric}' 는 실행 어휘에 없다"
                ),
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
        self._set_scalar_slot(result, node, SLOT_METRIC_TREND, coerced)

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
        self._set_scalar_slot(
            result, node, SLOT_MEMBER_METRIC_RANKING, coerced,
            bucket=result.plan, path=SLOT_MEMBER_METRIC_RANKING,
        )

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
        self._set_scalar_slot(result, node, SLOT_ENTITY_SET, slot)

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
        self._set_scalar_slot(result, node, SLOT_RELATIONAL_OPERATION, coerced)

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
        # 카운트 대수가 임계 비교를 존재/부재로 환원했을 때의 착지 슬롯(_compile_count_presence).
        member_slot_path(SLOT_PURCHASE_MEMBERSHIP),
        member_slot_path(SLOT_PURCHASE_INACTIVITY),
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

# 컴파일러가 **환원으로 채울 수 있지만 소유하지는 않는** 슬롯.
#
# 소유(COMPILER_OWNED_SLOTS)는 "이 슬롯을 만드는 생산자는 컴파일러 하나뿐"이라는 선언이고,
# 그 대가로 그 슬롯은 LLM 노출면에서 빠진다(노출 XOR 컴파일러 소유). 존재/부재는 그 계약을
# 만족하지 못한다 — 원문 표면('구매한 적 있는', '최근 30일 미구매')이 곧 슬롯이라 구조화기가
# 직접 채우는 것이 맞고, 결정론 이탈 문형 정규화도 같은 슬롯을 쓴다. 그래서 두 갈래로 나눈다:
#   NODE_SLOT_MAP        — 사실 그대로(이 노드 타입이 이 슬롯으로 갈 수 있다). 매핑표는 거짓말하지 않는다.
#   COMPILER_OWNED_SLOTS — 배타적 소유만. 여기서 빼야 구조화기 노출·결정론 정규화가 이중 생산자가 되지 않는다.
SHARED_REDUCTION_SLOTS: frozenset[str] = frozenset({
    member_slot_path(SLOT_PURCHASE_MEMBERSHIP),
    member_slot_path(SLOT_PURCHASE_INACTIVITY),
})

# 이 컴파일러가 **배타적으로** 소유하는 슬롯 — 다른 생산자가 쓰면 드리프트 가드가 잡는다.
COMPILER_OWNED_SLOTS: frozenset[str] = frozenset(
    slot for slots in NODE_SLOT_MAP.values() for slot in slots
) - SHARED_REDUCTION_SLOTS


__all__ = [
    "COMPILER_OWNED_SLOTS",
    "SHARED_REDUCTION_SLOTS",
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
    "SLOT_PURCHASE_INACTIVITY",
    "SLOT_PURCHASE_MEMBERSHIP",
    "SLOT_RELATIONAL_OPERATION",
]
