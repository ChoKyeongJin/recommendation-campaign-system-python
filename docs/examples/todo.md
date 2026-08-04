Python으로 작성된 자연어 타게팅 파서를 리팩터링해줘.

현재 시스템은 `lexicon_patterns.json`에 한국어 표현을 계속 추가하는 방식이다.

현재 구조는 대략 다음과 같다.

```json
{
  "vocabularies": {
    "purchase_verb": ["구매", "구입", "주문", "샀"],
    "purchase_verb_completed_stem": ["샀"],
    "purchase_verb_colloquial": ["산", "사다"],
    "purchase_verb_colloquial_object_bound": ["사서", "사고"]
  },
  "patterns": {
    "some_pattern": {
      "include": ["purchase_verb"],
      "exclude": ["..."]
    }
  }
}
```

이 구조는 한국어 활용형, 동의어, 문맥별 예외가 계속 추가되어 유지보수가 어렵다.

목표는 사전을 완전히 제거하는 것이 아니라, 사전의 역할을 canonical 의미로 정규화하는 최소 alias 목록으로 제한하는 것이다.

전체 파이프라인을 다음 단계로 분리해줘.

```text
원문
→ 텍스트 전처리
→ 형태 및 표현 정규화
→ 의미 토큰 추출
→ 엔티티 해석
→ 의미 토큰 조합
→ Event IR 생성
→ IR 검증
```

## 핵심 설계 원칙

1. Event IR은 한국어 표면 표현을 포함하지 않는다.
2. Event IR에는 canonical enum과 typed value만 들어간다.
3. 한국어 활용형을 JSON 사전에 하나씩 추가하지 않는다.
4. 브랜드명, 상품명, 카테고리명 같은 실제 데이터 값은 lexicon에 넣지 않는다.
5. 실제 데이터 값은 entity resolver가 외부 데이터 또는 repository를 통해 해석한다.
6. `exclude` 중심 패턴을 최소화한다.
7. 규칙으로 처리할 수 없는 문장만 semantic fallback으로 전달한다.
8. 각 단계는 독립적으로 테스트할 수 있어야 한다.
9. 기존 시스템을 한 번에 제거하지 않고 점진적으로 마이그레이션할 수 있어야 한다.

## 원하는 디렉터리 구조

```text
event_ir/
├── __init__.py
├── models.py
├── enums.py
├── normalizer.py
├── tokenizer.py
├── extractors/
│   ├── __init__.py
│   ├── event.py
│   ├── temporal.py
│   ├── comparison.py
│   ├── count.py
│   ├── logic.py
│   └── scope.py
├── resolver/
│   ├── __init__.py
│   ├── base.py
│   ├── entity.py
│   └── repository.py
├── composer.py
├── validator.py
├── parser.py
├── fallback.py
└── legacy_adapter.py

resources/
├── canonical_aliases.json
└── legacy_lexicon_patterns.json

tests/
├── test_normalizer.py
├── test_extractors.py
├── test_entity_resolver.py
├── test_composer.py
├── test_validator.py
├── test_parser.py
└── test_legacy_compatibility.py
```

## 1. Canonical enum 정의

다음과 같은 enum을 만들어줘.

```python
from enum import StrEnum


class EventType(StrEnum):
    PURCHASE = "purchase"
    LOGIN = "login"
    CART = "cart"
    SIGNUP = "signup"


class EntityType(StrEnum):
    BRAND = "brand"
    PRODUCT = "product"
    CATEGORY = "category"
    MEMBER = "member"


class ComparisonOperator(StrEnum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"


class LogicOperator(StrEnum):
    AND = "and"
    OR = "or"
    NOT = "not"


class TimeUnit(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"


class MetricType(StrEnum):
    EVENT_COUNT = "event_count"
    SUM = "sum"
    AVERAGE = "average"
    MAXIMUM = "maximum"
    MINIMUM = "minimum"


class SortDirection(StrEnum):
    ASC = "asc"
    DESC = "desc"


class Polarity(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
```

Python 3.11 미만도 지원해야 한다면 `str, Enum` 방식으로 대체해도 된다.

## 2. Event IR 모델 정의

가능하면 dataclass 또는 Pydantic 중 하나를 사용해라.

외부 의존성을 최소화하려면 dataclass를 기본으로 사용해라.

다음 개념을 표현할 수 있어야 한다.

