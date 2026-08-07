"""오디언스 요청이 SQL 없이 끝난 **자리**를 하나의 파생 좌표로 — ``diagnose``.

이 저장소에서 "SQL 이 안 나왔다"는 아홉 갈래로 끝난다. 갈래마다 흔적이 남는 자리가 다르고
(플랜 안 / sql_result 안 / 둘 다), 이름도 다르다(``unsupported`` · ``semantic_ir.status`` ·
``parser.fallback_reason`` · ``failure_reason`` · 좌표 항목의 ``code``). 그래서 "이 요청은 어디서
끝났나"를 답하려면 다섯 군데를 각자의 규칙으로 읽어야 했고, 그 규칙은 응답 조립부·트레이스·
실패로그에 각각 조금씩 다르게 흩어져 있었다.

이 모듈은 **생산자를 하나도 바꾸지 않는다.** 위 다섯 입력을 읽어 좌표 하나를 **파생**할 뿐이다.
종착 상태 자체를 통합하는 것은 소비자가 최소 5계층이라 독립 커밋으로 쪼갤 수 없고, 그렇게 하면
회귀 원인이 "좌표 통합"인지 "게이트 변경"인지 갈리지 않는다.

## 판정 규칙 — 사유가 게이트를 지목하면 그 레인이다

``failure_reason`` 은 대부분 **종착 게이트의 이름**이다(``semantic_ir_*`` 는 의미 게이트가,
``plan_validation_*`` 은 검증 게이트가, ``audience_authority_invalid`` 는 진입 게이트가 낸다).
지목하지 못하는 것은 최종 반환 dict 가 내는 **거친 사유**(``no_sql_candidates`` ·
``sql_guard_failed`` 등)뿐이고, 그때만 플랜에 남은 잔여물(Event IR 좌표 → 선언된 미지원 →
미해결 원문 조건 → 구조화 폴백)로 정밀화한다.

반대로 하면(잔여물을 항상 우선) 앞 단계가 남긴 좌표가 실제 종착 게이트를 덮는다 — 예컨대
빌더가 좌표를 남긴 뒤 다른 게이트가 요청을 끝낸 플랜에서, 진단이 이미 지나간 자리를 가리킨다.

## ``stage`` 는 이 저장소에서 다섯 번째 축이다

같은 이름의 다른 축과 **절대 섞지 마라**. 자세한 표는 ``docs/operations/failure_diagnosis.md``.

| 축 | 소유자 | 값의 예 |
|---|---|---|
| UI 스텝퍼 단계 | ``rag/failure_stage.py`` | ``condition_recognition`` · ``sql_safety_validation`` |
| 엔드포인트 단계 | ``api.py`` 실패로그 | ``sql_generation`` · ``database_execution`` |
| Event IR 컴파일 내부 단계 | ``query_pipeline.QueryPipelineError.stage`` | ``ir_schema`` · ``sql_compilation`` |
| 검색 트레이스 단계 | ``rag/trace.build_stage_log`` | ``1. Query Planning`` |
| **종착 레인**(이 모듈) | ``LANE_STAGES`` | ``event_ir_compile`` · ``semantic_resolution`` |

UI 스텝퍼는 "사용자에게 어디까지 갔다고 보여줄까"의 축이고, 이 모듈의 레인은 "코드의 어느
소유자가 이 요청을 끝냈나"의 축이다. 하나로 합치면 한쪽이 반드시 거짓말을 한다 — 예컨대
``audience_authority_invalid`` 는 스텝퍼에서 첫 단계(사용자에겐 조건을 못 읽은 것으로 보인다)
지만, 소유자는 조건을 읽지도 않은 진입 게이트다.

## 순수 모듈 불변식

stdlib 만 import 한다. 이 좌표는 응답 조립·트레이스·실패로그 여러 곳에서 무조건 호출되므로,
여기에 코어 스키마 로딩이 딸려 들어오면 진단 계층이 그 로딩 실패에 묶인다(같은 이유로
``rag/failure_stage`` 는 리터럴 표를, ``failure_messages`` 는 지연 import 를 쓴다).
그래서 아래 표의 키·사유는 **리터럴**이고, 실제 소유 모듈과의 일치는
``tests/test_audience_failure_coordinate.py`` 가 강제한다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

# 어느 실패에도 이름이 붙지 않았을 때의 코드. 조용한 ``None`` 을 대신한다 — 실패했는데 아홉
# 레인 어디에도 안 걸린 것 자체가 결함이고, 그 사실이 응답에 남아야 다음 사람이 잡는다.
UNCLASSIFIED = "unclassified"

# 이 모듈이 리터럴로 들고 있는 플랜 키·사유. 소유 모듈과의 일치는 테스트가 강제한다.
PLAN_AUTHORITY_KEY = "audience_authority"
EVENT_IR_SOURCE = "event_ir"

# 종착 레인 선언표. 각 행의 ``distinct_because`` 는 주석이 아니라 **데이터**다 — 레인을 지우거나
# 합치려는 사람이 그 한 줄을 반증하지 못하면 합치면 안 된다.
LANE_STAGES: tuple[dict[str, str], ...] = (
    {
        "stage": "audience_authority",
        "label": "실행 권위 판정",
        "reads": "sql_result.failure_reason == 'audience_authority_invalid'",
        "distinct_because": (
            "조건을 하나도 읽기 전에 끝난다. 요청의 결함이 아니라 저장·생산 산출물의 결함이라, "
            "사용자에게 되물을 것이 없고 고칠 곳은 플랜을 만든 쪽이다."
        ),
    },
    {
        "stage": "semantic_resolution",
        "label": "의미 확정",
        "reads": "sql_result.failure_reason ∈ semantic_* · query_plan.semantic_ir.status",
        "distinct_because": (
            "무엇을 뜻하는지가 안 정해졌다 — 사용자의 재입력으로 풀리는 유일한 레인이다."
        ),
    },
    {
        "stage": "source_coverage",
        "label": "원문 조건 귀결",
        "reads": f"query_plan.unresolved_source_conditions[source≠'{EVENT_IR_SOURCE}']",
        "distinct_because": (
            "원문에 있는 조건이 어떤 실행 슬롯으로도 안 갔다. 조건을 인식조차 못 한 것"
            "(no_sql_candidates)과 달리 **무엇이** 남았는지가 항목으로 있다."
        ),
    },
    {
        "stage": "plan_validation",
        "label": "실행계획 검증",
        "reads": "sql_result.failure_reason 가 'plan_validation_' 로 시작",
        "distinct_because": (
            "플랜은 만들어졌지만 실행 계약을 위반했다. 어떤 빌더도 돌기 전에 막히므로 "
            "'후보가 0개'(sql_generation)와 원인도 고칠 곳도 다르다."
        ),
    },
    {
        "stage": "event_ir_compile",
        "label": "Event IR 컴파일",
        "reads": f"query_plan.unresolved_source_conditions[source='{EVENT_IR_SOURCE}']",
        "distinct_because": (
            "canonical 표현은 있는데 실DB 술어가 안 나온 것이다. 표현이 없거나 읽을 수 없는 것은 "
            "이 레인이 아니라 plan_validation 이 소유한다"
            "(canonical_event_expression_missing / event_expression_schema_invalid)."
        ),
    },
    {
        "stage": "execution_capability",
        "label": "실행 능력 선언",
        "reads": "query_plan.unsupported",
        "distinct_because": (
            "의미는 확정됐고 컴파일러가 '못 한다'고 **선언**했다. 값을 못 정한 것"
            "(semantic_resolution)과 정반대 방향이라, 같은 레인으로 묶으면 되물을 수 있는 실패와 "
            "되물어도 안 되는 실패가 한 배지로 나간다."
        ),
    },
    {
        "stage": "structuring",
        "label": "구조화",
        "reads": "query_plan.parser.fallback_reason",
        "distinct_because": (
            "요청이 아니라 **구조화기**가 실패했다(모델 불가·스키마 위반). 이 레인은 재입력이 "
            "아니라 복구로 푸는 유일한 갈래다 — 사용자에게 조건을 다시 쓰게 하면 같은 곳에서 또 막힌다."
        ),
    },
    {
        "stage": "sql_generation",
        "label": "SQL 생성·검증",
        "reads": "sql_result.failure_reason (그 외 전부)",
        "distinct_because": (
            "조건은 실행 슬롯까지 갔고 SQL 생성·검증에서 떨어졌다. 위 레인들의 잔여가 아니라 "
            "정상 경로의 실패이고, 여기 쌓이는 사유가 곧 다음에 구현할 것의 목록이다."
        ),
    },
    {
        "stage": UNCLASSIFIED,
        "label": "미분류",
        "reads": "(어느 입력에도 흔적이 없다)",
        "distinct_because": (
            "실패했는데 아홉 레인 어디에도 안 걸린 것 자체가 결함이다. 조용한 None 으로 지우면 "
            "그 결함은 관측되지 않는다 — 그래서 이것도 레인이다."
        ),
    },
)

_LANE_BY_STAGE: dict[str, dict[str, str]] = {row["stage"]: row for row in LANE_STAGES}

# 이 모듈이 읽는 입력 전부. ``sources`` 는 이 목록 중 **실제로 값이 있던** 것만 담는다 —
# 미분류로 끝났을 때 운영자가 저장된 JSONB 의 어디를 열어야 하는지가 그 목록이다.
INPUTS: tuple[str, ...] = (
    "query_plan.unresolved_source_conditions",
    "query_plan.unsupported",
    "query_plan.semantic_ir",
    "query_plan.parser.fallback_reason",
    "sql_result.failure_reason",
)

# 의미 게이트가 내는 사유. `semantic_ir_<status>` 외에 세 개가 더 있다 —
# failure_messages.semantic_failure_reason 이 failure_kind 로 갈라 만들거나(구조화기/실행설정),
# 판정한 계층이 semantic_ir.failure_reason 으로 명시한다(방출 실패).
_SEMANTIC_GATE_REASONS = frozenset({
    "semantic_structurer_failure",
    "semantic_registry_gap",
    "semantic_emission_failure",
    "semantic_system_failure",
})
_SEMANTIC_REASON_PREFIX = "semantic_ir_"

_PLAN_VALIDATION_PREFIX = "plan_validation_"
_AUTHORITY_INVALID_REASON = "audience_authority_invalid"
_SOURCE_COVERAGE_REASON = "query_plan_required_conditions_missing"

# `plan_validation_*` 가 아니면서도 같은 레인인 게이트 둘. 조건 IR 자체의 결함을 **빌더가 돌기
# 전에** 판정하고 근거를 `validation_errors` 로 남긴다는 점이 plan_validation 과 똑같다.
# sql_generation 으로 흘리면 "조건은 다 갔고 관문에서 떨어졌다"가 되어 고칠 곳을 잘못 가리킨다.
_PLAN_LEVEL_REASONS = frozenset({"semantic_condition_conflict", "invalid_dimension_filters"})

# 구조화기 폴백이 거친 사유보다 **더 나은 설명**이 되는 경우 — 전부 "플랜에 조건이 없다"는 뜻이다.
# LLM 구조화가 불가해 rules 로 떨어진 요청이 조건 없이 끝나면, 정직한 원인은 "조건을 못 찾았다"가
# 아니라 "구조화기가 못 돌았다"다. 그 외 사유(예: sql_guard_failed)는 조건이 있었다는 뜻이므로
# 폴백 사실이 원인을 덮어서는 안 된다.
_STRUCTURING_EXPLAINS = frozenset({
    "no_sql_candidates",
    "recognized_domain_unsupported",
    "semantic_conditions_not_extracted",
})


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _evidence(path: Any, code: Any = None, detail: Any = None) -> dict[str, Any]:
    """근거 항목의 **단일** 모양. 경로마다 모양이 갈리면 좌표가 다시 추측이 된다."""
    return {"path": _text(path), "code": _text(code), "detail": _text(detail)}


def _unresolved(
    plan: Mapping[str, Any], result: Mapping[str, Any]
) -> list[Mapping[str, Any]]:
    """미해결 좌표는 플랜과 차단 결과 **양쪽**에 실린다 — 있는 쪽을 읽는다.

    차단 결과 쪽을 함께 보는 이유: 실패로그에 남는 것은 sql_result 이고, 저장된 blob 만 가지고
    사후 진단할 때 플랜 사본이 항상 곁에 있지는 않다.
    """
    return _rows(plan.get("unresolved_source_conditions")) or _rows(
        result.get("unresolved_source_conditions")
    )


def _semantic_ir(plan: Mapping[str, Any], result: Mapping[str, Any]) -> Mapping[str, Any]:
    """의미 IR 은 차단 결과가 자기 사본을 싣는다 — 둘 중 있는 쪽을 읽는다."""
    return _mapping(result.get("semantic_ir")) or _mapping(plan.get("semantic_ir"))


def _sources(plan: Mapping[str, Any], result: Mapping[str, Any]) -> list[str]:
    """값이 실제로 있던 입력의 이름 — 미분류일 때 어디를 열어야 하는지의 목록."""
    parser = _mapping(plan.get("parser"))
    present = {
        "query_plan.unresolved_source_conditions": bool(_unresolved(plan, result)),
        "query_plan.unsupported": bool(_mapping(plan.get("unsupported")).get("reason")),
        "query_plan.semantic_ir": bool(_semantic_ir(plan, result).get("status")),
        "query_plan.parser.fallback_reason": bool(_text(parser.get("fallback_reason"))),
        "sql_result.failure_reason": bool(_text(result.get("failure_reason"))),
    }
    return [name for name in INPUTS if present[name]]


# ── 레인별 좌표 ────────────────────────────────────────────────────────────────
# 각 함수는 {stage, stage_detail, code, evidence, message} 를 돌려주거나 None(해당 없음).


def _authority(plan: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stage": "audience_authority",
        "stage_detail": None,
        "code": _AUTHORITY_INVALID_REASON,
        "evidence": [
            _evidence(
                f"query_plan.{PLAN_AUTHORITY_KEY}",
                code=_AUTHORITY_INVALID_REASON,
                detail=repr(plan.get(PLAN_AUTHORITY_KEY)),
            )
        ],
        "message": "저장된 실행 권위 값이 닫힌 어휘 밖입니다 — 조건을 읽기 전에 종결했습니다.",
    }


def _semantic(
    plan: Mapping[str, Any], result: Mapping[str, Any], reason: str
) -> dict[str, Any]:
    semantic_ir = _semantic_ir(plan, result)
    status = _text(semantic_ir.get("status"))
    fields = _strings(semantic_ir.get("missing_fields"))
    return {
        "stage": "semantic_resolution",
        # failure_kind 는 "사용자에게 물을 수 있는가"를 가르는 값이라 좌표에 남을 값이다.
        "stage_detail": _text(semantic_ir.get("failure_kind")) or status,
        "code": reason,
        "evidence": [
            _evidence(f"query_plan.semantic_ir.{field}", code=status) for field in fields
        ]
        or [_evidence("query_plan.semantic_ir", code=status or reason)],
        "message": _text(semantic_ir.get("message"))
        or "요청의 의미를 실행 가능한 값으로 확정하지 못했습니다.",
    }


def _plan_validation(result: Mapping[str, Any], reason: str) -> dict[str, Any]:
    validation = _mapping(result.get("plan_validation"))
    # 검증기 게이트는 `plan_validation.issues`, 조건 IR 게이트 둘은 `validation_errors` 에
    # 근거를 남긴다. 근거 자리가 둘이어도 좌표의 모양은 하나여야 한다.
    issues = _rows(validation.get("issues")) or _rows(result.get("validation_errors"))
    detail = _text(validation.get("status"))
    if detail is None and reason.startswith(_PLAN_VALIDATION_PREFIX):
        detail = reason[len(_PLAN_VALIDATION_PREFIX) :]
    return {
        "stage": "plan_validation",
        "stage_detail": detail,
        "code": reason,
        "evidence": [
            _evidence(
                issue.get("path"),
                code=issue.get("code"),
                detail=issue.get("status") or issue.get("message"),
            )
            for issue in issues
        ]
        or [_evidence("query_plan", code=reason)],
        "message": "요청을 실행 계획으로 확정하지 못했습니다.",
    }


def _event_ir(plan: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any] | None:
    coordinates = [
        item for item in _unresolved(plan, result) if item.get("source") == EVENT_IR_SOURCE
    ]
    if not coordinates:
        return None
    first = coordinates[0]
    return {
        "stage": "event_ir_compile",
        # 빌더가 남긴 파이프라인 단계(ir_schema / sql_compilation / …)를 그대로 승계한다.
        "stage_detail": _text(first.get("stage")),
        "code": _text(first.get("code")) or "event_ir_unresolved",
        "evidence": [
            _evidence(item.get("path"), code=item.get("code"), detail=item.get("reason"))
            for item in coordinates
        ],
        "message": _text(first.get("reason"))
        or "사건 조건을 실DB 술어로 컴파일하지 못했습니다.",
    }


def _declared_unsupported(plan: Mapping[str, Any]) -> dict[str, Any] | None:
    unsupported = _mapping(plan.get("unsupported"))
    reason = _text(unsupported.get("reason"))
    if not reason:
        return None
    return {
        "stage": "execution_capability",
        "stage_detail": None,
        "code": reason,
        "evidence": [_evidence("query_plan.unsupported", code=reason)],
        "message": _text(unsupported.get("message"))
        or _text(unsupported.get("clarification"))
        or "요청한 연산을 현재 실행 능력으로 표현할 수 없습니다.",
    }


def _source_coverage(
    plan: Mapping[str, Any], result: Mapping[str, Any], reason: str | None
) -> dict[str, Any] | None:
    remaining = [
        item for item in _unresolved(plan, result) if item.get("source") != EVENT_IR_SOURCE
    ]
    if not remaining and reason != _SOURCE_COVERAGE_REASON:
        return None
    return {
        "stage": "source_coverage",
        "stage_detail": _text(remaining[0].get("source")) if remaining else None,
        "code": reason or _SOURCE_COVERAGE_REASON,
        "evidence": [
            _evidence(item.get("path"), code=item.get("code"), detail=item.get("reason"))
            for item in remaining
        ]
        or [_evidence("query_plan.unresolved_source_conditions", code=reason)],
        "message": "원문의 조건이 실행 가능한 타겟 조건으로 귀결되지 않았습니다.",
    }


def _structuring(plan: Mapping[str, Any], reason: str | None) -> dict[str, Any] | None:
    parser = _mapping(plan.get("parser"))
    fallback = _text(parser.get("fallback_reason"))
    if not fallback:
        return None
    # 폴백 자체는 실패가 아니다(rules 로 떨어지고도 SQL 은 나온다). "플랜에 조건이 없다"는
    # 사유일 때만 이것이 그 사유보다 나은 설명이다.
    if reason is not None and reason not in _STRUCTURING_EXPLAINS:
        return None
    return {
        "stage": "structuring",
        "stage_detail": _text(parser.get("type")),
        "code": fallback,
        "evidence": [
            _evidence("query_plan.parser", code=fallback, detail=parser.get("requested"))
        ],
        "message": "구조화기가 요청을 실행 플랜으로 만들지 못해 폴백으로 진행했습니다.",
    }


def _sql_generation(result: Mapping[str, Any], reason: str | None) -> dict[str, Any] | None:
    if not reason:
        return None
    missing = _rows(result.get("missing_input_conditions"))
    return {
        "stage": "sql_generation",
        "stage_detail": _text(result.get("error_code")),
        "code": reason,
        "evidence": [
            _evidence(item.get("path"), code=item.get("code"), detail=item.get("label"))
            for item in missing
        ]
        or [_evidence("sql_result", code=reason)],
        "message": "조건은 실행 슬롯까지 갔지만 SQL 생성·검증에서 막혔습니다.",
    }


def _named_gate(
    plan: Mapping[str, Any], result: Mapping[str, Any], reason: str | None
) -> dict[str, Any] | None:
    """사유가 종착 게이트를 이름으로 지목하는 경우(닫힌 매핑). 아니면 ``None``."""
    if reason is None:
        return None
    if reason == _AUTHORITY_INVALID_REASON:
        return _authority(plan)
    if reason.startswith(_PLAN_VALIDATION_PREFIX) or reason in _PLAN_LEVEL_REASONS:
        return _plan_validation(result, reason)
    if reason.startswith(_SEMANTIC_REASON_PREFIX) or reason in _SEMANTIC_GATE_REASONS:
        return _semantic(plan, result, reason)
    if reason == _SOURCE_COVERAGE_REASON:
        return _source_coverage(plan, result, reason)
    # 선언된 미지원은 최종 dict 가 사유로 **승격**한다(graph_rag 의 unsupported_intent 분기).
    # 사유가 그 선언과 같으면 그것이 종착 게이트다.
    if _text(_mapping(plan.get("unsupported")).get("reason")) == reason:
        return _declared_unsupported(plan)
    return None


def _coordinate(found: Mapping[str, Any], sources: list[str]) -> dict[str, Any]:
    lane = _LANE_BY_STAGE[found["stage"]]
    return {
        "stage": found["stage"],
        "stage_label": lane["label"],
        "stage_detail": found.get("stage_detail"),
        "code": found["code"],
        "evidence": list(found.get("evidence") or []),
        "message": found["message"],
        "sources": sources,
    }


def diagnose(
    query_plan: Mapping[str, Any] | None,
    sql_result: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """이 요청이 **어느 레인에서** 끝났는지의 좌표(성공이면 ``None``).

    반환 shape::

        {stage, stage_label, stage_detail, code, evidence[], message, sources[]}

    - ``stage`` 는 :data:`LANE_STAGES` 의 값 하나. UI 스텝퍼 단계가 **아니다**(모듈 docstring 표).
    - ``stage_detail`` 은 그 레인 안의 하위 좌표(Event IR 컴파일 단계 · plan_validation 상태 ·
      semantic failure_kind · 파서 종류). 없으면 ``None``.
    - ``code`` 는 항상 닫힌 코드다. 한국어 문장은 ``message`` 로만 나간다.
    - ``sources`` 는 값이 있던 입력의 이름 — ``code='unclassified'`` 일 때 어디를 열어야 하는지다.

    판정 기준을 새로 만들지 않는다. 이미 있는 종착 상태를 모듈 docstring 의 규칙으로 읽을 뿐이다.
    """
    result = _mapping(sql_result)
    if result.get("is_success"):
        return None
    plan = _mapping(query_plan)
    reason = _text(result.get("failure_reason"))
    sources = _sources(plan, result)

    found = _named_gate(plan, result, reason)
    if found is None:
        # 거친 사유(또는 사유 없음) — 플랜 잔여물로 정밀화한다.
        for candidate in (
            _event_ir(plan, result),
            _declared_unsupported(plan),
            _source_coverage(plan, result, reason),
            _structuring(plan, reason),
            _sql_generation(result, reason),
        ):
            if candidate is not None:
                found = candidate
                break

    if found is None:
        found = {
            "stage": UNCLASSIFIED,
            "stage_detail": None,
            "code": UNCLASSIFIED,
            "evidence": [],
            "message": (
                "실패했지만 알려진 종착 상태 어디에도 흔적이 없습니다 — 진단 배선의 결함입니다."
            ),
        }
    return _coordinate(found, sources)


__all__ = ["LANE_STAGES", "INPUTS", "UNCLASSIFIED", "diagnose"]
