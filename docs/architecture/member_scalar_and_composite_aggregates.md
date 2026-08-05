# 회원별 스칼라 지표와 복합 집계 (canonical Event IR)

2026-08-05 SemanticPlanV2 폐기 때 함께 죽은 세 축 중 **둘이 돌아왔다**. 돌아온 근거는 옛 계층의
복원이 아니라 **canonical Event IR 이 그 뜻을 표현하게 됐다**는 것이다. 그 구분이 이 문서의 전부다.

| 축 | 상태 | 근거 |
|---|---|---|
| 등급/상태 이력·전이 | **폐기 유지** | 표현은 되지만 `member_state_history` 의무에 컴파일 영수증을 발급하는 경로가 없다 |
| 프로필 스칼라 지표(9종) | 복귀 | 카탈로그에 회원별 스칼라 metric kind 를 추가했다(코드 확장 아님) |
| 캠페인당 평균 구매금액 | 복귀 | Event IR 에 행 값(`tuple`)과 0 값 가드(`null_if`)를 추가했다 |

---

## 1. 전역 순위 지표와 회원별 스칼라 지표는 다른 계약이다

`CRM_MB_MONTHCRMINFO` 의 같은 컬럼에 두 계약이 걸려 있고, **둘을 한 kind 로 두면 안 된다**.

| | `member_metric_<id>` | `member_scalar_<id>` |
|---|---|---|
| `kind` | `aggregate` | `member_scalar` |
| `cardinality` | `set` | `scalar`(회원당 0 또는 1) |
| entity grain | `subject` | `subject` |
| 순위 의미 | 있음(`ranking_*` 레시피) | **없음** |
| 집계 함수 | 있음(SUM/AVG/MIN/MAX) | **없음**(값이 이미 회원 행에 있다) |
| 모집단 정책 | `active_members`, 동점자 `exact_count`, null `exclude`, 소표본 `ceil` | 해당 없음 |
| 낮추는 모양 | `Exists(semi Join(회원, Limit(Order(Summarize(전역 Source)))))` | `Exists(Filter(Source, Comparison(FieldRef, Literal)))` |
| 고정 술어 | 최신 월 + `active_members` 조인 + 값 `IS NOT NULL` | 최신 월만 |

순위는 **모집단** 개념이고 회원별 스칼라는 **회원 한 명**을 말한다. 전자를 후자의 자리에 쓰면
모집단·정렬 방향·동점자 정책이 조용히 사라진다. 그것이 폐기 당시 관측된
`catalog_contract_mismatch` 의 실제 내용이었고, 지금은 계약이 그 오용을 이름과 함께 막는다:

```text
member_scalar_metrics.validate_member_scalar_contract(catalog, "buy_cycle", ...)
→ MemberScalarContractError(catalog_contract_mismatch,
    "metric 'buy_cycle' is declared as 'aggregate', not 'member_scalar'; "
    "a global ranking metric cannot be used as a per-member scalar threshold")
```

### grain 과 cardinality

`cardinality="scalar"` 는 "회원당 결과 행이 0 또는 1"이라는 **주장**이고, 그 주장을 물리적으로
성립시키는 것은 소스 선언의 최신 월 고정(`YYYYMM = (SELECT MAX(YYYYMM) …)`)이다. 고정이 빠지면
같은 회원의 과거 월 행이 조건을 만족시켜 대상이 부푼다 — 오류가 아니라 **조용히 다른 오디언스**다.
그래서 계약 테스트가 소스의 고정 술어 존재까지 함께 잰다.

### NULL 과 0행

둘 다 선언이고 둘 다 `exclude` 다. 그리고 둘 다 **SQL 이 만드는 결말**이지 덧붙인 술어가 아니다.

* 값이 `NULL` → `EXISTS` 안의 비교가 UNKNOWN → 그 회원은 빠진다.
* 스냅샷 행 자체가 없음 → `EXISTS` 가 거짓 → 그 회원은 빠진다.