```python
@dataclass(frozen=True)
class RelativeTimeWindow:
    value: int
    unit: TimeUnit


@dataclass(frozen=True)
class AbsoluteDateRange:
    start: date | None
    end: date | None


@dataclass(frozen=True)
class ScopeFilter:
    entity_type: EntityType
    operator: ComparisonOperator
    value: str
    resolved_id: str | None = None
    confidence: float | None = None


@dataclass(frozen=True)
class MetricCondition:
    metric_type: MetricType
    operator: ComparisonOperator
    value: int | float


@dataclass(frozen=True)
class EventCondition:
    event_type: EventType
    polarity: Polarity = Polarity.POSITIVE
    time_window: RelativeTimeWindow | AbsoluteDateRange | None = None
    scopes: tuple[ScopeFilter, ...] = ()
    metric: MetricCondition | None = None


@dataclass(frozen=True)
class EventIR:
    target_entity: EntityType
    condition: EventCondition
```

복수 조건을 지원하기 위해 추후 다음 구조로 확장 가능하게 설계해라.

```python
@dataclass(frozen=True)
class ConditionGroup:
    operator: LogicOperator
    children: tuple["ConditionNode", ...]


ConditionNode = EventCondition | ConditionGroup
```

순환 타입 힌트가 문제되면 `from __future__ import annotations`를 사용해라.

## 3. Alias 사전 단순화

현재처럼 문맥별 vocabulary를 많이 만들지 말고, canonical alias만 관리해라.

예시 파일:

```json
{
  "event": {
    "purchase": ["구매", "구입", "주문", "결제", "사다"],
    "login": ["로그인", "접속", "방문"],
    "cart": ["장바구니", "카트"],
    "signup": ["가입", "회원가입", "등록"]
  },
  "entity": {
    "member": ["회원", "고객", "사용자", "유저", "고객님", "사람"],
    "brand": ["브랜드"],
    "product": ["상품", "제품", "품목"],
    "category": ["카테고리"]
  },
  "comparison": {
    "gte": ["이상", "보다 많", "넘"],
    "lte": ["이하", "보다 적"],
    "gt": ["초과"],
    "lt": ["미만"],
    "eq": ["정확히", "딱"]
  },
  "logic": {
    "and": ["그리고", "및", "동시에", "이면서"],
    "or": ["또는", "혹은", "아니면"],
    "not": ["제외", "아닌", "없"]
  }
}
```

주의사항:

* `샀`, `산`, `사서`, `사고`를 각각 alias로 추가하지 마라.
* 정규화 단계에서 가능한 경우 `사다`로 변환해라.
* 완벽한 한국어 형태소 분석기를 직접 구현하지 마라.
* 우선은 제한된 규칙 기반 정규화기를 만들고, 형태소 분석기 연동이 가능하도록 인터페이스를 분리해라.
* alias 목록은 의미가 같은 표현만 포함해야 한다.
* 문맥에 따라 의미가 달라지는 표현은 alias에 강제로 넣지 마라.

## 4. 정규화기 구현

다음 인터페이스를 만들어줘.

```python
class TextNormalizer(Protocol):
    def normalize(self, text: str) -> str:
        ...
```

기본 구현체:

```python
class RuleBasedKoreanNormalizer:
    def normalize(self, text: str) -> str:
        ...
```

처리 항목:

* Unicode 정규화
* 중복 공백 제거
* 영문 소문자 변환
* 숫자 표현 정규화
* 제한적인 한국어 수사 변환
* 일부 불규칙 활용 정규화
* 문장 부호 정리

최소한 다음은 처리해라.

```text
두 번 → 2번
세 회 → 3회
한 건 → 1건
이번달 → 이번 달
지난달 → 지난 달
샀다 → 사다
샀던 → 사다
산 고객 → 사다 고객
```

단, 무리하게 문자열 전체를 치환해서 상품명이나 브랜드명을 훼손하면 안 된다.

정규화 결과와 원문의 위치 정보를 연결할 수 있도록 가능하면 다음 모델도 고려해라.

```python
@dataclass(frozen=True)
class NormalizedText:
    original: str
    normalized: str
```

초기 버전에서는 위치 매핑을 생략해도 되지만 확장 가능한 구조로 작성해라.

## 5. 의미 토큰 모델

각 extractor는 곧바로 Event IR을 만들지 말고, 중간 의미 토큰을 반환하게 해라.

```python
@dataclass(frozen=True)
class SemanticToken:
    kind: str
    value: object
    start: int
    end: int
    raw_text: str
    confidence: float = 1.0
```

가능한 kind 예시:

```text
EVENT
TARGET
TIME_WINDOW
DATE_RANGE
ENTITY_TYPE
ENTITY_VALUE
COUNT_CONDITION
AGGREGATE
POLARITY
LOGIC
SORT
```

