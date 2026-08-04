# Canonical Event IR Python 개발 지침

이 프로젝트는 다양한 자연어, DSL, 외부 이벤트 형식을 하나의 Canonical Event IR로 변환하고 평가하는 Python 시스템이다.

코드를 작성하거나 수정할 때 다음 규칙을 항상 따른다.

---

## 1. 코드를 수정하기 전에 기존 구조부터 확인한다

작업 시작 전에 반드시 다음 항목을 확인한다.

- 이벤트 모델
- Expression AST 모델
- Parser와 Adapter
- Normalizer와 Lowering
- Validator
- Evaluator
- Operator Registry
- Serializer
- 버전 관리 방식
- 기존 테스트
- 사용 중인 Python 버전
- 사용 중인 모델 라이브러리

기존 구조를 확인하지 않고 새로운 클래스, 필드, 연산자, 모듈을 임의로 추가하지 않는다.

프로젝트에서 `dataclass`, Pydantic, attrs 중 어떤 방식을 사용하는지 먼저 확인하고 기존 방식을 유지한다.

---

## 2. 새 표현이 나왔다고 IR 모델부터 수정하지 않는다

새 표현을 발견하면 다음 순서로 판단한다.

1. 기존 표현과 같은 의미인데 문법만 다른가?
2. 기존 연산자의 조합으로 표현 가능한가?
3. Normalization으로 통일할 수 있는가?
4. Lowering을 통해 기본 연산자로 변환 가능한가?
5. 정말 새로운 의미를 가진 연산자인가?
6. 특정 도메인에서만 사용하는 확장인가?

단순히 표현 문구가 새롭다는 이유로 Core IR 모델을 수정하지 않는다.

예를 들어 다음 표현은 같은 의미로 정규화할 수 있다.

```text
결제 완료
결제가 끝남
payment completed
payment status is PAID
```

정규화 결과:

```python
CanonicalEvent(
    type="commerce.payment.completed",
    attributes={
        "status": "PAID",
    },
)
```

---

## 3. Core IR은 작고 안정적으로 유지한다

Core IR에 요구사항이 생길 때마다 최상위 필드를 추가하지 않는다.

권장 예시:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class EntityRef:
    entity_type: str
    entity_id: str


@dataclass(frozen=True, slots=True)
class SourceInfo:
    provider: str
    raw_type: str | None = None
    source_id: str | None = None


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    version: int
    type: str
    attributes: dict[str, Any]
    source: SourceInfo
    subject: EntityRef | None = None
    occurred_at: datetime | None = None
    condition: Expression | None = None
    extensions: dict[str, Any] = field(default_factory=dict)
```

특정 도메인에서만 사용하는 데이터는 다음 위치 중 하나에 둔다.

- `attributes`
- `extensions`
- 명확한 namespace를 가진 별도 모델

Core IR에 `top_percent`, `coupon`, `delivery_region` 같은 전용 필드를 직접 추가하지 않는다.

---

## 4. Expression은 범용 AST로 구성한다

표현식마다 새로운 Python 클래스를 계속 추가하지 않는다.

피해야 할 구조:

```python
class EqualsExpression:
    ...

class ContainsExpression:
    ...

class TopTenPercentExpression:
    ...

class TopFivePercentExpression:
    ...

class WithinBusinessDaysExpression:
    ...
```

권장 구조:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class LiteralExpression:
    kind: Literal["literal"] = "literal"
    value: Any = None


@dataclass(frozen=True, slots=True)
class FieldExpression:
    path: str
    kind: Literal["field"] = "field"


@dataclass(frozen=True, slots=True)
class CallExpression:
    operator: str
    args: tuple[Expression, ...]
    options: dict[str, Any] = field(default_factory=dict)
    kind: Literal["call"] = "call"


@dataclass(frozen=True, slots=True)
class UnknownExpression:
    name: str
    raw: Any
    kind: Literal["unknown"] = "unknown"


Expression: TypeAlias = (
    LiteralExpression
    | FieldExpression
    | CallExpression
    | UnknownExpression
)
```

새로운 의미는 가능하면 새 AST 클래스가 아니라 새로운 operator로 표현한다.

---

## 5. Parser, Validator, Normalizer, Lowering, Evaluator를 분리한다

전체 처리 흐름은 다음과 같이 유지한다.

```text
원본 입력
→ Parser 또는 Adapter
→ Canonical IR
→ Validation
→ Normalization
→ Lowering
→ Evaluation
→ Serialization
```

각 단계의 책임은 다음과 같다.

### Parser

입력 문법을 Expression 또는 CanonicalEvent로 변환한다.

Parser 안에서 데이터베이스 조회, 퍼센타일 계산, 이벤트 실행을 하지 않는다.

### Adapter

외부 시스템의 이벤트 형식을 내부 Canonical IR로 변환한다.

외부 필드 이름과 내부 필드 이름의 매핑을 담당한다.

### Validator

다음을 검증한다.

- 필수 인자
- 인자 개수
- 값의 타입
- 숫자 범위
- 옵션 형식
- 지원되는 operator
- 허용된 field path
- 버전 호환성

### Normalizer

서로 다른 표현을 같은 canonical 표현으로 통일한다.

### Lowering

고수준 표현을 더 작은 기본 연산자의 조합으로 변환한다.

### Evaluator

검증되고 정규화된 표현을 실행한다.

### Serializer

IR을 JSON 등으로 변환하고 다시 복원한다.

---

## 6. 새 표현은 가능한 한 Lowering한다

예를 들어 다음 표현을 전용 실행 로직으로 만들지 않는다.

```text
age between 20 and 30
```

다음 표현으로 lowering한다.

```python
CallExpression(
    operator="core.and",
    args=(
        CallExpression(
            operator="core.gte",
            args=(
                FieldExpression(path="age"),
                LiteralExpression(value=20),
            ),
        ),
        CallExpression(
            operator="core.lte",
            args=(
                FieldExpression(path="age"),
                LiteralExpression(value=30),
            ),
        ),
    ),
)
```

