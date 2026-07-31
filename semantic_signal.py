"""의미 신호의 단일 계층 — '무슨 낱말이 있는가'가 아니라 '그 뜻이 실제로 있는가'를 판정한다.

배경
----
같은 뜻을 읽는 규칙이 계층마다 흩어져 있었고, 그 규칙들이 전부 **표면 문자열 목록**이었다.
구매를 예로 들면 활용형('샀'), 중의 활용형('산'), 명사형('구매 이력'), 부정형('미구매')을 각각
정규식 갈래로 넣어야 했고, 목록에 없는 말투('질렀다'·'장만했다')는 조용히 침묵했다. 더 나쁜 것은
같은 판정이 **여러 단계에서 반복**됐다는 점이다 — 파서가 한 번 읽고, 재작성 게이트가 재작성본
문자열을 다시 읽고, 상품 추출 게이트가 또 읽었다. 그래서 한 곳만 고치면 다른 곳에서 샜다.

이 모듈이 그 판정을 한 계층으로 모은다.

    상태(status)   — 그 뜻이 어떤 양상으로 존재하는가. 닫힌 집합이고 코드가 소유한다(구조).
    정책(policy)   — 어떤 상태를 '실제 발생'으로 볼 것인가. 신호마다 다르고 데이터가 소유한다.
    표면어(surface) — 그 뜻을 사람이 말하는 방식. **어느 목록도 소유하지 않는다** — 추출기가 읽는다.

핵심 규약은 넷이다.

  1. **한 번만 판정한다.** 원문에서 한 번 구조화하고, 이후 단계(게이트·필터·재작성 비교)는 같은
     :class:`SemanticSignal` 값을 재사용한다. 재작성본 문자열을 같은 키워드로 다시 검사하지 않는다.
  2. **boolean 을 뭉개지 않는다.** 실제 발생·과거 이력·진행·의향·가정·단순 언급·부정을 상태로
     구분하고, ``detected`` 는 :func:`detected_for` 라는 **한** 정책 함수가 계산한다.
  3. **폴백은 우선순위지 OR 이 아니다.** 검증된 구조화 결과가 있으면 하위 폴백을 보지 않는다.
     하위 폴백이 "구매 낱말이 있다"고 해도 상위가 '의향'이라고 판정했으면 의향이다.
  4. **메타데이터는 의미가 아니다.** 출처·모델·소요시간·스키마 실패 여부는 :func:`canonical_form`
     에 들어가지 않는다. 재작성 전후 비교가 "출처가 다르다"는 이유로 같은 뜻을 폐기하면 안 된다.

이 모듈은 저장소의 다른 모듈을 import 하지 않는다(순수 계층). LLM 호출·형태 판정은 호출부가
콜러블로 주입한다 — :mod:`lexicon_llm` 이 개념 해석에 쓰는 것과 같은 관례다.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

DEFAULT_SIGNALS_PATH = Path(
    os.getenv(
        "SEMANTIC_SIGNALS_PATH",
        str(Path(__file__).resolve().parent / "docs" / "data" / "semantic_signals.json"),
    )
)

PROMPT_FILENAME = "semantic_signal_extract_system.txt"
# 프롬프트 계약이 바뀌면 올린다. 관측 로그에만 남고 의미 비교에는 쓰이지 않는다.
PROMPT_VERSION = "1"


# ── 상태(닫힌 집합) ────────────────────────────────────────────────────────────────────────
COMPLETED = "completed"
HISTORY = "history"
ONGOING = "ongoing"
INTENT = "intent"
HYPOTHETICAL = "hypothetical"
MENTIONED = "mentioned"
DENIED = "denied"
NONE = "none"
UNKNOWN = "unknown"

# 롤업 우선순위(앞이 셀수록 강한 주장). 복합 문장에서 문장 단위 status 를 고르는 순서다.
STATUS_ORDER = (COMPLETED, HISTORY, ONGOING, DENIED, INTENT, HYPOTHETICAL, MENTIONED, NONE, UNKNOWN)
STATUSES = frozenset(STATUS_ORDER)

# 그 관계를 **주장한** 상태(존재든 부재든). '이 문장이 그 뜻을 단정했는가'를 묻는 소비자가 쓴다.
ASSERTED = frozenset({COMPLETED, HISTORY, ONGOING, DENIED})
# 그 뜻이 **화제로 등장한** 상태. 발생 여부와 무관하게 '이 문장이 그 얘기인가'를 묻는 문맥 게이트용.
# 발생 판정(detected)과 문맥 판정을 같은 boolean 으로 뭉개지 않으려고 둘을 따로 둔다.
CONTEXTUAL = ASSERTED | frozenset({INTENT, HYPOTHETICAL, MENTIONED})

# ── 출처(메타데이터) ───────────────────────────────────────────────────────────────────────
SOURCE_LLM = "llm"
SOURCE_RULES = "rules"
SOURCE_CONSERVATIVE = "conservative"
SOURCE_NONE = "none"
SOURCE_UNKNOWN = "unknown"

# 판정 근거로 인정할 최소 길이. 한 글자 근거는 어떤 문장에도 걸려 검증이 무력해진다.
MIN_EVIDENCE_LENGTH = 2


def compact(text: str) -> str:
    """근거·엔티티 대조용 정규형 — 공백 제거 + 소문자. 파이프라인의 compact 규약과 같다."""
    return (text or "").replace(" ", "").casefold()


# ── 값 ─────────────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SignalClaim:
    """대상 하나에 대한 판정. ``entity`` 가 빈 문자열이면 대상을 특정하지 않은 문장 단위 주장이다.

    엔티티별로 나누는 이유는 한 문장이 대상마다 다른 답을 갖기 때문이다 — "노트북은 사지 않았지만
    모니터는 샀다" 를 문장 하나의 boolean 으로 접으면 둘 중 하나가 반드시 틀린다.
    """

    entity: str
    status: str
    detected: bool

    def normalized(self) -> "SignalClaim":
        return SignalClaim(entity=" ".join(self.entity.split()).casefold(), status=self.status, detected=self.detected)


@dataclass(frozen=True)
class SemanticSignal:
    """구조화된 의미 신호 하나.

    앞쪽 다섯 필드가 **의미**이고 :func:`canonical_form` 이 비교에 쓰는 전부다. 뒤쪽은 전부
    메타데이터이며, 라우팅·관측·디버깅용이지 의미 동일성 판정에 섞이지 않는다.
    """

    signal: str
    detected: bool
    status: str
    negated: bool
    claims: tuple[SignalClaim, ...] = ()
    evidence: str | None = None
    # ── 여기부터 메타데이터(의미 비교 제외) ────────────────────────────────────────────────
    source: str = SOURCE_NONE
    fallback_used: bool = False
    schema_error: str | None = None
    model: str | None = None
    prompt_version: str | None = None
    elapsed_ms: float | None = None

    @property
    def entities(self) -> tuple[str, ...]:
        """판정 대상 이름들(대상 미지정 주장은 뺀다). 외부 소비자가 읽는 평면 목록이다."""
        seen: list[str] = []
        for claim in self.claims:
            if claim.entity and claim.entity not in seen:
                seen.append(claim.entity)
        return tuple(seen)

    def entity_status(self, entity: str) -> str:
        """그 대상의 상태. 대상별 주장이 없으면 문장 단위 상태로 답한다."""
        key = " ".join((entity or "").split()).casefold()
        for claim in self.claims:
            if claim.normalized().entity == key:
                return claim.status
        return self.status


# ── 신호 선언(데이터) ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class SignalSpec:
    """판정 대상 신호 하나. 표면어는 없고 뜻과 정책만 있다."""

    signal: str
    label: str
    description: str
    detected_statuses: frozenset[str]
    guidance: tuple[str, ...] = ()
    positive_examples: tuple[Mapping[str, Any], ...] = ()
    negative_examples: tuple[Mapping[str, Any], ...] = ()


def _example_rows(raw: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item for item in raw if isinstance(item, dict) and isinstance(item.get("text"), str))


@functools.lru_cache(maxsize=4)
def load_specs(path_text: str = str(DEFAULT_SIGNALS_PATH)) -> Mapping[str, SignalSpec]:
    """신호 선언을 읽는다. 파일이 없거나 파손되면 빈 매핑(= 추출 계층 비활성, 폴백만 동작)."""
    path = Path(path_text)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    raw = payload.get("signals") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return {}
    specs: dict[str, SignalSpec] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("signal")
        description = item.get("description")
        if not (isinstance(name, str) and name and isinstance(description, str) and description):
            continue
        declared = item.get("detected_statuses")
        statuses = frozenset(
            value for value in (declared if isinstance(declared, list) else []) if value in STATUSES
        )
        examples = item.get("examples") if isinstance(item.get("examples"), dict) else {}
        specs[name] = SignalSpec(
            signal=name,
            label=str(item.get("label") or name),
            description=description,
            detected_statuses=statuses,
            guidance=tuple(
                line for line in (item.get("guidance") or []) if isinstance(line, str) and line
            ),
            positive_examples=_example_rows(examples.get("positive")),
            negative_examples=_example_rows(examples.get("negative")),
        )
    return specs


def spec_for(signal: str) -> SignalSpec | None:
    return load_specs().get(signal)


# ── 정책: detected 는 여기서만 계산된다 ────────────────────────────────────────────────────
def detected_for(spec: SignalSpec | None, status: str, negated: bool) -> bool:
    """이 상태를 '실제 발생'으로 볼 것인가 — 신호 전체에서 이 함수 하나만 답한다.

    선언이 없으면(파일 부재·미등록 신호) 어떤 상태도 발생으로 보지 않는다. 판정 근거를 모르는 채
    참을 돌려주는 것보다 침묵하는 쪽이 안전하다.
    """
    if negated or spec is None:
        return False
    return status in spec.detected_statuses


def rollup(spec: SignalSpec | None, claims: Iterable[SignalClaim]) -> tuple[str, bool, bool]:
    """대상별 주장들 → (문장 단위 status, detected, negated).

    복합 문장 정책을 여기 한 곳에 적어 둔다. **하나라도 발생이면 문장은 발생이다** — 오디언스
    타겟팅에서 "A는 안 샀지만 B는 샀다"는 구매 이력이 있는 회원이기 때문이다. 부정은 발생 주장이
    하나도 없고 명시적 부정이 있을 때만 문장 단위 부정으로 올라간다.
    """
    rows = tuple(claims)
    if not rows:
        return NONE, False, False
    order = {status: index for index, status in enumerate(STATUS_ORDER)}
    detected_rows = [row for row in rows if row.detected]
    pool = detected_rows or list(rows)
    status = min(pool, key=lambda row: order.get(row.status, len(order))).status
    detected = bool(detected_rows)
    negated = not detected and any(row.status == DENIED for row in rows)
    return status, detected, negated


def build(
    signal: str,
    claims: Iterable[SignalClaim],
    *,
    spec: SignalSpec | None = None,
    evidence: str | None = None,
    source: str = SOURCE_NONE,
    fallback_used: bool = False,
    schema_error: str | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
    elapsed_ms: float | None = None,
) -> SemanticSignal:
    """주장들에서 신호 하나를 만든다. ``detected`` 는 언제나 정책 함수가 계산한 값이다."""
    resolved_spec = spec if spec is not None else spec_for(signal)
    rows = tuple(claims)
    status, detected, negated = rollup(resolved_spec, rows)
    return SemanticSignal(
        signal=signal,
        detected=detected,
        status=status,
        negated=negated,
        claims=rows,
        evidence=evidence,
        source=source,
        fallback_used=fallback_used,
        schema_error=schema_error,
        model=model,
        prompt_version=prompt_version,
        elapsed_ms=elapsed_ms,
    )


def claim(signal: str, status: str, entity: str = "", *, spec: SignalSpec | None = None) -> SignalClaim:
    """주장 하나. ``detected`` 는 여기서도 정책 함수가 계산한다(호출부가 임의로 못 정한다)."""
    resolved = spec if spec is not None else spec_for(signal)
    return SignalClaim(entity=entity, status=status, detected=detected_for(resolved, status, False))


def empty(signal: str, *, source: str = SOURCE_NONE, **metadata: Any) -> SemanticSignal:
    """'그 뜻 없음'. 판정에 성공했고 결과가 없는 것이지, 판정 실패(unknown)와는 다르다."""
    return build(signal, (), source=source, **metadata)


def unknown(signal: str, *, schema_error: str | None = None, **metadata: Any) -> SemanticSignal:
    """판정 실패. 호출부가 '조용히 false'로 처리하지 않도록 상태로 드러낸다."""
    metadata.setdefault("source", SOURCE_UNKNOWN)
    metadata.setdefault("fallback_used", True)
    return SemanticSignal(
        signal=signal, detected=False, status=UNKNOWN, negated=False, schema_error=schema_error, **metadata
    )


# ── 정규형과 의미 동일성 ───────────────────────────────────────────────────────────────────
def canonical_form(signal: SemanticSignal) -> dict[str, Any]:
    """의미 동일성 비교용 정규형. **메타데이터는 들어가지 않는다.**

    엔티티는 순서가 뜻에 영향을 주지 않으므로 정렬해 집합처럼 다룬다 — 재작성이 나열 순서만 바꿨을
    때 의미가 달라졌다고 판정하면 정상 재작성이 폐기된다.
    """
    return {
        "signal": signal.signal,
        "status": signal.status,
        "detected": signal.detected,
        "negated": signal.negated,
        "claims": tuple(
            sorted(
                {
                    (row.entity, row.status, row.detected)
                    for row in (item.normalized() for item in signal.claims)
                }
            )
        ),
    }


def same_meaning(left: SemanticSignal, right: SemanticSignal) -> bool:
    """두 판정이 같은 뜻인가. 출처·모델·소요시간이 달라도 뜻이 같으면 같다."""
    return canonical_form(left) == canonical_form(right)


# 의미 서명 토큰. 재작성 전후를 집합 차분으로 비교하는 기존 게이트가 읽는 형태다.
EXISTS_SUFFIX = "exists"
ABSENT_SUFFIX = "absent"


def signature(signal: SemanticSignal) -> frozenset[str]:
    """관계 서명 — ``<signal>:exists`` / ``<signal>:absent`` 집합.

    "그 관계를 주장했는가"만 담는다(의향·가정·단순 언급은 관계 주장이 아니라 빈 집합이다).
    대상별 주장이 있으면 대상마다 따로 서명해, 재작성이 한 대상의 극성만 뒤집는 것도 잡는다.
    """
    tokens: set[str] = set()
    for row in signal.claims:
        suffix = EXISTS_SUFFIX if row.detected else (ABSENT_SUFFIX if row.status == DENIED else None)
        if suffix is None:
            continue
        entity = row.normalized().entity
        tokens.add(f"{signal.signal}:{suffix}" if not entity else f"{signal.signal}:{suffix}:{entity}")
    return frozenset(tokens)


@dataclass(frozen=True)
class RewriteDiff:
    """재작성 전후의 의미 차이. 소실과 추가를 **따로** 들고 있는다.

    둘을 하나로 합치면 "재작성이 조건을 지웠다"와 "재작성이 없던 조건을 지어냈다"를 구분할 수 없고,
    후자는 원문에 없는 조건으로 SQL 이 나가는 더 나쁜 결함이다.
    """

    dropped: tuple[str, ...]
    added: tuple[str, ...]

    @property
    def preserved(self) -> bool:
        return not self.dropped and not self.added


def compare_rewrite(original: SemanticSignal, rewritten: SemanticSignal) -> RewriteDiff:
    """원문 판정과 재작성본 판정의 의미 차이. 문자열이 아니라 구조화 값끼리 비교한다."""
    before, after = signature(original), signature(rewritten)
    return RewriteDiff(dropped=tuple(sorted(before - after)), added=tuple(sorted(after - before)))


# ── LLM 응답 검증 ──────────────────────────────────────────────────────────────────────────
def _valid_status(value: Any) -> str | None:
    return value if isinstance(value, str) and value in STATUSES else None


def _evidence_in_source(value: Any, haystack: str) -> str | None:
    """근거는 원문에 글자 그대로 있어야 한다(번역·요약·유추 금지)."""
    if not isinstance(value, str):
        return None
    span = compact(value)
    if len(span) < MIN_EVIDENCE_LENGTH or span not in haystack:
        return None
    return span


def validate(payload: Any, signal: str, text: str, spec: SignalSpec | None = None) -> SemanticSignal | None:
    """구조화 응답을 닫힌 상태 집합 + 원문 근거로 검증한다. 계약 위반이면 None(→ 폴백).

    ``detected`` 는 응답에서 읽지 않는다 — 모델이 무엇을 보내든 :func:`detected_for` 가 다시 계산한다.
    그렇게 해야 "프롬프트마다 다른 기준으로 boolean 을 만든다"가 구조적으로 불가능해진다.
    """
    resolved = spec if spec is not None else spec_for(signal)
    if not isinstance(payload, dict):
        return None
    if payload.get("signal") not in (None, signal):
        return None
    status = _valid_status(payload.get("status"))
    if status is None:
        return None
    haystack = compact(text)
    evidence = _evidence_in_source(payload.get("evidence"), haystack)
    # 관계를 주장했다면 근거를 원문에서 오려낼 수 있어야 한다. 없으면 환각으로 보고 폴백한다.
    if status in ASSERTED and evidence is None:
        return None

    raw_entities = payload.get("entities")
    claims: list[SignalClaim] = []
    seen: set[str] = set()
    for item in raw_entities if isinstance(raw_entities, list) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("entity")
        if not isinstance(name, str) or not name.strip():
            continue
        # 대상 이름도 원문에 있어야 한다 — 모델이 없는 상품을 지어내면 여기서 걸린다.
        if compact(name) not in haystack:
            continue
        entity_status = _valid_status(item.get("status")) or status
        key = " ".join(name.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        claims.append(claim(signal, entity_status, name.strip(), spec=resolved))

    if not claims and status != NONE:
        claims.append(claim(signal, status, spec=resolved))

    negated_hint = payload.get("negated")
    built = build(signal, claims, spec=resolved, evidence=evidence, source=SOURCE_LLM)
    # 모델이 부정이라고 했는데 상태가 발생이면 서로 모순이다 — 모순 응답은 채택하지 않는다.
    if isinstance(negated_hint, bool) and negated_hint and built.detected:
        return None
    return built


def json_schema(signal: str, spec: SignalSpec | None = None) -> dict[str, Any]:
    """응답 계약(JSON Schema). 프롬프트에 그대로 실어 허용 값을 닫는다."""
    resolved = spec if spec is not None else spec_for(signal)
    statuses = sorted(STATUSES)
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["signal", "status", "negated", "entities"],
        "properties": {
            "signal": {"const": signal},
            "status": {"type": "string", "enum": statuses},
            "negated": {"type": "boolean"},
            "evidence": {"type": "string"},
            "entities": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["entity", "status"],
                    "properties": {
                        "entity": {"type": "string"},
                        "status": {"type": "string", "enum": statuses},
                    },
                },
            },
        },
        "x-detected-statuses": sorted(resolved.detected_statuses) if resolved else [],
    }


@functools.lru_cache(maxsize=4)
def _status_catalog(path_text: str = str(DEFAULT_SIGNALS_PATH)) -> str:
    """상태 설명 블록. 상태의 **뜻**도 데이터가 소유해야 프롬프트와 정책이 갈라지지 않는다."""
    path = Path(path_text)
    rows: list[tuple[str, str]] = []
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        for item in payload.get("statuses", []) if isinstance(payload, dict) else []:
            if isinstance(item, dict) and item.get("status") in STATUSES:
                rows.append((str(item["status"]), str(item.get("description", ""))))
    if not rows:
        rows = [(status, "") for status in STATUS_ORDER]
    return "\n".join(f"- {status}: {description}".rstrip(": ") for status, description in rows)


def prompt_substitutions(spec: SignalSpec) -> dict[str, str]:
    """프롬프트 자리표시자 → 채울 문자열. 문구는 파일이, 조립 규칙만 코드가 갖는다."""
    def _example(row: Mapping[str, Any]) -> str:
        entities = row.get("entities")
        tail = f" entities={list(entities)}" if isinstance(entities, list) and entities else ""
        return f'- "{row.get("text")}" → status={row.get("status")}{tail}'

    examples = [_example(row) for row in (*spec.positive_examples, *spec.negative_examples)]
    return {
        "{signal_id}": spec.signal,
        "{signal_label}": spec.label,
        "{signal_description}": spec.description,
        "{signal_guidance}": "\n".join(f"- {line}" for line in spec.guidance),
        "{status_catalog}": _status_catalog(),
        "{signal_examples}": "\n".join(examples),
    }


def render_prompt(template: str, spec: SignalSpec) -> str:
    """자리표시자를 채운 시스템 프롬프트. ``str.format`` 이 아니라 리터럴 치환이다 —
    템플릿 끝의 JSON 예시가 중괄호를 그대로 담고 있어 format 은 쓸 수 없다."""
    rendered = template
    for placeholder, value in prompt_substitutions(spec).items():
        rendered = rendered.replace(placeholder, value)
    return rendered


# ── 폴백 사슬 ──────────────────────────────────────────────────────────────────────────────
Extractor = Callable[[str, SignalSpec], Any]
Judge = Callable[[str], SemanticSignal | None]


def enabled() -> bool:
    """구조화 의미 추출(1순위)을 켤지. 끄면 형태 판정 폴백만으로 동작한다."""
    value = os.getenv("SEMANTIC_SIGNAL_LLM", "true").strip().casefold()
    return value not in {"0", "false", "no", "off"}


def resolve(
    text: str,
    signal: str,
    *,
    extract: Extractor | None = None,
    rules: Judge | None = None,
    conservative: Judge | None = None,
    clock: Callable[[], float] | None = None,
) -> SemanticSignal:
    """의미 판정 한 번. **우선순위 사슬이지 OR 이 아니다.**

      1. 검증된 구조화 결과(LLM)
      2. 형태 판정(활용형·명사형·부정 범위를 구조로 읽는 기존 규칙 엔진)
      3. 최소한의 보수적 규칙(문맥 게이트용 — 발생으로 승격하지 않는다)
      4. unknown(판정 실패를 상태로 드러낸다)

    상위 단계가 답하면 하위는 보지 않는다. 하위가 "낱말이 있다"고 해도 상위가 '의향'이라 판정했으면
    의향이다 — 이 순서가 뒤집히면 부정문·의향문이 낱말 하나로 다시 발생이 된다.
    """
    source_text = text if isinstance(text, str) else ""
    spec = spec_for(signal)
    if not source_text.strip():
        return empty(signal)

    started = clock() if clock else None
    if extract is not None and enabled() and spec is not None:
        schema_error: str | None = None
        try:
            payload = extract(source_text, spec)
        except Exception as exc:  # noqa: BLE001 - 추출 실패가 파싱을 막아서는 안 된다.
            payload, schema_error = None, type(exc).__name__
        if payload is not None:
            validated = validate(payload, signal, source_text, spec)
            if validated is not None:
                elapsed = (clock() - started) * 1000 if (clock and started is not None) else None
                return replace(validated, prompt_version=PROMPT_VERSION, elapsed_ms=elapsed)
            schema_error = schema_error or "schema_mismatch"
        return _fallback(signal, source_text, rules, conservative, schema_error)
    return _fallback(signal, source_text, rules, conservative, None)


def _fallback(
    signal: str, text: str, rules: Judge | None, conservative: Judge | None, schema_error: str | None
) -> SemanticSignal:
    """2~4순위. 폴백은 정상 판정기가 아니라 장애 대응 수단이라 보수적으로 답한다.

    '침묵'과 '뜻 없음'을 구분한다: 어떤 판정기가 :data:`NONE` 을 돌려준 것은 "나는 못 읽었다"이지
    "이 문장에 그 뜻이 없다"가 아니다. 그래서 NONE 은 다음 순위를 막지 않고, 모든 순위가 NONE 일
    때에만 뜻 없음으로 확정한다 — 이걸 구분하지 않으면 상위 폴백의 침묵이 하위 폴백을 삼킨다.
    """
    silent: SemanticSignal | None = None
    for judge, source in ((rules, SOURCE_RULES), (conservative, SOURCE_CONSERVATIVE)):
        if judge is None:
            continue
        try:
            verdict = judge(text)
        except Exception:  # noqa: BLE001 - 폴백이 던지면 사슬 전체가 죽는다.
            continue
        if verdict is None:
            continue
        # 폴백이 답했다는 사실 자체가 메타데이터다 — 관측에서 1순위 실패율을 셀 수 있어야 한다.
        answered = replace(verdict, source=source, fallback_used=True, schema_error=schema_error)
        if answered.status != NONE:
            return answered
        silent = silent or answered
    if silent is not None:
        return silent
    # 아무 판정기도 답하지 않았다 — 조용히 false 로 접지 않고 판정 실패를 상태로 드러낸다.
    return unknown(signal, schema_error=schema_error)


# ── 질의 스코프(한 번 판정하고 이후 단계가 재사용) ─────────────────────────────────────────
# 재작성·절 분리·게이트가 **같은 값**을 읽게 하는 장치다. 하위 단계는 원문의 부분 문자열(타겟팅 절,
# 절 조각)로 다시 들어오므로, 상위에서 이미 판정했으면 대상·근거가 그 조각 안에 있는지로 투영해
# 재사용한다 — 조각마다 다시 물으면 같은 문장에 대해 다른 답이 나온다.
_SIGNALS: contextvars.ContextVar[tuple[str, dict[str, SemanticSignal]] | None] = contextvars.ContextVar(
    "semantic_signals", default=None
)


def project(signal: SemanticSignal, fragment: str) -> SemanticSignal:
    """상위 판정을 문장 조각에 투영한다. 그 조각이 근거·대상을 담고 있을 때만 신호가 성립한다."""
    haystack = compact(fragment)
    if not haystack:
        return empty(signal.signal, source=signal.source)
    kept = [row for row in signal.claims if row.entity and compact(row.entity) in haystack]
    if not kept:
        if signal.evidence and signal.evidence in haystack:
            kept = [row for row in signal.claims if not row.entity] or list(signal.claims)
        else:
            return empty(signal.signal, source=signal.source, fallback_used=signal.fallback_used)
    return build(
        signal.signal,
        kept,
        evidence=signal.evidence,
        source=signal.source,
        fallback_used=signal.fallback_used,
        model=signal.model,
        prompt_version=signal.prompt_version,
    )


@contextlib.contextmanager
def signal_scope(query: str, resolver: Callable[[str], dict[str, SemanticSignal]]):
    """질의 하나 동안 의미 판정을 열어 둔다. 이미 열린 스코프가 이 질의를 포함하면 재사용한다."""
    current_scope = _SIGNALS.get()
    if current_scope is not None and compact(query) in current_scope[0]:
        yield
        return
    token = _SIGNALS.set((compact(query), resolver(query)))
    try:
        yield
    finally:
        _SIGNALS.reset(token)


def current(signal: str, text: str) -> SemanticSignal | None:
    """열린 스코프에서 이 조각의 판정. 스코프가 없으면 None(호출부가 결정론 폴백을 쓴다)."""
    scope = _SIGNALS.get()
    if scope is None:
        return None
    resolved = scope[1].get(signal)
    if resolved is None:
        return None
    fragment = compact(text)
    if fragment == scope[0]:
        return resolved
    if not fragment or fragment not in scope[0]:
        return None
    # 조각으로 투영하려면 판정을 문장 안에 **위치시킬 수 있어야** 한다. 근거도 대상도 없는 판정
    # (형태 폴백이 그렇다)은 어느 조각의 것인지 알 수 없다 — 이때 투영하면 모든 조각에서 신호가
    # 사라진다. 위치를 모르면 None 을 돌려주고, 호출부가 그 조각에 결정론 판정을 다시 돌리게 둔다.
    if resolved.claims and not resolved.evidence and not resolved.entities:
        return None
    return project(resolved, text)


# ── 관측 ───────────────────────────────────────────────────────────────────────────────────
def log_evidence_enabled() -> bool:
    """근거(원문 조각)를 로그에 남길지. 기본은 끔 — 원문은 개인정보일 수 있다."""
    return os.getenv("SEMANTIC_SIGNAL_LOG_EVIDENCE", "").strip().casefold() in {"1", "true", "yes", "on"}


def observation(signal: SemanticSignal) -> dict[str, Any]:
    """운영에서 판정 문제를 분석하기 위한 최소 정보. 원문 전체는 남기지 않는다."""
    record: dict[str, Any] = {
        "signal": signal.signal,
        "status": signal.status,
        "detected": signal.detected,
        "negated": signal.negated,
        "entity_count": len(signal.entities),
        "source": signal.source,
        "fallback_used": signal.fallback_used,
        "schema_error": signal.schema_error,
        "model": signal.model,
        "prompt_version": signal.prompt_version,
        "elapsed_ms": signal.elapsed_ms,
    }
    if log_evidence_enabled() and signal.evidence:
        record["evidence"] = signal.evidence
    return record


__all__ = [
    "ASSERTED",
    "COMPLETED",
    "CONTEXTUAL",
    "DENIED",
    "HISTORY",
    "HYPOTHETICAL",
    "INTENT",
    "MENTIONED",
    "NONE",
    "ONGOING",
    "PROMPT_FILENAME",
    "PROMPT_VERSION",
    "SOURCE_CONSERVATIVE",
    "SOURCE_LLM",
    "SOURCE_NONE",
    "SOURCE_RULES",
    "SOURCE_UNKNOWN",
    "STATUSES",
    "STATUS_ORDER",
    "UNKNOWN",
    "RewriteDiff",
    "SemanticSignal",
    "SignalClaim",
    "SignalSpec",
    "build",
    "canonical_form",
    "claim",
    "compact",
    "compare_rewrite",
    "current",
    "detected_for",
    "empty",
    "enabled",
    "json_schema",
    "load_specs",
    "log_evidence_enabled",
    "observation",
    "project",
    "prompt_substitutions",
    "render_prompt",
    "resolve",
    "rollup",
    "same_meaning",
    "signal_scope",
    "signature",
    "spec_for",
    "unknown",
    "validate",
]
