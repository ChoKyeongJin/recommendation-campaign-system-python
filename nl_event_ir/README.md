# nl_event_ir — 자연어 → Canonical Event IR

한국어 타게팅 문장을 canonical enum 과 typed value 로만 이루어진 Event IR 로 바꾸는 파이프라인.

> **패키지 이름 주의.** 요청서의 디렉터리 이름은 `event_ir/` 였으나, 이 저장소에는 이미 루트 모듈
> `event_ir.py`(조건 대수 IR)가 있고 **110개 파일이 그것을 import** 한다. 같은 이름의 패키지를 만들면
> 파이썬이 패키지를 우선 해석해 기존 모듈을 가리고 저장소가 통째로 깨진다. 그래서 이름만 `nl_event_ir`
> (natural-language Event IR)로 두었고 내부 구조는 요청서 그대로다. 두 계층의 역할은 다르다.
>
> | | 역할 |
> |---|---|
> | `nl_event_ir/` (이 패키지) | 자연어 **프런트엔드**. 문장 → Event IR |
> | `event_ir.py` (기존) | 조건 **대수 IR**. IR → 검증 → `event_compiler` → SQL |

---

## 1. 이 리팩터링이 고치는 것

기존 구조는 표현이 하나 늘 때마다 `parser_lexicon.json` 에 어휘를 한 줄 더하는 방식이었고, 그 결과
어휘 하나가 문맥별로 갈라졌다.

```json
"purchase_verb":                            ["구매", "구입", "주문", "샀"],
"purchase_verb_completed_stem":             ["샀"],
"purchase_verb_colloquial":                 ["산", "사다"],
"purchase_verb_colloquial_object_bound":    ["사서", "사고"]
```

원인은 사전이 **두 가지 일**을 겸한 것이다.

1. 표면어 → canonical 의미 (`구입` → `purchase`) — 사전이 할 수 있는 일
2. 문맥별 판정 (`사서`가 구매인가) — 사전이 **할 수 없는** 일

(2)를 낱말 목록으로 흉내 내면 목록이 무한히 갈라진다. 그래서 이 패키지는 (1)만 사전에 두고, (2)를
구조로 옮긴다.

| 예전에 사전이 하던 일 | 이제 누가 하는가 |
|---|---|
| 활용형 나열 (`샀`/`산`/`샀던`) | `normalizer.py` 규칙 2개 |
| 브랜드·상품명 나열 | `resolver/` + 외부 repository |
| `exclude` 로 예외 빼기 | extractor 의 구조 조건 + parser 의 스팬 중재 |
| 문맥별 어휘 분화 | composer 의 귀속 규칙 |

**결과**: alias 파일은 canonical 값 매핑 8개 절만 남았고, 활용형·데이터 값·문맥 예외는 한 줄도 없다.

---

## 2. 파이프라인

```text
원문
 → 텍스트 전처리·형태 정규화     normalizer.py
 → 의미 토큰 추출                extractors/*.py
 → 스팬 중재                     parser.py
 → 잔여 구간 엔티티 후보          extractors/scope.py (extract_residual)
 → 레거시 보완(선택)              legacy_adapter.py
 → 엔티티 해석                   resolver/
 → 의미 토큰 조합                composer.py
 → IR 검증                       validator.py
 → semantic fallback             fallback.py
```

각 단계는 앞뒤 단계를 모른다. 그래서 단계마다 단독으로 테스트한다(`tests/test_*.py` 가 단계별로 1:1).

### 단계별 책임

| 파일 | 책임 | 하지 않는 일 |
|---|---|---|
| `normalizer.py` | 유니코드·공백·수사·활용 정규화 | 의미 판정 |
| `extractors/*.py` | 자기 축의 토큰만 추출 | 다른 축 참조, IR 생성 |
| `resolver/` | 데이터 값 접지 | 문법 판정 |
| `composer.py` | 토큰 → IR 조합, 귀속 결정 | 문자열 재파싱 |
| `validator.py` | IR 의 업무 규칙 검증 | 조합 |
| `parser.py` | 단계 연결 + 스팬 중재 | 표면어 규칙 |