입력 단계에서는 `core.between`을 허용할 수 있지만 실행 전에는 가능하면 기본 연산자로 변환한다.

---

## 7. `상위 10%`는 상대적 집합 표현으로 처리한다

`상위 10%`는 단순 숫자 비교가 아니다.

다음 정보가 필요하다.

- 비교 필드
- 모집단
- 집계 기간
- 그룹 기준
- 정렬 방향
- 동점자 정책
- null 정책
- 최소 모집단 크기
- 기준 시각
- 순위 계산 방식

권장 표현:

```python
CallExpression(
    operator="rank.top_percent",
    args=(
        FieldExpression(path="user.purchase_amount"),
    ),
    options={
        "percent": 10,
        "population": "active_users",
        "window": "P30D",
        "order": "desc",
        "tie_policy": "include",
        "null_policy": "exclude",
        "minimum_population": 20,
    },
)
```

다음처럼 단순히 처리하지 않는다.

```python
purchase_amount >= 90
```

`값이 90 이상`과 `상위 10%`는 전혀 다른 의미다.

---

## 8. 상위 퍼센트 표현의 의미를 명확히 정의한다

`상위 10%`는 프로젝트에서 다음 방식 중 하나로 명확히 정의한다.

### Percentile rank 방식

```text
percentile_rank(value) >= 90
```

### Percentile threshold 방식

```text
value >= percentile(population, field, 90)
```

두 방식은 데이터 분포, 동점자, 표본 크기에 따라 결과가 달라질 수 있다.

프로젝트 전체에서 하나의 기본 정책을 정하고 테스트로 고정한다.

---

## 9. Operator Registry를 사용한다

연산자를 여러 파일의 `if`, `elif`, `match` 문에 분산하지 않는다.

권장 인터페이스:

```python
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    path: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    valid: bool
    issues: tuple[ValidationIssue, ...] = ()


class OperatorValidator(Protocol):
    def __call__(
        self,
        args: Sequence[Expression],
        options: dict[str, Any],
    ) -> ValidationResult:
        ...


class OperatorEvaluator(Protocol):
    def __call__(
        self,
        values: Sequence[Any],
        context: EvaluationContext,
        options: dict[str, Any],
    ) -> Any | Awaitable[Any]:
        ...


@dataclass(frozen=True, slots=True)
class OperatorDefinition:
    name: str
    min_args: int
    max_args: int | None
    validate: OperatorValidator
    evaluate: OperatorEvaluator
```

Registry 예시:

```python
class OperatorRegistry:
    def __init__(self) -> None:
        self._operators: dict[str, OperatorDefinition] = {}

    def register(self, definition: OperatorDefinition) -> None:
        if definition.name in self._operators:
            raise ValueError(
                f"Operator already registered: {definition.name}"
            )

        self._operators[definition.name] = definition

    def get(self, name: str) -> OperatorDefinition:
        try:
            return self._operators[name]
        except KeyError as exc:
            raise UnsupportedOperatorError(name) from exc

    def contains(self, name: str) -> bool:
        return name in self._operators
```

---

## 10. Operator 이름에는 namespace를 사용한다

모호한 operator 이름을 사용하지 않는다.

피해야 할 이름:

```text
equals
top
within
contains
rank
```

권장 이름:

```text
core.equals
core.and
core.gte
collection.contains
temporal.within
rank.top_percent
rank.percentile_rank
aggregate.percentile
commerce.has_coupon
geo.inside
```

Python 함수 이름은 snake_case를 사용하더라도 외부 IR operator 이름은 프로젝트의 직렬화 규칙에 맞춰 일관되게 유지한다.

---

## 11. 지원하지 않는 표현을 조용히 무시하지 않는다

지원하지 않는 표현에 대해 다음 처리를 금지한다.

- 표현을 삭제한다.
- 항상 `True`로 처리한다.
- 항상 `False`로 처리한다.
- 가장 비슷해 보이는 operator로 임의 변환한다.
- 오류를 숨긴다.
- 기본값을 임의로 삽입한다.

지원하지 않는 표현은 원본을 보존한다.

```python
UnknownExpression(
    name="roughly_within",
    raw={
        "value": 3,
        "unit": "business_days",
    },
)
```

평가 결과도 명확히 구분한다.

```python
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias


@dataclass(frozen=True, slots=True)
class EvaluationSuccess:
    status: Literal["success"]
    value: Any


@dataclass(frozen=True, slots=True)
class EvaluationUnsupported:
    status: Literal["unsupported"]
    expression: Expression
    reason: str


@dataclass(frozen=True, slots=True)
class EvaluationInvalid:
    status: Literal["invalid"]
    issues: tuple[ValidationIssue, ...]


@dataclass(frozen=True, slots=True)
class EvaluationError:
    status: Literal["error"]
    message: str
    error_code: str


EvaluationResult: TypeAlias = (
    EvaluationSuccess
    | EvaluationUnsupported
    | EvaluationInvalid
    | EvaluationError
)
```

업무상 예상 가능한 오류를 예외로만 처리하지 않는다.

평가 불가 상태가 정상적인 도메인 결과라면 명시적인 결과 객체로 반환한다.

---

## 12. 의미가 불명확하면 추측해서 구현하지 않는다

다음 표현은 정의 없이 구현하지 않는다.

- 최근 사용자
- 우수 고객
- 상위 고객
- 많이 구매한 사용자
- 거의 동일
- 자주 방문한 사용자
- 일정 기간 내
- 활성 사용자
- 비정상적인 요청

예를 들어 `최근 사용자`는 다음 중 어떤 의미인지 정의가 필요하다.

- 최근 가입
- 최근 로그인
- 최근 구매
- 최근 이벤트 발생
- 최근 7일
- 최근 30일