`include_as_zero`(`COALESCE(col, 0)`)는 어휘에 없다. 그 뜻을 표현하려면 IR 에 `Coalesce` 스칼라가
먼저 있어야 하고, 없는 정책을 어휘에만 올리면 선언은 통과하는데 SQL 이 다른 뜻이 된다.

### 9종과 그 단위

`docs/data/runtime/sql/member_metrics.json` 한 파일이 지표 목록을 소유하고, 두 계약이 그것을
각자 읽는다. `threshold_unit` 은 회원별 스칼라 계약만 쓴다(순위에는 임계값이 없다).

| metric_id | 컬럼 | 단위 |
|---|---|---|
| `total_buy_amt` | `TOTAL_BUY_AMT` | KRW |
| `total_buy_cnt` | `TOTAL_BUY_CNT` | count |
| `mean_buy_amt` | `MEAN_BUY_AMT` | KRW |
| `max_buy_amt` | `MAX_BUY_AMT` | KRW |
| `min_buy_amt` | `MIN_BUY_AMT` | KRW |
| `total_buy_qty` | `TOTAL_BUY_QTY` | count |
| `buy_cycle` | `BUY_CYCLE` | day |
| `activity_month_cnt` | `ACTIVITY_MONTH_CNT` | month |
| `buy_product_cnt` | `BUY_PRODUCT_CNT` | count |

단위 선언이 없는 지표는 **회원별 스칼라 계약을 갖지 않는다**(순위 계약은 그대로). 없는 선언을
오류로 만들면 순위만 쓰는 지표를 등록할 때 쓰지도 않을 단위를 지어내야 하고, 지어낸 단위는 곧
틀린 임계 비교가 된다.

### 평균 구매주기의 계산 방식과 단위

`buy_cycle` 은 **월 스냅샷에 이미 계산돼 있는 정수 컬럼**(`CRM_MB_MONTHCRMINFO.BUY_CYCLE`, 단위
`day`)이다. 이벤트 순서를 훑어 `Lag(purchase_at)` + `DateDiff` 로 다시 계산하지 **않는다** — 저장소
어디에도 그 계산이 있었던 흔적이 없고(과거 `metrics/buy_cycle.json` 도 이 컬럼을 가리킨다),
없는 계산을 지어내면 스냅샷과 다른 값이 나오면서 둘 다 그럴듯해 보인다.

그래서 window/lag IR 노드는 **추가하지 않았다**. 필요해지는 날의 조건은 명확하다: 회원별 구매
이벤트에서 주기를 직접 계산해야 하는 요구가 생기고, 그 계산의 정의(첫 구매 포함 여부·취소 주문
처리·구매 0~1회 회원의 결과가 NULL 인지 0 인지)가 **먼저 문서로 확정될 때**다.

---

## 2. 캠페인당 평균 구매금액이 `AVG` 가 아닌 이유

같은 문장에서 두 값이 나온다.

```text
"캠페인별 구매반응 금액이 평균 10만 원 이상인 회원"

캠페인 분모 평균: SUM(BUY_AMT) * 1.0 / COUNT(DISTINCT (CAMP_ID, CAMP_EXEC_NO))
반응 행당 평균:   AVG(BUY_AMT)
```

분모가 다르다 — 앞은 **서로 다른 캠페인 실행의 수**, 뒤는 **반응 행의 수**. 한 캠페인에서 세 번
반응한 회원의 금액 합이 600 이면 앞은 600, 뒤는 200 이다. 둘 다 SQL 이 나오고 둘 다 성공으로
보이므로, 조용히 바뀌면 아무도 눈치채지 못한다.

모델은 뒤를 낸다(Event IR 의 `Aggregate(avg)` 가 행 단위 평균이므로). `campaign_metric_claims` 가
카탈로그 선언 두 가지의 논리곱으로 그 모순을 판정하고 — (a) 선언 소스의 금액 필드를 행 단위로
평균 내는 집계가 표현에 있다, (b) 선언된 grain 표면어가 원문에 등장한다(어순·조사·띄어쓰기 무관) —
그 자리를 정확한 복합식으로 **바꾼다**.

