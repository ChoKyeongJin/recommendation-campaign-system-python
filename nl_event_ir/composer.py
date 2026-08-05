"""의미 토큰 → Event IR 조합.

composer 는 **문자열을 다시 읽지 않는다.** 여기서 정규식을 하나라도 쓰기 시작하면 추출
규칙이 두 계층에 흩어지고, 그 순간 '어디를 고쳐야 하는가'가 불분명해진다. 이 파일이 보는
것은 토큰의 ``kind``·``value``·``span`` 뿐이다.

핵심 결정은 **귀속(attachment)** 하나다: 시간·횟수·범위·극성을 어느 사건에 붙일 것인가.
문장 전체에서 전역 목록으로 뽑아 두었다가 나중에 짝짓는 방식은 사건이 둘 이상이면 무너진다
(극성이 다른 두 조건이 있을 때 창이 반대편에 붙는다). 여기서는 **스팬 거리**로 귀속을
정한다 — 각 수식 토큰은 자기와 가장 가까운 사건의 것이다.

조합할 수 없는 상태를 조용히 넘기지 않는다. 사건이 없거나, 한 사건에 서로 다른 집계가
둘 붙거나, 엔티티가 모호하면 IR 을 만들지 않고 이유를 남긴다(CLAUDE.md 11·12).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from nl_event_ir.enums import (
    ComparisonOperator,
    EntityType,
    EventType,
    IssueSeverity,
    LogicOperator,
    MetricType,
    Polarity,
)
from nl_event_ir.models import (
    AbsoluteDateRange,
    ConditionGroup,
    ConditionNode,
    EventCondition,
    EventIR,
    MetricCondition,
    RankSpec,
    RelativeTimeWindow,
    ScopeFilter,
    ValidationIssue,
)
from nl_event_ir.extractors.scope import RESIDUAL_OBJECT_SOURCE
from nl_event_ir.resolver.base import EntityResolution
from nl_event_ir.tokenizer import SemanticToken, TokenKind, sort_tokens

__all__ = ["ComposeOutcome", "EventIRComposer"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ComposeOutcome:
    """조합 결과. IR 과 그 과정에서 나온 문제를 함께 돌려준다."""

    ir: EventIR | None
    issues: tuple[ValidationIssue, ...] = ()
    unresolved_values: tuple[str, ...] = ()


@dataclass(slots=True)
class _EventDraft:
    """조합 중인 사건 하나. 토큰을 모으는 임시 바구니이며 IR 로 굳기 전 상태다."""

    event_type: EventType
    anchor: SemanticToken
    time_tokens: list[SemanticToken] = field(default_factory=list)
    metric_tokens: list[SemanticToken] = field(default_factory=list)
    scope_tokens: list[SemanticToken] = field(default_factory=list)
    polarity_tokens: list[SemanticToken] = field(default_factory=list)


# 사건에 귀속시킬 수식 토큰의 종류.
_MODIFIER_KINDS = frozenset(
    {
        TokenKind.TIME_WINDOW,
        TokenKind.DATE_RANGE,
        TokenKind.COUNT_CONDITION,
        TokenKind.AGGREGATE,
        TokenKind.ENTITY_VALUE,
        TokenKind.POLARITY,
    }
)


class EventIRComposer:
    """토큰 묶음을 하나의 :class:`EventIR` 로 조합한다."""

    def compose(
        self,
        text: str,
        tokens: Sequence[SemanticToken],
    ) -> ComposeOutcome:
        ordered = sort_tokens(tokens)
        issues: list[ValidationIssue] = []

        target_entity = self._resolve_target(ordered, issues)
        self._report_unparsed(ordered, issues)
        drafts = self._collect_drafts(ordered)
        if not drafts:
            issues.append(
                ValidationIssue(
                    code="event.missing",
                    message=(
                        "문장에서 사건(구매/로그인/장바구니/가입)을 찾지 못해 조건을 만들 수 "
                        "없습니다."
                    ),
                    path="condition",
                    severity=IssueSeverity.ERROR,
                )
            )
            return ComposeOutcome(ir=None, issues=tuple(issues))

        self._attach_modifiers(ordered, drafts)

        unresolved: list[str] = []
        conditions: list[ConditionNode] = []
        for index, draft in enumerate(drafts):
            node = self._build_condition(draft, index, issues, unresolved, ordered)
            if node is None:
                return ComposeOutcome(
                    ir=None, issues=tuple(issues), unresolved_values=tuple(unresolved)
                )
            conditions.append(node)

        scope_tokens = [token for token in ordered if token.kind is TokenKind.ENTITY_VALUE]
        for orphan in _unattached_not_tokens(ordered, scope_tokens):
            issues.append(
                ValidationIssue(
                    code="logic.not_unattached",
                    message=(
                        f"배제 표현 '{orphan.raw_text}' 가 어떤 값에도 붙지 않았습니다. "
                        "이 표현을 무시하면 결과가 정반대가 되므로 진행하지 않습니다."
                    ),
                    path="condition",
                    severity=IssueSeverity.ERROR,
                )
            )

        condition = self._combine(conditions, drafts, ordered)
        logger.debug(
            "ir_composed",
            extra={"events": len(drafts), "issues": len(issues)},
        )
        return ComposeOutcome(
            ir=EventIR(
                target_entity=target_entity,
                condition=condition,
                rank=self._select_rank(ordered, issues),
            ),
            issues=tuple(issues),
            unresolved_values=tuple(unresolved),
        )

    def _select_rank(
        self,
        tokens: Sequence[SemanticToken],
        issues: list[ValidationIssue],
    ) -> RankSpec | None:
        """순위 한정은 조건이 아니라 **결과 집합 전체**에 걸리므로 IR 최상위에 둔다."""

        ranks = [
            token.value
            for token in tokens
            if token.kind is TokenKind.SORT and isinstance(token.value, RankSpec)
        ]
        if not ranks:
            return None
        if len({(rank.limit, rank.direction) for rank in ranks}) > 1:
            issues.append(
                ValidationIssue(
                    code="rank.conflict",
                    message="한 문장에 서로 다른 순위 한정이 둘 이상 있습니다.",
                    path="rank",
                    severity=IssueSeverity.ERROR,
                )
            )
        return ranks[0]

    # ── 대상 ────────────────────────────────────────────────────────────────────

    def _resolve_target(
        self,
        tokens: Sequence[SemanticToken],
        issues: list[ValidationIssue],
    ) -> EntityType:
        targets = [token for token in tokens if token.kind is TokenKind.TARGET]
        if targets:
            value = targets[0].value
            if isinstance(value, EntityType):
                return value

        # 대상 명사가 없으면 회원으로 읽되, 가정했다는 사실을 남긴다. 조용히 기본값을
        # 넣는 것과 기본값을 넣었다고 말하는 것은 다르다.
        issues.append(
            ValidationIssue(
                code="target.defaulted",
                message="조회 대상 명사가 없어 회원(member)으로 가정했습니다.",
                path="target_entity",
                severity=IssueSeverity.WARNING,
            )
        )
        return EntityType.MEMBER

    def _report_unparsed(
        self,
        tokens: Sequence[SemanticToken],
        issues: list[ValidationIssue],
    ) -> None:
        """값으로 만들지 못한 구간을 오류로 올린다.

        이 구간들을 조용히 넘기면 그 조건이 통째로 빠진 IR 이 **정상처럼** 보인다. 기간
        조건이 빠지면 조회 범위가 전체로 넓어지므로, 결과가 '조금 다른' 정도가 아니라
        완전히 다른 모수가 된다.
        """

        for token in tokens:
            if token.kind is not TokenKind.UNPARSED:
                continue
            issues.append(
                ValidationIssue(
                    code="expression.unparsed",
                    message=(
                        f"'{token.raw_text}' 의 형태는 인식했지만 값으로 해석할 수 없습니다. "
                        "이 조건을 무시하면 조회 범위가 의도보다 넓어지므로 진행하지 않습니다."
                    ),
                    path="condition",
                    severity=IssueSeverity.ERROR,
                )
            )

    # ── 사건 수집과 귀속 ─────────────────────────────────────────────────────────

    def _collect_drafts(self, tokens: Sequence[SemanticToken]) -> list[_EventDraft]:
        drafts: list[_EventDraft] = []
        for token in tokens:
            if token.kind is not TokenKind.EVENT:
                continue
            if not isinstance(token.value, EventType):  # pragma: no cover
                continue
            drafts.append(_EventDraft(event_type=token.value, anchor=token))
        return drafts

    def _attach_modifiers(
        self,
        tokens: Sequence[SemanticToken],
        drafts: list[_EventDraft],
    ) -> None:
        """각 수식 토큰을 가장 가까운 사건에 붙인다."""

        for token in tokens:
            if token.kind not in _MODIFIER_KINDS:
                continue
            draft = _nearest_draft(token, drafts)
            if token.kind in (TokenKind.TIME_WINDOW, TokenKind.DATE_RANGE):
                draft.time_tokens.append(token)
            elif token.kind in (TokenKind.COUNT_CONDITION, TokenKind.AGGREGATE):
                draft.metric_tokens.append(token)
            elif token.kind is TokenKind.ENTITY_VALUE:
                draft.scope_tokens.append(token)
            else:
                draft.polarity_tokens.append(token)

    # ── 조건 생성 ────────────────────────────────────────────────────────────────

    def _build_condition(
        self,
        draft: _EventDraft,
        index: int,
        issues: list[ValidationIssue],
        unresolved: list[str],
        all_tokens: Sequence[SemanticToken],
    ) -> ConditionNode | None:
        path = f"condition[{index}]"

        time_window = self._select_time_window(draft, path, issues)
        metric, metric_conflicted = self._select_metric(draft, path, issues)
        if metric_conflicted:
            return None

        scopes = self._build_scopes(draft, path, issues, unresolved, all_tokens)
        if scopes is None:
            return None

        polarity = Polarity.NEGATIVE if draft.polarity_tokens else Polarity.POSITIVE

        # '한 번도 ~ 않다' 는 횟수 0 과 부정이 같은 뜻이다. 둘 다 남기면 IR 에 같은 의미가
        # 두 번 들어가므로, 횟수 0 을 극성으로 접어 canonical 형태를 하나로 고정한다.
        if (
            metric is not None
            and metric.metric_type is MetricType.EVENT_COUNT
            and metric.operator is ComparisonOperator.EQ
            and metric.value == 0
        ):
            polarity = Polarity.NEGATIVE
            metric = None

        condition = EventCondition(
            event_type=draft.event_type,
            polarity=polarity,
            time_window=time_window,
            scopes=scopes,
            metric=metric,
        )
        return self._expand_scope_disjunction(condition, draft, all_tokens)

    def _select_time_window(
        self,
        draft: _EventDraft,
        path: str,
        issues: list[ValidationIssue],
    ) -> RelativeTimeWindow | AbsoluteDateRange | None:
        if not draft.time_tokens:
            return None
        if len(draft.time_tokens) > 1:
            issues.append(
                ValidationIssue(
                    code="time_window.multiple",
                    message=(
                        "한 사건에 기간 표현이 둘 이상입니다: "
                        f"{', '.join(token.raw_text for token in draft.time_tokens)}. "
                        "첫 번째 기간만 사용했습니다."
                    ),
                    path=f"{path}.time_window",
                    severity=IssueSeverity.WARNING,
                )
            )
        value = draft.time_tokens[0].value
        if isinstance(value, RelativeTimeWindow | AbsoluteDateRange):
            return value
        return None  # pragma: no cover

    def _select_metric(
        self,
        draft: _EventDraft,
        path: str,
        issues: list[ValidationIssue],
    ) -> tuple[MetricCondition | None, bool]:
        """``(집계, 충돌 여부)``.

        충돌을 sentinel 객체로 돌려주던 예전 시그니처(``MetricCondition | None | object``)는
        '집계 없음'과 '충돌'을 같은 반환 타입에 섞어 두어, 호출부에서 타입이 좁혀지지 않았다.
        두 상태는 뜻이 다르므로 자리를 나눈다.
        """

        metrics = [
            token.value
            for token in draft.metric_tokens
            if isinstance(token.value, MetricCondition)
        ]
        if not metrics:
            return None, False

        distinct = {(metric.metric_type, metric.operator, metric.value) for metric in metrics}
        if len(distinct) > 1:
            # 서로 다른 집계 조건 둘을 한 사건에 담을 자리가 IR 에 없다. 하나를 골라
            # 버리면 나머지 조건이 조용히 사라지므로 여기서 멈춘다.
            issues.append(
                ValidationIssue(
                    code="metric.conflict",
                    message=(
                        "한 사건에 서로 다른 집계 조건이 둘 이상 붙었습니다: "
                        f"{', '.join(token.raw_text for token in draft.metric_tokens)}."
                    ),
                    path=f"{path}.metric",
                    severity=IssueSeverity.ERROR,
                )
            )
            return None, True

        for token in draft.metric_tokens:
            if token.kind is TokenKind.AGGREGATE and token.confidence < 1.0:
                issues.append(
                    ValidationIssue(
                        code="metric.aggregation_assumed",
                        message=(
                            f"'{token.raw_text}' 에 집계 표지(합계/평균 등)가 없어 기간 합계로 "
                            "읽었습니다. 건별 조건이라면 문장에 집계를 명시해 주세요."
                        ),
                        path=f"{path}.metric.metric_type",
                        severity=IssueSeverity.WARNING,
                    )
                )
        return metrics[0], False

    def _build_scopes(
        self,
        draft: _EventDraft,
        path: str,
        issues: list[ValidationIssue],
        unresolved: list[str],
        all_tokens: Sequence[SemanticToken],
    ) -> tuple[ScopeFilter, ...] | None:
        scopes: list[ScopeFilter] = []
        for token in draft.scope_tokens:
            resolution = token.value
            if not isinstance(resolution, EntityResolution):
                # 해석기를 거치지 않은 후보. 접지되지 않은 값으로 SQL 을 만들 수 없다.
                unresolved.append(token.raw_text)
                continue
            if resolution.is_unknown:
                unresolved.append(resolution.raw_value)
                if token.source == RESIDUAL_OBJECT_SOURCE:
                    # 목적격 조사가 붙은 말은 화자가 명시한 대상이다. 카탈로그에 없다고
                    # 조용히 빼면 '나이키를 산 고객'이 '아무거나 산 고객'이 되어 모수가
                    # 통째로 넓어진다. 조건을 못 거는 것과 조건을 뺀 것은 다르다.
                    issues.append(
                        ValidationIssue(
                            code="scope.value_not_found",
                            message=(
                                f"'{resolution.raw_value}' 를 카탈로그에서 찾지 못했습니다. "
                                "이 조건을 빼고 진행하면 대상이 의도보다 넓어지므로 "
                                "진행하지 않습니다."
                            ),
                            path=f"{path}.scopes",
                            severity=IssueSeverity.ERROR,
                        )
                    )
                continue
            if resolution.selected is None:
                issues.append(
                    ValidationIssue(
                        code="entity.ambiguous",
                        message=(
                            f"'{resolution.raw_value}' 는 "
                            f"{', '.join(_describe(candidate) for candidate in resolution.candidates)}"
                            " 중 무엇인지 문장만으로 정할 수 없습니다."
                        ),
                        path=f"{path}.scopes",
                        severity=IssueSeverity.ERROR,
                    )
                )
                return None
            selected = resolution.selected
            scopes.append(
                ScopeFilter(
                    entity_type=selected.entity_type,
                    # 배제 표현(``제외``/``아닌``)이 이 값에 붙어 있으면 같음이 아니라
                    # 다름이다. 이 판정을 빠뜨리면 '나이키를 제외한'이 '나이키를'이 되어
                    # 결과가 정확히 반대가 된다.
                    operator=(
                        ComparisonOperator.NE
                        if _is_negated_scope(token, all_tokens)
                        else ComparisonOperator.EQ
                    ),
                    value=resolution.raw_value,
                    resolved_id=selected.entity_id,
                    confidence=selected.score,
                )
            )
        return tuple(scopes)

    # ── 논리 결합 ────────────────────────────────────────────────────────────────

    def _expand_scope_disjunction(
        self,
        condition: EventCondition,
        draft: _EventDraft,
        all_tokens: Sequence[SemanticToken],
    ) -> ConditionNode:
        """``A 또는 B 를 구매`` 를 조건 두 개의 OR 로 편다.

        :class:`EventCondition` 의 ``scopes`` 는 AND 로 읽히므로, OR 를 그 튜플에 담으면
        뜻이 뒤집힌다(둘 다 산 사람만 걸린다). 그래서 OR 는 조건 수준으로 올린다.
        """

        if len(condition.scopes) < 2:
            return condition
        if not _has_or_between_scopes(draft, all_tokens):
            return condition
        return ConditionGroup(
            operator=LogicOperator.OR,
            children=tuple(
                EventCondition(
                    event_type=condition.event_type,
                    polarity=condition.polarity,
                    time_window=condition.time_window,
                    scopes=(scope,),
                    metric=condition.metric,
                )
                for scope in condition.scopes
            ),
        )

    def _combine(
        self,
        conditions: list[ConditionNode],
        drafts: list[_EventDraft],
        all_tokens: Sequence[SemanticToken],
    ) -> ConditionNode:
        if len(conditions) == 1:
            return conditions[0]
        operator = _connective_between(drafts, all_tokens)
        return ConditionGroup(operator=operator, children=tuple(conditions))


# 배제 표지가 값에 붙어 있다고 볼 최대 거리(글자). ``나이키를 제외한`` 처럼 조사 한두 자만
# 사이에 오는 경우를 포섭하되, 문장 반대편의 배제어를 끌어오지 않을 만큼 좁게 잡는다.
_NEGATION_ATTACH_DISTANCE = 4


def _describe(candidate: object) -> str:
    entity_type = getattr(candidate, "entity_type", None)
    entity_id = getattr(candidate, "entity_id", "?")
    label = entity_type.value if entity_type is not None else "?"
    return f"{label}({entity_id})"


def _is_negated_scope(
    scope_token: SemanticToken,
    all_tokens: Sequence[SemanticToken],
) -> bool:
    """그 값 바로 뒤에 배제 표지(``제외``/``아닌``)가 붙어 있는가.

    '뒤'로 한정하는 이유는 한국어 어순이다. ``나이키를 제외한`` · ``나이키가 아닌`` 처럼
    배제어는 대상 뒤에 온다. 앞뒤를 모두 보면 ``제외한 브랜드의 나이키`` 같은 문장에서
    엉뚱한 값에 붙는다.
    """

    for token in all_tokens:
        if token.kind is not TokenKind.LOGIC or token.value is not LogicOperator.NOT:
            continue
        distance = token.start - scope_token.end
        if 0 <= distance <= _NEGATION_ATTACH_DISTANCE:
            return True
    return False


def _unattached_not_tokens(
    tokens: Sequence[SemanticToken],
    scope_tokens: Sequence[SemanticToken],
) -> list[SemanticToken]:
    """어떤 값에도 붙지 못한 배제 표지. 이것을 버리면 배제 조건이 통째로 사라진다."""

    return [
        token
        for token in tokens
        if token.kind is TokenKind.LOGIC
        and token.value is LogicOperator.NOT
        and not any(
            0 <= token.start - scope.end <= _NEGATION_ATTACH_DISTANCE
            for scope in scope_tokens
        )
    ]


def _nearest_draft(token: SemanticToken, drafts: list[_EventDraft]) -> _EventDraft:
    """토큰과 가장 가까운 사건. 동점이면 앞선 사건을 택해 결과를 재현 가능하게 한다."""

    return min(
        drafts,
        key=lambda draft: (_distance(token, draft.anchor), draft.anchor.start),
    )


def _distance(left: SemanticToken, right: SemanticToken) -> int:
    if left.end <= right.start:
        return right.start - left.end
    if right.end <= left.start:
        return left.start - right.end
    return 0


def _has_or_between_scopes(
    draft: _EventDraft,
    all_tokens: Sequence[SemanticToken],
) -> bool:
    scope_spans = sorted((token.start, token.end) for token in draft.scope_tokens)
    if len(scope_spans) < 2:
        return False
    region_start = scope_spans[0][1]
    region_end = scope_spans[-1][0]
    return any(
        token.kind is TokenKind.LOGIC
        and token.value is LogicOperator.OR
        and region_start <= token.start
        and token.end <= region_end
        for token in all_tokens
    )


def _connective_between(
    drafts: list[_EventDraft],
    all_tokens: Sequence[SemanticToken],
) -> LogicOperator:
    """사건들 사이에 놓인 접속어. OR 가 하나라도 있으면 OR, 그 외에는 AND 로 읽는다."""

    anchors = sorted(draft.anchor.start for draft in drafts)
    region_start, region_end = anchors[0], anchors[-1]
    for token in all_tokens:
        if token.kind is not TokenKind.LOGIC:
            continue
        if token.value is LogicOperator.OR and region_start <= token.start <= region_end:
            return LogicOperator.OR
    return LogicOperator.AND