---

## 3. 빠른 시작

```python
from datetime import date

from nl_event_ir import EntityCandidate, EntityType, InMemoryEntityRepository
from nl_event_ir.parser import build_default_parser

repository = InMemoryEntityRepository(
    [EntityCandidate(EntityType.BRAND, "brand:nike", "나이키", 1.0)]
)
parser = build_default_parser(repository, reference_date=date(2026, 8, 5))

result = parser.parse("최근 3개월 동안 나이키를 두 번 이상 산 고객")
print(result.normalized_text)   # 최근 3개월 동안 나이키를 2번 이상 사다 고객
print(result.ir.to_dict())
```

```json
{
  "target_entity": "member",
  "condition": {
    "event_type": "purchase",
    "polarity": "positive",
    "time_window": {"value": 3, "unit": "month"},
    "scopes": [
      {"entity_type": "brand", "operator": "eq", "value": "나이키",
       "resolved_id": "brand:nike", "confidence": 1.0}
    ],
    "metric": {"metric_type": "event_count", "operator": "gte", "value": 2}
  }
}
```

기준 시각은 **주입한다**. `reference_date` 없이 `오늘`·`이번 달` 을 만나면 뜻이 정해지지 않으므로
토큰을 만들지 않는다(추측하지 않는다).

---

## 4. 확장하는 법

### 4.1 같은 뜻의 새 말투 → alias 한 줄

```jsonc
// resources/canonical_aliases.json
"event": { "purchase": ["구매", "구입", "주문", "결제", "사다", "구매완료"] }
```

코드 변경 없음. 단, **활용형은 넣지 않는다**(`구매한`/`구매했던`은 `구매` 부분 일치로 이미 잡힌다).

### 4.2 새 사건 종류 → enum + alias

```python
# enums.py
class EventType(StrEnum):
    ...
    REFUND = "refund"
```
```jsonc
"event": { "refund": ["환불", "반품"] }
```

alias 파일에만 추가하고 enum 을 빠뜨리면 로딩이 **실패한다**(사전과 코드가 어긋난 채 도는 것을 막는다).

### 4.3 새 브랜드·상품 → **아무것도 하지 않는다**

카탈로그 repository 에 데이터가 있으면 그날로 인식된다. 사전 수정도 배포도 필요 없다.
이것이 데이터 값을 사전에서 뺀 이유다.

### 4.4 새 활용형 → 정규화 규칙

`normalizer.py` 의 `_DEFAULT_RULES` 에 `NormalizationRule` 하나를 추가한다. 규칙에는 반드시 한글
경계 조건(`(?<![가-힣])` / `(?![가-힣])`)을 붙인다 — 없으면 상품명·지명이 깨진다.

### 4.5 실제 카탈로그 붙이기

```python
class CatalogRepository:            # EntityRepository 프로토콜만 만족하면 된다
    def search(self, text, entity_types=None) -> list[EntityCandidate]: ...
```

### 4.6 LLM fallback 붙이기

```python
class LlmFallback:                  # SemanticFallback 프로토콜
    def parse(self, text, partial_tokens) -> EventIR | None: ...

parser = build_default_parser(repository, fallback=LlmFallback())
```

산출물은 반드시 같은 `EventIR` 스키마여야 한다. 자유 형식 JSON 은 받지 않는다.

---

## 5. 설계 규칙(어기면 예전 구조로 되돌아간다)

1. **IR 에 한국어 표면어를 넣지 않는다.** `ScopeFilter.value` 만 예외이며, 그것은 표현이 아니라 데이터 값이다.
2. **활용형을 사전에 넣지 않는다.** 활용은 목록이 아니라 규칙이다.
3. **데이터 값을 사전에 넣지 않는다.** 넣는 순간 사전이 카탈로그의 낡은 사본이 된다.
4. **불용어/exclude 목록을 만들지 않는다.** 잔여 구간 후보의 진위는 repository 가 판정한다.
5. **모르면 만들지 않는다.** 모호하면 `ir=None` + `fallback_required=True` + 구체적 `issue`.
6. **문장 유형별 정규식을 추가하지 않는다.** 새 문장은 기존 축의 새 조합이어야 한다.