정의가 없으면 validation 오류 또는 unsupported 결과로 처리한다.

---

## 13. 단위를 명시적으로 관리한다

숫자 값만 저장하고 단위를 추측하지 않는다.

피해야 할 예:

```python
{"value": 10}
```

권장 예:

```python
{"value": 10, "unit": "percent"}
```

```python
{"value": 30, "unit": "day"}
```

```python
{"amount": 10_000, "currency": "KRW"}
```

가능하면 단위를 Enum 또는 값 객체로 관리한다.

```python
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class RatioUnit(StrEnum):
    PERCENT = "percent"
    FRACTION = "fraction"


@dataclass(frozen=True, slots=True)
class Ratio:
    value: Decimal
    unit: RatioUnit
```

---

## 14. 금액과 비율에는 float를 사용하지 않는다

금액, 비율, 퍼센타일 임계값처럼 정밀도가 필요한 값에는 `float` 사용을 피한다.

권장:

```python
from decimal import Decimal

percent = Decimal("10")
amount = Decimal("10000.50")
```

피해야 할 예:

```python
percent = 0.1
amount = 10000.50
```

JSON 직렬화 시 `Decimal` 처리 정책을 명확히 정의한다.

---

## 15. 시간은 timezone-aware datetime을 사용한다

naive datetime을 사용하지 않는다.

피해야 할 예:

```python
from datetime import datetime

now = datetime.now()
```

권장 예:

```python
from datetime import UTC, datetime

now = datetime.now(UTC)
```

평가 중 현재 시각이 필요하다면 evaluator 내부에서 직접 생성하지 않는다.

```python
@dataclass(frozen=True, slots=True)
class EvaluationContext:
    now: datetime
    timezone: str
```

권장 사용:

```python
now = context.now
```

같은 입력, 같은 데이터, 같은 기준 시각에서는 같은 결과가 나와야 한다.

---

## 16. 기간은 문자열 또는 명확한 값 객체로 관리한다

`30일` 같은 기간을 임의 정수로만 저장하지 않는다.

예:

```python
{
    "window": "P30D",
}
```

또는:

```python
from dataclasses import dataclass
from datetime import timedelta


@dataclass(frozen=True, slots=True)
class TimeWindow:
    duration: timedelta
    direction: str
    inclusive: bool
```

달, 영업일, 달력일은 서로 다르므로 모두 `timedelta`로 단순 변환하지 않는다.

---

## 17. 동점자 정책을 명확히 한다

상위 10% 경계에서 동일한 값이 여러 개 존재할 수 있다.

지원 가능한 정책 예:

```python
from enum import StrEnum


class TiePolicy(StrEnum):
    INCLUDE = "include"
    EXCLUDE = "exclude"
    DENSE_RANK = "dense_rank"
    EXACT_COUNT = "exact_count"
```

기본 정책을 함수 내부에 숨기지 않는다.

IR 옵션이나 명시적인 설정에 포함한다.

---

## 18. 작은 모집단 정책을 명확히 한다

예를 들어 3명 중 상위 10%는 0.3명이다.

가능한 정책:

- 최소 1명 포함
- 내림
- 올림
- 반올림
- 최소 모집단 미달로 평가하지 않음
- 퍼센타일 임계값 방식 사용

정책을 코드 작성자가 임의로 선택하지 않는다.

예:

```python
class SmallPopulationPolicy(StrEnum):
    INCLUDE_AT_LEAST_ONE = "include_at_least_one"
    FLOOR = "floor"
    CEIL = "ceil"
    REJECT = "reject"
```

---

## 19. null과 missing을 구분한다

다음 상태를 동일하게 취급하지 않는다.

- 키가 존재하지 않음
- 값이 `None`
- 빈 문자열
- 숫자 0
- 빈 리스트
- 집계 결과 없음
- 타입 변환 실패
- 데이터 조회 실패

필요하면 missing sentinel을 사용한다.

```python
class _Missing:
    __slots__ = ()


MISSING = _Missing()
```

```python
value = resolve_field(data, path, default=MISSING)

if value is MISSING:
    ...
elif value is None:
    ...
```

순위 계산 시 null 정책을 명시한다.

```python
class NullPolicy(StrEnum):
    EXCLUDE = "exclude"
    INCLUDE_AS_LOWEST = "include_as_lowest"
    INCLUDE_AS_HIGHEST = "include_as_highest"
    ERROR = "error"
```

---

## 20. 암묵적 타입 변환을 피한다

다음 값들을 자동으로 같은 값으로 처리하지 않는다.

```text
"10"
10
Decimal("10")
"10%"
Decimal("0.1")
```

타입 변환이 필요하면 명시적인 변환 단계를 둔다.

예:

```text
core.to_decimal
core.to_string
ratio.from_percent
datetime.parse_iso8601
```

Python의 truthy, falsy 규칙을 도메인 평가 결과에 그대로 사용하지 않는다.

피해야 할 예:

```python
if value:
    ...
```

값이 `0`, `False`, 빈 리스트일 수 있다면 명확하게 비교한다.

---

## 21. Field path 접근을 안전하게 구현한다

다음처럼 `getattr`을 무제한으로 연결하지 않는다.

```python
getattr(getattr(obj, first), second)
```

다음 경로는 차단한다.

```text
__class__
__dict__
__bases__
__subclasses__
__globals__
__code__
__proto__
constructor
```

권장 예:

```python
BLOCKED_PATH_PARTS = {
    "__class__",
    "__dict__",
    "__bases__",
    "__subclasses__",
    "__globals__",
    "__code__",
    "__getattribute__",
}


def validate_field_path(path: str) -> tuple[str, ...]:
    parts = tuple(path.split("."))

    if not parts or any(not part for part in parts):
        raise ValueError("Field path contains an empty component")

    for part in parts:
        if part in BLOCKED_PATH_PARTS or part.startswith("__"):
            raise ValueError(f"Blocked field path component: {part}")

    return parts
```