문장:

```text
최근 3개월 동안 나이키를 두 번 이상 산 고객
```

예상 토큰:

```python
[
    SemanticToken(
        kind="TIME_WINDOW",
        value=RelativeTimeWindow(value=3, unit=TimeUnit.MONTH),
        start=0,
        end=6,
        raw_text="최근 3개월",
    ),
    SemanticToken(
        kind="ENTITY_VALUE",
        value="나이키",
        start=10,
        end=13,
        raw_text="나이키",
    ),
    SemanticToken(
        kind="COUNT_CONDITION",
        value=MetricCondition(
            metric_type=MetricType.EVENT_COUNT,
            operator=ComparisonOperator.GTE,
            value=2,
        ),
        start=15,
        end=21,
        raw_text="두 번 이상",
    ),
    SemanticToken(
        kind="EVENT",
        value=EventType.PURCHASE,
        start=22,
        end=23,
        raw_text="산",
    ),
    SemanticToken(
        kind="TARGET",
        value=EntityType.MEMBER,
        start=24,
        end=26,
        raw_text="고객",
    ),
]
```

정확한 start/end 값은 구현 결과에 따라 달라도 된다.

## 6. Extractor 분리

각 extractor는 하나의 책임만 가져야 한다.

예시 인터페이스:

```python
class TokenExtractor(Protocol):
    def extract(self, text: str) -> list[SemanticToken]:
        ...
```

구현할 extractor:

```text
EventExtractor
TemporalExtractor
ComparisonExtractor
CountExtractor
LogicExtractor
ScopeExtractor
TargetExtractor
PolarityExtractor
```

예를 들어 `TemporalExtractor`는 다음을 처리한다.

```text
최근 7일
최근 3개월
지난 2주
오늘
어제
이번 달
지난 달
2026-07-01부터 2026-07-31까지
```

`CountExtractor`는 다음을 처리한다.

```text
2회 이상
3건 이하
정확히 1번
한 번도 없음
```

`EventExtractor`는 이벤트 표현만 추출한다.

```text
구매
구입
주문
결제
샀다
로그인
접속
방문
가입
등록
장바구니
```

## 7. 엔티티 값을 사전에서 분리

`나이키`, `아디다스`, 특정 상품명 등을 alias 파일에 추가하지 마라.

다음 repository 인터페이스를 구현해라.

```python
@dataclass(frozen=True)
class EntityCandidate:
    entity_type: EntityType
    entity_id: str
    canonical_name: str
    score: float


class EntityRepository(Protocol):
    def search(
        self,
        text: str,
        entity_types: tuple[EntityType, ...] | None = None,
    ) -> list[EntityCandidate]:
        ...
```

테스트용 구현체:

```python
class InMemoryEntityRepository:
    def __init__(self, entities: list[EntityCandidate]) -> None:
        self._entities = entities

    def search(
        self,
        text: str,
        entity_types: tuple[EntityType, ...] | None = None,
    ) -> list[EntityCandidate]:
        ...
```

resolver 인터페이스:

```python
class EntityResolver:
    def __init__(self, repository: EntityRepository) -> None:
        self._repository = repository

    def resolve(
        self,
        text: str,
        tokens: list[SemanticToken],
    ) -> list[SemanticToken]:
        ...
```

엔티티 후보가 여러 개이면 임의로 확정하지 말고 ambiguity 정보를 반환할 수 있도록 설계해라.

예시:

```python
@dataclass(frozen=True)
class EntityResolution:
    raw_value: str
    candidates: tuple[EntityCandidate, ...]
    selected: EntityCandidate | None
```

## 8. Composer 구현

Composer는 SemanticToken을 Event IR로 조합한다.

```python
class EventIRComposer:
    def compose(
        self,
        text: str,
        tokens: list[SemanticToken],
    ) -> EventIR:
        ...
```

Composer 책임:

* 이벤트와 시간 조건 연결
* 이벤트와 횟수 조건 연결
* 엔티티 값과 엔티티 타입 연결
* 대상 엔티티 결정
* 부정 조건 연결
* AND, OR, NOT 그룹 구성
* 충돌하는 토큰 탐지
* 필수 정보 누락 탐지

Composer는 문자열 표현을 직접 해석하지 말고 extractor가 만든 토큰을 조합하는 역할만 해야 한다.

## 9. Validator 구현

다음 검증을 수행해라.

```python
class EventIRValidator:
    def validate(self, ir: EventIR) -> list[str]:
        ...
```

