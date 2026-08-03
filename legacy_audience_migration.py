"""legacy 오디언스 슬롯 → canonical Event IR 어댑터(strangler 이행 1차 웨이브).

    legacy slot asset → legacy_slot_to_event_ir → plan.event_expression → event_compiler → SQL

**슬롯 이름은 이 파일 안에서만 산다.** Event IR 본체에는 ``purchase_date`` 같은 이름이 의미 노드로
들어가지 않는다 — 들어가는 순간 IR 이 옛 슬롯 모델을 이름만 바꿔 되풀이하게 된다. 물리 테이블·
컬럼·조인은 여기서도 만들지 않는다: 집계 레시피(함수·표현 필드·distinct·소스)는 **주입된 카탈로그**
에서 읽고, 실제 SQL 바인딩은 여전히 :mod:`event_compiler` 레지스트리가 소유한다.

설계 규칙 넷(전부 fail-close 쪽으로 기운다):

1. **부분 변환은 실행 가능하지 않다.** 의미 경로가 하나라도 미매핑이면 ``expression`` 은 있어도
   ``is_executable`` 이 False 다(그리고 상태는 BLOCKED_*). 반쯤 변환된 오디언스는 실패보다 나쁘다 —
   조용히 넓거나 좁은 대상이 성공으로 나간다.
2. **경로 회계는 전수다.** 오디언스 컨테이너의 모든 키는 consumed / ignored / unmapped / invalid
   넷 중 정확히 하나에 들어간다. '모르는 키를 조용히 지나침'이 이 이행에서 가장 위험한 침묵이다.
3. **근거를 지어내지 않는다.** legacy 슬롯에는 원문 스팬이 없다. 라벨을 evidence 로 승격하면
   검증 계층이 '검증된 근거'로 오인한다 — 원문 흔적은 IR 이 아니라 **영수증**에 보존한다.
4. **어댑터는 쓰지 않는다.** DB 도 플랜도 변형하지 않는 순수 함수다. 저장·권위 변경은 호출자 몫이고,
   그래야 dry-run 과 실제 실행이 같은 코드 경로를 쓴다.

A 그룹(무손실 변환 가능)의 근거는 legacy 실행 술어와 Event IR 컴파일 결과가 **같은 집합**임을
저장소에서 직접 대조해 정했다(자세한 대조표는 docs/plans_legacy_audience_strangler.md).
로그인 계열(recent_login/inactivity_period)이 A 그룹에서 빠진 이유도 거기 있다 — 표현은 되지만
NULL·경계·유효성 가드가 달라 **같은 집합이 아니다**.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence
from zoneinfo import ZoneInfo

import event_ir
import migration_fingerprint
from audience_authority import MigrationStatus

ADAPTER_VERSION = "1.0.0"
# 이행 metadata envelope 의 스키마 버전. 기존 event_expression 페이로드(schema_version 없음)는
# 0 으로 읽는다 — 역직렬화가 깨지지 않아야 한다.
ENVELOPE_SCHEMA_VERSION = 1
MIGRATION_SOURCE = "legacy_migration"

# 오디언스 조건이 사는 컨테이너. 이 밖의 플랜 키(intent/campaign_constraints/…)는 오디언스가 아니므로
# 어댑터의 경로 회계 범위 밖이다 — '무시했다'가 아니라 '보지 않는다'이고, 그 구분을 흐리면
# ignored_paths 가 의미를 잃는다.
AUDIENCE_CONTAINERS: tuple[str, ...] = ("target_user", "exclude")

# 비의미 키 — 값이 달라져도 **대상 집합이 달라지지 않는** 것만 넣는다. 조건 필드·연산자·기간·값·
# 부정 여부는 절대 들어오지 않는다(계약 테스트가 강제한다).
NON_SEMANTIC_KEYS: frozenset[str] = frozenset({
    "label",            # 세그먼트 표시 문자열(원문 라벨) — 영수증에 보존한다
    "surface",          # 원문 표면 구간 표기
    "sql_interval",     # value/unit 의 렌더 파생값
    "satisfied_by",     # 실행 계층이 나중에 붙이는 소유권 표식
    "note", "notes", "comment", "_comment", "description",
    "display_name", "created_at", "created_by", "updated_at", "generated_at",
})


# ── 분류 ──────────────────────────────────────────────────────────────────────────

CONVERTIBLE = "convertible"
NEEDS_CATALOG_BINDING = "needs_catalog_binding"
NEEDS_IR_EXTENSION = "needs_ir_extension"
NEEDS_DOMAIN_DECISION = "needs_domain_decision"
UNSUPPORTED = "unsupported"
INVALID_LEGACY_ASSET = "invalid_legacy_asset"

CLASSIFICATIONS: tuple[str, ...] = (
    CONVERTIBLE, NEEDS_CATALOG_BINDING, NEEDS_IR_EXTENSION,
    NEEDS_DOMAIN_DECISION, UNSUPPORTED, INVALID_LEGACY_ASSET,
)

# 분류 → 이행 상태. 자산 하나의 '왜 막혔나'가 상태로 그대로 드러나야 해소 주체가 정해진다.
_STATUS_BY_CLASSIFICATION: dict[str, MigrationStatus] = {
    CONVERTIBLE: MigrationStatus.CONVERTED,
    NEEDS_CATALOG_BINDING: MigrationStatus.BLOCKED_CATALOG,
    NEEDS_IR_EXTENSION: MigrationStatus.BLOCKED_IR_EXTENSION,
    NEEDS_DOMAIN_DECISION: MigrationStatus.BLOCKED_DOMAIN_DECISION,
    UNSUPPORTED: MigrationStatus.BLOCKED_IR_EXTENSION,
    INVALID_LEGACY_ASSET: MigrationStatus.INVALID_LEGACY_ASSET,
}

# 막힘 분류의 심각도 순서 — 자산 하나에 여러 사유가 겹치면 **가장 근본적인 것**이 자산의 분류다.
# (원본이 깨져 있으면 IR 확장을 논할 단계가 아니다.)
_SEVERITY: tuple[str, ...] = (
    INVALID_LEGACY_ASSET, NEEDS_DOMAIN_DECISION, UNSUPPORTED,
    NEEDS_IR_EXTENSION, NEEDS_CATALOG_BINDING, CONVERTIBLE,
)


class MigrationContextError(ValueError):
    """이행 문맥이 시간 계약을 만족하지 않는다(암묵 timezone/기준시각 금지)."""


@dataclass(frozen=True)
class MigrationContext:
    """변환 한 번의 **명시적** 문맥. 기본 timezone 도 기본 기준시각도 두지 않는다.

    시간 조건의 뜻은 기준 시각과 timezone 없이는 결정되지 않는다('최근 30일'이 언제부터인가).
    암묵 기본값을 두면 legacy 실행과 shadow 비교가 서로 다른 앵커를 쓰고, 그 차이는 경계
    근처 회원 몇 명으로만 나타나 눈에 띄지 않는다.

    1차 웨이브의 A 그룹은 ``as_of`` 에 **의존하지 않는** 창만 만든다(절대 구간은 자산이 확정해
    갖고 있고, 롤링 창은 컴파일러가 실행 시점 함수로 렌더한다). 그래도 앵커를 문맥에 싣는 이유는
    replay 비교가 legacy 와 Event IR 에 **같은 앵커**를 쓴 사실을 보고서에 남겨야 하기 때문이고,
    상대 창을 쓰는 다음 웨이브가 이 값을 그대로 소비하기 때문이다.
    """

    as_of: datetime
    timezone: str
    catalog_version: str
    compiler_version: str
    adapter_version: str = ADAPTER_VERSION
    # ``ResolvedSemanticCatalog`` 호환 객체(.metric/.source/.grain). 주입이라 어댑터는 순수하다.
    catalog: Any = None

    def __post_init__(self) -> None:
        if not isinstance(self.as_of, datetime):
            raise MigrationContextError("as_of 는 datetime 이어야 한다")
        if self.as_of.tzinfo is None or self.as_of.utcoffset() is None:
            raise MigrationContextError(
                "as_of 는 timezone 을 포함해야 한다 — naive 기준시각은 실행 환경의 로컬시간을 몰래 쓴다"
            )
        if not isinstance(self.timezone, str) or not self.timezone.strip():
            raise MigrationContextError("timezone 을 명시해야 한다(암묵 기본값 금지)")
        try:
            ZoneInfo(self.timezone)
        except Exception as exc:  # noqa: BLE001 — 알 수 없는 zone 이름은 문맥 오류다.
            raise MigrationContextError(f"알 수 없는 timezone: {self.timezone!r}") from exc

    @property
    def as_of_date(self) -> Any:
        """선언된 timezone 기준의 달력 날짜(상대 창 확정에 쓰는 유일한 기준일)."""
        return self.as_of.astimezone(ZoneInfo(self.timezone)).date()

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "timezone": self.timezone,
            "catalog_version": self.catalog_version,
            "compiler_version": self.compiler_version,
            "adapter_version": self.adapter_version,
        }


# ── 산출물 타입 ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MigrationError:
    code: str
    path: str
    message: str
    classification: str = NEEDS_DOMAIN_DECISION
    # 치명 오류는 실행 가능 판정을 무조건 막는다. 비치명은 진단이다(현재 웨이브에는 없음 —
    # 필드를 두는 이유는 '전부 치명'이라는 사실을 코드가 말하게 하기 위해서다).
    fatal: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "path": self.path, "message": self.message,
            "classification": self.classification, "fatal": self.fatal,
        }


@dataclass(frozen=True)
class IgnoredPath:
    """의미가 아니라서 소비하지 않은 경로. **사유 없는 무시는 허용하지 않는다.**"""

    path: str
    reason: str
    category: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "reason": self.reason, "category": self.category}


@dataclass(frozen=True)
class ProvenanceEdge:
    """원문(슬롯) 경로 ↔ IR 노드의 **다대다** 대응.

    '슬롯 하나 = 노드 하나' 규칙을 쓰지 않는 이유: 기간 슬롯 하나는 창 노드 + 술어 노드가 되고,
    나열 창 하나는 Or 아래 여러 Exists 가 되며, 여러 슬롯이 하나의 논리식으로 합쳐진다.
    """

    source_paths: tuple[str, ...]
    target_node_ids: tuple[str, ...]
    transformation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_paths": list(self.source_paths),
            "target_node_ids": list(self.target_node_ids),
            "transformation": self.transformation,
        }


@dataclass(frozen=True)
class MigrationReceipt:
    """변환 하나의 감사 기록 — 원문 근거·소스 경로·변환 이유를 보존한다."""

    source_paths: tuple[str, ...]
    transformation: str
    evidence: Mapping[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "source_paths": list(self.source_paths),
            "transformation": self.transformation,
            "evidence": dict(self.evidence),
        }
        if self.note:
            payload["note"] = self.note
        return payload


@dataclass(frozen=True)
class MigrationResult:
    expression: event_ir.Condition | None
    receipts: tuple[MigrationReceipt, ...]
    consumed_paths: tuple[str, ...]
    ignored_paths: tuple[IgnoredPath, ...]
    unmapped_paths: tuple[str, ...]
    invalid_paths: tuple[str, ...]
    fingerprint: str | None
    status: MigrationStatus
    errors: tuple[MigrationError, ...]
    classification: str = CONVERTIBLE
    provenance: tuple[ProvenanceEdge, ...] = ()
    source_fingerprint: str = ""
    source_schema_checksum: str = ""
    binding_fingerprint: str | None = None
    bindings: Mapping[str, Any] = field(default_factory=dict)

    @property
    def fatal_errors(self) -> tuple[MigrationError, ...]:
        return tuple(error for error in self.errors if error.fatal)

    @property
    def is_executable(self) -> bool:
        """이 변환 결과를 실행 가능한 표현으로 볼 수 있는가(권위 이관과는 별개 판정)."""
        return (
            self.expression is not None
            and not self.unmapped_paths
            and not self.invalid_paths
            and not self.fatal_errors
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "classification": self.classification,
            "is_executable": self.is_executable,
            "expression": self.expression.to_dict() if self.expression is not None else None,
            "semantic_fingerprint": self.fingerprint,
            "source_fingerprint": self.source_fingerprint,
            "source_schema_checksum": self.source_schema_checksum,
            "binding_fingerprint": self.binding_fingerprint,
            "consumed_paths": list(self.consumed_paths),
            "ignored_paths": [item.to_dict() for item in self.ignored_paths],
            "unmapped_paths": list(self.unmapped_paths),
            "invalid_paths": list(self.invalid_paths),
            "receipts": [item.to_dict() for item in self.receipts],
            "provenance": [item.to_dict() for item in self.provenance],
            "errors": [item.to_dict() for item in self.errors],
        }


# ── 슬롯 변환기 ───────────────────────────────────────────────────────────────────
# 변환기는 (조건, 영수증, 오류) 를 낸다. 조건이 None 이면 이 슬롯은 변환되지 않았고,
# 그 사실은 오류 목록으로만 보고된다 — 조용히 빠지는 길은 없다.

PURCHASE_SOURCE = "purchase"

# legacy 집계 지표 id → canonical 카탈로그 지표 id. 이 표가 어댑터에 있는 것이 맞다:
# 왼쪽은 legacy 슬롯 어휘이고 오른쪽은 카탈로그 심볼이며, 레시피(함수·필드·distinct)는
# 카탈로그가 소유한다(여기서 다시 적지 않는다).
AGGREGATE_METRIC_MAP: dict[str, str] = {
    "order_count": "purchase_count",
    "purchase_amount": "purchase_amount",
}

# 무손실이 성립하는 임계 방향. legacy 집계 술어는 사건이 있는 회원만 집계 행으로 만들고(INNER JOIN),
# Event IR 은 사건이 없는 회원도 0 으로 평가한다 — 임계값이 양수인 '이상/초과'에서만 두 해석이 같다.
LOSSLESS_AGGREGATE_OPERATORS: frozenset[str] = frozenset({">=", ">"})

# 변환하지 않는 legacy 집계 지표와 그 사유. '아직 안 했다'와 '표현할 수 없다'를 가른다.
AGGREGATE_METRIC_BLOCKED: dict[str, tuple[str, str]] = {
    "total_item_quantity": (NEEDS_CATALOG_BINDING, "주문 상세(수량) 소스가 오디언스 카탈로그에 선언되어 있지 않다"),
    "distinct_product_count": (NEEDS_CATALOG_BINDING, "상품 단위 distinct 집계 소스가 카탈로그에 없다"),
    "distinct_brand_count": (NEEDS_CATALOG_BINDING, "브랜드 컬럼 바인딩이 카탈로그에 없다"),
    "distinct_category_count": (NEEDS_CATALOG_BINDING, "카테고리 컬럼 바인딩이 카탈로그에 없다"),
    "discount_amount": (NEEDS_CATALOG_BINDING, "할인 금액 필드가 카탈로그에 선언되어 있지 않다"),
    "average_order_amount": (NEEDS_IR_EXTENSION, "비율 지표(합/건수)는 Aggregate 노드 하나로 표현되지 않는다"),
    "last_purchase_date": (NEEDS_IR_EXTENSION, "MAX(날짜) 집계 결과의 비교는 현재 대수에 없다"),
    "first_purchase_date": (NEEDS_IR_EXTENSION, "MIN(날짜) 집계 결과의 비교는 현재 대수에 없다"),
}

# 이번 웨이브가 변환하지 않는 슬롯과 그 사유. 표에 있으면 **왜** 막혔는지 말할 수 있고,
# 표에 없으면 그 사실 자체가 결정 대기(LEGACY_PATH_UNCLASSIFIED)다.
BLOCKED_SLOTS: dict[str, tuple[str, str, str]] = {
    # (분류, 코드, 사유)
    "recent_login": (
        NEEDS_CATALOG_BINDING, "BINDING_VALIDITY_GUARD_MISSING",
        "legacy 술어는 LAST_LOGIN_DATE 의 8자리 유효성 가드(LEN=8)를 함께 걸지만 카탈로그 login 소스에는 "
        "그 가드 선언이 없다 — 가드 없이 변환하면 형식이 깨진 값이 사전식 비교로 통과해 대상이 넓어진다",
    ),
    "inactivity_period": (
        NEEDS_DOMAIN_DECISION, "ABSENCE_NULL_AND_BOUNDARY_SEMANTICS",
        "legacy 미접속은 'LAST_LOGIN_DATE IS NOT NULL AND <= 컷오프'(한 번도 접속 안 한 회원 제외, 경계일 포함)"
        "이고 Event IR 부재는 NOT(접속이 컷오프 이후에 있음)(NULL 포함, 경계일 제외)이다 — 둘은 다른 집합이라"
        " 업무 결정 없이 어느 쪽으로도 바꿀 수 없다",
    ),
    "signup_target": (
        NEEDS_DOMAIN_DECISION, "ANCHOR_POLICY_NOT_DECLARED",
        "가입 창의 기준일(anchor)이 설정값(getdate/data_max)이라 IR 창 하나로 확정되지 않는다",
    ),
    "birthday_target": (NEEDS_IR_EXTENSION, "MMDD_COMPARISON_NOT_EXPRESSIBLE",
                        "생일 타겟은 연도를 뺀 월일(MMDD) 비교라 현재 시간 대수에 표현 노드가 없다"),
    "cart_retention": (NEEDS_CATALOG_BINDING, "CART_RETENTION_BINDING",
                       "장바구니 보관 기간은 UPD_DT 기준 창이고 그 시간 바인딩이 카탈로그에 선언되어 있지 않다"),
    "cart_type": (NEEDS_CATALOG_BINDING, "CART_TYPE_FIELD_MISSING",
                  "CART_TYPE_CD 필드가 오디언스 카탈로그에 선언되어 있지 않다"),
    "cart_absence": (NEEDS_CATALOG_BINDING, "CART_ABSENCE_BINDING",
                     "장바구니 부재 조건의 카탈로그 바인딩이 이번 웨이브에 확인되지 않았다"),
    "cart_aggregate": (NEEDS_CATALOG_BINDING, "CART_AGGREGATE_METRIC_MISSING",
                       "장바구니 집계 지표가 카탈로그 metrics 에 없다"),
    "purchase_object": (NEEDS_CATALOG_BINDING, "PRODUCT_TEXT_MATCH_BINDING",
                        "상품 자유텍스트 매칭은 상품 마스터 조인·LIKE 바인딩이 필요하고 카탈로그에 없다"),
    "metric_trend": (NEEDS_IR_EXTENSION, "PERIOD_OVER_PERIOD_NOT_EXPRESSIBLE",
                     "두 기간 집계의 증감 비교는 현재 대수에 관계 노드가 없다"),
    "member_metric_ranking": (NEEDS_IR_EXTENSION, "RANKED_SET_LOWERING_MISSING",
                              "회원 지표 랭킹의 lowering 이 슬롯 경로에는 구현되어 있지 않다"),
    "member_metric_selection": (NEEDS_IR_EXTENSION, "RANKED_SET_LOWERING_MISSING",
                                "상위 N/N% 선택은 순위 집합 노드가 필요하다"),
    "entity_set_condition": (NEEDS_IR_EXTENSION, "ENTITY_SET_LOWERING_MISSING",
                             "파생 엔터티 집합(순위 서브쿼리) 변환은 이번 웨이브 범위 밖이다"),
    "purchase_count_ranking": (NEEDS_IR_EXTENSION, "RANKED_SET_LOWERING_MISSING",
                               "구매 건수 랭킹은 순위 집합 노드가 필요하다"),
    "region_density_target": (NEEDS_IR_EXTENSION, "GROUP_RANKING_NOT_EXPRESSIBLE",
                              "지역 밀집 랭킹은 그룹 랭킹 노드가 필요하다"),
    "group_ranking_target": (NEEDS_IR_EXTENSION, "GROUP_RANKING_NOT_EXPRESSIBLE",
                             "그룹별 상위 N 은 PARTITION 윈도 노드가 필요하다"),
    "relational_operation": (NEEDS_DOMAIN_DECISION, "ATTRIBUTE_HISTORY_SLOT_ROUTE",
                             "속성 이력 조건은 의미 노드 경로(semantic_plan)가 이미 lowering 을 갖고 있어 "
                             "슬롯에서 다시 변환하면 두 생산자가 생긴다 — 어느 경로가 소유할지 결정이 필요하다"),
    "balance_conditions": (NEEDS_CATALOG_BINDING, "PROFILE_METRIC_NOT_IN_CATALOG",
                           "회원 프로필 수치 지표가 오디언스 카탈로그 metrics 에 없다"),
    "profile_date_conditions": (NEEDS_CATALOG_BINDING, "PROFILE_DATE_STATE_NOT_IN_CATALOG",
                                "프로필 날짜 상태 어휘가 카탈로그에 선언되어 있지 않다"),
    "campaign_responses": (NEEDS_DOMAIN_DECISION, "CAMPAIGN_RESPONSE_SLOT_ROUTE",
                           "캠페인 반응은 canonical 경로가 이미 Event IR 로 직접 생산한다 — 슬롯 변환은 이중 생산자다"),
    "campaign_response_frequency": (NEEDS_DOMAIN_DECISION, "CAMPAIGN_RESPONSE_SLOT_ROUTE",
                                    "캠페인 반응 횟수는 canonical 경로가 이미 Event IR 로 직접 생산한다"),
    "campaign_buy_amount": (NEEDS_DOMAIN_DECISION, "CAMPAIGN_RESPONSE_SLOT_ROUTE",
                            "캠페인 귀속 금액은 canonical 경로가 이미 Event IR 로 직접 생산한다"),
    "campaign_buy_count": (NEEDS_DOMAIN_DECISION, "CAMPAIGN_RESPONSE_SLOT_ROUTE",
                           "캠페인 귀속 건수는 canonical 경로가 이미 Event IR 로 직접 생산한다"),
    "cell_rate_target": (NEEDS_IR_EXTENSION, "CELL_RATE_NOT_EXPRESSIBLE",
                         "셀 단위 비율 조건은 셀 grain 집계 + 비율 비교라 현재 대수에 없다"),
    "price_sensitivity": (UNSUPPORTED, "NO_PHYSICAL_SOURCE",
                          "가격 민감도는 실DB 원천이 없다"),
    "interests": (UNSUPPORTED, "NO_PHYSICAL_SOURCE", "관심사는 실DB 원천이 없다"),
    "preferred_channels": (UNSUPPORTED, "NO_PHYSICAL_SOURCE", "선호 채널은 실DB 원천이 없다"),
    "lifecycle": (NEEDS_CATALOG_BINDING, "LIFECYCLE_VALUE_DOMAIN_MISSING",
                  "생애주기 값 도메인이 카탈로그 subject 필드로 선언되어 있지 않다"),
    "gender": (NEEDS_CATALOG_BINDING, "MEMBER_ATTRIBUTE_SLOT_ROUTE",
               "회원 속성은 subject.* 필드로 표현되지만 슬롯 값 어휘(male/female)와 canonical 값 도메인의 "
               "대응이 이번 웨이브에서 검증되지 않았다"),
    "age_min": (NEEDS_CATALOG_BINDING, "MEMBER_ATTRIBUTE_SLOT_ROUTE", "연령 하한 슬롯의 값 대응 미검증"),
    "age_max": (NEEDS_CATALOG_BINDING, "MEMBER_ATTRIBUTE_SLOT_ROUTE", "연령 상한 슬롯의 값 대응 미검증"),
    "age_exclude_ranges": (NEEDS_IR_EXTENSION, "RANGE_EXCLUSION_NOT_EXPRESSIBLE",
                           "연령 제외 구간 목록은 구간 차집합이라 현재 대수 조합이 검증되지 않았다"),
    "purchase_objects": (NEEDS_CATALOG_BINDING, "PRODUCT_TEXT_MATCH_BINDING",
                         "상품 자유텍스트 매칭 바인딩이 카탈로그에 없다"),
    "purchase_object_kind": (NEEDS_CATALOG_BINDING, "PRODUCT_TEXT_MATCH_BINDING",
                             "상품 매칭 종류 표식은 상품 바인딩과 함께만 뜻을 갖는다"),
}

# 행동 어휘 중 이번 웨이브가 변환하는 것. 나머지는 미매핑이다.
CONVERTIBLE_BEHAVIORS: frozenset[str] = frozenset({"no_purchase"})


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value > 0 else None


def _rolling_days(days: int) -> event_ir.RollingWindow:
    """롤링 창은 **일 단위로 정규화**한다.

    legacy 창 슬롯은 표면 쌍(value/unit)과 확정 일수(min_days)를 함께 들고 있고, 실행 술어는
    일수만 쓴다(``char8_cutoff(min_days)``). 그래서 '3개월'과 '90일'은 legacy 에서 이미 같은
    조건이다 — 지문이 둘을 다르게 보면 같은 뜻이 두 지문을 갖게 된다.
    """
    return event_ir.RollingWindow(value=days, unit="day")


def _purchase_relation(window: event_ir.TimeWindow | None) -> event_ir.Relation:
    return event_ir.event_relation(PURCHASE_SOURCE, window)


@dataclass
class _SlotOutcome:
    condition: event_ir.Condition | None = None
    receipts: list[MigrationReceipt] = field(default_factory=list)
    errors: list[MigrationError] = field(default_factory=list)
    ignored: list[IgnoredPath] = field(default_factory=list)
    consumed: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    # (원자 조건, 그 원자를 만든 원문 경로들). 노드 id 부여 전이며 provenance 간선의 오른쪽이 된다.
    # 슬롯 단위가 아니라 원자 단위로 들고 있어야 '집계 조건 3개 중 두 번째'의 출처가 보존된다.
    atoms: list[tuple[Any, tuple[str, ...]]] = field(default_factory=list)


def _convert_purchase_date(path: str, value: Any, context: MigrationContext) -> _SlotOutcome:
    """구매 발생 기간(절대 달력 창) → ``Exists(purchase in [start, end))`` (나열은 Or)."""
    outcome = _SlotOutcome()
    if not isinstance(value, Mapping):
        outcome.invalid.append(path)
        outcome.errors.append(MigrationError(
            "LEGACY_SLOT_SHAPE_INVALID", path, "purchase_date 슬롯은 객체여야 한다", INVALID_LEGACY_ASSET))
        return outcome

    raw_windows = value.get("windows")
    items: list[tuple[str, Mapping[str, Any]]] = []
    if isinstance(raw_windows, list) and raw_windows:
        for index, item in enumerate(raw_windows):
            if not isinstance(item, Mapping):
                outcome.invalid.append(f"{path}.windows[{index}]")
                outcome.errors.append(MigrationError(
                    "LEGACY_SLOT_SHAPE_INVALID", f"{path}.windows[{index}]",
                    "창 항목은 객체여야 한다", INVALID_LEGACY_ASSET))
                continue
            items.append((f"{path}.windows[{index}]", item))
    else:
        items.append((path, value))

    intervals: list[tuple[str, event_ir.AbsoluteInterval]] = []
    for item_path, item in items:
        for key in ("from_time", "to_time"):
            if str(item.get(key) or "").strip():
                outcome.unmapped.append(f"{item_path}.{key}")
                outcome.errors.append(MigrationError(
                    "TIME_OF_DAY_NOT_EXPRESSIBLE", f"{item_path}.{key}",
                    "시각(HHMMSS) 경계를 담을 시간 노드가 Event IR 에 없다", NEEDS_IR_EXTENSION))
        start, end = str(item.get("from") or ""), str(item.get("to") or "")
        if not (len(start) == 8 and start.isdigit() and len(end) == 8 and end.isdigit()):
            # legacy 실행기는 8자리가 아닌 토큰의 술어를 **아예 만들지 않는다**(조건이 조용히 사라진다).
            # 그것을 IR 구간으로 승격하면 대상 집합이 바뀐다 — 승인된 버그 수정이지 무손실 변환이 아니다.
            outcome.unmapped.append(f"{item_path}.from|to")
            outcome.errors.append(MigrationError(
                "DATE_TOKEN_NOT_EXECUTABLE", item_path,
                f"legacy 실행은 8자리 날짜 토큰만 술어로 만든다(받은 값: {start!r}~{end!r}) — "
                "변환하면 legacy 가 만들지 않던 조건이 생긴다", NEEDS_DOMAIN_DECISION))
            continue
        interval = event_ir.AbsoluteInterval.from_calendar_window({"from": start, "to": end})
        if interval is None:  # pragma: no cover — 위 검사를 통과하면 도달하지 않는다.
            outcome.invalid.append(item_path)
            outcome.errors.append(MigrationError(
                "LEGACY_SLOT_SHAPE_INVALID", item_path, "달력 창을 구간으로 읽지 못했다", INVALID_LEGACY_ASSET))
            continue
        intervals.append((item_path, interval))

    if not intervals:
        return outcome

    # 정렬·중복 제거: 같은 구간이 두 번 적힌 자산이 서로 다른 지문을 갖지 않게 한다.
    unique: dict[tuple[str, str], tuple[str, event_ir.AbsoluteInterval]] = {}
    for item_path, interval in intervals:
        unique.setdefault((interval.start.isoformat(), interval.end_exclusive.isoformat()), (item_path, interval))
    ordered = [unique[key] for key in sorted(unique)]

    operands = [event_ir.Exists(relation=_purchase_relation(interval)) for _p, interval in ordered]
    outcome.atoms.extend(
        (atom, (item_path,)) for atom, (item_path, _interval) in zip(operands, ordered)
    )
    outcome.condition = event_ir.disjunction(list(operands))
    outcome.consumed.extend(item_path for item_path, _ in ordered)
    label = value.get("label")
    if isinstance(label, str) and label.strip():
        outcome.ignored.append(IgnoredPath(
            f"{path}.label", "세그먼트 표시 라벨 — 대상 집합을 바꾸지 않는다(원문은 영수증에 보존)", "ui_label"))
    outcome.receipts.append(MigrationReceipt(
        source_paths=tuple(item_path for item_path, _ in ordered),
        transformation="purchase_date → Exists(purchase, AbsoluteInterval[)",
        evidence={
            "label": label if isinstance(label, str) else "",
            "windows": [interval.to_calendar_window() for _p, interval in ordered],
        },
        note="반개구간 [start, end_exclusive) 로 옮긴다 — legacy 문자열 BETWEEN 은 일 단위 컬럼에서 같은 집합이다",
    ))
    return outcome


def _convert_purchase_membership(path: str, value: Any, context: MigrationContext) -> _SlotOutcome:
    """구매 이력 존재 → ``Exists(purchase[, 최근 N일])``."""
    outcome = _SlotOutcome()
    if not isinstance(value, Mapping):
        outcome.invalid.append(path)
        outcome.errors.append(MigrationError(
            "LEGACY_SLOT_SHAPE_INVALID", path, "purchase_membership 슬롯은 객체여야 한다", INVALID_LEGACY_ASSET))
        return outcome
    operator = str(value.get("operator") or "").strip().casefold()
    if operator != "exists":
        outcome.invalid.append(f"{path}.operator")
        outcome.errors.append(MigrationError(
            "UNKNOWN_OPERATOR", f"{path}.operator",
            f"구매 존재 조건의 연산자는 'exists' 뿐이다(받은 값: {value.get('operator')!r})",
            INVALID_LEGACY_ASSET))
        return outcome
    raw_window = value.get("window_days")
    window: event_ir.TimeWindow | None = None
    if raw_window is not None:
        days = _positive_int(raw_window)
        if days is None:
            outcome.invalid.append(f"{path}.window_days")
            outcome.errors.append(MigrationError(
                "WINDOW_VALUE_INVALID", f"{path}.window_days",
                f"구매 존재 창은 양의 정수(일)여야 한다(받은 값: {raw_window!r})", INVALID_LEGACY_ASSET))
            return outcome
        window = _rolling_days(days)
    atom = event_ir.Exists(relation=_purchase_relation(window))
    outcome.atoms.append((atom, (path,)))
    outcome.condition = atom
    outcome.consumed.append(path)
    outcome.receipts.append(MigrationReceipt(
        source_paths=(path,),
        transformation="purchase_membership → Exists(purchase[, RollingWindow])",
        evidence={"operator": "exists", "window_days": _positive_int(raw_window)},
    ))
    return outcome


def _window_slot_days(path: str, value: Any, outcome: _SlotOutcome) -> int | None:
    """창 슬롯(``{value, unit, min_days}``)의 확정 일수. 표면 쌍과 어긋나면 원본을 무효로 본다."""
    if not isinstance(value, Mapping):
        outcome.invalid.append(path)
        outcome.errors.append(MigrationError(
            "LEGACY_SLOT_SHAPE_INVALID", path, "기간 슬롯은 객체여야 한다", INVALID_LEGACY_ASSET))
        return None
    days = _positive_int(value.get("min_days"))
    if days is None:
        outcome.invalid.append(f"{path}.min_days")
        outcome.errors.append(MigrationError(
            "WINDOW_VALUE_INVALID", f"{path}.min_days",
            f"확정 일수(min_days)가 양의 정수가 아니다(받은 값: {value.get('min_days')!r})",
            INVALID_LEGACY_ASSET))
        return None
    surface_value, surface_unit = _positive_int(value.get("value")), event_ir.canonical_unit(value.get("unit"))
    if surface_value is not None and surface_unit in event_ir.WINDOW_UNITS:
        expected = surface_value * event_ir.UNIT_DAYS[surface_unit]
        if expected != days:
            outcome.invalid.append(path)
            outcome.errors.append(MigrationError(
                "WINDOW_SURFACE_INCONSISTENT", path,
                f"표면 기간({surface_value} {surface_unit})과 확정 일수({days}일)가 어긋난다 — "
                "둘 중 어느 쪽이 사용자의 요청인지 원본만으로 판정할 수 없다", INVALID_LEGACY_ASSET))
            return None
    return days


def _convert_purchase_inactivity(path: str, value: Any, context: MigrationContext) -> _SlotOutcome:
    """최근 N일 미구매 → ``Not(Exists(purchase, 최근 N일))``(legacy anti-join 과 같은 집합)."""
    outcome = _SlotOutcome()
    days = _window_slot_days(path, value, outcome)
    if days is None:
        return outcome
    atom = event_ir.Exists(relation=_purchase_relation(_rolling_days(days)))
    outcome.atoms.append((atom, (path,)))
    outcome.condition = event_ir.Not(operand=atom)
    outcome.consumed.append(path)
    if isinstance(value, Mapping) and value.get("sql_interval"):
        outcome.ignored.append(IgnoredPath(
            f"{path}.sql_interval", "value/unit 의 렌더 파생값 — 조건이 아니다", "render_derivative"))
    outcome.receipts.append(MigrationReceipt(
        source_paths=(path,),
        transformation="purchase_inactivity → Not(Exists(purchase, RollingWindow))",
        evidence={"min_days": days},
        note="legacy 는 NOT EXISTS(주문 ≥ 컷오프), IR 은 NOT(EXISTS(...)) — 같은 집합이다",
    ))
    return outcome


def _convert_behaviors(path: str, value: Any, context: MigrationContext) -> _SlotOutcome:
    """행동 어휘 중 ``no_purchase``(평생 무구매)만 변환한다."""
    outcome = _SlotOutcome()
    if not isinstance(value, (list, tuple)):
        outcome.invalid.append(path)
        outcome.errors.append(MigrationError(
            "LEGACY_SLOT_SHAPE_INVALID", path, "behaviors 슬롯은 배열이어야 한다", INVALID_LEGACY_ASSET))
        return outcome
    consumed: list[str] = []
    for behavior in value:
        name = str(behavior)
        item_path = f"{path}[{name}]"
        if name in CONVERTIBLE_BEHAVIORS:
            consumed.append(item_path)
            continue
        outcome.unmapped.append(item_path)
        outcome.errors.append(MigrationError(
            "BEHAVIOR_NOT_IN_WAVE", item_path,
            f"행동 '{name}' 은 이번 웨이브의 변환 대상이 아니다", NEEDS_DOMAIN_DECISION))
    if not consumed:
        return outcome
    atom = event_ir.Exists(relation=_purchase_relation(None))
    outcome.atoms.append((atom, tuple(consumed)))
    outcome.condition = event_ir.Not(operand=atom)
    outcome.consumed.extend(consumed)
    outcome.receipts.append(MigrationReceipt(
        source_paths=tuple(consumed),
        transformation="behaviors:no_purchase → Not(Exists(purchase))",
        evidence={"behaviors": sorted(CONVERTIBLE_BEHAVIORS & {str(item) for item in value})},
    ))
    return outcome


_AGGREGATE_CORE_KEYS = frozenset({"metric_id", "operator", "threshold", "window_days", "window"})


def _convert_aggregate_conditions(path: str, value: Any, context: MigrationContext) -> _SlotOutcome:
    """집계 임계 → ``Comparison(Aggregate(...), Literal)``. 레시피는 **카탈로그**가 소유한다."""
    outcome = _SlotOutcome()
    if not isinstance(value, (list, tuple)):
        outcome.invalid.append(path)
        outcome.errors.append(MigrationError(
            "LEGACY_SLOT_SHAPE_INVALID", path, "aggregate_conditions 슬롯은 배열이어야 한다", INVALID_LEGACY_ASSET))
        return outcome
    operands: list[event_ir.Condition] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        condition = _convert_aggregate_item(item_path, item, context, outcome)
        if condition is not None:
            operands.append(condition)
    if operands:
        outcome.condition = event_ir.conjunction(operands)
    return outcome


def _convert_aggregate_item(
    path: str, item: Any, context: MigrationContext, outcome: _SlotOutcome
) -> event_ir.Condition | None:
    if not isinstance(item, Mapping):
        outcome.invalid.append(path)
        outcome.errors.append(MigrationError(
            "LEGACY_SLOT_SHAPE_INVALID", path, "집계 조건 항목은 객체여야 한다", INVALID_LEGACY_ASSET))
        return None

    scope = item.get("aggregation_scope")
    if isinstance(scope, str) and scope and scope != "per_member":
        outcome.unmapped.append(f"{path}.aggregation_scope")
        outcome.errors.append(MigrationError(
            "AGGREGATION_GRAIN_NOT_IN_WAVE", f"{path}.aggregation_scope",
            f"회원이 아닌 집계 grain('{scope}')은 카탈로그 grain 선언이 필요하다", NEEDS_IR_EXTENSION))
        return None
    if isinstance(item.get("scope"), Mapping) and item["scope"]:
        outcome.unmapped.append(f"{path}.scope")
        outcome.errors.append(MigrationError(
            "QUALIFIER_BINDING_MISSING", f"{path}.scope",
            "브랜드·카테고리 한정어의 물리 바인딩이 오디언스 카탈로그에 없다", NEEDS_CATALOG_BINDING))
        return None

    legacy_metric = str(item.get("metric_id") or "")
    blocked = AGGREGATE_METRIC_BLOCKED.get(legacy_metric)
    if blocked is not None:
        classification, reason = blocked
        outcome.unmapped.append(f"{path}.metric_id")
        outcome.errors.append(MigrationError(
            "AGGREGATE_METRIC_NOT_CONVERTIBLE", f"{path}.metric_id", reason, classification))
        return None
    canonical_metric = AGGREGATE_METRIC_MAP.get(legacy_metric)
    if canonical_metric is None:
        outcome.unmapped.append(f"{path}.metric_id")
        outcome.errors.append(MigrationError(
            "AGGREGATE_METRIC_UNKNOWN", f"{path}.metric_id",
            f"legacy 집계 지표 '{legacy_metric}' 의 canonical 대응이 선언되어 있지 않다", NEEDS_DOMAIN_DECISION))
        return None

    metric = _metric_spec(canonical_metric, path, outcome, context)
    if metric is None:
        return None

    operator = event_ir.canonical_comparison_operator(item.get("operator"))
    if operator is None:
        outcome.invalid.append(f"{path}.operator")
        outcome.errors.append(MigrationError(
            "UNKNOWN_OPERATOR", f"{path}.operator",
            f"알 수 없는 비교 연산자: {item.get('operator')!r}", INVALID_LEGACY_ASSET))
        return None
    if operator not in tuple(metric.allowed_operators):
        outcome.unmapped.append(f"{path}.operator")
        outcome.errors.append(MigrationError(
            "OPERATOR_NOT_ALLOWED", f"{path}.operator",
            f"지표 '{canonical_metric}' 는 연산자 {operator!r} 를 허용하지 않는다", NEEDS_CATALOG_BINDING))
        return None
    if operator not in LOSSLESS_AGGREGATE_OPERATORS:
        # legacy 집계는 **INNER JOIN + GROUP BY … HAVING** 이라 그 사건이 한 건도 없는 회원은 애초에
        # 집계 행을 갖지 않는다(조건을 평가받지 못하고 탈락). Event IR 은 스칼라 서브쿼리라 그 회원이
        # 0 으로 평가돼 '이하/미만'을 만족한다. 임계값이 양수인 '이상/초과'에서는 두 해석의 집합이
        # 같지만, '이하/미만'에서는 무주문 회원의 포함 여부가 통째로 갈린다 — 업무 결정 사안이다.
        outcome.unmapped.append(f"{path}.operator")
        outcome.errors.append(MigrationError(
            "AGGREGATE_ZERO_EVENT_MEMBER_SEMANTICS", f"{path}.operator",
            f"'{operator}' 임계는 사건이 0건인 회원의 포함 여부가 legacy(제외)와 Event IR(포함)에서 "
            "다르다 — 어느 쪽이 옳은지 업무 결정이 필요하다", NEEDS_DOMAIN_DECISION))
        return None

    threshold = item.get("threshold")
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
        outcome.invalid.append(f"{path}.threshold")
        outcome.errors.append(MigrationError(
            "THRESHOLD_TYPE_INVALID", f"{path}.threshold",
            f"임계값은 수치여야 한다(받은 값: {threshold!r})", INVALID_LEGACY_ASSET))
        return None
    if threshold <= 0:
        # 임계값이 0 이하면 '이상' 방향에서도 사건 0건 회원이 조건을 만족한다 — 위와 같은 갈림이다.
        outcome.unmapped.append(f"{path}.threshold")
        outcome.errors.append(MigrationError(
            "AGGREGATE_ZERO_EVENT_MEMBER_SEMANTICS", f"{path}.threshold",
            f"임계값 {threshold!r} 은 사건이 0건인 회원도 만족시킨다 — legacy 집계는 그 회원을 "
            "애초에 평가하지 않으므로 같은 집합이 아니다", NEEDS_DOMAIN_DECISION))
        return None

    window: event_ir.TimeWindow | None = None
    raw_window = item.get("window_days")
    if raw_window is not None:
        days = _positive_int(raw_window)
        if days is None:
            outcome.invalid.append(f"{path}.window_days")
            outcome.errors.append(MigrationError(
                "WINDOW_VALUE_INVALID", f"{path}.window_days",
                f"집계 창은 양의 정수(일)여야 한다(받은 값: {raw_window!r})", INVALID_LEGACY_ASSET))
            return None
        window = _rolling_days(days)
    elif isinstance(item.get("window"), Mapping) and item["window"]:
        # 정규화된 슬롯은 언제나 window_days 를 갖는다. 남아 있는 {value,unit} 은 정규화를 거치지
        # 않은 원본이라는 뜻이고, 여기서 다시 일수로 접으면 정규화기가 두 벌이 된다.
        outcome.invalid.append(f"{path}.window")
        outcome.errors.append(MigrationError(
            "WINDOW_NOT_NORMALIZED", f"{path}.window",
            "정규화되지 않은 창 표기({value,unit})다 — window_days 로 확정된 자산만 변환한다",
            INVALID_LEGACY_ASSET))
        return None

    relation = event_ir.event_relation(metric.source, window)
    aggregate = event_ir.Aggregate(
        function=str(metric.aggregate_function),
        relation=relation,
        expression=(event_ir.FieldRef(metric.expression_field) if metric.expression_field else None),
        distinct=bool(metric.distinct),
    )
    atom = event_ir.Comparison(operator=operator, left=aggregate, right=event_ir.Literal(threshold))
    outcome.atoms.append((atom, (path,)))
    outcome.consumed.append(path)
    if isinstance(item.get("label"), str) and item["label"].strip():
        outcome.ignored.append(IgnoredPath(
            f"{path}.label", "세그먼트 표시 라벨 — 대상 집합을 바꾸지 않는다", "ui_label"))
    for key in sorted(set(item) - _AGGREGATE_CORE_KEYS - NON_SEMANTIC_KEYS - {"aggregation_scope", "scope"}):
        outcome.unmapped.append(f"{path}.{key}")
        outcome.errors.append(MigrationError(
            "UNKNOWN_LEGACY_FIELD", f"{path}.{key}",
            f"집계 조건에 선언되지 않은 필드가 있다: {key!r}", NEEDS_DOMAIN_DECISION))
    outcome.receipts.append(MigrationReceipt(
        source_paths=(path,),
        transformation=f"aggregate_conditions → Comparison(Aggregate({canonical_metric}), Literal)",
        evidence={
            "legacy_metric_id": legacy_metric, "canonical_metric_id": canonical_metric,
            "operator": operator, "threshold": threshold,
            "window_days": _positive_int(raw_window),
            "label": item.get("label") if isinstance(item.get("label"), str) else "",
        },
        note="집계 함수·표현 필드·distinct 는 카탈로그 metric 선언에서 읽는다(어댑터가 정하지 않는다)",
    ))
    return atom


def _metric_spec(
    metric_id: str, path: str, outcome: _SlotOutcome, context: MigrationContext
) -> Any:
    """카탈로그 지표 스펙. A 그룹이 성립하려면 **회원 grain·필터 없는 aggregate** 여야 한다."""
    catalog = context.catalog
    if catalog is None:
        outcome.unmapped.append(path)
        outcome.errors.append(MigrationError(
            "CATALOG_NOT_INJECTED", path,
            "집계 변환에는 해소된 카탈로그가 필요하다(MigrationContext.catalog)", NEEDS_CATALOG_BINDING))
        return None
    try:
        metric = catalog.metric(metric_id)
    except Exception as exc:  # noqa: BLE001 — 카탈로그 예외 타입은 이 계층의 관심사가 아니다.
        outcome.unmapped.append(path)
        outcome.errors.append(MigrationError(
            "CATALOG_SYMBOL_UNRESOLVED", path,
            f"카탈로그에 지표 '{metric_id}' 가 없다: {exc}", NEEDS_CATALOG_BINDING))
        return None
    if metric.kind != "aggregate" or not metric.aggregate_function:
        outcome.unmapped.append(path)
        outcome.errors.append(MigrationError(
            "CATALOG_CONTRACT_MISMATCH", path,
            f"지표 '{metric_id}' 가 aggregate 가 아니다(kind={metric.kind!r})", NEEDS_CATALOG_BINDING))
        return None
    if metric.where is not None or metric.joins or metric.grain != "subject":
        # 고정 필터·조인·비회원 grain 이 붙은 지표는 legacy 슬롯 술어와 같은 집합이 아닐 수 있다.
        outcome.unmapped.append(path)
        outcome.errors.append(MigrationError(
            "CATALOG_RECIPE_NOT_IN_WAVE", path,
            f"지표 '{metric_id}' 의 레시피(필터/조인/grain)가 이번 웨이브의 무손실 대응 범위 밖이다",
            NEEDS_CATALOG_BINDING))
        return None
    return metric


# 변환기 등록표. **순서가 곧 결합 순서**다 — dict 순회 순서에 지문이 의존하지 않게 여기서 고정한다.
CONVERTERS: tuple[tuple[str, Callable[[str, Any, MigrationContext], _SlotOutcome]], ...] = (
    ("purchase_date", _convert_purchase_date),
    ("purchase_membership", _convert_purchase_membership),
    ("purchase_inactivity", _convert_purchase_inactivity),
    ("behaviors", _convert_behaviors),
    ("aggregate_conditions", _convert_aggregate_conditions),
)

CONVERTIBLE_SLOTS: frozenset[str] = frozenset(name for name, _ in CONVERTERS)


def legacy_slot_to_event_ir(
    legacy_plan: Mapping[str, Any], *, context: MigrationContext
) -> MigrationResult:
    """legacy 오디언스 슬롯 자산 하나를 canonical Event IR 로 변환한다(순수 함수).

    반환값이 실행 가능하다는 것은 ``expression`` 이 있다는 뜻이 아니다 —
    :attr:`MigrationResult.is_executable` 이 경로 회계까지 본 뒤에야 성립한다.
    """
    if not isinstance(legacy_plan, Mapping):
        raise TypeError("legacy_plan 은 매핑이어야 한다")

    consumed: list[str] = []
    ignored: list[IgnoredPath] = []
    unmapped: list[str] = []
    invalid: list[str] = []
    errors: list[MigrationError] = []
    receipts: list[MigrationReceipt] = []
    conditions: list[event_ir.Condition] = []
    provenance: list[ProvenanceEdge] = []
    atom_paths: list[tuple[Any, tuple[str, ...], str]] = []

    container = legacy_plan.get("target_user")
    container = container if isinstance(container, Mapping) else {}

    # ① 오디언스 컨테이너 밖의 조건 담기(exclude)는 이번 웨이브 범위 밖이다 — 빈 컨테이너만 통과.
    exclude = legacy_plan.get("exclude")
    if isinstance(exclude, Mapping):
        for key, value in sorted(exclude.items()):
            path = f"exclude.{key}"
            if migration_fingerprint.is_empty(value):
                ignored.append(IgnoredPath(path, "비어 있어 조건을 담고 있지 않다", "empty_slot"))
                continue
            unmapped.append(path)
            errors.append(MigrationError(
                "EXCLUDE_CONTAINER_NOT_IN_WAVE", path,
                "제외 컨테이너의 조건 변환은 이번 웨이브 범위 밖이다(Not 결합 규칙을 먼저 확정해야 한다)",
                NEEDS_DOMAIN_DECISION))

    # ② 등록된 변환기 — 선언 순서대로.
    for slot, convert in CONVERTERS:
        if slot not in container:
            continue
        value = container.get(slot)
        path = f"target_user.{slot}"
        if migration_fingerprint.is_empty(value):
            ignored.append(IgnoredPath(path, "비어 있어 조건을 담고 있지 않다", "empty_slot"))
            continue
        outcome = convert(path, value, context)
        consumed.extend(outcome.consumed)
        ignored.extend(outcome.ignored)
        unmapped.extend(outcome.unmapped)
        invalid.extend(outcome.invalid)
        errors.extend(outcome.errors)
        receipts.extend(outcome.receipts)
        atom_paths.extend((atom, paths, slot) for atom, paths in outcome.atoms)
        if outcome.condition is not None:
            conditions.append(outcome.condition)

    # ③ 나머지 키 — 전수 회계. 조용히 지나가는 길은 없다.
    for key in sorted(container):
        if key in CONVERTIBLE_SLOTS:
            continue
        path = f"target_user.{key}"
        value = container.get(key)
        if key in NON_SEMANTIC_KEYS:
            ignored.append(IgnoredPath(path, "비의미 metadata(표시·감사용)", "non_semantic_metadata"))
            continue
        if migration_fingerprint.is_empty(value):
            ignored.append(IgnoredPath(path, "비어 있어 조건을 담고 있지 않다", "empty_slot"))
            continue
        unmapped.append(path)
        declared = BLOCKED_SLOTS.get(key)
        if declared is None:
            errors.append(MigrationError(
                "LEGACY_PATH_UNCLASSIFIED", path,
                f"'{key}' 슬롯의 이행 분류가 선언되어 있지 않다 — 업무 결정이 필요하다",
                NEEDS_DOMAIN_DECISION))
            continue
        classification, code, reason = declared
        errors.append(MigrationError(code, path, reason, classification))

    expression = event_ir.conjunction(conditions) if conditions else None
    if expression is None and not errors:
        # 오디언스 조건이 하나도 없는 자산. '변환 완료'로 부르면 조건 없는 표현이 전체 회원을 뜻하게
        # 되고, Event IR 대수에는 그 뜻을 담는 노드가 없다(항진 노드를 만들지 않는 것이 설계다).
        errors.append(MigrationError(
            "NO_AUDIENCE_CONDITION", "target_user",
            "오디언스 조건이 없다 — '전체 회원'을 canonical 표현으로 어떻게 적을지는 업무 결정 사안이다",
            NEEDS_DOMAIN_DECISION))

    # ④ provenance — IR 노드 id 는 **결정론 위치 id** 다(식 안에서의 등장 순서). IR 자체는 id 를
    #    갖지 않으므로 매 실행 재계산되고, 그래서 의미 지문에 영향을 주지 않는다.
    if expression is not None:
        node_ids = {id(atom): f"atom-{index}" for index, atom in enumerate(event_ir.iter_atoms(expression))}
        grouped: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for atom, paths, slot in atom_paths:
            node_id = node_ids.get(id(atom))
            if node_id is None:
                continue
            grouped.setdefault((slot, paths), []).append(node_id)
        provenance = [
            ProvenanceEdge(source_paths=paths, target_node_ids=tuple(ids), transformation=slot)
            for (slot, paths), ids in grouped.items()
        ]

    classification = _worst_classification(errors)
    status = _STATUS_BY_CLASSIFICATION[classification]
    source_fingerprint = migration_fingerprint.compute_source_fingerprint(
        _audience_scope(legacy_plan), non_semantic_keys=NON_SEMANTIC_KEYS
    )
    schema_checksum = migration_fingerprint.compute_schema_checksum(
        _audience_scope(legacy_plan), non_semantic_keys=NON_SEMANTIC_KEYS
    )
    semantic = (
        migration_fingerprint.compute_semantic_fingerprint(expression.to_dict())
        if expression is not None else None
    )
    bindings = _bindings_for(expression, context) if expression is not None else {}
    binding_fingerprint = (
        migration_fingerprint.compute_binding_fingerprint(
            expression.to_dict(),
            catalog_version=context.catalog_version,
            compiler_version=context.compiler_version,
            bindings=bindings,
        )
        if expression is not None else None
    )
    return MigrationResult(
        expression=expression,
        receipts=tuple(receipts),
        consumed_paths=tuple(consumed),
        ignored_paths=tuple(ignored),
        unmapped_paths=tuple(unmapped),
        invalid_paths=tuple(invalid),
        fingerprint=semantic,
        status=status,
        errors=tuple(errors),
        classification=classification,
        provenance=tuple(provenance),
        source_fingerprint=source_fingerprint,
        source_schema_checksum=schema_checksum,
        binding_fingerprint=binding_fingerprint,
        bindings=bindings,
    )


def _audience_scope(legacy_plan: Mapping[str, Any]) -> dict[str, Any]:
    """지문 대상 — 오디언스 컨테이너만. 캠페인 제약·의도는 조건이 아니라 지문에 넣지 않는다."""
    return {
        name: legacy_plan.get(name)
        for name in AUDIENCE_CONTAINERS
        if isinstance(legacy_plan.get(name), Mapping)
    }


def _worst_classification(errors: Sequence[MigrationError]) -> str:
    if not errors:
        return CONVERTIBLE
    present = {error.classification for error in errors}
    for candidate in _SEVERITY:
        if candidate in present:
            return candidate
    return NEEDS_DOMAIN_DECISION


def _bindings_for(expression: event_ir.Condition, context: MigrationContext) -> dict[str, Any]:
    """이 식이 실제로 닿는 심볼의 물리 선언만 모은다(바인딩 지문 입력)."""
    catalog = context.catalog
    if catalog is None:
        return {}
    bindings: dict[str, Any] = {}
    for symbol in sorted(event_ir.sources(expression)):
        try:
            spec = catalog.source(symbol)
        except Exception:  # noqa: BLE001 — 미해결 심볼은 컴파일 단계가 판정한다.
            continue
        event = spec.event
        bindings[symbol] = {
            "table": event.table, "alias": event.alias,
            "subject_key": event.subject_key, "event_subject_key": event.event_subject_key,
            "time_column": event.time_column, "time_format": event.time_format,
            "binding": event.binding, "extra_predicates": list(event.extra_predicates),
        }
    for symbol in sorted(event_ir.field_names(expression)):
        try:
            spec = catalog.field(symbol)
        except Exception:  # noqa: BLE001
            continue
        bindings[symbol] = {
            "column": spec.compiler_field.column,
            "data_type": spec.compiler_field.data_type,
            "expression": spec.compiler_field.expression,
        }
    return bindings


# ── 이행 metadata envelope ────────────────────────────────────────────────────────


def build_migration_envelope(
    result: MigrationResult,
    *,
    asset_id: str,
    asset_revision: int,
    context: MigrationContext,
    converted_at: str,
) -> dict[str, Any]:
    """저장용 envelope. **기존 ``event_expression`` 페이로드와 같은 자리에 얹히는 형태**다.

    기존 소비자는 ``expression``/``source``/``receipts`` 세 키만 읽는다. 이행 metadata 를 최상위에
    흩뿌리지 않고 ``migration`` 하나로 모으면, 예전 스키마 역직렬화가 그대로 통과한다.
    """
    return {
        "schema_version": ENVELOPE_SCHEMA_VERSION,
        "expression": result.expression.to_dict() if result.expression is not None else None,
        # 실행 권위는 이 표식이 아니라 plan.audience_authority 가 갖는다. 이행 변환물이 ingress
        # 표식(audience_requirement)을 달지 않는 것이 dual-storage ≠ dual-execution 의 물리적 실체다.
        "source": MIGRATION_SOURCE,
        "receipts": [receipt.to_dict() for receipt in result.receipts],
        "migration": {
            "adapter_version": context.adapter_version,
            "catalog_version": context.catalog_version,
            "compiler_version": context.compiler_version,
            "source_asset_id": asset_id,
            "source_revision": asset_revision,
            "source_fingerprint": result.source_fingerprint,
            "semantic_fingerprint": result.fingerprint,
            "binding_fingerprint": result.binding_fingerprint,
            "source_schema_checksum": result.source_schema_checksum,
            "classification": result.classification,
            "migration_status": result.status.value,
            "is_executable": result.is_executable,
            "as_of": context.as_of.isoformat(),
            "timezone": context.timezone,
            "converted_at": converted_at,
            "validated_at": None,
            "consumed_paths": list(result.consumed_paths),
            "ignored_paths": [item.to_dict() for item in result.ignored_paths],
            "unmapped_paths": list(result.unmapped_paths),
            "invalid_paths": list(result.invalid_paths),
            "provenance": [item.to_dict() for item in result.provenance],
            "errors": [item.to_dict() for item in result.errors],
        },
    }


@dataclass(frozen=True)
class MigrationManifestItem:
    asset_id: str
    asset_revision: int
    slot_types: tuple[str, ...]
    classification: str
    source_fingerprint: str
    migration_status: str
    reason_codes: tuple[str, ...]
    semantic_fingerprint: str | None = None
    source_schema_checksum: str = ""
    unmapped_paths: tuple[str, ...] = ()
    invalid_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "asset_revision": self.asset_revision,
            "slot_types": list(self.slot_types),
            "classification": self.classification,
            "source_fingerprint": self.source_fingerprint,
            "semantic_fingerprint": self.semantic_fingerprint,
            "source_schema_checksum": self.source_schema_checksum,
            "migration_status": self.migration_status,
            "reason_codes": list(self.reason_codes),
            "unmapped_paths": list(self.unmapped_paths),
            "invalid_paths": list(self.invalid_paths),
        }


def audience_slot_types(legacy_plan: Mapping[str, Any]) -> tuple[str, ...]:
    """이 자산이 실제로 값을 담고 있는 오디언스 슬롯 이름(빈 슬롯 제외)."""
    names: list[str] = []
    for container in AUDIENCE_CONTAINERS:
        values = legacy_plan.get(container)
        if not isinstance(values, Mapping):
            continue
        for key, value in values.items():
            if migration_fingerprint.is_empty(value) or str(key) in NON_SEMANTIC_KEYS:
                continue
            names.append(f"{container}.{key}")
    return tuple(sorted(names))


def manifest_item(
    asset_id: str, asset_revision: int, legacy_plan: Mapping[str, Any], result: MigrationResult
) -> MigrationManifestItem:
    return MigrationManifestItem(
        asset_id=asset_id,
        asset_revision=asset_revision,
        slot_types=audience_slot_types(legacy_plan),
        classification=result.classification,
        source_fingerprint=result.source_fingerprint,
        migration_status=result.status.value,
        reason_codes=tuple(sorted({error.code for error in result.errors})),
        semantic_fingerprint=result.fingerprint,
        source_schema_checksum=result.source_schema_checksum,
        unmapped_paths=result.unmapped_paths,
        invalid_paths=result.invalid_paths,
    )


def stale_against(stored_source_fingerprint: str, result: MigrationResult) -> bool:
    """저장된 변환이 현재 원본과 어긋나는가(cut-over 차단 조건 ①)."""
    return bool(stored_source_fingerprint) and stored_source_fingerprint != result.source_fingerprint


def migration_status_for(result: MigrationResult, *, stored_source_fingerprint: str = "") -> MigrationStatus:
    """저장 상태까지 반영한 이 자산의 상태(원본이 바뀌었으면 무조건 STALE)."""
    if stale_against(stored_source_fingerprint, result):
        return MigrationStatus.STALE
    return result.status


__all__ = [
    "ADAPTER_VERSION",
    "AGGREGATE_METRIC_BLOCKED",
    "AGGREGATE_METRIC_MAP",
    "AUDIENCE_CONTAINERS",
    "BLOCKED_SLOTS",
    "CLASSIFICATIONS",
    "CONVERTERS",
    "CONVERTIBLE",
    "CONVERTIBLE_BEHAVIORS",
    "CONVERTIBLE_SLOTS",
    "ENVELOPE_SCHEMA_VERSION",
    "INVALID_LEGACY_ASSET",
    "LOSSLESS_AGGREGATE_OPERATORS",
    "IgnoredPath",
    "MIGRATION_SOURCE",
    "MigrationContext",
    "MigrationContextError",
    "MigrationError",
    "MigrationManifestItem",
    "MigrationReceipt",
    "MigrationResult",
    "NEEDS_CATALOG_BINDING",
    "NEEDS_DOMAIN_DECISION",
    "NEEDS_IR_EXTENSION",
    "NON_SEMANTIC_KEYS",
    "ProvenanceEdge",
    "UNSUPPORTED",
    "audience_slot_types",
    "build_migration_envelope",
    "legacy_slot_to_event_ir",
    "manifest_item",
    "migration_status_for",
    "stale_against",
]