```text
Arithmetic('/',
    Arithmetic('*', Aggregate(sum, campaign_purchase_response.amount), Literal(1.0)),
    NullIf(Aggregate(count, distinct=True,
                     Tuple(campaign_purchase_response.campaign_id,
                           campaign_purchase_response.execution_no)),
           Literal(0)))
```

### distinct 분모의 정확한 정의

**`(CAMP_ID, CAMP_EXEC_NO)` 두 컬럼의 서로 다른 조합의 수**다. 기존 SQL 의
`COUNT(DISTINCT CONCAT(R.CAMP_ID, ':', R.CAMP_EXEC_NO))` 와 같은 키이며, 카탈로그의
`campaign_purchase_response.execution_id` 필드 식이 그 사실을 선언하고 있다.

구분자 결합 문자열을 **쓰지 않는** 이유는 충돌이다. `('A:B','C')` 와 `('A','B:C')` 는 서로 다른
키인데 결합하면 둘 다 `'A:B:C'` 다 — 분모가 절반이 되어 평균이 두 배로 부푼다. 이것이 기존
표현과 의도적으로 다른 **유일한** 자리이고, 그 차이가 IR 확장의 이유다
(`tests/test_composite_aggregate_sql_results.py::test_multi_column_distinct_beats_delimiter_concatenation`
가 두 값을 나란히 재고, 그 옆 테스트가 **충돌 사례를 뺀 나머지 전부에서 값이 같다**는 것을 잰다).

키 일부가 `NULL` 인 행은 **센다**(`SELECT DISTINCT` 가 NULL 조합을 하나의 키로 묶는다). 이는 기존
T-SQL `CONCAT`(NULL 을 빈 문자열로 접는다)의 결말과 같다.

### 분모 0 처리

`NULLIF(분모, 0)` 로 NULL 을 만들고, `NULL` 과의 비교는 UNKNOWN 이라 그 회원은 대상에서 빠진다.
기존 SQL 에는 `NULLIF` 가 없었지만 결말은 같았다(반응 행이 없으면 `SUM` 이 NULL 이라 `NULL/0` 이
되고, T-SQL 은 그것을 NULL 로 돌려준다). 명시적으로 접는 이유는 **이식성**이다 — PostgreSQL 은
0 나눗셈을 `division_by_zero` 오류로 던지므로, 접지 않으면 같은 IR 이 엔진에 따라 오류가 된다.

### 정수 나눗셈 방지

`* 1.0` 이다. `CAST(... AS DECIMAL(p,s))` 로 바꾸지 않는다 — 결과 타입과 정밀도가 기존 산출물과
달라진다. 승수는 카탈로그 선언(`decimal_multiplier`)이고 코드 상수가 아니다.

### 분자와 분모는 같은 관계를 물려받는다

모델이 낸 `Aggregate.relation`(기간 필터 포함)을 둘 다 그대로 쓴다. 새 관계를 만들면 기간이 한쪽에만
걸리거나 창이 통째로 사라지고, 그 오류는 값의 차이로만 드러난다.

---

## 3. Event IR 확장분

새 노드 **둘**뿐이고, 둘 다 문장 하나를 위한 타입이 아니라 범용 값 노드다.

| 노드 | 뜻 | 왜 조합으로는 안 되는가 |
|---|---|---|
| `Tuple(items)` | 둘 이상의 스칼라를 하나의 행 값으로 | '키가 몇 개의 값으로 이루어지는가'는 의미다. 문자열 결합으로 대신하면 값 안의 구분자·NULL 이 서로 다른 키를 같게 만든다 |
| `NullIf(expression, value)` | 그 값이면 없는 값으로 본다 | 0 분모를 접는 유일한 방언 독립 수단 |

`SafeDivide` 전용 노드는 두지 **않았다** — 안전 나눗셈은 `Arithmetic('/')` 와 `NullIf` 의 조합이고,
조합으로 표현되는 것에 타입을 주면 같은 뜻이 두 모양을 갖는다.