---

## 6. 실행

```powershell
# 테스트 (이 머신은 asyncio DLL 이 차단돼 있어 -p no:debugging 이 필요하다)
.\.venv\Scripts\python.exe -m pytest tests/test_normalizer.py tests/test_extractors.py `
  tests/test_entity_resolver.py tests/test_composer.py tests/test_validator.py `
  tests/test_parser.py tests/test_legacy_compatibility.py -q -p no:debugging

# 데모
$env:PYTHONIOENCODING="utf-8"; .\.venv\Scripts\python.exe -m nl_event_ir.demo
```

---

## 7. 기존 시스템에서 이전하기

기존 파서를 지우지 않는다. 순서는 다음과 같다.

1. **병행 실행.** 새 파서를 붙이고 기존 경로와 결과를 비교(shadow)한다. 아직 아무것도 바꾸지 않는다.
2. **레거시 보완 켜기.** `build_default_parser(..., use_legacy_adapter=True)`.
   새 규칙이 읽지 못한 자리에서만 기존 사전이 동작하고, 그 토큰은 `confidence=0.6`·`source="legacy"` 다.
3. **진척 측정.** `result.diagnostics["legacy_token_count"]` 가 이행 진척도다. 이 값이 큰 문장 유형이
   다음에 규칙으로 옮길 대상이다.
4. **어휘 이관.** `parser_lexicon.json` 의 항목을 하나씩 판정한다.
   - 같은 뜻의 다른 말 → `canonical_aliases.json` 으로
   - 활용형 → `normalizer.py` 규칙으로
   - 데이터 값 → repository 로
   - 문맥 판정 → extractor 구조로
5. **레거시 제거.** `legacy_token_count` 가 0 으로 수렴하면 `legacy_adapter.py` 와
   `resources/legacy_lexicon_patterns.json` 을 함께 지운다. 원본 사전은 그때까지 남긴다.

---

## 8. 아직 지원하지 않는 표현 (알려진 한계)

정직하게 적는다. 아래는 **틀리게 처리하는 것이 아니라 처리하지 않는다** — `fallback_required=True` 로 드러난다.

### 정규화

- **`산` 오탐(알려진 트레이드오프).** 요구 사양이 `산 고객 → 사다 고객` 을 명시적으로 요구하므로
  단독 어절 `산` 을 구매 동사로 읽는다. 그 대가로 `산 정상 근처 매장` 의 `산`(山)도 구매로 읽힌다.
  경계 조건(`(?<![가-힣])`/`(?![가-힣])`)이 `부산`·`계산`·`등산`·`산에` 는 지키지만, **띄어쓴 단독
  `산`은 문맥 없이 구분할 수 없다.** 이 동작은 테스트로 고정되어 있으므로 정책을 바꾸려면
  `test_normalizer.py` 의 해당 테스트를 함께 바꾸면 된다.
- `사서`·`사고` — `사고`는 '事故'와 동음이라 문맥 없이 구분 불가. 규칙으로 바꾸지 않는다.
  다만 **레거시 어댑터를 켜면 복구된다**(기존 사전의 `purchase_verb_colloquial_object_bound`).
- 붙여 쓴 `나이키샀다` — `샀` 앞에 한글이 오면 다른 낱말의 일부로 보고 건드리지 않는다.
- `삽니다`·`구입하신` 등 존대·복합 어미.
- 오탈자·띄어쓰기 오류 교정 없음.

### 시간

- `올해`·`작년`·`상반기`·`분기`·`주말`·`영업일`.
- `2026년 7월` 형태의 한국어 연월 표기(ISO `2026-07-01` 만 지원).
- `3월` 은 '3개월'이 아니라 달 이름이므로 **의도적으로** 읽지 않는다.
- 시각(`오후 3시`), 요일, 타임존.
- 지원함: `최근 N일/주/개월/년`, `N일 동안/이내`, `일주일`, `한 달`, `두 달간`, `3주일`,
  `오늘`/`어제`/`그제`, `이번 달`/`지난 달`, `YYYY-MM-DD부터 ~까지`.