가능하면 dict 기반 데이터와 허용된 스키마 필드만 조회한다.

---

## 22. eval과 임의 코드 실행을 금지한다

다음 기능을 사용하지 않는다.

```python
eval(...)
exec(...)
compile(...)
```

외부 입력으로 다음 작업을 하지 않는다.

- 동적 import
- 임의 함수 호출
- 임의 모듈 접근
- 임의 클래스 생성
- 임의 SQL 문자열 생성
- 임의 파일 경로 접근
- 임의 shell 실행
- pickle 역직렬화

연산자는 사전에 Registry에 등록된 구현만 실행한다.

---

## 23. pickle을 외부 데이터 직렬화에 사용하지 않는다

외부 입력 또는 신뢰할 수 없는 저장 데이터에 대해 `pickle.loads()`를 사용하지 않는다.

IR 저장은 다음과 같이 검증 가능한 형식을 사용한다.

- JSON
- MessagePack
- Protobuf
- 명시적인 schema 기반 직렬화

JSON 역직렬화 후 반드시 schema validation을 수행한다.

---

## 24. SQL을 문자열로 조합하지 않는다

표현식을 SQL로 변환할 때 값과 식별자를 구분한다.

피해야 할 예:

```python
query = f"SELECT * FROM users WHERE score > {value}"
```

파라미터 바인딩을 사용한다.

```python
query = "SELECT * FROM users WHERE score > :value"
params = {"value": value}
```

field path나 정렬 컬럼은 파라미터 바인딩이 되지 않는 경우가 많으므로 allowlist로 제한한다.

---

## 25. 정규식은 비용과 안전성을 고려한다

외부 입력으로 정규식을 허용할 경우 다음을 제한한다.

- 최대 패턴 길이
- 실행 시간
- 입력 문자열 길이
- 허용 플래그
- 복잡한 중첩 패턴
- catastrophic backtracking 가능성

정규식 검증 없이 사용자 입력을 `re.compile()` 하지 않는다.

---

## 26. 비동기와 동기 코드를 혼합하지 않는다

데이터베이스나 외부 API 조회가 필요한 evaluator라면 async 정책을 프로젝트 전체에서 통일한다.

피해야 할 예:

```python
asyncio.run(...)
```

라이브러리 내부 또는 이미 실행 중인 event loop 안에서 `asyncio.run()`을 호출하지 않는다.

동기 evaluator와 비동기 evaluator의 인터페이스를 임의로 혼합하지 않는다.

필요하면 evaluator를 처음부터 async 인터페이스로 통일한다.

```python
class AsyncOperatorEvaluator(Protocol):
    async def __call__(
        self,
        values: Sequence[Any],
        context: EvaluationContext,
        options: dict[str, Any],
    ) -> Any:
        ...
```

---

## 27. 전체 데이터 반복 조회를 금지한다

상위 10%, 퍼센타일, 집계 연산자는 전체 모집단 조회를 유발할 수 있다.

다음 항목을 검토한다.

- 조회 기간
- 모집단 크기
- 그룹 수
- 인덱스
- 캐시
- 사전 집계
- 최대 실행 시간
- pagination
- 메모리 제한
- 동일 기준값 재사용 가능성

사용자별로 모집단 전체를 다시 계산하지 않는다.

피해야 할 흐름:

```python
for user in users:
    threshold = calculate_percentile(all_users)
    evaluate(user, threshold)
```

권장 흐름:

```python
threshold = calculate_percentile(all_users)

results = [
    evaluate(user, threshold)
    for user in users
]
```

가능하면 데이터베이스 window function이나 집계 쿼리를 사용한다.

---

## 28. Operator 내부에서 데이터 조회를 숨기지 않는다

순수 비교 연산자와 데이터 조회 연산자의 책임을 구분한다.

예:

```text
aggregate.percentile
rank.top_percent
```

이런 연산자가 데이터 조회를 필요로 한다면 repository 또는 provider 인터페이스를 context로 주입한다.

```python
class PopulationProvider(Protocol):
    async def get_values(
        self,
        *,
        population: str,
        field: str,
        window: str | None,
        group_by: tuple[str, ...],
    ) -> Sequence[Decimal]:
        ...
```

전역 데이터베이스 연결이나 전역 repository를 operator에서 직접 참조하지 않는다.

---

## 29. 의존성 주입을 사용한다

다음 값을 전역 변수에서 직접 가져오지 않는다.

- 현재 시각
- 데이터베이스
- 캐시
- 외부 API
- 사용자 권한
- feature flag
- 로거
- 설정

EvaluationContext 또는 명시적인 생성자 인자로 주입한다.

```python
@dataclass(frozen=True, slots=True)
class EvaluationContext:
    now: datetime
    timezone: str
    population_provider: PopulationProvider
    permissions: frozenset[str]
```

---

## 30. 전역 mutable state를 피한다

피해야 할 예:

```python
OPERATORS = {}
CACHE = {}
CURRENT_CONTEXT = None
```

Registry가 필요하다면 애플리케이션 초기화 단계에서 구성하고 명시적으로 전달한다.

테스트 간 상태가 공유되지 않도록 한다.

---

## 31. Mutable default argument를 사용하지 않는다

피해야 할 예:

```python
def parse_event(
    raw: dict,
    errors: list[str] = [],
) -> CanonicalEvent:
    ...
```

권장 예:

```python
def parse_event(
    raw: dict,
    errors: list[str] | None = None,
) -> CanonicalEvent:
    current_errors = [] if errors is None else errors
```

dataclass에서는 `default_factory`를 사용한다.

```python
@dataclass
class Event:
    attributes: dict[str, Any] = field(default_factory=dict)
```

---

## 32. 가능한 경우 immutable 모델을 사용한다

