"""실행 가능한 capability 레지스트리 — base×qualifier 지원 여부를 **정적 플래그가 아니라 실제 실행 자산과
연결된 정의**로 관리한다(요청 2·6·7).

지금까지 requirement_capabilities.json 은 `{supported: bool, message, compiler_strategy}` 뿐이고,
compiler_strategy 는 어디서도 dispatch 되지 않는 죽은 라벨이었다(그래서 supported=true 로 바꿔도 실제
필터가 SQL 에 안 들어가는 사일런트 실패가 났다). 이 모듈은 각 capability 를 RequirementCapability 로
로드하고, 다음을 근거로 **상태를 산출**한다:

    supported 선언 + compiler_strategy 등록됨(compiler_strategies) + join_path 존재(join_paths) +
    filter_field 가 스키마(schema_catalog)에 존재  ⇒  status="supported"

하나라도 어긋나면 supported 를 신뢰하지 않고 not_implemented/no_join_path/missing_field/
compilation_unavailable 로 강등한다. 정책적으로 막은 조합은 policy.disabled 로 별도 표시(status=
disabled_by_policy) 해 '기술 미구현'과 '정책 비활성'을 혼동하지 않는다.

validate_capabilities() 는 애플리케이션 시작/CI 에서 전체 정의를 정적 검증한다(요청 7): supported=true
인데 compiler/join/field 가 없으면 배포 이전에 실패한다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import compiler_strategies
from join_paths import JOIN_PATHS

DEFAULT_CAPABILITIES_PATH = Path("docs/data/requirement_capabilities.json")
DEFAULT_SCHEMA_CATALOG_PATH = Path("docs/data/schema_catalog.json")


# 상태 taxonomy(요청 6) — '기술 미구현/스키마 없음/정책 비활성/입력 부족/검증 실패'를 혼용하지 않는다.
STATUS_SUPPORTED = "supported"                       # 실제 컴파일 가능
STATUS_NOT_IMPLEMENTED = "not_implemented"           # 아직 compiler 없음
STATUS_NO_JOIN_PATH = "no_join_path"                 # 조인 경로 미등록
STATUS_MISSING_FIELD = "missing_filter_field"        # filter_field 가 스키마에 없음
STATUS_COMPILATION_UNAVAILABLE = "compilation_unavailable"  # strategy 라벨이 dispatch 미등록
STATUS_DISABLED_BY_POLICY = "disabled_by_policy"     # 기술적으로 가능해도 정책상 막음

SUPPORTED_STATUSES = frozenset({STATUS_SUPPORTED})


class CapabilityDefinitionError(ValueError):
    """capability 정의가 스키마/실행자산 계약을 위반했을 때(정적 검증 실패)."""


@dataclass(frozen=True)
class RequirementCapability:
    base: str
    qualifier: str
    supported_flag: bool                     # JSON 의 선언값(원문). 최종 상태는 resolve 로 산출.
    message: str | None = None
    compiler_strategy: str | None = None
    join_path: str | None = None
    filter_field: str | None = None          # 이름 매칭 컬럼(예: BRAND_NAME)
    code_field: str | None = None            # 코드 매칭 컬럼(하이브리드, 예: BRAND_ID)
    value_type: str = "string"
    supports_multiple: bool = True
    policy_disabled: bool = False
    policy_reason: str | None = None

    def resolve_status(self, schema_fields: "SchemaFields") -> str:
        """실제 실행 자산 존재 여부로 상태를 산출한다(선언 supported 를 맹신하지 않음)."""
        if self.policy_disabled:
            return STATUS_DISABLED_BY_POLICY
        if not self.supported_flag:
            return STATUS_NOT_IMPLEMENTED
        if not compiler_strategies.has_strategy(self.compiler_strategy):
            return STATUS_COMPILATION_UNAVAILABLE
        if not compiler_strategies.join_path_exists(self.join_path):
            return STATUS_NO_JOIN_PATH
        target_table = JOIN_PATHS[self.join_path].target_table if self.join_path else None
        for col in (self.filter_field, self.code_field):
            if col and target_table and not schema_fields.has(target_table, col):
                return STATUS_MISSING_FIELD
        return STATUS_SUPPORTED

    def is_supported(self, schema_fields: "SchemaFields") -> bool:
        return self.resolve_status(schema_fields) in SUPPORTED_STATUSES


# ── 스키마 필드 조회(컬럼 이름만 보고 추론 금지 — 실제 카탈로그로 확인) ────────────────────────
@dataclass
class SchemaFields:
    tables: dict[str, frozenset[str]]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_SCHEMA_CATALOG_PATH) -> "SchemaFields":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CapabilityDefinitionError(f"[{Path(path).name}] 스키마 카탈로그 로드 실패: {exc}") from exc
        tables_node = payload.get("tables") if isinstance(payload, dict) else None
        tables: dict[str, frozenset[str]] = {}
        if isinstance(tables_node, dict):
            for tname, tspec in tables_node.items():
                cols = tspec.get("columns") if isinstance(tspec, dict) else None
                names = {c.get("name") for c in cols if isinstance(c, dict) and c.get("name")} if isinstance(cols, list) else set()
                tables[tname] = frozenset(names)
        elif isinstance(tables_node, list):  # 방어: 리스트 형태 카탈로그
            for tspec in tables_node:
                if isinstance(tspec, dict) and tspec.get("table_name"):
                    cols = tspec.get("columns") or []
                    tables[tspec["table_name"]] = frozenset(
                        c.get("name") for c in cols if isinstance(c, dict) and c.get("name")
                    )
        return cls(tables=tables)

    def has(self, table: str, column: str) -> bool:
        cols = self.tables.get(table)
        # 카탈로그에 테이블이 아예 없으면(부분 스냅샷) 보수적으로 '있음'으로 봐 오탐 차단은 validator 계약에 맡긴다.
        return True if cols is None else column in cols


# ── 레지스트리 ──────────────────────────────────────────────────────────────────────────────
@dataclass
class CapabilityRegistry:
    capabilities: dict[tuple[str, str], RequirementCapability]
    default_qualifiers: dict[str, RequirementCapability]

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CAPABILITIES_PATH) -> "CapabilityRegistry":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CapabilityDefinitionError(f"[{Path(path).name}] 읽기/파싱 실패: {exc}") from exc
        caps_node = payload.get("capabilities") if isinstance(payload, dict) else None
        if not isinstance(caps_node, dict) or not caps_node:
            raise CapabilityDefinitionError(f"[{Path(path).name}] 'capabilities' 객체 필요")
        capabilities: dict[tuple[str, str], RequirementCapability] = {}
        default_qualifiers: dict[str, RequirementCapability] = {}
        for base_name, base_spec in caps_node.items():
            qualifiers = (base_spec or {}).get("qualifiers") if isinstance(base_spec, dict) else None
            if not isinstance(qualifiers, dict):
                raise CapabilityDefinitionError(f"[{base_name}] qualifiers 객체 필요")
            for q_domain, q_spec in qualifiers.items():
                cap = cls._parse(base_name, q_domain, q_spec)
                if base_name == "_default":
                    default_qualifiers[q_domain] = cap
                else:
                    capabilities[(base_name, q_domain)] = cap
        return cls(capabilities=capabilities, default_qualifiers=default_qualifiers)

    @staticmethod
    def _parse(base: str, qualifier: str, spec: Any) -> RequirementCapability:
        if not isinstance(spec, dict) or not isinstance(spec.get("supported"), bool):
            raise CapabilityDefinitionError(f"[{base}.{qualifier}] supported(bool) 필수")
        policy = spec.get("policy") if isinstance(spec.get("policy"), dict) else {}
        policy_disabled = bool(policy.get("disabled"))
        supported = bool(spec.get("supported"))
        message = spec.get("message") if isinstance(spec.get("message"), str) else None
        # 미지원/정책차단이면 안내 메시지가 있어야 조용한 차단이 안 된다.
        if (not supported or policy_disabled) and not (message and message.strip()):
            raise CapabilityDefinitionError(f"[{base}.{qualifier}] 미지원/정책차단이면 message 필수")
        return RequirementCapability(
            base=base, qualifier=qualifier, supported_flag=supported, message=message,
            compiler_strategy=spec.get("compiler_strategy"),
            join_path=spec.get("join_path"),
            filter_field=spec.get("filter_field"),
            code_field=spec.get("code_field"),
            value_type=spec.get("value_type", "string"),
            supports_multiple=bool(spec.get("supports_multiple", True)),
            policy_disabled=policy_disabled,
            policy_reason=policy.get("reason") if isinstance(policy, dict) else None,
        )

    def get(self, base: str, qualifier: str) -> RequirementCapability | None:
        cap = self.capabilities.get((base, qualifier))
        if cap is not None:
            return cap
        return self.default_qualifiers.get(qualifier)


# ── 정적 검증(요청 7): 시작/CI 에서 전체 capability 정의를 실행자산과 대조 ──────────────────────
def validate_capabilities(
    registry: CapabilityRegistry | None = None,
    schema_fields: SchemaFields | None = None,
) -> list[str]:
    """capability 정의 전체를 정적 검증한다. 위반 문자열 리스트를 반환(빈 리스트 = 정상).

    검사: supported=true 인데 (1) compiler_strategy 미등록, (2) join_path 미등록, (3) filter_field/
    code_field 가 스키마에 없음, (4) 하이브리드인데 code_field 만 있고 supports_multiple 불일치 등.
    supported=true 인데 상태가 supported 로 산출되지 않으면(=선언과 구현 불일치) 오류로 승격한다."""
    registry = registry or CapabilityRegistry.load()
    schema_fields = schema_fields or SchemaFields.load()
    errors: list[str] = []
    for cap in registry.capabilities.values():
        loc = f"base={cap.base} qualifier={cap.qualifier}"
        if cap.policy_disabled and cap.supported_flag:
            errors.append(f"Invalid capability: {loc} reason=policy.disabled 인데 supported=true (모순)")
            continue
        if not cap.supported_flag:
            continue  # 미지원은 message 만 있으면 됨(로더가 강제)
        if not compiler_strategies.has_strategy(cap.compiler_strategy):
            errors.append(f"Invalid capability: {loc} reason=compiler strategy '{cap.compiler_strategy}' is not registered")
        if not compiler_strategies.join_path_exists(cap.join_path):
            errors.append(f"Invalid capability: {loc} reason=join_path '{cap.join_path}' is not registered")
        status = cap.resolve_status(schema_fields)
        if status == STATUS_MISSING_FIELD:
            errors.append(f"Invalid capability: {loc} reason=filter_field/code_field not in schema for join target")
        elif status != STATUS_SUPPORTED and compiler_strategies.has_strategy(cap.compiler_strategy) \
                and compiler_strategies.join_path_exists(cap.join_path):
            errors.append(f"Invalid capability: {loc} reason=supported=true but resolved status={status}")
    return errors