### 수량·집계

- `10만원` 이상의 큰 단위 조합(`1억 5천만원`).
- `평균 구매액이 3만원 이상인 달이 2번 이상` 같은 중첩 집계.
- 상위 N%·퍼센타일(`rank.top_percent`) — 이 저장소의 `targeting_ir` 가 이미 소유하는 축이라
  여기서 중복 구현하지 않았다. `상위 N명`(개수 한정)은 지원한다.
- 한 사건에 서로 다른 집계 두 개(`2회 이상 그리고 5회 이하`)는 IR 에 담을 자리가 없어
  `metric.conflict` 로 fail-close 한다.

### 논리·구조

- 3개 이상 조건의 중첩 괄호(`(A 또는 B) 그리고 C`) — 평평한 AND/OR 만 만든다.
- `A를 사고 B는 안 산` 처럼 한 절에 극성이 섞인 나열.
- `~한 사람 중에서` 같은 집합 한정(`clause_scope_marker`).
- 상관 조건(`첫 구매 후 30일 이내 재구매`) — 기존 `event_ir.TemporalRelation` 의 축이다.

### 엔티티

- 한 글자 브랜드·상품명(조사 잔여물과 구분 불가).
- 조사가 두 번 붙은 형태(`나이키에서의`) — 조사는 한 번만 절삭한다.
- 띄어쓴 다어절 상품명(`나이키 에어 맥스 270`) — 어절 단위 후보만 만든다.

### 구조적 한계

- 위치 매핑 기준 좌표계는 **NFKC 정규화된 원문**이다. NFKC 가 길이를 바꾸는 입력(전각 호환 문자)에서는
  원문 인덱스와 어긋날 수 있다.
- `_arbitrate_spans` 는 '긴 스팬이 이긴다' 하나로만 중재한다. 길이가 같고 의미가 다른 두 해석이
  겹치는 경우는 `_KIND_PRIORITY` 선언 순서로 결정되며, 이는 문맥을 보지 않는다.
- 엔티티 값 후보는 **어절 단위**다. 조사는 한 번만 절삭하고, 절삭 결과가 두 글자 미만이면
  절삭하지 않는다(`진로`·`이도` 같은 두 글자 브랜드 보호).

---

## 9. fail-close 목록 — 조용히 넘어가지 않는 것들

이 파서에서 가장 위험한 실패는 예외가 아니라 **조건이 사라진 채 IR 이 정상처럼 보이는 것**이다.
대상이 수백 배로 늘어나도 결과만 보면 알 수 없다. 그래서 아래 상황은 전부 `ir=None` +
`fallback_required=True` + 구체적 `issue` 로 끝난다.

| 상황 | issue code |
|---|---|
| 사건을 못 찾음 | `event.missing` |
| 엔티티가 모호(브랜드/상품 양쪽 후보) | `entity.ambiguous` |
| 목적격으로 지목된 값이 카탈로그에 없음 | `scope.value_not_found` |
| 형태는 맞지만 값이 안 되는 구간(`2026-02-30`, 개수 없는 `상위`) | `expression.unparsed` |
| 배제어가 어떤 값에도 안 붙음 | `logic.not_unattached` |
| 한 사건에 서로 다른 집계 둘 | `metric.conflict` |
| 기간이 0 이하 / 날짜 구간 역전 | `time_window.non_positive`, `date_range.reversed` |
| 접지 안 된 scope 로 조회 시도 | `scope.unresolved` |

경고(진행은 하되 표시)로 남는 것:

| 상황 | issue code |
|---|---|
| 대상 명사가 없어 회원으로 가정 | `target.defaulted` |
| 집계 표지 없는 금액을 기간 합계로 읽음 | `metric.aggregation_assumed` |
| 한 사건에 기간 표현이 둘 | `time_window.multiple` |
| 접지 신뢰도가 기준 미만 | `scope.low_confidence` |