IR은 생성 이후 임의로 변경되면 추적이 어렵다.

가능하면 다음 옵션을 사용한다.

```python
@dataclass(frozen=True, slots=True)
class CallExpression:
    ...
```

리스트 대신 tuple을 고려한다.

```python
args: tuple[Expression, ...]
```

변경이 필요하면 새 객체를 생성한다.

---

## 33. 너무 넓은 예외 처리를 하지 않는다

피해야 할 예:

```python
try:
    ...
except Exception:
    return False
```

이 코드는 버그와 평가 실패를 숨긴다.

예상 가능한 예외를 구체적으로 처리한다.

```python
try:
    operator = registry.get(expression.operator)
except UnsupportedOperatorError as exc:
    return EvaluationUnsupported(
        status="unsupported",
        expression=expression,
        reason=str(exc),
    )
```

예상하지 못한 시스템 오류는 로그를 남기고 명확한 error 결과 또는 상위 계층 예외로 전달한다.

---

## 34. 오류 메시지는 구체적으로 작성한다

피해야 할 메시지:

```text
Invalid expression
```

권장 메시지:

```text
rank.top_percent requires percent between 0 and 100.
```

```text
rank.top_percent cannot be evaluated because population is missing.
```

```text
Unsupported operator: rank.top_percent.v2
```

가능하면 다음 정보를 포함한다.

- expression 경로
- operator 이름
- 잘못된 값
- 기대 타입
- schema version
- 해결 가능한 조치

개인정보나 원본 이벤트 전체를 오류 메시지에 넣지 않는다.

---

## 35. 타입 힌트를 생략하지 않는다

공개 함수, 핵심 도메인 함수, operator 구현에는 타입 힌트를 작성한다.

피해야 할 예:

```python
def evaluate(expr, ctx):
    ...
```

권장 예:

```python
def evaluate(
    expression: Expression,
    context: EvaluationContext,
) -> EvaluationResult:
    ...
```

`Any`를 사용해 타입 오류를 숨기지 않는다.

`Any`가 필요한 경계에서는 사용 범위와 이유를 제한한다.

---

## 36. `cast()`로 실제 오류를 숨기지 않는다

피해야 할 예:

```python
from typing import cast

value = cast(Decimal, raw_value)
```

`cast()`는 런타임 변환이나 검증을 수행하지 않는다.

외부 입력은 실제 타입 검사나 schema validation을 거쳐야 한다.

```python
if not isinstance(raw_value, Decimal):
    raise TypeError("Expected Decimal")
```

---

## 37. Pydantic을 사용할 경우 strict validation을 고려한다

Pydantic을 사용하는 프로젝트라면 암묵적 coercion으로 의미가 변하지 않도록 한다.

예:

```python
from pydantic import BaseModel, ConfigDict


class LiteralModel(BaseModel):
    model_config = ConfigDict(
        strict=True,
        extra="forbid",
    )

    kind: Literal["literal"]
    value: object
```

스키마에 정의되지 않은 필드는 무조건 무시하지 않는다.

필요에 따라 `extra="forbid"` 또는 별도 extension 필드를 사용한다.

---

## 38. dataclass와 dict를 무분별하게 섞지 않는다

도메인 계층에서는 명확한 모델을 사용하고, 직렬화 경계에서 dict로 변환한다.

피해야 할 흐름:

```python
event["condition"]["args"][0]["path"]
```

권장 흐름:

```python
event.condition.args[0].path
```

단, 외부 JSON을 파싱하는 경계에서는 dict 입력을 허용하고 즉시 검증된 모델로 변환한다.

---

## 39. JSON 직렬화 규칙을 명확히 한다

다음 타입은 JSON에서 자동 처리되지 않으므로 규칙을 정한다.

- `datetime`
- `Decimal`
- `Enum`
- tuple
- UUID
- unknown raw 데이터

권장 예:

```python
def serialize_datetime(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Naive datetime cannot be serialized")

    return value.isoformat()
```

직렬화와 역직렬화가 대칭이어야 한다.

---

## 40. Round-trip 의미 보존 테스트를 작성한다

다음 과정을 거친 뒤 의미가 달라지면 안 된다.

```text
Expression
→ JSON
→ 저장
→ JSON 로드
→ Expression
```

다음을 보존해야 한다.

- operator
- argument 순서
- options
- Decimal 정밀도
- datetime timezone
- unknown expression의 raw 데이터
- extensions
- schema version
- capability 정보

---

## 41. 스키마 버전과 기능 버전을 분리한다

이벤트 구조 버전과 operator 지원 버전을 하나로 묶지 않는다.

예:

```python
event = {
    "version": 1,
    "capabilities": [
        "expr.core.v1",
        "operator.rank.v1",
        "operator.temporal.v2",
        "extension.commerce.v1",
    ],
}
```

operator 하나를 추가하기 위해 전체 이벤트 스키마 버전을 무조건 올리지 않는다.

기존 데이터의 역직렬화와 migration 정책을 확인한다.

---

## 42. 테스트를 먼저 정의하고 구현한다

새 표현을 추가할 때 최소한 다음 순서로 진행한다.

1. 의미를 정의한다.
2. 입력 예시를 작성한다.
3. canonical 결과를 정의한다.
4. validation 규칙을 정의한다.
5. lowering 결과를 정의한다.
6. 평가 결과를 정의한다.
7. 경계값 테스트를 작성한다.
8. 구현한다.
9. 회귀 테스트를 실행한다.

코드를 먼저 작성하고 의미를 나중에 맞추지 않는다.

---

## 43. pytest 테스트를 충분히 작성한다

새 operator를 추가할 때 최소한 다음 테스트를 작성한다.

