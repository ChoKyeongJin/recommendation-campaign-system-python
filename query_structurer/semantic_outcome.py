"""Typed application outcome projected to the legacy ``semantic_ir`` JSON shape."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Literal, Mapping, get_args


SemanticStatus = Literal[
    "resolved",
    "policy_applied",
    "needs_clarification",
    "unsupported",
]
FailureKind = Literal[
    "user_clarification",
    "structurer_failure",
    "system_failure",
    "unsupported",
]
# 실패 **사유**의 명시 선언(닫힌 어휘). 보통 사유는 failure_kind 에서 파생되지만
# (`failure_messages.semantic_failure_reason`), 한 kind 안에 서로 다른 고칠 곳이 섞이면
# 파생만으로는 구별할 수 없다:
#
#   semantic_registry_gap      실행 자산/레지스트리가 그 축을 낼 수 없다 → 설정·컴파일러 작업
#   semantic_emission_failure  자산도 컴파일러도 있는데 구조화기가 표현을 못 냈다 → 방출 품질
#
# 둘 다 운영상 system_failure(사용자가 답할 수 없는 실패)지만 고칠 사람이 다르다. 파생 사유를
# 그대로 쓰면 후자가 '레지스트리 구멍'으로 보고돼 없는 결함을 찾게 된다.
FailureReason = Literal["semantic_emission_failure"]
FAILURE_REASON_EMISSION: FailureReason = "semantic_emission_failure"
SEMANTIC_STATUSES = frozenset(get_args(SemanticStatus))
FAILURE_KINDS = frozenset(get_args(FailureKind))
FAILURE_REASONS = frozenset(get_args(FailureReason))


@dataclass(frozen=True, slots=True)
class WireValueSpec:
    """One recursively validated JSON value declaration."""

    json_type: Literal["string", "array", "object"]
    nullable: bool = False
    enum: tuple[Any, ...] = ()
    non_blank: bool = False
    min_items: int = 0
    items: WireValueSpec | ClosedObjectSpec | None = None
    object_spec: ClosedObjectSpec | None = None

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": [self.json_type, "null"] if self.nullable else self.json_type,
        }
        if self.enum:
            schema["enum"] = [*self.enum, None] if self.nullable else list(self.enum)
        if self.non_blank:
            schema["minLength"] = 1
            schema["pattern"] = r".*\S.*"
        if self.json_type == "array" and self.items is not None:
            schema["items"] = self.items.json_schema()
            if self.min_items:
                schema["minItems"] = self.min_items
        if self.json_type == "object" and self.object_spec is not None:
            return self.object_spec.json_schema()
        return schema

    def parse(self, value: Any, path: str) -> Any:
        if value is None and self.nullable:
            return None
        expected = {
            "string": str,
            "array": list,
            "object": Mapping,
        }[self.json_type]
        if not isinstance(value, expected):
            raise ValueError(f"{path} must be {self.json_type}")
        if self.enum and value not in self.enum:
            raise ValueError(f"{path} is invalid")
        if self.non_blank and isinstance(value, str) and not value.strip():
            raise ValueError(f"{path} must be non-blank")
        if isinstance(value, list):
            if len(value) < self.min_items:
                raise ValueError(f"{path} requires at least {self.min_items} item(s)")
            if self.items is not None:
                return [self.items.parse(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if isinstance(value, Mapping):
            if self.object_spec is not None:
                return self.object_spec.parse(value, path)
            return copy.deepcopy(dict(value))
        return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class WireFieldSpec:
    name: str
    value: WireValueSpec
    required_on_input: bool = True
    default: Any = None
    emit_default: bool = False


@dataclass(frozen=True, slots=True)
class ClosedObjectSpec:
    fields: tuple[WireFieldSpec, ...]
    closed: bool = True

    def json_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {
            "type": "object",
            "properties": {
                field.name: field.value.json_schema() for field in self.fields
            },
        }
        if self.closed:
            schema["additionalProperties"] = False
        required = [field.name for field in self.fields if field.required_on_input]
        if required:
            schema["required"] = required
        return schema

    def parse(self, value: Any, path: str = "semantic_ir") -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{path} must be object")
        declared = {field.name: field for field in self.fields}
        missing = [
            field.name
            for field in self.fields
            if field.required_on_input and field.name not in value
        ]
        if missing:
            raise ValueError(f"{path} is missing required fields: {', '.join(missing)}")
        unexpected = sorted(set(value) - set(declared))
        if self.closed and unexpected:
            raise ValueError(f"{path} has unsupported fields: {', '.join(unexpected)}")
        parsed: dict[str, Any] = {}
        for field in self.fields:
            if field.name in value:
                parsed[field.name] = field.value.parse(value[field.name], f"{path}.{field.name}")
            elif field.emit_default:
                parsed[field.name] = copy.deepcopy(field.default)
        return parsed


NON_BLANK_STRING = WireValueSpec("string", non_blank=True)
STRING = WireValueSpec("string")
ARBITRARY_OBJECT = WireValueSpec("object", object_spec=ClosedObjectSpec((), closed=False))

_BINDING_SPEC = ClosedObjectSpec((
    WireFieldSpec("role", WireValueSpec("string", enum=("baseline", "current", "threshold", "comparison"))),
    WireFieldSpec("literal_id", NON_BLANK_STRING),
))
_OPERATION_SPEC = ClosedObjectSpec((
    WireFieldSpec("kind", WireValueSpec("string", enum=("period_over_period_change",))),
    WireFieldSpec("metric_id", NON_BLANK_STRING),
    WireFieldSpec("direction", WireValueSpec("string", enum=("increase", "decrease"))),
    WireFieldSpec("bindings", WireValueSpec("array", items=_BINDING_SPEC)),
))
_POLICY_SPEC = ClosedObjectSpec((
    WireFieldSpec("policy_id", NON_BLANK_STRING),
    WireFieldSpec("fields", WireValueSpec("array", min_items=1, items=NON_BLANK_STRING)),
))
_UNSUPPORTED_SPEC = ClosedObjectSpec((
    WireFieldSpec("kind", NON_BLANK_STRING),
    WireFieldSpec("reason", NON_BLANK_STRING),
    WireFieldSpec("evidence", STRING),
))

SEMANTIC_OUTCOME_WIRE_SPEC = ClosedObjectSpec((
    WireFieldSpec("status", WireValueSpec("string", enum=tuple(sorted(SEMANTIC_STATUSES)))),
    WireFieldSpec("operations", WireValueSpec("array", items=_OPERATION_SPEC)),
    WireFieldSpec("missing_fields", WireValueSpec("array", items=NON_BLANK_STRING)),
    WireFieldSpec(
        "missing_field_causes",
        WireValueSpec("array", items=ARBITRARY_OBJECT),
        required_on_input=False,
        default=[],
        emit_default=True,
    ),
    WireFieldSpec(
        "failure_kind",
        WireValueSpec("string", nullable=True, enum=tuple(sorted(FAILURE_KINDS))),
        required_on_input=False,
        default=None,
        emit_default=True,
    ),
    # 파생 사유로는 구별되지 않는 실패에만 실린다. 기본값을 내보내지 않는 이유는 기존 투영이
    # 바이트 동일해야 하기 때문이다 — 이 키가 없으면 사유는 예전처럼 kind 에서 파생된다.
    WireFieldSpec(
        "failure_reason",
        WireValueSpec("string", nullable=True, enum=tuple(sorted(FAILURE_REASONS))),
        required_on_input=False,
        default=None,
        emit_default=False,
    ),
    WireFieldSpec("policy_applications", WireValueSpec("array", items=_POLICY_SPEC)),
    WireFieldSpec("unsupported_operations", WireValueSpec("array", items=_UNSUPPORTED_SPEC)),
    WireFieldSpec("message", WireValueSpec("string", nullable=True)),
))


def semantic_outcome_json_schema() -> dict[str, Any]:
    return SEMANTIC_OUTCOME_WIRE_SPEC.json_schema()


def parse_semantic_outcome_projection(value: Any) -> dict[str, Any]:
    return SEMANTIC_OUTCOME_WIRE_SPEC.parse(value)


def validate_semantic_outcome_state(
    *,
    status: SemanticStatus,
    missing_fields: tuple[str, ...] | list[str],
    missing_field_causes: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    policy_applications: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
    unsupported_operations: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> None:
    """Validate status-dependent fields independently from the wire shape."""

    if status == "needs_clarification":
        if not missing_fields:
            raise ValueError("needs_clarification requires missing_fields")
    elif missing_fields or missing_field_causes:
        raise ValueError(f"{status} cannot contain missing fields")

    if status == "policy_applied":
        if not policy_applications:
            raise ValueError("policy_applied requires policy_applications")
    elif policy_applications:
        raise ValueError(f"{status} cannot contain policy applications")

    if status == "unsupported":
        if not unsupported_operations:
            raise ValueError("unsupported requires unsupported_operations")
    elif unsupported_operations:
        raise ValueError(f"{status} cannot contain unsupported operations")


@dataclass(frozen=True, slots=True)
class SemanticOutcome:
    """Immutable top-level outcome; mutable JSON records are copied at both boundaries."""

    status: SemanticStatus
    missing_fields: tuple[str, ...] = ()
    missing_field_causes: tuple[Mapping[str, Any], ...] = ()
    failure_kind: FailureKind | None = None
    policy_applications: tuple[Mapping[str, Any], ...] = ()
    unsupported_operations: tuple[Mapping[str, Any], ...] = ()
    message: str | None = None
    failure_reason: FailureReason | None = None

    def __post_init__(self) -> None:
        if self.status not in SEMANTIC_STATUSES:
            raise ValueError(f"invalid SemanticOutcome status: {self.status!r}")
        if self.failure_kind is not None and self.failure_kind not in FAILURE_KINDS:
            raise ValueError(f"invalid SemanticOutcome failure_kind: {self.failure_kind!r}")
        if self.failure_reason is not None and self.failure_reason not in FAILURE_REASONS:
            raise ValueError(
                f"invalid SemanticOutcome failure_reason: {self.failure_reason!r}"
            )
        if self.message is not None and not isinstance(self.message, str):
            raise TypeError("SemanticOutcome message must be a string or None")

        missing_fields = tuple(self.missing_fields)
        if not all(isinstance(field, str) and field.strip() for field in missing_fields):
            raise ValueError("SemanticOutcome missing_fields must contain non-blank strings")
        if not all(isinstance(record, Mapping) for record in self.missing_field_causes):
            raise TypeError("SemanticOutcome missing_field_causes must contain mappings")
        missing_field_causes = tuple(
            copy.deepcopy(dict(record)) for record in self.missing_field_causes
        )
        policy_applications = tuple(
            _POLICY_SPEC.parse(record, f"SemanticOutcome.policy_applications[{index}]")
            for index, record in enumerate(self.policy_applications)
        )
        unsupported_operations = tuple(
            _UNSUPPORTED_SPEC.parse(record, f"SemanticOutcome.unsupported_operations[{index}]")
            for index, record in enumerate(self.unsupported_operations)
        )

        object.__setattr__(self, "missing_fields", missing_fields)
        object.__setattr__(self, "missing_field_causes", missing_field_causes)
        object.__setattr__(self, "policy_applications", policy_applications)
        object.__setattr__(self, "unsupported_operations", unsupported_operations)
        validate_semantic_outcome_state(
            status=self.status,
            missing_fields=missing_fields,
            missing_field_causes=missing_field_causes,
            policy_applications=policy_applications,
            unsupported_operations=unsupported_operations,
        )

    @classmethod
    def resolved(
        cls,
        *,
        message: str | None = None,
        failure_kind: FailureKind | None = None,
    ) -> SemanticOutcome:
        return cls(status="resolved", message=message, failure_kind=failure_kind)

    @classmethod
    def needs_clarification(
        cls,
        *,
        missing_fields: list[str] | tuple[str, ...],
        missing_field_causes: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
        message: str | None = None,
        failure_kind: FailureKind | None = None,
        failure_reason: FailureReason | None = None,
    ) -> SemanticOutcome:
        return cls(
            status="needs_clarification",
            missing_fields=tuple(missing_fields),
            missing_field_causes=tuple(copy.deepcopy(list(missing_field_causes))),
            message=message,
            failure_kind=failure_kind,
            failure_reason=failure_reason,
        )

    @classmethod
    def unsupported(
        cls,
        *,
        operations: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        message: str | None = None,
        failure_kind: FailureKind | None = "unsupported",
    ) -> SemanticOutcome:
        if not operations:
            raise ValueError("unsupported SemanticOutcome requires operations")
        return cls(
            status="unsupported",
            unsupported_operations=tuple(copy.deepcopy(list(operations))),
            message=message,
            failure_kind=failure_kind,
        )

    @classmethod
    def policy_applied(
        cls,
        *,
        applications: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        message: str | None = None,
        failure_kind: FailureKind | None = None,
    ) -> SemanticOutcome:
        if not applications:
            raise ValueError("policy_applied SemanticOutcome requires applications")
        return cls(
            status="policy_applied",
            policy_applications=tuple(copy.deepcopy(list(applications))),
            message=message,
            failure_kind=failure_kind,
        )

    def to_legacy_dict(self) -> dict[str, Any]:
        projection: dict[str, Any] = {
            "status": self.status,
            # 신규 outcome은 실행 operation을 생산하지 않는다. 이 필드는 wire 호환 projection이다.
            "operations": [],
            "missing_fields": list(self.missing_fields),
            "missing_field_causes": copy.deepcopy(list(self.missing_field_causes)),
            "failure_kind": self.failure_kind,
            "policy_applications": copy.deepcopy(list(self.policy_applications)),
            "unsupported_operations": copy.deepcopy(list(self.unsupported_operations)),
            "message": self.message,
        }
        # 명시 사유가 없으면 키 자체를 싣지 않는다 — 기존 투영과 바이트 동일하게 남긴다.
        if self.failure_reason is not None:
            projection["failure_reason"] = self.failure_reason
        return projection


__all__ = [
    "FAILURE_KINDS",
    "FAILURE_REASONS",
    "FAILURE_REASON_EMISSION",
    "SEMANTIC_OUTCOME_WIRE_SPEC",
    "SEMANTIC_STATUSES",
    "ClosedObjectSpec",
    "FailureKind",
    "FailureReason",
    "SemanticOutcome",
    "SemanticStatus",
    "WireFieldSpec",
    "WireValueSpec",
    "parse_semantic_outcome_projection",
    "semantic_outcome_json_schema",
    "validate_semantic_outcome_state",
]