`Tuple` 은 아무 데나 놓을 수 있는 값이 아니다. `Aggregate(function="count", distinct=True)` 의
인자 자리에서만 유효하고(그 밖에서는 `IrSchemaError`), 그 밖의 자리에 있으면 컴파일러가
조용히 이어 붙이지 않고 멈춘다.

### capability

`event_ir.CAPABILITIES` 가 어휘의 단일 소유자다. 카탈로그는 지표별로 필요한 capability 를 선언하고,
컴파일러는 방언별로 제공 가능한 집합을 선언한다. 그래서 부족은 **SQL 생성 도중의 예외가 아니라
lowering 전의 판정 결과**로 나온다(`compiler_capability_unsupported` / `catalog_capability_unsupported`).

| capability | 뜻 |
|---|---|
| `metric.member_scalar` | 회원당 0..1 값을 읽는 지표 계약(표현 모양이 아니라 카탈로그 계약이다) |
| `scalar.arithmetic` | 스칼라 산술 |
| `scalar.tuple` | 행 값 |
| `scalar.null_if` | 값 가드 |
| `scalar.safe_divide` | `Arithmetic('/')` 의 분모가 `NullIf` 인 조합 |
| `aggregate.scalar` | 관계에 대한 스칼라 집계 |
| `aggregate.count_distinct` | 중복 제거 집계 |
| `aggregate.multi_column_count_distinct` | 행 값 기준 중복 제거 집계 |
| `aggregate.derived_expression` | 집계 결과 위의 파생식(분자/분모 조합) |
| `relation.membership_join` | semi/anti 조인 |
| `relation.ranked_limit` | 순위 상위 N/N% |

`metric.member_scalar` 의 제공자는 `member_scalar_metrics` 이고 나머지는 `event_compiler` 다.

### 방언별 다중 컬럼 distinct

**네 방언(tsql·mysql·postgres·ansi) 모두 DISTINCT 서브쿼리로 낮춘다.**

```sql
(SELECT COUNT(*) FROM (SELECT DISTINCT <k1>, <k2> FROM <source> WHERE <filters>) AS ED0)
```

네이티브 문법을 **쓰지 않는 것이 결정**이다. NULL 규칙이 엔진마다 다르기 때문이다:

| 엔진 | 문법 | NULL 인자를 가진 행 |
|---|---|---|
| MySQL/MariaDB | `COUNT(DISTINCT a, b)` | **세지 않는다** |
| PostgreSQL | `COUNT(DISTINCT (a, b))` | **센다**(행 값 자체는 NULL 이 아니다) |
| T-SQL / ANSI | 없음 | — |

같은 IR 이 방언에 따라 다른 수를 세면 그것은 최적화가 아니라 조용한 오답이다. 서브쿼리 형태는
세 엔진 모두에서 NULL 조합을 하나의 키로 세고, 그것이 기존 T-SQL `CONCAT` 렌더의 결말과 같다.

---

## 4. 지원하지 않는 것(폐기 유지)

* **등급/상태 이력·전이**(`member_grade_transition` 등). 카탈로그 선언은 남아 있지만
  `member_state_history` 의무에 컴파일 영수증을 발급하는 경로가 없어 어떤 요청도 해소되지 않는다.
  되살리려면 (1) 그 경로가 먼저 서야 하고, (2) '승급'은 등급 서열 비교이므로 코드값 사전순 비교로
  컴파일되지 않는지 따로 확인해야 한다. `tests/test_retired_axes_fail_close.py` 가 이 축의 fail-close 를
  고정하고, `tests/test_revived_axes_event_ir_only.py` 가 이번 복귀 작업이 그 축까지 열지 않았음을 잰다.
* **Event IR 실패 시의 폴백 경로**. 없다. 계약이 어긋나면 합성하지 않고 모델의 미지원 신고가 그대로
  남아 fail-close 한다 — 비슷한 지표로 갈아타지 않는다.
* **`include_as_zero` null 정책**, **소수 임계값의 Decimal 통과**. 둘 다 선언 어휘 밖이고, 필요해지면
  각각 `Coalesce` 스칼라와 IR 리터럴의 Decimal 표기가 먼저 있어야 한다.