- 정상 입력
- 잘못된 타입
- 인자 누락
- 인자 초과
- 잘못된 옵션
- 경계값
- null
- missing field
- 빈 모집단
- 최소 모집단 미달
- 동점자
- timezone
- 날짜 경계
- unsupported operator
- 직렬화 round-trip
- 버전 호환성
- 권한 없는 field
- 대량 데이터
- 데이터 조회 실패

`상위 10%` 테스트 예:

```python
import pytest


@pytest.mark.parametrize(
    ("population_size", "percent", "expected_count"),
    [
        (100, 10, 10),
        (20, 10, 2),
        (10, 10, 1),
    ],
)
def test_top_percent_count(
    population_size: int,
    percent: int,
    expected_count: int,
) -> None:
    ...
```

추가 필수 사례:

- 3명 중 상위 10%
- 경계값에 동점자 존재
- 모든 값이 동일
- null 포함
- 빈 모집단
- 그룹별 상위 10%
- 최근 30일 집계
- 오름차순
- 내림차순
- 정확히 90퍼센타일
- 최소 표본 수 미달

---

## 44. 시간 테스트에는 고정 시각을 사용한다

테스트에서 실제 현재 시간을 사용하지 않는다.

피해야 할 예:

```python
datetime.now(UTC)
```

권장 예:

```python
from datetime import UTC, datetime

FIXED_NOW = datetime(
    2026,
    8,
    4,
    5,
    0,
    tzinfo=UTC,
)
```

EvaluationContext에 고정된 시각을 주입한다.

---

## 45. Property-based testing을 고려한다

순위, 퍼센타일, 직렬화, lowering 로직에는 Hypothesis 사용을 고려한다.

검증할 수 있는 속성 예:

- 직렬화 후 복원하면 동일하다.
- 상위 비율이 커지면 결과 집합은 줄어들지 않는다.
- 같은 값과 같은 context에서는 같은 결과가 나온다.
- lowering 전후 평가 결과가 같다.
- 값 순서를 바꿔도 퍼센타일 임계값이 변하지 않는다.
- percent가 0보다 작거나 100보다 크면 항상 validation 실패한다.

---

## 46. Snapshot 테스트만 사용하지 않는다

JSON snapshot이 같더라도 의미가 틀릴 수 있다.

다음 테스트를 분리해서 작성한다.

- Parser 테스트
- Validator 테스트
- Normalizer 테스트
- Lowering 테스트
- Evaluator 테스트
- Serializer 테스트
- Integration 테스트
- Round-trip 테스트

---

## 47. 기존 테스트를 삭제하거나 약화하지 않는다

새 기능을 통과시키기 위해 다음 행동을 하지 않는다.

- 기존 assertion 삭제
- 테스트 skip
- xfail 남용
- fixture 값 임의 변경
- 타입 검사 제외
- lint rule 비활성화
- coverage 대상 제외
- 예외를 넓게 처리해 테스트만 통과

기존 동작을 변경해야 한다면 변경 이유와 migration 영향을 명확히 기록한다.

---

## 48. 로깅에 민감한 데이터를 남기지 않는다

로그에 다음 내용을 그대로 출력하지 않는다.

- 전체 이벤트 payload
- 개인정보
- 결제정보
- 인증 토큰
- 사용자 입력 원문
- 비밀번호
- 세션 정보

구조화 로그에는 필요한 메타데이터만 남긴다.

예:

```python
logger.info(
    "expression_evaluated",
    extra={
        "operator": expression.operator,
        "schema_version": schema_version,
        "duration_ms": duration_ms,
        "population_size": population_size,
        "cache_hit": cache_hit,
    },
)
```

---

## 49. print를 운영 로깅으로 사용하지 않는다

피해야 할 예:

```python
print("unsupported operator", operator)
```

프로젝트의 logging 정책을 따른다.

개발 중 추가한 임시 출력문은 작업 완료 전에 제거한다.

---

## 50. 성능 측정 없이 최적화하지 않는다

먼저 명확하고 올바른 구현을 작성한다.

성능 문제가 예상되면 다음을 측정한다.

- evaluator 호출 횟수
- 데이터베이스 쿼리 수
- 모집단 조회 시간
- 퍼센타일 계산 시간
- 메모리 사용량
- 캐시 hit 비율
- 직렬화 비용

다만 N+1, 무제한 전체 조회, 반복 퍼센타일 계산처럼 명백한 구조적 문제는 처음부터 피한다.

---

## 51. 캐시 키에 평가 문맥을 포함한다

상위 10% 임계값을 캐시한다면 다음 값을 캐시 키에 포함한다.

- 모집단
- 필드
- 기간
- 그룹
- 정렬 방향
- null 정책
- 동점자 정책
- 기준 시각 또는 데이터 버전
- 권한 범위
- schema/operator 버전

피해야 할 캐시 키:

```python
cache_key = "top_10_percent"
```

권장 개념:

```python
cache_key = (
    population,
    field,
    window,
    group_by,
    order,
    null_policy,
    tie_policy,
    data_version,
)
```

---

## 52. 함수는 한 가지 책임만 갖게 한다

피해야 할 함수:

```python
def parse_validate_normalize_evaluate_and_save(raw):
    ...
```

권장 구조:

```python
expression = parser.parse(raw)
validation = validator.validate(expression)
normalized = normalizer.normalize(expression)
lowered = lowerer.lower(normalized)
result = evaluator.evaluate(lowered, context)
repository.save(result)
```

각 단계를 독립적으로 테스트할 수 있어야 한다.

---

## 53. 함수와 클래스 이름은 의미 중심으로 작성한다

피해야 할 이름:

```python
handle_data
process_item
run_logic
do_rank
helper
utils
```

권장 이름:

```python
parse_canonical_event
validate_expression
lower_top_percent_expression
calculate_percentile_threshold
resolve_population_values
evaluate_call_expression
```

`utils.py`에 관련 없는 기능을 계속 추가하지 않는다.

도메인별 모듈로 분리한다.

---

## 54. 과도한 추상화를 피한다

