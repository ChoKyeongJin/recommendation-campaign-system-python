"""의미 지문과 실행 지문 — **섞으면 둘 다 못 쓴다**.

배경(실측 2026-08-06)
---------------------
같은 프롬프트가 실행마다 다른 귀결을 냈다(#2 · #6 · #26). "무엇이 달라졌는가"에 답하려면 두
가지를 따로 물어야 한다:

    의미가 달라졌는가?   → semantic fingerprint
    환경이 달라졌는가?   → execution fingerprint

하나의 해시에 모델 이름과 IR 을 함께 넣으면 둘 다 못 묻는다. 모델을 바꾼 순간 모든 의미가
'달라진' 것으로 보이고, 반대로 IR 이 조용히 바뀌어도 같은 모델이면 눈치채지 못한다.

의미 지문의 정규화 대상(이것들이 달라져도 지문은 같아야 한다)::

    dict 키 순서 · AND 피연산자 순서 · 생성 순서에 따른 임시 id · SQL 별칭 ·
    로그 timestamp · 랜덤 trace id

실행 지문은 반대다 — 그 환경 값이 하나라도 다르면 지문이 달라야 한다.

실행: python -m pytest tests/test_compile_outcome_totality.py -q  (이 모듈의 계약이 그 파일에 있다)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

SEMANTIC_FINGERPRINT_KEY = "semantic_fingerprint"
EXECUTION_FINGERPRINT_KEY = "execution_fingerprint"

# 뜻이 아니라 **출처·생성 흔적**인 키. 값이 달라도 의미는 같으므로 정규화에서 지운다.
#
# ``evidence`` 가 여기 있는 이유(실측 2026-08-06): 같은 프롬프트를 두 번 돌렸을 때 출고 SQL 이
# **바이트 동일**한데 지문이 갈렸다. 차이는 IR 원자에 붙은 근거 구간(모델이 고른 표면 구절과
# 좌표)뿐이었다. 근거는 "이 조건이 원문 어디서 왔나"이지 "이 조건이 무엇을 뜻하나"가 아니다 —
# 지문에 남기면 같은 뜻이 매 실행 다른 지문을 갖고, 그 순간 이 지표는 의미 편차를 재지 못한다.
_VOLATILE_KEYS: frozenset[str] = frozenset({
    "id",
    "node_id",
    "receipt_id",
    "artifact_id",
    "requirement_id",
    "trace_id",
    "request_id",
    "created_at",
    "updated_at",
    "timestamp",
    "elapsed_ms",
    "duration_ms",
    "timings_ms",
    "evidence",
    "source_span",
    "source_text",
    "semantic_evidence",
})

# 순서가 의미를 갖지 않는 컨테이너 키. AND 피연산자 순서가 대표 사례다.
_ORDER_FREE_KEYS: frozenset[str] = frozenset({"operands", "issues", "missing_fields", "symbols"})

_TEMP_ALIAS_RE = re.compile(r"^(?:t|a|s|sub|q|audience)\d*$", re.IGNORECASE)


def _canonical(value: Any, *, key: str | None = None) -> Any:
    """의미 비교용 정규형. 키 정렬 · 임시 id 제거 · 순서 자유 컨테이너 정렬."""
    if isinstance(value, Mapping):
        folded = {
            str(item_key): _canonical(item_value, key=str(item_key))
            for item_key, item_value in value.items()
            if str(item_key) not in _VOLATILE_KEYS and not str(item_key).startswith("_")
        }
        return {name: folded[name] for name in sorted(folded)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items = [_canonical(item) for item in value]
        if key in _ORDER_FREE_KEYS:
            return sorted(items, key=lambda item: json.dumps(item, sort_keys=True, default=str))
        return items
    if isinstance(value, str) and _TEMP_ALIAS_RE.match(value):
        return "<alias>"
    return value


def _hash(payload: Any, prefix: str) -> str:
    text = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str
    )
    return f"{prefix}_{hashlib.sha256(text.encode('utf-8')).hexdigest()[:20]}"


def _changes_meaning(decision: Mapping[str, Any]) -> bool:
    """이 정책 결정이 **결과 집합의 뜻**을 바꾸는가.

    되묻기·미지원·거부는 뜻을 바꾼다(그 결정이 곧 결과다). 통과 결정 중에서는 정책이 **원문에
    없던 값을 지어낸** 것만 뜻을 바꾼다.

    ``stated_period`` 계열은 여기 들어가지 않는다(실측 2026-08-06). 그 교정은 원문이 이미 말한
    기간을 되찾아 주는 것이고, 되찾은 값은 이미 IR 안에 있어 지문의 IR 부분이 잡는다. 이것을
    의미 변화로 세면 "모델이 한 번에 맞힌 실행"과 "앱이 교정한 실행"이 같은 뜻인데 다른 지문을
    갖고, 그 순간 이 지표는 의미 편차를 재지 못한다. 적재 범위 '고지'도 같은 이유로 제외한다.
    """
    if decision.get("decision") != "allow":
        return True
    return str(decision.get("reason_code") or "").startswith("default_period")


def semantic_fingerprint(
    *,
    canonical_ir: Any,
    result_shape: Any = None,
    clarification_requirements: Sequence[Any] = (),
    policy_decisions: Sequence[Mapping[str, Any]] = (),
) -> str:
    """같은 뜻이면 같은 값. 모델·환경이 달라도 변하지 않는다.

    정책 결정 중 **의미를 바꾸는 것만** 재료로 쓴다 — 기본 기간을 채운 결정은 의미를 바꾸므로
    포함하고, 적재 범위를 '고지만' 한 결정은 의미를 바꾸지 않으므로 제외한다. 그 구분이
    없으면 안내 문구 하나가 지문을 흔든다.
    """
    meaning_changing = [
        {
            "policy_id": item.get("policy_id"),
            "decision": item.get("decision"),
            "reason_code": item.get("reason_code"),
        }
        for item in policy_decisions
        if isinstance(item, Mapping) and _changes_meaning(item)
    ]
    payload = {
        "ir": _canonical(canonical_ir),
        "result_shape": _canonical(result_shape),
        "clarifications": sorted(
            json.dumps(_canonical(item), sort_keys=True, default=str)
            for item in clarification_requirements
        ),
        "policies": sorted(
            json.dumps(item, sort_keys=True, default=str) for item in meaning_changing
        ),
    }
    return _hash(payload, "sem")


def execution_fingerprint(
    *,
    prompt: str,
    model_provider: str | None,
    model_name: str | None,
    schema_digest: str | None,
    catalog_digest: str | None,
    policy_digest: str | None,
    compiler_version: str | None,
    reference_time: str | None,
    timezone: str | None,
    cache_mode: str | None = None,
) -> str:
    """환경이 하나라도 다르면 다른 값. 프롬프트는 원문이 아니라 다이제스트로만 넣는다."""
    payload = {
        "prompt_digest": hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:20],
        "model_provider": model_provider,
        "model_name": model_name,
        "schema_digest": schema_digest,
        "catalog_digest": catalog_digest,
        "policy_digest": policy_digest,
        "compiler_version": compiler_version,
        "reference_time": reference_time,
        "timezone": timezone,
        "cache_mode": cache_mode,
    }
    return _hash(payload, "exe")


def write_fingerprints(plan: dict[str, Any], *, semantic: str, execution: str) -> None:
    plan[SEMANTIC_FINGERPRINT_KEY] = semantic
    plan[EXECUTION_FINGERPRINT_KEY] = execution


def _file_digest(path: Any) -> str | None:
    """파일 내용 다이제스트(읽을 수 없으면 None — 없는 것을 '같다'고 하지 않는다)."""
    from pathlib import Path

    try:
        return hashlib.sha256(Path(str(path)).read_bytes()).hexdigest()[:16]
    except OSError:
        return None


def _catalog_digest() -> str | None:
    try:
        import audience_runtime

        snapshot = audience_runtime.catalog_snapshot()
    except Exception:  # noqa: BLE001 — 지문 계산이 요청을 깨뜨리지 않는다
        return None
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# 의미 지문의 재료가 되는 플랜 키. 여기 없는 키는 지문을 흔들지 않는다.
_IR_KEYS = ("event_expression", "audience_requirement")
_SHAPE_KEY = "result_shape"


def write_request_fingerprints(
    plan: dict[str, Any],
    *,
    prompt: str,
    llm_model: str | None,
    schema_path: Any,
    reference_time: Any,
    timezone: str,
    compiler_version: str | None,
) -> None:
    """이 요청의 두 지문을 플랜에 남긴다(응답·러너가 같은 값을 읽는다).

    **재료를 호출자가 고르지 않는다.** 조립이 호출자에 있으면 지문 정의가 호출자 수만큼
    갈라지고, 그 순간 "같은 뜻이면 같은 지문"이라는 성질 자체가 사라진다.
    """

    canonical_ir = next(
        (plan.get(key) for key in _IR_KEYS if plan.get(key) is not None), None
    )
    clarifications = [
        item.get("code")
        for item in plan.get("unresolved_source_conditions") or ()
        if isinstance(item, Mapping)
    ]
    policy_decisions = _policy_decisions(plan)
    write_fingerprints(
        plan,
        semantic=semantic_fingerprint(
            canonical_ir=canonical_ir,
            result_shape=plan.get(_SHAPE_KEY),
            clarification_requirements=clarifications,
            policy_decisions=policy_decisions,
        ),
        execution=execution_fingerprint(
            prompt=prompt,
            model_provider="openai" if llm_model else None,
            model_name=llm_model,
            schema_digest=_file_digest(schema_path),
            catalog_digest=_catalog_digest(),
            policy_digest=_policy_digest(plan),
            compiler_version=compiler_version,
            reference_time=str(reference_time) if reference_time else None,
            timezone=timezone,
        ),
    )


def _policy_decisions(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    try:
        import targeting_policy

        return targeting_policy.decisions(plan)
    except Exception:  # noqa: BLE001 — 지문 계산이 요청을 깨뜨리지 않는다
        return []


def _policy_digest(plan: Mapping[str, Any]) -> str | None:
    try:
        import targeting_policy

        return targeting_policy.digest(plan)
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "EXECUTION_FINGERPRINT_KEY",
    "SEMANTIC_FINGERPRINT_KEY",
    "execution_fingerprint",
    "semantic_fingerprint",
    "write_fingerprints",
    "write_request_fingerprints",
]