검증 예시:

* 이벤트 종류가 없는 경우
* count 값이 음수인 경우
* time window 값이 0 이하인 경우
* scope에 entity type이 없는 경우
* comparison operator와 값 타입이 맞지 않는 경우
* 동일 조건에서 서로 충돌하는 범위가 있는 경우
* resolution confidence가 기준 이하인 경우

가능하면 문자열 오류 대신 구조화된 오류를 사용해라.

```python
@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
    path: str
    severity: str
```

## 10. 최상위 Parser 구현

사용법은 단순해야 한다.

```python
parser = EventParser(
    normalizer=RuleBasedKoreanNormalizer(),
    extractors=[
        TemporalExtractor(),
        CountExtractor(),
        EventExtractor(alias_registry),
        TargetExtractor(alias_registry),
        ScopeExtractor(alias_registry),
        LogicExtractor(alias_registry),
        PolarityExtractor(alias_registry),
    ],
    entity_resolver=EntityResolver(repository),
    composer=EventIRComposer(),
    validator=EventIRValidator(),
)

result = parser.parse(
    "최근 3개월 동안 나이키를 두 번 이상 산 고객"
)
```

반환 모델:

```python
@dataclass(frozen=True)
class ParseResult:
    original_text: str
    normalized_text: str
    tokens: tuple[SemanticToken, ...]
    ir: EventIR | None
    issues: tuple[ValidationIssue, ...]
    fallback_required: bool
```

예상 IR:

```json
{
  "target_entity": "member",
  "condition": {
    "event_type": "purchase",
    "polarity": "positive",
    "time_window": {
      "value": 3,
      "unit": "month"
    },
    "scopes": [
      {
        "entity_type": "brand",
        "operator": "eq",
        "value": "나이키",
        "resolved_id": "brand:nike",
        "confidence": 1.0
      }
    ],
    "metric": {
      "metric_type": "event_count",
      "operator": "gte",
      "value": 2
    }
  }
}
```

## 11. Semantic fallback

규칙으로 처리할 수 없는 문장만 fallback으로 보낸다.

```python
class SemanticFallback(Protocol):
    def parse(
        self,
        text: str,
        partial_tokens: list[SemanticToken],
    ) -> EventIR | None:
        ...
```

초기 구현은 실제 LLM을 호출하지 않아도 된다.

다음 더미 구현만 만들어도 된다.

```python
class NullSemanticFallback:
    def parse(
        self,
        text: str,
        partial_tokens: list[SemanticToken],
    ) -> EventIR | None:
        return None
```

fallback 조건을 명확히 해라.

예시:

* 이벤트를 찾지 못함
* 동일한 표현이 여러 의미로 해석됨
* 엔티티 후보가 여러 개이고 선택할 근거가 부족함
* 토큰은 찾았지만 유효한 IR로 조합할 수 없음
* 지원하지 않는 복합 조건이 포함됨

## 12. 기존 시스템과의 호환

현재 `legacy_lexicon_patterns.json`을 당장 삭제하지 마라.

다음 adapter를 만들어라.

```python
class LegacyPatternAdapter:
    def extract(self, text: str) -> list[SemanticToken]:
        ...
```

새 extractor가 먼저 동작하고, 새 시스템이 인식하지 못한 부분에 대해서만 legacy adapter를 사용할 수 있게 해라.

우선순위:

```text
새 정규화기
→ 새 extractor
→ entity resolver
→ composer
→ legacy adapter 보완
→ semantic fallback
```

legacy adapter가 반환한 토큰은 confidence를 낮게 설정해라.

```python
confidence=0.6
```

## 13. 절대 하지 말아야 할 것

다음 방식으로 구현하지 마라.

```python
if "샀" in text:
    event = "purchase"
elif "산" in text:
    event = "purchase"
elif "사서" in text:
    event = "purchase"
```

다음과 같이 문장 전체 정규식을 계속 추가하지 마라.

```python
r"최근\s+\d+개월\s+동안\s+.+를\s+\d+회\s+이상\s+구매한\s+고객"
```

다음 값을 alias JSON에 추가하지 마라.

```json
{
  "brands": [
    "나이키",
    "아디다스",
    "뉴발란스"
  ]
}
```

다음처럼 넓은 vocabulary를 include한 뒤 exclude를 계속 추가하지 마라.

```json
{
  "include": ["all_member_nouns"],
  "exclude": ["사용자", "사람", "가입자"]
}
```

## 14. 테스트 케이스