operator 하나를 추가하기 위해 불필요한 추상 계층을 여러 개 만들지 않는다.

다음 기준을 따른다.

- 현재 요구사항에 필요한 최소 구조를 사용한다.
- 기존 패턴을 유지한다.
- 미래 요구사항을 추측한 추상화를 만들지 않는다.
- 동일한 패턴이 실제로 반복될 때 공통화한다.
- 단순한 함수로 충분하면 클래스를 만들지 않는다.

---

## 55. AI가 관련 없는 리팩터링을 하지 않게 한다

기능 하나를 구현하면서 다음 작업을 함께 하지 않는다.

- unrelated 파일명 변경
- 전체 폴더 구조 변경
- formatter 변경
- 라이브러리 교체
- 공개 API 변경
- 기존 모델 전면 재작성
- 타입 체계 변경
- 관련 없는 성능 개선
- 관련 없는 코드 정리

변경 범위를 최소화한다.

리팩터링이 필요하다면 기능 변경과 별도 커밋 또는 별도 작업으로 분리한다.

---

## 56. 새로운 라이브러리를 임의로 추가하지 않는다

새 dependency가 필요해 보이면 먼저 다음을 확인한다.

- 표준 라이브러리로 가능한가?
- 기존 dependency로 가능한가?
- 프로젝트의 Python 버전과 호환되는가?
- 유지보수 상태가 적절한가?
- 라이선스 문제가 없는가?
- 보안상 위험이 없는가?
- 번들 크기와 배포에 영향을 주는가?

필요성이 명확하지 않으면 dependency를 추가하지 않는다.

---

## 57. formatter와 lint 설정을 존중한다

프로젝트에서 사용하는 도구를 확인한다.

예:

- Ruff
- Black
- isort
- mypy
- Pyright
- pylint
- pytest

설정 파일을 임의로 변경하지 않는다.

오류를 없애기 위해 다음을 남용하지 않는다.

```python
# type: ignore
# noqa
# pylint: disable
```

불가피하게 사용할 경우 구체적인 오류 코드와 이유를 기록한다.

```python
value = external_call()  # type: ignore[no-untyped-call]
```

---

## 58. import 부작용을 만들지 않는다

모듈 import 시 다음 작업을 수행하지 않는다.

- 데이터베이스 연결
- 외부 API 호출
- 파일 생성
- 무거운 데이터 로딩
- operator 실행
- 환경 설정 변경
- event loop 실행

Registry 등록이 import 부작용으로 이루어진다면 순환 import와 테스트 격리를 주의한다.

가능하면 명시적인 초기화 함수를 사용한다.

```python
def create_default_operator_registry() -> OperatorRegistry:
    registry = OperatorRegistry()
    register_core_operators(registry)
    register_rank_operators(registry)
    return registry
```

---

## 59. 순환 import를 피한다

도메인 모델이 evaluator, repository, 프레임워크에 의존하지 않게 한다.

권장 의존 방향:

```text
domain models
← parser / validator / evaluator interfaces
← infrastructure implementations
← application entrypoint
```

Core IR 모델에서는 웹 프레임워크, ORM, DB 세션을 import하지 않는다.

---

## 60. ORM 모델과 도메인 모델을 분리한다

SQLAlchemy, Django ORM 모델을 Canonical IR 자체로 사용하지 않는다.

ORM 모델은 저장 계층이고, IR 모델은 도메인 표현이다.

필요한 경우 명시적인 mapper를 둔다.

```python
def to_record(event: CanonicalEvent) -> EventRecord:
    ...


def from_record(record: EventRecord) -> CanonicalEvent:
    ...
```

---

## 61. 권한 검사를 evaluator 밖으로 누락하지 않는다

field path나 모집단 데이터가 권한에 따라 제한된다면 context에 권한 정보를 포함한다.

평가 전에 다음을 확인한다.

- 해당 필드를 읽을 권한
- 모집단을 조회할 권한
- 민감한 attribute 접근 권한
- tenant 경계
- 사용자 범위

테넌트 간 데이터가 순위 모집단에 섞이지 않게 한다.

---

## 62. 멀티테넌트 환경에서는 모집단 범위를 명시한다

상위 10% 계산 시 전체 서비스 사용자가 아니라 현재 tenant 내 사용자일 수 있다.

IR 또는 context에서 범위를 명확히 한다.

```python
options={
    "population": "active_users",
    "scope": {
        "tenant_id": "tenant-123",
    },
}
```

tenant 범위를 기본값에 의존하거나 누락하지 않는다.

---

## 63. 결과의 재현성을 유지한다

같은 입력과 문맥에서는 같은 결과가 나와야 한다.

정렬 시 값이 같은 경우 추가 정렬 키를 명시한다.

피해야 할 예:

```python
sorted(users, key=lambda user: user.score, reverse=True)
```

결과 순서가 중요하다면 안정적인 보조 키를 사용한다.

```python
sorted(
    users,
    key=lambda user: (
        -user.score,
        user.user_id,
    ),
)
```

동점자 처리와 결과 순서를 구분한다.

---

## 64. 순위 계산에는 정렬 방향을 명시한다

높은 값이 좋은지 낮은 값이 좋은지 필드마다 다를 수 있다.

예:

- 매출: 높은 값이 상위
- 장애 시간: 낮은 값이 상위
- 응답 시간: 낮은 값이 상위
- 위험 점수: 도메인 정책에 따라 다름

`top`이라는 단어만 보고 무조건 내림차순으로 계산하지 않는다.

IR 옵션에 정렬 방향을 포함한다.

---

## 65. 집계 함수와 순위 함수를 분리한다

예를 들어 최근 30일 총 구매액 상위 10%는 두 단계다.

```text
최근 30일 구매액 합계 계산
→ 사용자별 합계 순위 계산
```

IR에서도 집계와 순위를 구분한다.

