"""물리 바인딩을 **해석된 카탈로그에서 파생**한다(손으로 적지 않는다).

같은 사실(어느 논리 소스가 어느 테이블인가)을 카탈로그와 이 계층이 각자 적으면 둘은
반드시 갈라지고, 갈라진 순간 "무엇이 실행됐는가"의 답이 둘이 된다. 이 저장소가 값 사전·
지표 정의에서 이미 지불한 비용이라 같은 실수를 반복하지 않는다.

여기서 하는 일은 **형태 변환뿐**이다: 컴파일러 레지스트리(사건/필드/주체)를
:class:`SchemaBindings` 로 옮긴다.

계산식으로 바인딩된 필드(``FieldSpec.expression``)는 평문 컬럼이 아니므로 여기 담기지
않는다. 그런 속성을 일반 컴파일러가 만나면 조용히 빼지 않고
:class:`~query_pipeline.compiler.base.MissingBindingError` 로 멈춘다 — 실CRM 경로는
어차피 :mod:`event_compiler` 가 그 계산식을 소유한다.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

import event_compiler
import event_ir
from query_pipeline.compiler.models import SchemaBinding, SchemaBindings

_PLAIN_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")


def schema_bindings_from_compiler(
    *,
    events: Mapping[str, event_compiler.EventSpec],
    fields: Mapping[str, event_compiler.FieldSpec],
    subject: event_compiler.SubjectSpec,
) -> SchemaBindings:
    """컴파일러 레지스트리 → 논리 entity 별 물리 바인딩."""
    attributes: dict[str, dict[str, str]] = {}
    types: dict[str, dict[str, str]] = {}
    for field_id, field_spec in fields.items():
        entity, _, attribute = field_id.partition(".")
        if field_spec.expression or not _PLAIN_COLUMN_RE.match(field_spec.column or ""):
            continue
        attributes.setdefault(entity, {})[attribute] = field_spec.column
        types.setdefault(entity, {})[attribute] = field_spec.data_type

    bindings: dict[str, SchemaBinding] = {}
    for entity, event_spec in events.items():
        if not _PLAIN_COLUMN_RE.match(event_spec.table.split(".")[-1] or ""):
            continue
        columns = dict(attributes.get(entity, {}))
        column_types = dict(types.get(entity, {}))
        if _PLAIN_COLUMN_RE.match(event_spec.time_column or ""):
            columns.setdefault(event_ir.TIME_FIELD_SUFFIX, event_spec.time_column)
            # 시간 컬럼의 저장 표기는 사건 선언의 ``time_format`` 이 소유한다. 그 표기를
            # 여기서 다시 고르지 않고 컴파일러의 grain 표를 통해 data_type 으로 옮긴다.
            column_types.setdefault(
                event_ir.TIME_FIELD_SUFFIX,
                event_compiler.time_format_data_type(event_spec.time_format),
            )
        correlations = (
            {subject.name: event_spec.event_subject_key}
            if event_spec.binding == "fact_table"
            and _PLAIN_COLUMN_RE.match(event_spec.event_subject_key or "")
            else {}
        )
        bindings[entity] = SchemaBinding(
            entity=entity,
            table=event_spec.table,
            attributes=columns,
            data_type=column_types,
            correlations=correlations,
        )

    subject_columns = dict(attributes.get(subject.name, {}))
    subject_types = dict(types.get(subject.name, {}))
    subject_columns.setdefault(subject.key.casefold(), subject.key)
    bindings[subject.name] = SchemaBinding(
        entity=subject.name,
        table=subject.table,
        attributes=subject_columns,
        data_type=subject_types,
        alias=subject.alias,
        key=subject.key,
    )
    return SchemaBindings(bindings=bindings)


__all__ = ["schema_bindings_from_compiler"]