pytest로 다음 테스트를 작성해라.

### 정상 사례

```text
최근 3개월 동안 나이키를 두 번 이상 산 고객
지난 7일 동안 로그인하지 않은 회원
이번 달 주문 금액이 100000원 이상인 고객
아디다스 또는 나이키 상품을 구매한 사용자
정확히 1회 구매한 회원
한 번도 로그인하지 않은 고객
```

### 표현 변형 사례

다음 문장들이 같은 canonical 이벤트로 해석되어야 한다.

```text
상품을 구매한 고객
상품을 구입한 고객
상품을 주문한 고객
상품을 결제한 고객
상품을 산 고객
```

모두 다음 이벤트를 가져야 한다.

```python
EventType.PURCHASE
```

### 엔티티 사례

```text
나이키를 구매한 고객
```

`나이키`는 alias 파일이 아니라 repository에서 다음처럼 해석되어야 한다.

```python
EntityCandidate(
    entity_type=EntityType.BRAND,
    entity_id="brand:nike",
    canonical_name="나이키",
    score=1.0,
)
```

### 애매한 사례

```text
애플을 구매한 고객
```

`애플`이 브랜드와 상품명 후보를 동시에 가질 경우 임의로 선택하지 말고 ambiguity issue를 반환해라.

### 실패 사례

```text
최근에 뭔가 많이 한 사람
```

유효한 이벤트를 특정할 수 없으므로 다음 결과가 되어야 한다.

```python
result.ir is None
result.fallback_required is True
```

## 15. 품질 요구사항

* 모든 공개 클래스와 함수에 타입 힌트를 작성해라.
* `Any` 사용을 최소화해라.
* 정규식은 미리 compile해라.
* extractor 간 전역 상태를 공유하지 마라.
* immutable dataclass를 우선 사용해라.
* 파일 로드 오류가 발생하면 명확한 예외를 발생시켜라.
* alias 파일의 schema validation을 추가해라.
* 중복 alias를 탐지해라.
* 하나의 alias가 서로 다른 canonical 값에 등록되면 충돌 오류를 발생시켜라.
* 로깅은 Python `logging` 모듈을 사용해라.
* 테스트 가능한 순수 함수를 우선 사용해라.
* 코드에 한국어 예제를 포함해도 되지만 변수명과 클래스명은 영어로 작성해라.
* 핵심 로직에 설명 주석을 작성해라.
* README에 아키텍처와 확장 방법을 작성해라.

## 16. 구현 순서

다음 순서로 구현해라.

1. enum과 IR dataclass
2. alias registry와 JSON loader
3. 정규화기
4. SemanticToken
5. event, temporal, count extractor
6. target, scope, polarity extractor
7. entity repository와 resolver
8. composer
9. validator
10. 최상위 parser
11. legacy adapter
12. semantic fallback 인터페이스
13. pytest 테스트
14. README

각 단계가 완료될 때마다 실행 가능한 상태를 유지해라.

최종 답변에는 다음을 포함해라.

1. 생성하거나 수정한 파일 목록
2. 핵심 아키텍처 설명
3. 전체 코드
4. 실행 명령어
5. pytest 실행 명령어
6. 기존 방식에서 새 방식으로 이전하는 방법
7. 아직 지원하지 않는 한국어 표현과 한계

우선 최소 동작 버전을 완성해라.

최소 동작 버전에서 반드시 처리해야 하는 문장은 다음이다.

```text
최근 3개월 동안 나이키를 두 번 이상 산 고객
```

최소 동작 버전의 예상 결과는 다음과 같다.

```python
ParseResult(
    original_text="최근 3개월 동안 나이키를 두 번 이상 산 고객",
    normalized_text="최근 3개월 동안 나이키를 2번 이상 사다 고객",
    tokens=(...),
    ir=EventIR(
        target_entity=EntityType.MEMBER,
        condition=EventCondition(
            event_type=EventType.PURCHASE,
            polarity=Polarity.POSITIVE,
            time_window=RelativeTimeWindow(
                value=3,
                unit=TimeUnit.MONTH,
            ),
            scopes=(
                ScopeFilter(
                    entity_type=EntityType.BRAND,
                    operator=ComparisonOperator.EQ,
                    value="나이키",
                    resolved_id="brand:nike",
                    confidence=1.0,
                ),
            ),
            metric=MetricCondition(
                metric_type=MetricType.EVENT_COUNT,
                operator=ComparisonOperator.GTE,
                value=2,
            ),
        ),
    ),
    issues=(),
    fallback_required=False,
)
```