```python
CallExpression(
    operator="rank.top_percent",
    args=(
        CallExpression(
            operator="aggregate.sum",
            args=(
                FieldExpression(path="order.amount"),
            ),
            options={
                "window": "P30D",
                "group_by": ["user.id"],
            },
        ),
    ),
    options={
        "percent": 10,
        "order": "desc",
    },
)
```

순위 operator 안에 모든 집계 로직을 하드코딩하지 않는다.

---

## 66. 입력 표현과 canonical 표현을 구분한다

사용자가 입력한 편의 표현과 내부 canonical 표현은 같을 필요가 없다.

입력:

```text
구매액 상위 10%
```

초기 AST:

```python
CallExpression(
    operator="rank.top_percent",
    ...
)
```

Lowering 이후 canonical AST:

```python
CallExpression(
    operator="core.gte",
    args=(
        CallExpression(
            operator="rank.percentile_rank",
            args=(FieldExpression(path="purchase_amount"),),
            options={...},
        ),
        LiteralExpression(value=Decimal("90")),
    ),
)
```

단, lowering이 실제 의미를 정확히 보존할 때만 변환한다.

---

## 67. 새 표현 추가 작업 순서

새 표현을 구현할 때 다음 순서를 반드시 따른다.

1. 표현의 정확한 의미를 문서로 정의한다.
2. 필요한 문맥과 옵션을 정의한다.
3. 기존 연산자로 표현 가능한지 확인한다.
4. 입력 AST를 정의한다.
5. canonical AST를 정의한다.
6. validation 규칙을 작성한다.
7. normalization 규칙을 작성한다.
8. lowering 규칙을 작성한다.
9. evaluator를 구현한다.
10. serializer 호환성을 확인한다.
11. 단위 테스트를 작성한다.
12. 통합 테스트를 작성한다.
13. 기존 데이터 호환성을 확인한다.
14. 성능과 권한 영향을 확인한다.
15. 문서와 예제를 추가한다.

---

## 68. 코딩 작업 전 출력해야 하는 내용

AI 코딩 도구는 코드를 수정하기 전에 다음 내용을 짧게 정리한다.

```text
변경 목적:
영향받는 파일:
기존 구조:
추가하거나 수정할 operator:
IR 변경 여부:
Lowering 가능 여부:
예상 호환성 영향:
필요한 테스트:
```

불필요한 장문 계획은 작성하지 않되, 변경 방향이 검토 가능해야 한다.

---

## 69. 코딩 작업 후 출력해야 하는 내용

작업 완료 후 다음 내용을 정리한다.

```text
변경한 파일:
핵심 변경 사항:
추가한 operator:
선택한 정책:
기존 동작에 미치는 영향:
하위 호환성:
추가한 테스트:
실행한 검사:
남은 위험:
```

테스트를 실행하지 못했다면 실행했다고 말하지 않는다.

실행하지 못한 검사와 이유를 명확히 작성한다.

---

## 70. 완료 조건

다음 항목을 모두 충족해야 작업 완료로 판단한다.

- 표현의 의미가 명확히 정의되었다.
- Core IR을 불필요하게 확장하지 않았다.
- Parser 또는 Adapter가 입력을 처리한다.
- Validation이 동작한다.
- Normalization 또는 Lowering 결과가 명확하다.
- Evaluator가 명시적인 결과를 반환한다.
- unsupported 표현이 손실 없이 보존된다.
- 직렬화 round-trip이 보장된다.
- 기존 테스트가 통과한다.
- 새 경계값 테스트가 추가되었다.
- 시간과 timezone 정책이 명확하다.
- null과 missing 정책이 명확하다.
- 동점자 정책이 명확하다.
- 작은 모집단 정책이 명확하다.
- 임의 코드 실행이 없다.
- Field path 접근이 안전하다.
- 전체 데이터 반복 조회가 없다.
- tenant와 권한 범위가 유지된다.
- 오류와 로그가 운영 환경에서 추적 가능하다.
- 관련 없는 리팩터링이 포함되지 않았다.

---

# AI 코딩 도구 최종 지시

기존 코드를 먼저 읽고 현재 설계와 스타일을 유지한다.

요구사항이 불명확한 경우 임의로 도메인 의미를 만들어 구현하지 않는다.

새로운 표현이 추가되더라도 Core IR 모델을 바로 확장하지 않는다.

먼저 기존 operator 조합, normalization, lowering, extension으로 해결 가능한지 검토한다.

`상위 10%` 같은 표현은 단순 숫자 비교로 처리하지 않는다.

모집단, 기간, 집계, 정렬 방향, 동점자, null, 최소 표본 수, 기준 시각을 명확히 한다.

지원하지 않는 표현을 삭제하거나 무시하지 않는다.

알 수 없는 원본 표현은 `UnknownExpression` 또는 `unsupported` 결과로 보존한다.

`eval`, `exec`, `pickle.loads`, 임의 동적 import, shell 실행을 사용하지 않는다.

외부 입력을 신뢰하지 말고 operator, field path, option, 단위, 날짜, 숫자 범위를 검증한다.

금액과 정밀한 비율에는 `Decimal`을 사용한다.

시간에는 timezone-aware `datetime`을 사용하고 현재 시각은 context로 주입한다.

전역 mutable state와 mutable default argument를 사용하지 않는다.

가능한 경우 immutable dataclass와 명확한 타입 힌트를 사용한다.

예외를 넓게 잡아 오류를 숨기지 않는다.

새 dependency를 임의로 추가하지 않는다.

관련 없는 리팩터링을 함께 하지 않는다.

기존 테스트를 삭제하거나 약화하지 않는다.

모든 기능 변경에 테스트를 추가한다.

코드를 작성하기 전에 변경 방향을 요약하고, 작성 후 변경 파일, 설계 결정, 테스트 결과, 호환성, 남은 위험을 보고한다.
