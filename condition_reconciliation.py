"""조건 소유권 재조정 — 여러 파서가 병행 해석한 같은 조건에 canonical owner 하나만 남긴다.

배경: 파서는 서로를 모른 채 후보를 만든다. 회원 속성/지역/파생 엔터티 집합은 전용 슬롯
(``exclude.gender`` / ``dimension_filters`` / ``target_user.entity_set_condition``)이 실DB 컬럼까지
확정해 소유하는데, 일반 집합식 파서가 같은 어구를 한 번 더 소비해 ``unknown_operand`` 를 남기면
그 하나 때문에 플랜 전체가 clarification 으로 막혔다. 표현형("…빼줘" vs "… 중 … 제외")에 따라
집합식 파서가 발동하기도 안 하기도 해서, **같은 요청이 문장 형태에 따라 통과/차단으로 갈렸다**.

이 모듈은 파서 뒤·최종 clarification 판정 앞에 들어가는 조정 단계다. 파서는 자유롭게 후보를
만들고, 여기서 소유권을 정리한 뒤 **남은 진짜 미해결만** clarification 이 된다.

구성(요구 인터페이스 그대로):

    policy     = ConditionPolicyLoader.load(path)
    candidates = collect_condition_candidates(plan, policy)
    ownership  = ConditionOwnershipResolver(policy).resolve(candidates)
    result     = SetExpressionReconciler(policy).apply(plan, ownership)
    verdict    = ClarificationEvaluator(policy).evaluate(plan, ownership)

정책(슬롯 이름·우선순위·매칭 임계·충돌 처리)은 전부 ``docs/data/condition_ownership_policy.json``
이 소유한다. 이 파일에는 특정 도시/성별/문장 표현이 없다 — 슬롯을 늘리거나 우선순위를 바꾸는
일은 JSON 한 줄이고, 여기 파이썬은 정책 해석과 일반 알고리즘(스팬 겹침·토큰 정규화·AST 재구성)만
가진다.

AST 재구성의 안전 규칙(의미 보존):

  * 교집합(``*``) 하위의 소유된 operand 는 지워도 된다 — 소유 슬롯이 같은 술어를 AND 로 건다.
  * 차집합(``-``) 우변의 소유된 operand 도 지워도 된다 — 제외를 소유 슬롯이 이미 건다.
  * 차집합 좌변이 통째로 소유되면 ``universe - X`` 로 남겨 **부정을 보존**한다(전칭 노드는
    ``1=1`` 로 컴파일된다).
  * 합집합(``+``)은 하위 operand 를 개별로 지우지 않는다 — 소유 슬롯들은 AND 로 결합되므로
    OR 한 항을 지우면 결과가 좁아진다. 하위 전부가 **같은 소유 인스턴스**(예: 한 dimension_filter
    의 IN 목록)일 때만 그 합집합 전체를 소유된 것으로 본다.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import set_expression_engine

# 플랜에 남는 조정 흔적(조건이 아니라 계측 — plan_decisions.NON_CONDITION_PLAN_KEYS 에 등록돼 있다).
TRACE_KEY = "condition_reconciliation"

DEFAULT_POLICY_PATH = Path(
    os.getenv("GRAPH_RAG_CONDITION_OWNERSHIP_POLICY", "docs/data/condition_ownership_policy.json")
)

# 집합식 AST 리프 종류(피연산자). set_expression_engine 이 만드는 노드 타입과 같다.
_LEAF_TYPES = ("operand", "unknown_operand", "age_range")
# 소유 슬롯이 이미 술어를 걸어 '자리만 남은' 노드. 컴파일러는 항진식(1=1)으로 읽는다.
UNIVERSE_TYPE = "universe"
UNIVERSE_NODE: dict[str, Any] = {"type": UNIVERSE_TYPE}

_TOKEN_SPLIT = re.compile(r"[^0-9A-Za-z가-힣]+")

# 정책이 이름으로 고르는 속성 변환기(일반 문자열 연산만 — 어휘/도메인 지식이 아니다).
_ATTRIBUTE_TRANSFORMS: dict[str, Any] = {
    "last_path_segment_lower": lambda value: str(value).split(".")[-1].casefold(),
    "casefold": lambda value: str(value).casefold(),
    "identity": lambda value: str(value),
}


def _is_universe(node: Any) -> bool:
    return isinstance(node, dict) and node.get("type") == UNIVERSE_TYPE


# ────────────────────────────── 정책 ──────────────────────────────


@dataclass(frozen=True)
class ConditionPolicy:
    """조건 소유권 정책(JSON 원문 + 조회 헬퍼). 슬롯 이름을 코드가 알 필요가 없게 하는 것이 목적."""

    raw: dict[str, Any]

    @property
    def version(self) -> Any:
        return self.raw.get("version")

    @property
    def owners(self) -> dict[str, dict[str, Any]]:
        owners = self.raw.get("owners")
        return owners if isinstance(owners, dict) else {}

    @property
    def priority(self) -> list[str]:
        ownership = self.raw.get("ownership")
        priority = ownership.get("priority") if isinstance(ownership, dict) else None
        return [str(tier) for tier in priority] if isinstance(priority, list) else []

    def priority_index(self, tier: str) -> int:
        """tier 우선순위(작을수록 강함). 목록에 없으면 가장 약하게 본다."""
        priority = self.priority
        return priority.index(tier) if tier in priority else len(priority)

    def is_authoritative(self, owner_key: str) -> bool:
        spec = self.owners.get(owner_key)
        return bool(spec and spec.get("authoritative"))

    def section(self, *path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for key in path:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    @property
    def suppression(self) -> dict[str, Any]:
        section = self.section("suppression", "set_expression_operands", default={})
        return section if isinstance(section, dict) else {}

    @property
    def matching(self) -> dict[str, Any]:
        section = self.section("matching", default={})
        return section if isinstance(section, dict) else {}

    @property
    def clarification(self) -> dict[str, Any]:
        section = self.section("clarification", default={})
        return section if isinstance(section, dict) else {}

    @property
    def conflicts(self) -> dict[str, Any]:
        section = self.section("conflicts", default={})
        return section if isinstance(section, dict) else {}

    @property
    def observability(self) -> dict[str, Any]:
        section = self.section("observability", default={})
        return section if isinstance(section, dict) else {}

    @property
    def ignore_tokens(self) -> tuple[str, ...]:
        tokens = self.section("matching", "normalized_text", "ignore_tokens", default=[])
        values = [str(token) for token in tokens if isinstance(token, str) and token] if isinstance(tokens, list) else []
        return tuple(sorted(set(values), key=len, reverse=True))


class ConditionPolicyLoader:
    """정책 로더(경로+mtime 캐시). 정책 파일이 없으면 '조정하지 않음'(빈 정책)으로 동작한다."""

    _cache: dict[tuple[str, float], ConditionPolicy] = {}

    @classmethod
    def load(cls, path: Path | str | None = None) -> ConditionPolicy:
        policy_path = Path(path) if path is not None else DEFAULT_POLICY_PATH
        try:
            stamp = policy_path.stat().st_mtime
        except OSError:
            return ConditionPolicy(raw={})
        key = (str(policy_path), stamp)
        cached = cls._cache.get(key)
        if cached is not None:
            return cached
        try:
            raw = json.loads(policy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ConditionPolicy(raw={})
        policy = ConditionPolicy(raw=raw if isinstance(raw, dict) else {})
        cls._cache[key] = policy
        return policy


# ────────────────────────────── 후보 ──────────────────────────────


@dataclass
class ConditionCandidate:
    """어느 슬롯이 어떤 조건을 주장하는지의 정규 레코드(파서 종류와 무관한 공통형)."""

    condition_id: str
    owner: str               # 소유 인스턴스 id (예: dimension_filters[0])
    owner_key: str           # 정책 owners 키
    tier: str
    authoritative: bool
    domain: str | None = None
    attribute: str | None = None
    polarity: str = "unknown"
    values: tuple[str, ...] = ()
    text: str = ""
    spans: tuple[tuple[int, int], ...] = ()
    trust_polarity: bool = True
    node: Any = None         # 집합식 operand 인 경우 원 AST 노드
    extra: dict[str, Any] = field(default_factory=dict)

    def text_hash(self, algorithm: str = "sha1", length: int = 12) -> str:
        digest = hashlib.new(algorithm, self.text.encode("utf-8")).hexdigest()
        return digest[:length] if length > 0 else digest


def _dig(node: Any, dotted: str) -> Any:
    for key in str(dotted).split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _as_spans(value: Any) -> list[tuple[int, int]]:
    """[s,e] 또는 [[s,e], …] 를 스팬 목록으로 정규화한다."""
    if not isinstance(value, (list, tuple)) or not value:
        return []
    if len(value) == 2 and all(isinstance(item, int) for item in value):
        return [(int(value[0]), int(value[1]))]
    spans: list[tuple[int, int]] = []
    for item in value:
        spans.extend(_as_spans(item))
    return spans


def _string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if isinstance(item, str) and item.strip()]
    return []


def _normalize_values(values: list[str]) -> tuple[str, ...]:
    seen: list[str] = []
    for value in values:
        folded = value.strip().casefold()
        if folded and folded not in seen:
            seen.append(folded)
    return tuple(seen)


def _fields(spec: dict[str, Any], name: str) -> list[str]:
    value = spec.get("fields", {}).get(name) if isinstance(spec.get("fields"), dict) else None
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def _attribute_of(spec: dict[str, Any], payload: dict[str, Any]) -> str | None:
    """정책이 지정한 필드/변환으로 조건 속성명을 만든다(없으면 선언된 semantic_attribute)."""
    fields = _fields(spec, "attribute")
    for name in fields:
        raw = _dig(payload, name)
        if isinstance(raw, str) and raw.strip():
            transform = _ATTRIBUTE_TRANSFORMS.get(str(spec.get("attribute_transform", "casefold")))
            return transform(raw) if transform else raw.casefold()
    attribute = spec.get("semantic_attribute")
    return str(attribute).casefold() if isinstance(attribute, str) else None


def _authoritative_here(spec: dict[str, Any], payload: Any) -> bool:
    """정책의 authoritative_when(필드=값) 조건까지 만족해야 소유권을 인정한다."""
    if not spec.get("authoritative"):
        return False
    guard = spec.get("authoritative_when")
    if not isinstance(guard, dict):
        return True
    if not isinstance(payload, dict):
        return False
    return all(_dig(payload, key) == expected for key, expected in guard.items())


def collect_condition_candidates(plan: dict[str, Any], policy: ConditionPolicy) -> list[ConditionCandidate]:
    """플랜의 각 슬롯을 정책 선언대로 읽어 공통 조건 레코드로 만든다(집합식 operand 제외).

    shape 은 슬롯 모양만 말한다: ``value``(스칼라) / ``value_list``(값 목록 = 한 소유 인스턴스) /
    ``object_list``(원소마다 소유 인스턴스) / ``object``(단일 객체). 새 슬롯은 JSON 한 블록이면 된다.
    """
    candidates: list[ConditionCandidate] = []
    for owner_key, spec in policy.owners.items():
        if not isinstance(spec, dict) or spec.get("shape") == "set_expressions":
            continue
        payload = _dig(plan, str(spec.get("path", owner_key)))
        if payload is None:
            continue
        shape = str(spec.get("shape", "object"))
        if shape == "value":
            entries: list[tuple[str, Any]] = [(owner_key, payload)] if isinstance(payload, str) and payload.strip() else []
        elif shape == "value_list":
            values = _string_values(payload)
            entries = [(owner_key, values)] if values else []
        elif shape == "object_list":
            entries = [
                (f"{owner_key}[{index}]", item)
                for index, item in enumerate(payload if isinstance(payload, list) else [])
                if isinstance(item, dict)
            ]
        else:
            entries = [(owner_key, payload)] if isinstance(payload, dict) and payload else []

        for owner_id, item in entries:
            candidate = _candidate_from(owner_id, owner_key, spec, item, policy)
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _candidate_from(
    owner_id: str,
    owner_key: str,
    spec: dict[str, Any],
    payload: Any,
    policy: ConditionPolicy,
) -> ConditionCandidate | None:
    if isinstance(payload, dict):
        values = _normalize_values([value for name in _fields(spec, "values") for value in _string_values(_dig(payload, name))])
        texts = [text for name in _fields(spec, "text") for text in _string_values(_dig(payload, name))]
        spans = tuple(span for name in _fields(spec, "spans") for span in _as_spans(_dig(payload, name)))
        polarity_field = _fields(spec, "polarity")
        polarity = next(
            (str(_dig(payload, name)) for name in polarity_field if isinstance(_dig(payload, name), str)),
            str(spec.get("polarity", "unknown")),
        )
        attribute = _attribute_of(spec, payload)
    else:
        values = _normalize_values(_string_values(payload))
        texts = list(values)
        spans = ()
        polarity = str(spec.get("polarity", "unknown"))
        attribute = _attribute_of(spec, {})

    if not _authoritative_here(spec, payload) and spec.get("authoritative"):
        return None  # 소유 조건(authoritative_when)을 못 채운 슬롯은 소유권을 주장하지 않는다
    if not values and not texts and not spans:
        return None

    text = next((item for item in texts if item.strip()), "")
    return ConditionCandidate(
        condition_id=_condition_id(owner_id, text or ",".join(values)),
        owner=owner_id,
        owner_key=owner_key,
        tier=str(spec.get("tier", owner_key)),
        authoritative=bool(spec.get("authoritative")),
        domain=str(spec["semantic_domain"]).casefold() if isinstance(spec.get("semantic_domain"), str) else None,
        attribute=attribute,
        polarity=polarity,
        values=values,
        text=text,
        spans=tuple(spans),
        trust_polarity=bool(spec.get("trust_polarity", True)),
    )


def _condition_id(owner_id: str, text: str) -> str:
    digest = hashlib.sha1(f"{owner_id}|{text}".encode("utf-8")).hexdigest()[:8]
    return f"cond_{digest}"


# ─────────────────────────── 소비 매칭 ───────────────────────────


class SourceConsumptionMatcher:
    """후보 둘이 '원문의 같은 조건'인지 판정한다(스팬 겹침 → 의미 지문 → 정규화 텍스트 순)."""

    def __init__(self, policy: ConditionPolicy) -> None:
        self.policy = policy
        self._ignore = policy.ignore_tokens

    def methods(self) -> list[str]:
        methods = self.policy.matching.get("methods")
        return [str(method) for method in methods] if isinstance(methods, list) else [
            "source_span", "semantic_fingerprint", "normalized_text"
        ]

    def matches(self, operand: ConditionCandidate, owned: ConditionCandidate) -> str | None:
        """매칭되면 사용한 방법 이름, 아니면 None."""
        for method in self.methods():
            config = self.policy.matching.get(method)
            config = config if isinstance(config, dict) else {}
            if not config.get("enabled", True):
                continue
            handler = getattr(self, f"_match_{method}", None)
            if handler is None:
                continue
            if handler(operand, owned, config):
                return method
        return None

    # ── 방법들 ──

    def _match_source_span(self, operand: ConditionCandidate, owned: ConditionCandidate, config: dict[str, Any]) -> bool:
        if not operand.spans or not owned.spans:
            return False
        if not self._polarity_ok(operand, owned):
            return False
        threshold = float(config.get("minimum_overlap_ratio", 0.6))
        basis = str(config.get("overlap_basis", "shorter"))
        return any(
            _span_overlap_ratio(left, right, basis) >= threshold
            for left in operand.spans
            for right in owned.spans
        )

    def _match_semantic_fingerprint(
        self, operand: ConditionCandidate, owned: ConditionCandidate, config: dict[str, Any]
    ) -> bool:
        if not owned.values or not operand.values:
            return False
        if config.get("require_same_polarity", True) and not self._polarity_ok(operand, owned):
            return False
        if config.get("require_same_attribute_when_known", True) and operand.attribute and owned.attribute:
            if operand.attribute != owned.attribute:
                return False
        threshold = float(config.get("minimum_value_overlap_ratio", 1.0))
        shared = len(set(owned.values) & set(operand.values))
        return shared / len(owned.values) >= threshold

    def _match_normalized_text(
        self, operand: ConditionCandidate, owned: ConditionCandidate, config: dict[str, Any]
    ) -> bool:
        if not self._polarity_ok(operand, owned):
            return False
        operand_tokens = set(self.tokens(operand.text)) | set(operand.values)
        if not operand_tokens:
            return False
        threshold = float(config.get("minimum_overlap_ratio", 0.6))
        # 값을 가진 소유자(지역/등급 등)는 '값이 operand 표면에 나타나는가'로 본다.
        if owned.values:
            if not config.get("allow_value_only_match", True):
                return False
            value_tokens = {token for value in owned.values for token in self.tokens(value) or [value]}
            shared = len(value_tokens & operand_tokens)
            ratio = float(self.policy.matching.get("semantic_fingerprint", {}).get("minimum_value_overlap_ratio", 1.0))
            return bool(value_tokens) and shared / len(value_tokens) >= ratio
        # 값 없이 표면 어구만 가진 소유자(파생 엔터티 집합 등). 토큰 겹침만으로 소유를 확정하면 과매칭
        # 위험이 크므로(표면어가 흔한 명사로만 이뤄질 수 있다) 정책이 요구하면 명시적 source span 을
        # 가진 소유자에게만, 더 높은 임계값으로 허용한다. 근거가 없으면 소유를 주장하지 않는다.
        valueless = config.get("valueless_owner")
        valueless = valueless if isinstance(valueless, dict) else {}
        if valueless.get("require_explicit_span", True) and not owned.spans:
            return False
        threshold = float(valueless.get("minimum_overlap_ratio", threshold))
        owner_tokens = set(self.tokens(owned.text))
        if not owner_tokens:
            return False
        return len(owner_tokens & operand_tokens) / len(owner_tokens) >= threshold

    # ── 공통 ──

    def _polarity_ok(self, operand: ConditionCandidate, owned: ConditionCandidate) -> bool:
        """극성이 어긋나면 다른 조건으로 본다. 단 '믿지 않는' 극성(비권위 파서)은 판단에 쓰지 않는다."""
        if not self.policy.matching.get("semantic_fingerprint", {}).get("require_same_polarity", True):
            return True
        if not operand.trust_polarity or not owned.trust_polarity:
            return True
        if "unknown" in (operand.polarity, owned.polarity):
            return True
        return operand.polarity == owned.polarity

    def tokens(self, text: str) -> list[str]:
        """조사/무의미 명사를 걷어낸 비교용 토큰(정책 ignore_tokens 가 소유)."""
        tokens: list[str] = []
        for raw in _TOKEN_SPLIT.split(text or ""):
            token = _strip_ignored_suffixes(raw, self._ignore).casefold()
            if not token or token in {item.casefold() for item in self._ignore}:
                continue
            tokens.append(token)
        return tokens


def _strip_ignored_suffixes(token: str, ignore: tuple[str, ...]) -> str:
    changed = True
    while changed and token:
        changed = False
        for suffix in ignore:
            if len(token) > len(suffix) and token.endswith(suffix):
                token = token[: -len(suffix)]
                changed = True
    return token


def _span_overlap_ratio(left: tuple[int, int], right: tuple[int, int], basis: str) -> float:
    overlap = max(0, min(left[1], right[1]) - max(left[0], right[0]))
    if overlap <= 0:
        return 0.0
    left_len, right_len = max(1, left[1] - left[0]), max(1, right[1] - right[0])
    if basis == "operand":
        return overlap / left_len
    if basis == "owner":
        return overlap / right_len
    return overlap / min(left_len, right_len)


# ─────────────────────────── 소유권 판정 ───────────────────────────


@dataclass
class OwnershipResult:
    """소유권 판정 결과: 권위 후보, 충돌, 그리고 '소비된' 집합식 operand 표시."""

    authoritative: list[ConditionCandidate] = field(default_factory=list)
    conflicts: list[dict[str, Any]] = field(default_factory=list)
    consumed: dict[str, dict[str, Any]] = field(default_factory=dict)

    def mark_consumed(self, operand_id: str, owner: ConditionCandidate, method: str) -> None:
        self.consumed[operand_id] = {"owner": owner.owner, "owner_key": owner.owner_key, "match_method": method}

    def is_consumed(self, operand_id: str) -> bool:
        return operand_id in self.consumed

    def owner_of(self, operand_id: str) -> str | None:
        entry = self.consumed.get(operand_id)
        return entry["owner"] if entry else None

    def requires_clarification(self) -> bool:
        return any(conflict.get("requires_clarification") for conflict in self.conflicts)


class ConditionOwnershipResolver:
    """후보들에 canonical owner 를 배정하고, 권위 슬롯끼리의 충돌을 정책대로 분류한다."""

    def __init__(self, policy: ConditionPolicy) -> None:
        self.policy = policy

    def resolve(self, candidates: list[ConditionCandidate]) -> OwnershipResult:
        authoritative = sorted(
            (candidate for candidate in candidates if candidate.authoritative),
            key=lambda candidate: self.policy.priority_index(candidate.tier),
        )
        return OwnershipResult(authoritative=authoritative, conflicts=self._conflicts(authoritative))

    def _conflicts(self, authoritative: list[ConditionCandidate]) -> list[dict[str, Any]]:
        rules = self.policy.conflicts
        groups: dict[tuple[str, str], list[ConditionCandidate]] = defaultdict(list)
        for candidate in authoritative:
            if candidate.attribute:
                groups[(candidate.domain or "", candidate.attribute)].append(candidate)

        conflicts: list[dict[str, Any]] = []
        for (domain, attribute), members in groups.items():
            if len(members) < 2:
                continue
            resolution = self._resolution_for(members, rules)
            if resolution is None:
                continue
            conflicts.append(
                {
                    "domain": domain or None,
                    "attribute": attribute,
                    "owners": [
                        {"owner": member.owner, "polarity": member.polarity, "values": list(member.values)}
                        for member in members
                    ],
                    "resolution": resolution,
                    "requires_clarification": resolution == "clarify",
                    "question": (
                        f"같은 조건('{attribute}')이 서로 다른 방향으로 지정됐습니다"
                        f"({', '.join(f'{member.owner}={member.polarity}' for member in members)}). "
                        "포함인지 제외인지 명확히 지정해 주세요."
                    )
                    if resolution == "clarify"
                    else None,
                    "canonical_owner": members[0].owner,
                }
            )
        return conflicts

    def _resolution_for(self, members: list[ConditionCandidate], rules: dict[str, Any]) -> str | None:
        polarities = {member.polarity for member in members if member.polarity != "unknown"}
        value_sets = [set(member.values) for member in members]
        shares_value = any(
            left & right for index, left in enumerate(value_sets) for right in value_sets[index + 1 :]
        )
        if len(polarities) > 1 and shares_value:
            return str(rules.get("same_attribute_opposite_polarity", "clarify"))
        if shares_value:
            return str(rules.get("same_attribute_same_value", "deduplicate"))
        if len({member.tier for member in members}) > 1:
            return str(rules.get("multiple_authoritative_owners", "highest_priority_or_clarify"))
        return str(rules.get("same_attribute_different_values", "merge_when_compatible"))


# ─────────────────────── 집합식 재구성(prune/rebuild) ───────────────────────


def iter_set_operands(node: Any, polarity: str = "include", path: str = "") -> Iterator[tuple[dict[str, Any], str, str]]:
    """집합식 AST 의 리프를 (노드, 극성, 경로)로 훑는다. 차집합 우변은 극성이 뒤집힌다."""
    if not isinstance(node, dict):
        return
    if node.get("type") in _LEAF_TYPES:
        yield node, polarity, path or "root"
        return
    if node.get("type") != "set_op":
        return
    flip = node.get("op") == "-"
    yield from iter_set_operands(node.get("left"), polarity, f"{path}L")
    yield from iter_set_operands(node.get("right"), _flip(polarity) if flip else polarity, f"{path}R")


def _flip(polarity: str) -> str:
    return {"include": "exclude", "exclude": "include"}.get(polarity, polarity)


def _operand_text(node: dict[str, Any]) -> str:
    for key in ("matched_text", "label", "text", "value", "canonical"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _operand_values(node: dict[str, Any]) -> tuple[str, ...]:
    return _normalize_values([value for key in ("canonical", "value") for value in _string_values(node.get(key))])


@dataclass
class ReconciliationResult:
    """조정 결과 — 무엇을 억제했고 무엇이 미해결로 남았는가."""

    trace: list[dict[str, Any]] = field(default_factory=list)
    suppressed: list[dict[str, Any]] = field(default_factory=list)
    remaining_unresolved: list[dict[str, Any]] = field(default_factory=list)
    parser_diagnostics: dict[str, Any] = field(default_factory=dict)
    dropped_expressions: int = 0
    rebuilt_expressions: int = 0


class SetExpressionReconciler:
    """권위 슬롯이 소유한 operand 를 집합식에서 걷어내고, 남은 진짜 집합 연산만 재구성한다."""

    def __init__(self, policy: ConditionPolicy) -> None:
        self.policy = policy
        self.matcher = SourceConsumptionMatcher(policy)

    def apply(self, plan: dict[str, Any], ownership: OwnershipResult) -> ReconciliationResult:
        result = ReconciliationResult()
        expressions = plan.get("set_expressions")
        if not isinstance(expressions, list) or not expressions:
            return result
        if not self.policy.suppression.get("enabled", True):
            return result

        spec = self.policy.owners.get("set_expressions", {})
        trust_polarity = bool(spec.get("trust_polarity", True))
        kept: list[dict[str, Any]] = []
        for index, expression in enumerate(expressions):
            if not isinstance(expression, dict):
                kept.append(expression)
                continue
            self._record_local_diagnostics(expression, result)
            ast = expression.get("set_ast")
            if not isinstance(ast, dict):
                kept.append(expression)
                continue

            operands = self._operand_candidates(expression, index, trust_polarity)
            self._consume(operands, ownership, result)
            rebuilt = self._rewrite(ast, ownership, operands, "include")
            if rebuilt is None or _is_universe(rebuilt):
                result.dropped_expressions += 1
                result.trace.append(
                    {
                        "candidate_source": "set_expressions",
                        "expression_id": expression.get("expression_id"),
                        "action": "drop_expression",
                        "reason": "all_operands_owned_by_authoritative_slots",
                    }
                )
                continue
            if rebuilt != ast:
                result.rebuilt_expressions += 1
                result.trace.append(
                    {
                        "candidate_source": "set_expressions",
                        "expression_id": expression.get("expression_id"),
                        "action": "rebuild_expression",
                        "reason": "unconsumed_operands_only",
                    }
                )
            expression["set_ast"] = rebuilt
            self._annotate(rebuilt, operands)
            kept.append(expression)
        plan["set_expressions"] = kept
        return result

    # ── 후보/소비 ──

    def _operand_candidates(
        self, expression: dict[str, Any], index: int, trust_polarity: bool
    ) -> dict[int, ConditionCandidate]:
        """집합식 operand 를 공통 후보 레코드로 만든다(원문 스팬은 표현 원문에서 복원)."""
        expression_text = expression.get("expression_text")
        expression_text = expression_text if isinstance(expression_text, str) else ""
        candidates: dict[int, ConditionCandidate] = {}
        for node, polarity, path in iter_set_operands(expression.get("set_ast")):
            text = _operand_text(node)
            span = _locate(expression_text, text)
            candidates[id(node)] = ConditionCandidate(
                condition_id=_condition_id(f"set_expressions[{index}].{path}", text),
                owner=f"set_expressions[{index}].{path}",
                owner_key="set_expressions",
                tier=str(self.policy.owners.get("set_expressions", {}).get("tier", "set_expressions")),
                authoritative=False,
                domain=None,
                attribute=None,
                polarity=polarity,
                values=_operand_values(node),
                text=text,
                spans=(span,) if span else (),
                trust_polarity=trust_polarity,
                node=node,
                extra={"node_type": str(node.get("type")), "path": path},
            )
        return candidates

    def _consume(
        self,
        operands: dict[int, ConditionCandidate],
        ownership: OwnershipResult,
        result: ReconciliationResult,
    ) -> None:
        suppression = self.policy.suppression
        if not suppression.get("suppress_when_owned_by_authoritative_slot", True):
            return
        allow_unknown = suppression.get("suppress_unknown_when_source_is_consumed", True)
        for candidate in operands.values():
            is_unknown = candidate.extra.get("node_type") == "unknown_operand"
            if is_unknown and not allow_unknown:
                continue
            for owner in ownership.authoritative:
                method = self.matcher.matches(candidate, owner)
                if method is None:
                    continue
                ownership.mark_consumed(candidate.owner, owner, method)
                entry = self._trace_entry(candidate, owner, method, is_unknown)
                result.trace.append(entry)
                result.suppressed.append(
                    {
                        "parser": "set_expressions",
                        "reason": "operand_owned_by_authoritative_slot"
                        if not is_unknown
                        else "consumed_unknown_operand",
                        "owner": owner.owner,
                        **({"raw_text": candidate.text} if self._include_raw_text() else {}),
                        "condition_id": candidate.condition_id,
                    }
                )
                break

    def _trace_entry(
        self, candidate: ConditionCandidate, owner: ConditionCandidate, method: str, is_unknown: bool
    ) -> dict[str, Any]:
        observability = self.policy.observability
        entry: dict[str, Any] = {
            "candidate_source": "set_expressions",
            "condition_id": candidate.condition_id,
            "text_hash": candidate.text_hash(
                str(observability.get("hash_algorithm", "sha1")), int(observability.get("hash_length", 12))
            ),
            "matched_owner": owner.owner,
            "action": "suppress_consumed_unknown" if is_unknown else "suppress_duplicate",
            "match_method": method,
        }
        if self._include_raw_text():
            entry["candidate"] = candidate.text
        return entry

    def _include_raw_text(self) -> bool:
        return bool(self.policy.observability.get("include_raw_text", True))

    def _record_local_diagnostics(self, expression: dict[str, Any], result: ReconciliationResult) -> None:
        """파서가 스스로 낸 clarification 을 '진단 후보'로 보관한다(최종 판정은 조정 후)."""
        unknowns = [
            _operand_text(node)
            for node, _polarity, _path in iter_set_operands(expression.get("set_ast"))
            if node.get("type") == "unknown_operand"
        ]
        if not expression.get("requires_clarification") and not unknowns:
            return
        diagnostics = result.parser_diagnostics.setdefault(
            "set_expressions", {"local_requires_clarification": False, "reasons": []}
        )
        diagnostics["local_requires_clarification"] = bool(
            diagnostics["local_requires_clarification"] or expression.get("requires_clarification")
        )
        for text in unknowns:
            reason: dict[str, Any] = {"type": "unknown_operand"}
            if self._include_raw_text():
                reason["raw_text"] = text
            reason["text_hash"] = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
            diagnostics["reasons"].append(reason)

    # ── AST 재구성 ──

    def _rewrite(
        self,
        node: Any,
        ownership: OwnershipResult,
        operands: dict[int, ConditionCandidate],
        polarity: str,
    ) -> Any:
        """소유된 operand 를 걷어낸 AST 를 돌려준다(전부 사라지면 None).

        'None = 이 자리는 소유 슬롯이 이미 건다' 이지 '참(TRUE)' 이 아니다 — 부정 문맥에서 참으로
        접으면 결과가 공집합이 되므로, 부모가 자기 연산에 맞게 **제거**로 흡수한다. 유일한 예외가
        차집합 좌변으로, 거기서만 전칭 노드(``universe``)를 남겨 우변의 부정을 보존한다.
        """
        if not isinstance(node, dict):
            return None
        if node.get("type") in _LEAF_TYPES:
            candidate = operands.get(id(node))
            if candidate is not None and ownership.is_consumed(candidate.owner):
                return None
            return node
        if node.get("type") != "set_op":
            return node

        op = node.get("op")
        if op == "+" and self._union_needs_same_owner(polarity):
            return self._rewrite_union(node, ownership, operands)

        left = self._rewrite(node.get("left"), ownership, operands, polarity)
        right = self._rewrite(
            node.get("right"), ownership, operands, _flip(polarity) if op == "-" else polarity
        )
        return self._simplify(node, op, left, right)

    def _union_needs_same_owner(self, polarity: str) -> bool:
        """합집합 내부 억제를 막아야 하는 문맥인가.

        긍정 문맥의 OR 은 소유 슬롯들이 AND 로 결합되므로 한 항을 지우면 결과가 좁아진다. 반대로
        **부정 문맥의 OR('A 또는 B 를 제외')은 드모르간으로 각 항의 제외들의 AND** 와 같아서, 각
        소유 슬롯이 자기 제외를 걸어 두면 항을 지워도 의미가 보존된다. 어느 극성에서 보호할지는
        정책(union_same_owner_required_polarities)이 정한다.
        """
        suppression = self.policy.suppression
        if not suppression.get("union_operands_require_same_owner", True):
            return False
        polarities = suppression.get("union_same_owner_required_polarities", ["include"])
        return polarity in {str(item) for item in polarities} if isinstance(polarities, list) else True

    def _rewrite_union(
        self, node: dict[str, Any], ownership: OwnershipResult, operands: dict[int, ConditionCandidate]
    ) -> Any:
        """보호 문맥의 합집합은 하위 전부가 '같은 소유 인스턴스'일 때만 통째로 소유된 것으로 본다."""
        owners: set[str] = set()
        for leaf, _polarity, _path in iter_set_operands(node):
            candidate = operands.get(id(leaf))
            owner = ownership.owner_of(candidate.owner) if candidate is not None else None
            if owner is None:
                return node  # 하나라도 미소유면 합집합 내부는 손대지 않는다
            owners.add(owner)
        return None if len(owners) == 1 else node

    def _simplify(self, node: dict[str, Any], op: Any, left: Any, right: Any) -> Any:
        if left is None and right is None:
            return None
        if op == "-":
            if right is None:
                return left           # 제외는 소유 슬롯이 이미 건다 → 우변 제거
            if left is None:
                # 좌변(모집합)만 소유됐다 → 우변의 부정을 보존해야 하므로 전칭 노드를 세운다.
                rebuilt = dict(node)
                rebuilt["left"], rebuilt["right"] = copy.deepcopy(UNIVERSE_NODE), right
                return rebuilt
        elif left is None or right is None:
            return left if right is None else right
        rebuilt = dict(node)
        rebuilt["left"], rebuilt["right"] = left, right
        return rebuilt

    def _annotate(self, node: Any, operands: dict[int, ConditionCandidate]) -> None:
        """남은 operand 에 관측용 식별자/스팬을 실어 다음 단계가 같은 조건을 다시 추적할 수 있게 한다."""
        if not self.policy.suppression.get("annotate_operands", True):
            return
        for leaf, _polarity, _path in iter_set_operands(node):
            candidate = operands.get(id(leaf))
            if candidate is None:
                continue
            leaf.setdefault("condition_id", candidate.condition_id)
            if candidate.spans:
                start, end = candidate.spans[0]
                leaf.setdefault("source_span", {"start": start, "end": end})


def _locate(haystack: str, needle: str) -> tuple[int, int] | None:
    if not haystack or not needle:
        return None
    start = haystack.find(needle)
    if start < 0:
        return None
    return (start, start + len(needle))


# ─────────────────────────── clarification 판정 ───────────────────────────


class ClarificationEvaluator:
    """최종 clarification 을 계산한다 — 조정 후에도 아무 슬롯이 소유하지 못한 것만 남긴다."""

    def __init__(self, policy: ConditionPolicy) -> None:
        self.policy = policy

    def evaluate(self, plan: dict[str, Any], ownership: OwnershipResult) -> dict[str, Any]:
        config = self.policy.clarification
        unknown_blocks = bool(config.get("unknown_operand_requires_clarification", True))
        remaining: list[dict[str, Any]] = []
        issues: list[dict[str, Any]] = []

        for expression in plan.get("set_expressions", []) or []:
            if not isinstance(expression, dict):
                continue
            unresolved = [
                _operand_text(node)
                for node, _polarity, _path in iter_set_operands(expression.get("set_ast"))
                if node.get("type") == "unknown_operand"
            ]
            had_unknown_before = bool(expression.get("_unknown_operands_before", unresolved))
            other_reason = bool(expression.get("requires_clarification")) and not had_unknown_before
            requires = (bool(unresolved) and unknown_blocks) or other_reason
            expression["requires_clarification"] = requires
            if unresolved and unknown_blocks:
                expression["clarification_question"] = set_expression_engine.clarification_question(unresolved)
            elif not requires:
                expression["clarification_question"] = None
            for text in unresolved:
                remaining.append({"raw_text": text, "reason": "no_authoritative_owner"})
                issues.append(
                    {
                        "type": "unknown_operand",
                        "raw_text": text,
                        "is_blocking": unknown_blocks,
                        "is_suppressed": False,
                        "is_consumed_by_authoritative_owner": False,
                    }
                )

        for conflict in ownership.conflicts:
            if conflict.get("requires_clarification"):
                issues.append(
                    {
                        "type": "authoritative_owner_conflict",
                        "attribute": conflict.get("attribute"),
                        "question": conflict.get("question"),
                        "is_blocking": True,
                        "is_suppressed": False,
                        "is_consumed_by_authoritative_owner": False,
                    }
                )

        requires_clarification = any(
            issue["is_blocking"] and not issue["is_suppressed"] and not issue["is_consumed_by_authoritative_owner"]
            for issue in issues
        )
        return {
            "requires_clarification": requires_clarification,
            "issues": issues,
            "remaining_unresolved": remaining,
        }


# ─────────────────────────── 파이프라인 진입점 ───────────────────────────


def reconcile_plan(plan: dict[str, Any], policy: ConditionPolicy | None = None) -> dict[str, Any]:
    """파서 결과 병합 이후·최종 clarification 판정 이전에 부르는 조정 단계.

    1) 후보 수집 → 2) 소유권 판정 → 3) 소비 표시 → 4) 집합식 prune/rebuild →
    5) 미해결 재수집 → 6) 최종 requires_clarification 계산 → 7) 흔적 기록.
    """
    active_policy = policy or ConditionPolicyLoader.load()
    if not active_policy.owners:
        return {}

    for expression in plan.get("set_expressions", []) or []:
        if isinstance(expression, dict):
            expression["_unknown_operands_before"] = [
                _operand_text(node)
                for node, _polarity, _path in iter_set_operands(expression.get("set_ast"))
                if node.get("type") == "unknown_operand"
            ]

    candidates = collect_condition_candidates(plan, active_policy)
    ownership = ConditionOwnershipResolver(active_policy).resolve(candidates)
    result = SetExpressionReconciler(active_policy).apply(plan, ownership)
    verdict = ClarificationEvaluator(active_policy).evaluate(plan, ownership)

    for expression in plan.get("set_expressions", []) or []:
        if isinstance(expression, dict):
            expression.pop("_unknown_operands_before", None)

    max_entries = int(active_policy.observability.get("max_trace_entries", 50))
    trace = {
        "policy_version": active_policy.version,
        "parser_diagnostics": result.parser_diagnostics,
        "trace": result.trace[:max_entries],
        "suppressed_diagnostics": result.suppressed[:max_entries],
        "remaining_unresolved": verdict["remaining_unresolved"],
        "conflicts": ownership.conflicts,
        "issues": verdict["issues"],
        "requires_clarification": verdict["requires_clarification"],
        "dropped_expressions": result.dropped_expressions,
        "rebuilt_expressions": result.rebuilt_expressions,
    }
    has_content = bool(
        result.trace or ownership.conflicts or verdict["remaining_unresolved"] or result.parser_diagnostics
    )
    if has_content:
        plan[TRACE_KEY] = trace
    else:
        plan.pop(TRACE_KEY, None)
    return trace


def conflict_clarifications(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """권위 슬롯끼리의 충돌로 확인이 필요한 항목(없으면 빈 목록). 상위 검증 단계가 읽는다."""
    trace = plan.get(TRACE_KEY)
    if not isinstance(trace, dict):
        return []
    return [
        conflict for conflict in trace.get("conflicts", [])
        if isinstance(conflict, dict) and conflict.get("requires_clarification")
    ]
