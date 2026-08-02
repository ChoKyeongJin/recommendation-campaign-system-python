"""**도메인 조립 지점** — 범용 코어 파이프라인과 회원 타기팅 실행 플랜의 배선.

graph_rag 가 부르는 단 하나의 진입점이며, 하는 일은 **주입**뿐이다: 실행 레지스트리
(슬롯 shape·닫힌 어휘·지표 카탈로그)를 CompileContext 로 묶고, 도메인 컴파일러와 슬롯
카탈로그·라벨러를 코어에 넘기고, 산출물을 플랜에 쓴다.

계층:
    graph_rag → semantic_plan_bridge(도메인 조립) → semantic_pipeline(범용 코어)
                        │
                        ├─ targeting_domain(도메인 선언·어휘·시간 한정어 결속)
                        └─ legacy_plan_compiler(실행 슬롯 지식)

이 파일이 대체한 것(전부 삭제됨): 카트/주문·캠페인·프로필·파생 집합·속성 이력·동시구매의
정규식 백필과, 미귀결 라벨 힌트 재방출, 결핍 사후 삭제 계열. 원문을 다시 읽는 곳은
coverage 검증 하나뿐이고 그 결과는 슬롯이 아니라 패치 요청이다.

순수 모듈 규약: graph_rag 를 import 하지 않는다.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable, Mapping, Sequence

import legacy_plan_compiler
import plan_decisions
import requirement_ledger
import semantic_coverage
import semantic_pipeline
import semantic_plan
import semantic_reemission
import semantic_retype
import targeting_domain
from compile_contract import CompileContext
from semantic_normalizers import MetricResolver
from semantic_pipeline import PipelineResult
from semantic_plan import SemanticPlanV2

PLAN_KEY = "semantic_plan"
PIPELINE_KEY = "semantic_pipeline"
REQUIREMENTS_KEY = "requirements"
# 결정론 생산자가 남긴 슬롯 출처 구간 저장소(slot_ownership.SLOT_SPANS_KEY 와 같은 키).
# 이 모듈은 slot_ownership 을 import 하지 않는다 — 읽기 전용 관찰이라 키 이름만 알면 된다.
_SLOT_SPANS_KEY = "_slot_spans"


def build_context(
    *,
    slot_shapes: Mapping[str, Any],
    allowed: Mapping[str, Any],
    aggregate_metric_specs: Mapping[str, Any] | None = None,
    member_metric_specs: Sequence[Mapping[str, Any]] | None = None,
    cart_metric_ids: Sequence[str] = (),
    campaign_event_specs: Mapping[str, Any] | None = None,
    profile_metric_specs: Mapping[str, Any] | None = None,
    history_attribute_specs: Mapping[str, Any] | None = None,
    entity_set_measures: Mapping[str, Any] | None = None,
    node_vocabularies: Mapping[str, Any] | None = None,
    today: date | None = None,
) -> CompileContext:
    """실행 어휘를 도메인별 MetricResolver 로 묶는다(컴파일러는 레지스트리를 스스로 열지 않는다)."""
    resolvers: dict[str, Callable[[Any], str | None]] = {
        "aggregate": _resolver(MetricResolver.from_specs(aggregate_metric_specs or {})),
        "member_metric": _resolver(MetricResolver.from_specs(list(member_metric_specs or []))),
        "cart": _resolver(MetricResolver.from_specs({metric: {} for metric in cart_metric_ids})),
        "campaign_event": _resolver(MetricResolver.from_specs(campaign_event_specs or {})),
        "profile": _resolver(MetricResolver.from_specs(profile_metric_specs or {})),
        "history_attribute": _resolver(MetricResolver.from_specs(history_attribute_specs or {})),
        "entity_set_measure": _resolver(MetricResolver.from_specs(entity_set_measures or {})),
    }
    return CompileContext(
        slot_shapes=slot_shapes,
        allowed=allowed,
        node_vocabularies=dict(node_vocabularies or {}),
        today=today or date.today(),
        metric_resolvers=resolvers,
        count_metrics={"aggregate": _count_metric_ids(aggregate_metric_specs)},
    )


# 정수 카운트를 뜻하는 지표 스펙 선언값. '무엇이 카운트인가'는 지표 스펙이 이미 알고 있으므로
# (`semantic_type`), 코드에 지표 이름 목록을 다시 적지 않는다 — 새 카운트 지표는 설정 한 줄이다.
_COUNT_SEMANTIC_TYPES: frozenset[str] = frozenset({"count", "count_distinct"})


def _count_metric_ids(specs: Mapping[str, Any] | None) -> frozenset[str]:
    """존재/부재로 환원해도 되는 카운트 지표 id 집합.

    카운트라는 것만으로는 부족하다. 환원이 성립하려면 **행이 없을 때 그 지표가 0** 이어야 한다
    (`null_as: 0`) — 그래야 `COUNT>0 ⟺ 행 있음`, `COUNT=0 ⟺ 행 없음` 이 참이다.

    부분 차원을 세는 카운트는 이 조건을 만족하지 않고, 만족하지 않는 채로 환원하면 **틀린 대상**이
    나간다: 브랜드가 비어 있는 주문만 가진 회원은 `distinct_brand_count = 0` 이지만 구매는 했다.
    그런 지표는 임계 경로에 남고, 0 임계는 정직하게 거부된다(사유는 슬롯이 말한다).
    """
    return frozenset(
        str(metric_id)
        for metric_id, spec in (specs or {}).items()
        if isinstance(spec, Mapping)
        and str(spec.get("semantic_type") or "") in _COUNT_SEMANTIC_TYPES
        and spec.get("null_as") == 0
    )


def _resolver(metric_resolver: MetricResolver) -> Callable[[Any], str | None]:
    def resolve(surface: Any) -> str | None:
        # 카탈로그가 해소하지 못하면 원문 표현을 그대로 넘긴다 — 닫힌 어휘 coerce 가 최종
        # 판정자이므로, 여기서 조용히 버리면 '왜 떨어졌는지'가 사라진다.
        resolved = metric_resolver.resolve(surface)
        if resolved:
            return resolved
        return str(surface) if isinstance(surface, str) and surface.strip() else None

    return resolve


def _plan_from_payload(
    query_plan: Mapping[str, Any], query: str, *, context: CompileContext | None = None
) -> tuple[SemanticPlanV2, list[str], list[semantic_retype.RetypeOutcome]]:
    """LLM payload → 정규화 → 타입드 플랜.

    **정규화가 타입드 노드 생성보다 먼저다.** `node_from_dict` 는 선언되지 않은 필드를 버리므로,
    타입 판정을 그 뒤에 하면 판정에 필요한 payload 가 이미 사라진다(실측: entity_set_membership
    으로 제안된 노드의 metric/operator/value 가 통째로 소실).
    """
    raw = query_plan.get(PLAN_KEY)
    if not isinstance(raw, Mapping):
        return SemanticPlanV2(source_query=query), [], []
    nodes = raw.get("nodes")
    normalized, outcomes = semantic_retype.normalize_payloads(
        nodes if isinstance(nodes, list) else [],
        vocabularies=(context.node_vocabularies if context is not None else None),
        resolve=_canonical_forms(context),
    )
    payload = {**dict(raw), "nodes": normalized}
    try:
        return semantic_plan.plan_from_dict(payload, source_query=query), [], outcomes
    except semantic_plan.SemanticPlanError as exc:
        plan = SemanticPlanV2(source_query=query)
        plan.validation_errors.append({
            "failure_code": semantic_plan.VALIDATION_MISMATCH,
            "reason": f"semantic_plan 을 해석하지 못했습니다: {exc}",
        })
        return plan, [f"semantic_plan_invalid:{exc}"], outcomes


def _canonical_forms(context: CompileContext | None) -> Callable[[Any], set[str]] | None:
    """표면 표현 → 가능한 canonical 집합(등록된 모든 지표 해소기의 합집합).

    타입 판정은 어휘 이름을 알 필요가 없다 — '이 표현이 저 어휘의 값이 되는가'만 알면 된다.
    그래서 어휘 키와 해소기 도메인을 잇는 **두 번째 손 지도를 만들지 않고**, 전 해소기를 훑어
    나온 canonical 이 기대 어휘에 있는지로 판정한다.
    """
    if context is None or not context.metric_resolvers:
        return None

    def resolve(value: Any) -> set[str]:
        forms: set[str] = set()
        for resolver in context.metric_resolvers.values():
            try:
                resolved = resolver(value)
            except Exception:  # noqa: BLE001 — 해소기 사고가 타입 판정을 죽이지 않는다.
                continue
            if isinstance(resolved, str) and resolved:
                forms.add(resolved)
        return forms

    return resolve


def _resolve_plan_path(query_plan: Mapping[str, Any], path: str) -> Any:
    """점 표기 경로를 실행 플랜에서 따라간다. 도달 못 하면 None."""
    cursor: Any = query_plan
    for part in str(path).split("."):
        if not isinstance(cursor, Mapping):
            return None
        if part not in cursor:
            return None
        cursor = cursor[part]
    return cursor


def claimed_evidence_spans(
    query_plan: Mapping[str, Any], *, query: str = ""
) -> tuple[list[tuple[int, int]], list[dict[str, Any]]]:
    """다른 소유자(V4 슬롯 계층)가 **실제로 채운 슬롯**의 근거 구간과, 기각된 청구 목록.

    coverage 검증이 '내가 안 만든 조건'까지 누락으로 보고하지 않게 하는 이행기 장치다.
    슬롯 계층이 전부 SemanticPlan 으로 옮겨오면 이 목록은 자연히 빈다.

    **청구는 슬롯을 가리켜야 한다.** 실측(2026-08-02)에서 LLM 이 `path="User Query"` 로 원문
    전체를 한 덩어리로 청구했고, 그것이 그대로 claimed_spans 가 되어 **모든 앵커가 커버된
    것으로 처리**됐다 — uncovered 영구 0, 재방출 영구 미발동. 그래서 두 가지를 요구한다:

      ① path 가 실행 플랜에서 **값이 채워진 곳**으로 해소될 것(슬롯을 안 채웠으면 청구가 아니다)
      ② 청구 구간이 원문 전체를 덮지 않을 것(전체 청구는 정보가 없고 검증기만 껀다)

    기각된 청구는 버리지 않고 돌려준다 — 왜 커버리지가 그렇게 나왔는지가 관측 가능해야 한다.
    """
    spans: list[tuple[int, int]] = []
    rejected: list[dict[str, Any]] = []
    whole = len((query or "").strip())
    for item in _evidence_claims(query_plan):
        if not isinstance(item, Mapping):
            continue
        start, end = item.get("start"), item.get("end")
        if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end):
            continue
        path = str(item.get("path") or "")
        record = {"path": path, "start": start, "end": end, "text": item.get("text")}
        if _empty_claim(_resolve_plan_path(query_plan, path)):
            rejected.append({**record, "reason": "청구 경로가 채워진 슬롯이 아님"})
            continue
        if whole and (end - start) >= whole:
            rejected.append({**record, "reason": "원문 전체를 덮는 청구"})
            continue
        spans.append((start, end))
    return spans, rejected


def _evidence_claims(query_plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """청구의 두 출처를 한 형태로 합친다: LLM 의 `semantic_evidence` + 결정론 생산자의 슬롯 구간.

    결정론 정규화기(이탈 문형 합성 등)도 원문의 구간을 읽어 슬롯을 채운다 — 정규식 매치가 곧
    구간이다. 그 청구가 여기 보이지 않으면, 같은 의미를 다시 방출한 노드가 '유실된 의미'로
    보고돼 요청 전체를 막는다. 청구의 출처가 사람인지 코드인지는 소유 판정과 무관하다.
    """
    claims = [item for item in (query_plan.get("semantic_evidence") or []) if isinstance(item, Mapping)]
    spans = query_plan.get(_SLOT_SPANS_KEY)
    if isinstance(spans, Mapping):
        for path, record in spans.items():
            if not isinstance(record, Mapping):
                continue
            claims.append({
                "path": str(path),
                "start": record.get("start"),
                "end": record.get("end"),
                "text": record.get("text"),
            })
    return claims


def _empty_claim(value: Any) -> bool:
    """슬롯이 '채워지지 않음'인가 — semantic_plan 의 결핍 정의와 같은 기준."""
    if value is None or value == "" or value == [] or value == {}:
        return True
    if isinstance(value, Mapping):
        return all(_empty_claim(item) for item in value.values())
    return False


def _partition_by_slot_ownership(
    outcomes: Sequence[semantic_retype.RetypeOutcome],
    *,
    query: str,
    claimed: Sequence[tuple[int, int]],
) -> tuple[list[semantic_retype.RetypeOutcome], list[dict[str, Any]]]:
    """타입 확정 실패 후보를 '진짜 실패'와 '슬롯이 이미 소유한 중복'으로 가른다.

    **하나의 사용자 의미는 하나의 canonical fact 만 소유한다.** 슬롯 계층이 이미 그 구간의
    값 원자를 전부 근거로 청구했다면, 같은 구간을 다시 노드로 만든 것은 새 의미가 아니라
    중복 방출이다 — 실패로 올리면 이미 표현된 조건 때문에 요청 전체가 막힌다
    (실측: '30대 여성'은 age/gender 슬롯이 채워졌는데도 metric='age' 노드 하나로 차단됐다).

    반대로 값 원자가 슬롯 청구 밖에 남아 있으면 그것은 실제로 유실된 의미이므로 실패로 남는다.
    """
    anchors = semantic_coverage.source_anchors(query)
    unresolved: list[semantic_retype.RetypeOutcome] = []
    superseded: list[dict[str, Any]] = []
    for outcome in outcomes:
        bounds = _span_bounds(query, outcome)
        if bounds is None:
            unresolved.append(outcome)
            continue
        if _claims_cover(bounds, anchors=anchors, claimed=claimed):
            superseded.append({**outcome.to_dict(), "superseded_by": "slot_claim"})
            continue
        unresolved.append(outcome)
    return unresolved, superseded


def _claims_cover(
    bounds: tuple[int, int],
    *,
    anchors: Sequence[Any],
    claimed: Sequence[tuple[int, int]],
    require_anchor: bool = False,
) -> bool:
    """이 구간의 값 원자가 **전부** 슬롯 청구 안에 들어 있는가(= 같은 의미가 이미 표현됐다).

    `require_anchor` 는 값 원자가 **하나도 없는** 구간을 '중복'으로 판정하지 못하게 한다. 원자가
    없으면 `all([])` 이 참이라 청구가 무엇이든 통과하는데, 그건 증명이 아니라 증명 대상의 부재다.
    타입 확정 실패에는 이 관대함이 안전하지만(그 노드는 어차피 폐기된다), 컴파일 실패에는 위험하다 —
    '이 연산은 지원하지 않는다'는 **명시적 판정**이 근거 없이 침묵으로 바뀐다.

    다만 원자의 부재가 곧 근거의 부재는 아니다. 값 없는 범주 조건('구매반응이 없는 회원')은
    셀 수 있는 원자를 하나도 갖지 않는데, 그런 구절이야말로 슬롯 계층이 통째로 소유한다.
    그래서 원자가 없을 때는 요구를 **구간 포함**으로 바꾼다 — 청구가 이 구간 자체를 덮고
    있는가. 공허참(청구가 무엇이든 통과)은 그대로 막히고, 슬롯이 같은 구절을 정확히 청구한
    경우만 중복으로 인정된다.
    """
    start, end = bounds
    inside = [anchor for anchor in anchors if start <= anchor.start and anchor.end <= end]
    if not inside:
        if not require_anchor:
            return True
        return any(low <= start and end <= high for low, high in claimed)
    return all(
        any(low <= anchor.start and anchor.end <= high for low, high in claimed)
        for anchor in inside
    )


def supersede_slot_owned_failures(
    plan: SemanticPlanV2,
    *,
    query: str,
    claimed: Sequence[tuple[int, int]],
    compiled: Any = None,
) -> list[dict[str, Any]]:
    """슬롯 계층이 이미 그 구간을 소유한 **실패**를 실패 목록에서 걷어낸다.

    타입 확정 실패에는 이 판정이 이미 있었는데(:func:`_partition_by_slot_ownership`) 컴파일
    실패에는 없었다. 그래서 판정이 뒤집힌 채로 돌았다 — 같은 프롬프트에서 **LLM 이 노드를 잘
    만들수록** 컴파일러까지 도달해 하드 에러가 되고, **못 만들수록** 타입 확정 단계에서 폐기돼
    결정론 합성이 통과했다(2026-08-02 실측 5회: 1회 성공은 '더 나쁜' 방출 덕분이었다).
    판정 기준은 한쪽만 고칠 이유가 없다: 같은 구간의 의미가 이미 실행 플랜에 있으면, 그것을 다시
    만든 노드는 유실된 의미가 아니라 중복 방출이다.

    **채널은 셋이다.** capability 판정(미지원)과 값 검증 실패(`validation_errors`), 그리고 그
    둘의 원천인 컴파일 실패 목록. 한 채널만 걷으면 다른 채널이 그대로 막는다 — 실측
    (2026-08-02): 슬롯 계층이 '구매반응이 없는 회원'을 정확히 컴파일해 두었는데도, 같은 구간을
    다시 수치 노드로 방출한 LLM 의 정규화 실패 하나가 `validation_errors` 에 남아 요청 전체가
    `semantic_registry_gap`("실행 설정이 준비되지 않았습니다")으로 막혔다. 설정은 멀쩡했다.

    `semantic_ir` 를 고치지 않는다 — 파생의 **입력**만 줄이고, 투영은 평소대로 다시 계산된다
    (파생값을 사후 수정하면 그때부터 상태와 근거가 어긋난다).
    """
    if not plan.capability_verdicts and not plan.validation_errors:
        return []
    anchors = semantic_coverage.source_anchors(query)
    spans_by_node = {
        node.id: (node.source_span or "", node.source_start, node.source_end)
        for node in plan.walk()
    }

    def _slot_owned(item: Any) -> tuple[str, str, tuple[int, int]] | None:
        """이 실패가 가리키는 노드의 구간을 슬롯 청구가 전부 덮는가."""
        if isinstance(item, Mapping) and item.get("supersedable") is False:
            # 생산자가 "이건 중복 방출이 아니다"라고 선언한 실패(슬롯 충돌 등)는 걷지 않는다.
            return None
        node_id = item.get("node_id") if isinstance(item, Mapping) else None
        located = spans_by_node.get(node_id) if node_id else None
        bounds = _resolve_bounds(query, *located) if located else None
        if bounds is None or not _claims_cover(
            bounds, anchors=anchors, claimed=claimed, require_anchor=True
        ):
            return None
        return str(node_id), located[0], bounds

    superseded: list[dict[str, Any]] = []
    owned_node_ids: set[str] = set()
    channels = (
        (plan.capability_verdicts, "compile_failure", "message"),
        (plan.validation_errors, "validation_error", "reason"),
    )
    for channel, action, reason_key in channels:
        kept: list[dict[str, Any]] = []
        for item in channel:
            owned = _slot_owned(item)
            if owned is None:
                kept.append(item)
                continue
            node_id, span, bounds = owned
            owned_node_ids.add(node_id)
            superseded.append({
                "node_id": node_id,
                "source_span": span,
                "source_start": bounds[0],
                "source_end": bounds[1],
                "action": action,
                "reason": str(item.get(reason_key) or item.get("failure_code") or ""),
                "superseded_by": "slot_claim",
            })
        channel[:] = kept

    # 컴파일 실패 목록도 함께 걷는다. 위 두 채널은 이 목록의 **파생**이고, 요구사항 원장은
    # 원본을 다시 읽는다 — 원본을 남기면 요청은 통과하는데 원장만 실패를 말하는 모순이 된다.
    failures = getattr(compiled, "failures", None)
    if owned_node_ids and isinstance(failures, list):
        failures[:] = [
            failure for failure in failures
            if not (
                isinstance(failure, Mapping)
                and str(failure.get("node_id") or "") in owned_node_ids
            )
        ]
    return superseded


def _drop_superseded_requirements(
    ledger: requirement_ledger.RequirementLedger, superseded: Sequence[Mapping[str, Any]]
) -> None:
    """슬롯 계층이 소유한 중복 방출은 요구사항 원장에서도 빠진다.

    남겨 두면 원장이 "이 조건은 실행 계획으로 가지 못했다"고 말하는데 실제로는 슬롯이 이미
    갖고 있다. 슬롯 계층이 단독으로 소유한 조건은 **원래부터 원장에 행이 없다**(원장은
    semantic_plan 노드만 센다) — 중복 방출을 빼는 것은 그 표현을 원래대로 되돌리는 것이지
    실패를 감추는 것이 아니다. 무엇을 왜 뺐는지는 파이프라인 감사(`superseded`)에 남는다.
    """
    owned = {
        str(item.get("node_id")) for item in superseded
        if isinstance(item, Mapping) and item.get("node_id")
    }
    if not owned:
        return
    ledger.requirements[:] = [
        item for item in ledger.requirements if item.requirement_id not in owned
    ]


def _resolve_bounds(query: str, text: str, start: Any, end: Any) -> tuple[int, int] | None:
    """근거 구간의 좌표. 좌표가 어긋나면 원문에서 문자열로 되찾는다(coverage 와 같은 규칙)."""
    if (
        isinstance(start, int) and isinstance(end, int)
        and 0 <= start <= end <= len(query) and query[start:end] == text
    ):
        return start, end
    if not text:
        return None
    found = query.find(text)
    return (found, found + len(text)) if found >= 0 else None


def _span_bounds(query: str, outcome: semantic_retype.RetypeOutcome) -> tuple[int, int] | None:
    """근거 구간의 좌표. 좌표가 어긋나면 원문에서 문자열로 되찾는다(coverage 와 같은 규칙)."""
    start, end = outcome.source_start, outcome.source_end
    text = outcome.source_span or ""
    if (
        isinstance(start, int) and isinstance(end, int)
        and 0 <= start <= end <= len(query) and query[start:end] == text
    ):
        return start, end
    if not text:
        return None
    found = query.find(text)
    return (found, found + len(text)) if found >= 0 else None


def _dedupe_issues(issues: Any) -> list[dict[str, Any]]:
    """같은 구간·같은 사유의 실패는 하나다 — 재방출 요청과 사용자 문구가 중복되면 안 된다."""
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for issue in issues:
        key = (issue.get("node_id"), issue.get("source_span"), issue.get("reason"))
        if key in seen:
            continue
        seen.add(key)
        out.append(issue)
    return out


def _drop_resolved_structurer_issues(plan: SemanticPlanV2) -> None:
    """재방출이 그 구간을 노드로 채웠으면 정규화 실패 기록은 더 이상 실패가 아니다.

    파생값을 누적하면 이전 라운드의 실패가 유령으로 남는다 — 코어가 매 라운드 파생 판정을
    비우는 것과 같은 이유로, 여기서도 해소된 항목을 걷어낸다.
    """
    covered = {
        "".join((node.source_span or "").split()) for node in plan.walk() if node.source_span
    }
    plan.structurer_issues = [
        issue for issue in plan.structurer_issues
        if "".join(str(issue.get("source_span") or "").split()) not in covered
    ]


def condition_label(node: Any) -> str:
    """요구사항 원장이 보여 줄 조건 라벨 — 조건 종류 + 집계 도메인(있으면)."""
    base = targeting_domain.condition_label(getattr(node, "type", ""))
    scope = (getattr(node, "values", {}) or {}).get("scope")
    if isinstance(scope, str) and scope:
        return f"{targeting_domain.scope_label(scope)} {base}"
    return base


def apply(
    query_plan: dict[str, Any],
    query: str,
    *,
    context: CompileContext,
    reextract: Callable[[str, Sequence[str]], tuple[SemanticPlanV2, list[str]]] | None = None,
    available_months: Mapping[str, int] | None = None,
    reemission_policy: semantic_reemission.ReemissionPolicy | None = None,
) -> PipelineResult:
    """플랜에 실린 semantic_plan 을 파이프라인에 통과시키고 산출물을 쓴다.

    쓰는 것: 컴파일된 실행 슬롯, 파생 semantic_ir, 요구사항 원장, 파이프라인 감사 기록.
    쓰지 않는 것: 그 외 무엇도. 특히 기존 missing_fields 를 고치지 않는다(파생값이라 고칠 게 없다).
    """
    base_plan, violations, retype_outcomes = _plan_from_payload(
        query_plan, query, context=context
    )

    def _extract(_query: str) -> tuple[SemanticPlanV2, list[str]]:
        return base_plan, violations

    claimed, rejected_claims = claimed_evidence_spans(query_plan, query=query)
    unresolved, superseded = _partition_by_slot_ownership(
        semantic_retype.unresolved_outcomes(retype_outcomes), query=query, claimed=claimed
    )
    base_plan.structurer_issues = _dedupe_issues(
        outcome.to_dict() for outcome in unresolved
    )
    for issue in base_plan.structurer_issues:
        # 사용자만 값을 줄 수 있는 구간은 재방출로 고쳐지지 않는다 — LLM 을 몇 번 더 불러도
        # 원문에 없는 브랜드명이 생기지 않는다. 재요청 대상에서 빼고 질문으로 귀결시킨다.
        if targeting_domain.user_omission_reason(issue.get("source_span") or ""):
            issue["reemission"] = "skip"

    def _normalizing_reextract(
        text: str, spans: Sequence[str], targets: Sequence[Mapping[str, Any]] = ()
    ) -> tuple[SemanticPlanV2, list[str]]:
        """재방출 결과도 같은 정규화를 통과시킨다 — 복구 경로가 우회로가 되면 안 된다."""
        try:
            patch, patch_violations = reextract(text, spans, targets)  # type: ignore[misc]
        except TypeError:  # 옛 2-인자 재추출 계약도 그대로 받는다.
            patch, patch_violations = reextract(text, spans)  # type: ignore[misc]
        normalized, outcomes = semantic_retype.normalize_payloads(
            (patch.to_dict().get("nodes") if patch is not None else []) or [],
            vocabularies=context.node_vocabularies,
            resolve=_canonical_forms(context),
        )
        retype_outcomes.extend(outcomes)
        if patch is None:
            return patch, patch_violations
        return (
            semantic_plan.plan_from_dict({"nodes": normalized}, source_query=text),
            patch_violations,
        )

    result = semantic_pipeline.run(
        query,
        extract=_extract,
        context=context,
        reextract=_normalizing_reextract if reextract is not None else None,
        claimed_spans=claimed,
        available_months=available_months,
        compiler=legacy_plan_compiler.LegacyQueryPlanCompiler(),
        slot_catalog=legacy_plan_compiler.NODE_SLOT_MAP,
        labeller=condition_label,
        reemission_policy=reemission_policy or semantic_reemission.ReemissionPolicy.from_env(),
    )
    _drop_resolved_structurer_issues(result.plan)
    written = semantic_pipeline.apply_to_query_plan(query_plan, result.compiled)
    # 슬롯 청구를 여기서 다시 읽는다 — 위 apply_to_query_plan 이 방금 슬롯을 채웠으므로, 파이프라인
    # 입력 시점의 청구 목록은 이미 낡았다. 컴파일 실패 소거는 최종 상태를 기준으로 판정해야 한다.
    final_claimed, _ = claimed_evidence_spans(query_plan, query=query)
    superseded.extend(
        supersede_slot_owned_failures(
            result.plan, query=query, claimed=final_claimed, compiled=result.compiled
        )
    )
    # 상태는 판정의 파생물이다 — 판정이 줄었으면 같은 규칙으로 다시 계산한다(고치는 것이 아니다).
    result.status = result.plan.status()
    _drop_superseded_requirements(result.ledger, superseded)
    query_plan[PLAN_KEY] = result.plan.to_dict()
    query_plan["semantic_ir"] = semantic_pipeline.project_semantic_ir(result.plan)
    query_plan[REQUIREMENTS_KEY] = result.ledger.to_dict()
    query_plan[PIPELINE_KEY] = {
        "status": result.status,
        "written_slots": written,
        "reextracted_spans": result.reextracted_spans,
        "contract_violations": result.contract_violations,
        "coverage": result.coverage.to_dict(),
        "compile_failures": result.compiled.failures,
        "node_slots": result.compiled.node_slots,
        "reemission": result.reemission,
        "clarification_targets": requirement_ledger.clarification_targets(result.ledger),
        # 커버리지 판정의 입력을 함께 남긴다 — 청구가 조용히 검증기를 끄는 사고를 관측 가능하게.
        "claimed_spans": [list(span) for span in claimed],
        "rejected_claims": rejected_claims,
        "normalization": semantic_retype.to_report(retype_outcomes),
        # 슬롯이 이미 소유해서 노드로 만들 필요가 없던 중복 방출(실패가 아니다).
        "superseded_candidates": superseded,
    }
    for slot in written:
        node_ids = [node for node, path in result.compiled.node_slots.items() if path == slot]
        plan_decisions.record(
            query_plan,
            filter_name="semantic_plan_compiler",
            action=plan_decisions.SET,
            slot=slot,
            reason="SemanticPlanV2 노드의 결정론 컴파일(원문 재해석 아님)",
            value=",".join(node_ids)[:80],
        )
    return result


__all__ = [
    "PIPELINE_KEY",
    "PLAN_KEY",
    "REQUIREMENTS_KEY",
    "apply",
    "build_context",
    "claimed_evidence_spans",
    "condition_label",
]
