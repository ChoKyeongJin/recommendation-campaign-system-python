"""TargetSpecification / ValidationResult 데이터 모델 — 의미 검증 파이프라인 v2 의 공용 타입.

배경/목표
---------
기존 의미 검증은 원문 NL 과 생성 SQL 을 **단일 LLM 호출**로 대조해 faithful 여부를 물었다. 경량 판정
모델이 도메인 인코딩(코드값·범위 확장·EXISTS/anti-join·날짜 창 방향)을 자주 오독해 **정상 SQL 을 자주
실패(오탐) 처리**했다. v2 는 이를 (1) 구조화된 요구사항(TargetSpecification), (2) AST 기반 SQL 의미
(SqlSemantics), (3) 규칙 기반 매핑/동치 판정, (4) pass/review/fail 판정으로 분리한다. LLM 은 규칙으로
결론 못 낸 항목의 근거 탐색으로만 제한한다.

이 모듈은 순수 타입/직렬화만 담는다 — graph_rag 를 import 하지 않는다(순환 방지). SqlSemantics 추출은
sql_semantics.py, 규칙 매핑/판정은 semantic_mapping.py, 오케스트레이션은 semantic_validation.py 가 맡는다.

실행: python -m pytest tests/test_semantic_validation_v2.py -q
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


ValidationStatus = Literal["pass", "review", "fail"]

# 요구사항이 SQL 에 어떻게 반영됐는지의 판정 상태.
RequirementCheckStatus = Literal[
    "matched",           # 표현까지 직접 일치(같은 필드·연산자·값)
    "equivalent",        # 표현은 다르나 결과 집합 의미가 동등(BETWEEN↔범위, IN↔EXISTS 등)
    "partially_matched", # 일부만 반영(예: 하한만 있고 상한 누락)
    "missing",           # SQL 에 대응 조건 없음
    "contradicted",      # 원문과 반대(극성/방향 반전, 잘못된 값)
    "ambiguous",         # 판단 근거 부족(규칙으로 결론 불가, 위반 근거도 없음)
    "not_applicable",    # 이 SQL/도메인에 해당 없음
]

# fail 로 귀결될 수 있는 판정. contradicted/missing(required) 만 결정적 실패 후보다.
FAILING_STATUSES = frozenset({"missing", "contradicted"})
# 확정 통과로 인정하는 판정.
PASSING_STATUSES = frozenset({"matched", "equivalent", "not_applicable"})
# 사람이 확인해야 하는(리뷰) 판정.
REVIEW_STATUSES = frozenset({"partially_matched", "ambiguous"})


@dataclass
class Requirement:
    """사용자 요청에서 뽑은 단일 타겟 조건. 각 요구사항은 고유 id 를 가진다(§1)."""

    id: str
    type: str                       # filter | membership | not_membership | aggregate | date_window | dedup
    field: str                      # 논리 필드명(예: 'age', 'orders.created_at', 'marketing_opt_out')
    operator: str | None = None     # '>=', '<=', '=', '<', '>', '!=', 'in', 'not_in', 'exists', 'not_exists', 'is_null', 'is_not_null'
    value: Any = None
    required: bool = True
    negated: bool = False
    window_days: int | None = None  # 날짜 창(rolling) 요구 시 일수
    metric: str | None = None       # 집계 요구 시 지표 id(order_count, purchase_amount 등)
    aggregate_func: str | None = None  # count | sum | avg | count_distinct
    # 범주형(코드/등급/권역): 자연어 값이 코드·집합으로 확장돼 SQL 에 들어간다. 값 확장의 완전성은
    # 판정하지 않고(§4) 컬럼 존재 + 극성(=/IN vs !=/NOT)만 본다. True 면 값 정확 비교를 건너뛴다.
    categorical: bool = False
    source_span: str | None = None  # 원문 근거 표현(관측성)
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None or k in ("value",)}


@dataclass
class Exclusion:
    """필수 제외 조건(마케팅 수신거부·탈퇴 등). SQL 에서 반드시 걸러져야 한다."""

    id: str
    field: str
    operator: str
    value: Any = None
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None or k in ("value",)}


@dataclass
class RetrievedEvidence:
    """SQL 생성 시 사용한 RAG 근거 문서(§7). 검증기가 동일 버전 근거를 우선 사용하도록 고정한다."""

    id: str
    version: str
    content_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TargetSpecification:
    """자연어 타겟 요청의 구조화 표현(§1)."""

    target_entity: str = "customer"
    requirements: list[Requirement] = field(default_factory=list)
    exclusions: list[Exclusion] = field(default_factory=list)
    deduplication_key: str | None = None
    aggregation: dict[str, Any] | None = None
    business_definition_versions: list[dict[str, Any]] = field(default_factory=list)
    # 임의 확정하지 않고 모호한 부분을 기록한다(§1) — id 목록.
    ambiguous_requirements: list[str] = field(default_factory=list)
    retrieved_evidence: list[RetrievedEvidence] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_entity": self.target_entity,
            "requirements": [r.to_dict() for r in self.requirements],
            "exclusions": [e.to_dict() for e in self.exclusions],
            "deduplication_key": self.deduplication_key,
            "aggregation": self.aggregation,
            "business_definition_versions": self.business_definition_versions,
            "ambiguous_requirements": self.ambiguous_requirements,
            "retrieved_evidence": [e.to_dict() for e in self.retrieved_evidence],
        }


@dataclass
class RequirementCheck:
    """요구사항 하나가 SQL 의 어느 구문으로 구현됐는지의 매핑 결과(§3)."""

    requirement_id: str
    status: RequirementCheckStatus
    sql_evidence: list[str] = field(default_factory=list)
    reason_code: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ExecutionAssertion:
    """샘플 실행 기반 검증 결과(§9). 데이터가 없어 실행 못 하면 status='skipped'."""

    name: str
    status: Literal["pass", "fail", "skipped"]
    actual: Any = None
    expected: Any = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyViolation:
    """정책/위험 조건 위반(§5). 기계가 읽는 code + 사람이 읽는 message."""

    code: str
    message: str
    requirement_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    """의미 검증 최종 결과(§권장 결과 타입)."""

    status: ValidationStatus
    checks: list[RequirementCheck] = field(default_factory=list)
    missing_requirements: list[str] = field(default_factory=list)
    extra_restrictions: list[str] = field(default_factory=list)
    ambiguous_requirements: list[str] = field(default_factory=list)
    policy_violations: list[PolicyViolation] = field(default_factory=list)
    confidence: float = 1.0
    parser_errors: list[str] = field(default_factory=list)
    execution_assertions: list[ExecutionAssertion] = field(default_factory=list)
    # fail/review 사유의 기계 판독 코드(관측성·디버깅). 최소 하나의 구체 근거를 담는다.
    reason_codes: list[str] = field(default_factory=list)

    # --- 기존 boolean 파이프라인 호환 어댑터(§12) ---
    @property
    def valid(self) -> bool:
        return self.status == "pass"

    @property
    def requires_review(self) -> bool:
        return self.status == "review"

    @property
    def invalid(self) -> bool:
        return self.status == "fail"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "checks": [c.to_dict() for c in self.checks],
            "missing_requirements": self.missing_requirements,
            "extra_restrictions": self.extra_restrictions,
            "ambiguous_requirements": self.ambiguous_requirements,
            "policy_violations": [p.to_dict() for p in self.policy_violations],
            "confidence": round(self.confidence, 4),
            "parser_errors": self.parser_errors,
            "execution_assertions": [a.to_dict() for a in self.execution_assertions],
            "reason_codes": self.reason_codes,
            # 호환 필드(기존 코드가 boolean 을 기대할 때).
            "valid": self.valid,
            "requires_review": self.requires_review,
            "invalid": self.invalid,
        }
