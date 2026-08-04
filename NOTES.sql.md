# Campaign Event IR SQL 성능 진단 및 애플리케이션 측 최적화 인수인계

작성일: 2026-08-04  
상태: 진단 및 구현 계획만 완료. 제품 소스와 DB 스키마/인덱스/통계는 변경하지 않음.

## 1. 요청과 제약

대상 자연어:

```text
최근 5일 동안 캠페인 발송 성공 횟수가 3회 이상인 회원
```

현재 생성 SQL:

```sql
SELECT DISTINCT
    B.MEMBER_NO AS CUST_ID,
    B.EMART_GRADE_CD AS member_grade,
    '최근 5일 캠페인 발송 성공 count >= 3' AS segment_label
FROM CRM_MB_BASEINFO B
WHERE B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'
  AND (
      SELECT COUNT(DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO))
      FROM Z_CAMP_MBR M
      INNER JOIN Z_CAMPAIGN ZC
          ON ZC.CAMP_ID = M.CAMP_ID
         AND ZC.CAMP_EXEC_NO = M.CAMP_EXEC_NO
      WHERE TRY_CAST(M.MBR_NO AS BIGINT) = B.MEMBER_NO
        AND M.CELL_TYPE_CD = 'T'
        AND M.CONTAC_SUCC_YN = 'Y'
        AND ISNULL(ZC.CANCEL_YN, 'N') = 'N'
        AND ZC.CAMP_SDATE >= CONVERT(
            CHAR(8),
            DATEADD(DAY, -5, GETDATE()),
            112
        )
  ) >= 3;
```

제약:

- DB 테이블, 컬럼, 인덱스, 통계를 변경할 수 없다.
- 애플리케이션 소스의 SQL 컴파일 단계에서만 최적화한다.
- Core Event IR 모델, operator, 직렬화 형식과 의미는 변경하지 않는다.
- 생성된 SQL 문자열을 정규식으로 사후 치환하지 않는다.

## 2. `DAY` 오류 관련 확인 결과

`DATEADD(DAY, -5, GETDATE())`의 `DAY`는 컬럼이 아니라 SQL Server의 `datepart`이다.

실제 API 컨테이너의 CRMDW 연결에서 확인한 서버는 Microsoft SQL Server 2012 SP4였으며 다음 식은 정상 실행됐다.

```sql
SELECT CONVERT(CHAR(8), DATEADD(DAY, -5, GETDATE()), 112) AS cutoff;
```

2026-08-04 확인 결과는 `20260730`이었다. 원본 전체 SQL도 `SELECT DISTINCT TOP 0` 형태로 바인딩/컴파일만 수행했을 때 오류 없이 통과했다.

저장소의 `sql_guard`도 이 SQL을 `tsql`로 파싱하며, sqlglot AST에서 `DAY`는 `Column`이 아니라 `DateAdd` 아래의 `Var`로 인식한다. 따라서 별도 화면에서 나온 `invalid column reference`는 CRMDW 엔진이 이 원본 SQL의 `DAY`에 대해 낸 오류가 아니라, 외부 컬럼 검증기/린터가 T-SQL datepart를 일반 식별자로 오인한 것으로 봐야 한다. 정확한 외부 계층은 오류 원문, provider와 실제 `executed_sql`이 없어서 특정하지 못했다.

관련 소유 지점:

- `sql_dialect.py`: `TSqlDialect.date_sub_days()`가 `DATEADD(DAY, ...)`를 렌더한다.
- `event_compiler.py`: rolling `char8` 시간 창을 dialect의 `char8_cutoff()`로 내린다.
- `sql_guard.py`: 참조 테이블로부터 `tsql`을 추론하고 실제 `exp.Column`만 스키마 검증한다.

`DAY`는 성능 저하 원인도 아니다. 실행 시점 상수로 한 번 계산되는 날짜 경계 표현식이다.

## 3. 실제 CRMDW 데이터/물리 구조 스냅샷

아래 값은 2026-08-04 읽기 전용 진단 시점의 스냅샷이다.

| 테이블 | 행 수 | 확인된 인덱스 |
|---|---:|---|
| `CRM_MB_BASEINFO` | 69,609 | `MEMBER_NO` nonclustered PK |
| `Z_CAMP_MBR` | 376,964 | 없음, heap |
| `Z_CAMPAIGN` | 31 | `(CAMP_ID, CAMP_EXEC_NO)` nonclustered PK |

선택도:

- 정상 회원: 69,308 / 69,609
- `Z_CAMP_MBR` 대상군: 356,728
- 접촉 성공: 170,933
- 대상군이면서 접촉 성공: 170,933
- `TRY_CAST(MBR_NO AS BIGINT)` 실패: 165,089
- 서로 다른 원본 `MBR_NO`: 210,042
- 2026-07-30 이후 취소되지 않은 캠페인: 0
- 위 최근 캠페인과 조인되는 성공 발송 행/회원: 0 / 0

회원 상태 조건은 약 99.6%를 통과하므로 `CRM_MB_BASEINFO` 스캔 자체는 이상하지 않다. 문제는 그 약 7만 회원 각각에 대해 안쪽 집계를 반복하는 구조다.

통계도 보조 원인이다.

- `Z_CAMP_MBR.MBR_NO` 통계: 2020-01-28
- `Z_CAMP_MBR.CAMP_ID`, `CAMP_EXEC_NO`, `CELL_TYPE_CD`, `CONTAC_SUCC_YN` 통계: 대부분 2017년
- `Z_CAMPAIGN.CAMP_SDATE` 통계: 2017-02-02, 당시 10행 기준
- 현재 `Z_CAMPAIGN` 실제 행 수는 31이지만 PK 통계도 2017년 10행 기준

DB의 `AUTO_CREATE_STATISTICS`와 `AUTO_UPDATE_STATISTICS`는 켜져 있으나 작은 테이블/변경 임계값과 오래된 분포 때문에 이번 조건에 유용한 통계로 갱신되지 않았다. DB를 변경할 수 없으므로 애플리케이션 최적화가 오래된 통계에도 덜 민감한 집합형 SQL을 만들어야 한다.

## 4. 확인된 실행계획과 직접 원인

원본 SQL은 `SET SHOWPLAN_XML ON`으로 예상 실행계획만 확인했다. 실제 전체 결과 쿼리는 실행하지 않았다.

주요 수치:

- `StatementSubTreeCost`: `19118.6`
- 바깥 `CRM_MB_BASEINFO` 예상 행: 약 69,287
- 안쪽 scalar aggregate 예상 rebind: 약 69,286회
- `Index Spool` 예상 rewind: 약 69,286회
- 회원별 `Distinct Sort`: 약 69,286회
- 회원별 `Stream Aggregate`: 약 69,286회
- `Z_CAMPAIGN` PK seek 및 RID lookup: 약 69,286회

SQL Server는 인덱스가 없는 `Z_CAMP_MBR` 376,964행을 매 회원마다 물리적으로 다시 읽는 최악의 계획 대신, 한 번 스캔해 임시 `Index Spool`을 만들었다. 그러나 이 spool을 약 7만 번 조회하고, 각 회원마다 다음 작업을 반복한다.

```text
TRY_CAST(MBR_NO)
→ 캠페인 PK 조회
→ CAMP_SDATE/CANCEL_YN RID lookup
→ CONCAT(CAMP_ID, ':', CAMP_EXEC_NO)
→ DISTINCT sort
→ COUNT aggregate
```

주원인은 다음 조합이다.

1. `event_compiler._aggregate_subquery()`가 회원 상관 `Aggregate`를 scalar subquery로 렌더한다.
2. 이벤트 카탈로그의 상관식이 `TRY_CAST(M.MBR_NO AS BIGINT) = B.MEMBER_NO`이다.
3. `Z_CAMP_MBR`가 heap이고 인덱스가 전혀 없다.
4. 캐스팅이 필터 대상 컬럼에 적용돼 raw `MBR_NO`로 직접 seek하기 어렵다.
5. `COUNT(DISTINCT CONCAT(...))`의 계산/정렬/집계가 회원마다 반복된다.
6. 최근 캠페인이 실제로 0건이어도 현재 SQL 모양은 `Z_CAMPAIGN`의 최근 집합부터 시작하지 않아 조기에 종료하지 못한다.

`Z_CAMP_MBR.MBR_NO` 중 약 44%가 숫자가 아니므로 `TRY_CAST`를 단순 제거하거나 `CAST`로 교체하면 안 된다. 이 캐스팅은 데이터 이상치를 오류 대신 제외하기 위한 의미 있는 정책이다.

애플리케이션의 MSSQL 연결 timeout은 현재 15초다(`db_connections._mssql_connection`). timeout을 늘리는 것은 원인 해결이 아니며 계획에 포함하지 않는다.

## 5. 비교용 집합형 SQL의 예상계획

동일 의미를 회원별로 먼저 한 번 집계한 뒤 회원 테이블에 조인하는 비교 SQL을 작성해 예상계획만 확인했다.

```sql
WITH eligible AS (
    SELECT TRY_CAST(M.MBR_NO AS BIGINT) AS MEMBER_NO
    FROM Z_CAMPAIGN ZC
    INNER JOIN Z_CAMP_MBR M
        ON M.CAMP_ID = ZC.CAMP_ID
       AND M.CAMP_EXEC_NO = ZC.CAMP_EXEC_NO
    WHERE M.CELL_TYPE_CD = 'T'
      AND M.CONTAC_SUCC_YN = 'Y'
      AND ISNULL(ZC.CANCEL_YN, 'N') = 'N'
      AND ZC.CAMP_SDATE >= CONVERT(
          CHAR(8),
          DATEADD(DAY, -5, GETDATE()),
          112
      )
      AND TRY_CAST(M.MBR_NO AS BIGINT) IS NOT NULL
    GROUP BY TRY_CAST(M.MBR_NO AS BIGINT)
    HAVING COUNT(
        DISTINCT CONCAT(M.CAMP_ID, ':', M.CAMP_EXEC_NO)
    ) >= 3
)
SELECT
    B.MEMBER_NO AS CUST_ID,
    B.EMART_GRADE_CD AS member_grade,
    '최근 5일 캠페인 발송 성공 count >= 3' AS segment_label
FROM CRM_MB_BASEINFO B
INNER JOIN eligible E
    ON E.MEMBER_NO = B.MEMBER_NO
WHERE B.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL';
```

비교 SQL의 예상 `StatementSubTreeCost`는 `3.70648`이었다. 원본의 `19118.6`과 비교한 예상 비용 차이는 약 5,000배지만, 이 수치는 실제 실행시간 배율이 아니라 동일 DB/통계에서의 옵티마이저 비용 비교다. 중요한 구조적 차이는 `Z_CAMP_MBR` 스캔과 정렬/집계가 바깥 회원마다 반복되지 않는다는 점이다.

## 6. Event IR 비침범 원칙

최적화는 Event IR 의미 변경이 아니라 물리 SQL lowering이어야 한다.

변경하지 않을 항목:

- Core Event IR dataclass/model
- `Comparison`, `Aggregate`, `Filter`, `RollingWindow` 구조
- operator와 canonical field path
- Event IR JSON 직렬화/역직렬화 형식
- expression fingerprint와 query identity
- validation/normalization 결과
- condition token 경로와 semantic receipt
- `campaign_contact_success`, `campaign_contact_success.occurred_at`, `campaign_contact_success.execution_id`의 의미

권장 파이프라인:

```text
검증되고 정규화된 Canonical Event IR
→ aggregate-membership physical lowering
→ SQL rendering
→ 기존 sql_guard/coverage/delivery validation
```

원본 Event IR 객체를 변형하지 말고, 컴파일러 내부의 새 relational plan 또는 새 SQL candidate를 만든다. 최적화 정보가 필요하면 IR capabilities가 아니라 compiler receipt/metadata로 남기며 query identity 계산에서 제외한다.

이벤트 카탈로그에 메타데이터가 필요하다면 Core IR이 아니라 compiler-only physical binding으로 추가한다. 예:

```json
{
  "group_subject_expression": "TRY_CAST({alias}.MBR_NO AS BIGINT)"
}
```

다만 카탈로그 변경은 catalog revision/cache invalidation에는 영향을 줄 수 있으므로 해당 revision 정책과 테스트를 확인한다. `correlation_sql` 문자열을 정규식으로 역파싱해 집계키를 추측하지 않는다.

## 7. 구현 계획

### 7.1 의미 계약 테스트를 먼저 작성

현재 Event IR과 SQL 의미를 테스트로 고정한다.

- source: `campaign_contact_success`
- window: rolling 5 day
- time expression: `ZC.CAMP_SDATE`
- 대상군: `CELL_TYPE_CD = 'T'`
- 성공: `CONTAC_SUCC_YN = 'Y'`
- 취소 제외: `ISNULL(CANCEL_YN, 'N') = 'N'`
- distinct 실행키: `CONCAT(CAMP_ID, ':', CAMP_EXEC_NO)`
- threshold: `COUNT >= 3`
- 회원 상관 의미: `TRY_CAST(MBR_NO AS BIGINT) = MEMBER_NO`

최적화 전후 Event IR JSON, fingerprint, query identity와 condition token path가 동일해야 한다.

### 7.2 저비용 쿼리 fast-path와 최적화 필요성 판정

모든 Event IR을 무조건 aggregate-membership lowering이나 shadow 비교에 태우지 않는다. 컴파일 초기에 DB 조회 없이 IR/relational plan 구조만 보는 가벼운 판정기를 두고, 명백히 튜닝 대상이 아닌 쿼리는 기존 컴파일 경로로 즉시 보낸다.

권장 판정 결과:

```text
NOT_APPLICABLE  → 최적화 패스 즉시 종료, 기존 SQL을 그대로 생성
ELIGIBLE        → 의미 동치가 증명된 lowering 적용
FALLBACK        → 위험하거나 불명확하므로 기존 컴파일러 사용 + 진단만 기록
```

다음은 `NOT_APPLICABLE`로 즉시 스킵한다.

- `Aggregate`가 전혀 없는 단순 회원 속성 비교
- `subject_column`에 직접 적용되는 로그인일/가입일 등의 단일 컬럼 조건
- 상관 scalar aggregate가 없는 `EXISTS`/일반 조인
- 이미 관계 계획에서 한 번만 `GROUP BY`하도록 materialize된 집계
- 이미 aggregate-membership lowering receipt가 붙은 계획
- optimizer가 소유하지 않는 다른 도메인의 단순 SQL

대표적인 fast-path:

```text
성별 = 여성
등급 = GOLD
가입일 >= 기준일
최근 로그인일 <= 기준일
```

다음 신호가 있을 때만 `ELIGIBLE` 검사를 계속한다.

- 바깥 subject를 참조하는 fact-table scalar aggregate
- 상관 집계 내부의 cast/함수 적용 회원키
- 상관 집계 내부의 `DISTINCT`, `Sort`, 복합 실행키 계산
- 동일한 fact source를 subject마다 반복 평가할 수 있는 구조

판정기는 다음 원칙을 지킨다.

- 요청마다 `SHOWPLAN`, 행 수 조회, 통계 조회 등 DB 호출을 하지 않는다.
- 현재 테이블 행 수가 작다는 이유만으로 스킵하지 않는다. 이번 사고처럼 행 수는 작아도 상관 반복으로 비용이 폭증할 수 있다.
- 바깥 `TOP`/`LIMIT`만 보고 스킵하지 않는다. 필터가 먼저 계산되면 결과 제한이 상관 집계 비용을 줄이지 못한다.
- 불확실한 비용 추정을 위해 무거운 cost model을 실행하지 않는다. 구조적으로 명백한 패턴만 최적화한다.
- fast-path에서는 기존 SQL 문자열, candidate id, condition token, fingerprint가 바이트 수준으로 동일해야 한다.
- 스킵 진단은 `optimization_skipped`와 안정적인 reason code 정도만 compiler diagnostics에 남기고 query identity에는 포함하지 않는다.

권장 스킵 reason code 예:

```text
NO_AGGREGATE
NO_CORRELATED_AGGREGATE
SUBJECT_COLUMN_FAST_PATH
ALREADY_SET_BASED
OPTIMIZATION_ALREADY_APPLIED
UNSUPPORTED_OPTIMIZATION_SCOPE
```

이 fast-path 자체의 시간복잡도는 IR 노드 수에 대한 단일 순회 수준으로 제한한다. 단순 쿼리에 최적화 분석 비용이나 shadow DB 실행 비용이 추가되면 안 된다.

### 7.3 적용 가능한 aggregate membership 패턴을 명시적으로 판별

초기 적용 범위를 좁힌다.

- fact-table relation의 회원별 aggregate
- 등록된 단일 subject correlation
- 등록된 group subject expression
- 집계 결과가 양수인 회원만 필요한 비교
- top-level 또는 안전한 `AND` 문맥
- T-SQL에서 지원되는 집계/표현식

초기 inner-join lowering이 안전한 대표 조건:

- `COUNT >= N`, `N > 0`
- `COUNT > N`, `N >= 0`
- `COUNT = N`, `N > 0`

초기에는 다음을 최적화하지 않고 기존 컴파일러로 fail-safe fallback한다.

- `COUNT = 0`
- `COUNT <= 0`
- 이벤트가 없는 회원도 참이 되는 조건
- `!=`
- `OR`, `NOT`
- 서로 다른 기간/모집단을 가진 여러 집계의 임의 병합
- 빈 집합에서 `NULL`과 0 의미가 다른 `SUM`/`AVG`

향후 0-sensitive 조건을 지원할 경우에는 단순 inner join이 아니라 `LEFT JOIN + COALESCE` 또는 anti-join을 사용하고 별도 동치 테스트를 둔다.

### 7.4 Event IR 컴파일 직전에 physical lowering 추가

문자열 SQL을 받은 후 고치는 것이 아니라, `Aggregate(Filter(Source(...)))`와 바깥 비교를 relational membership plan으로 낮춘다.

개념적 변환:

```text
subject WHERE correlated_scalar_count(event_relation) >= N
```

에서:

```text
subject INNER JOIN (
    event_relation
    → filter
    → group by normalized subject key
    → having count >= N
) eligible_subjects
```

으로 바꾼다.

후보 구현 위치는 `event_compiler.py`의 `compile_condition()`/`_aggregate_subquery()` 진입 전이다. 기존 `RelationPlan`, `Group`, `Summarize`, `Join`을 재사용할 수 있는지 먼저 확인하고, 가능하면 새 Core IR 노드를 만들지 않는다.

### 7.5 집계 필터 pushdown

derived relation 안에 다음 필터를 집계 전에 둔다.

- `Z_CAMPAIGN` 기간
- 취소 제외
- 대상군
- 발송 성공
- 변환 불가능한 회원키 제외

`TRY_CAST(MBR_NO AS BIGINT)`를 group key로 사용하면 원본 scalar correlation과 마찬가지로 `"1"`과 `"001"`이 같은 `MEMBER_NO=1`의 이벤트로 집계된다. 변환 실패 값은 NULL group으로 모일 수 있으므로 `IS NOT NULL`로 제외한다. 이는 non-null `CRM_MB_BASEINFO.MEMBER_NO`와 절대 매칭되지 않던 기존 의미를 보존한다.

`COUNT(DISTINCT CONCAT(...))`는 우선 그대로 보존한다. 이를 `COUNT(*)`, `COUNT(DISTINCT CAMP_ID)` 또는 다른 식으로 임의 단순화하지 않는다. 복합키 pre-dedup으로 바꾸려면 CONCAT 충돌 가능성과 NULL 처리까지 별도 증명해야 한다.

### 7.6 기존 검증 파이프라인 재사용

최적화 SQL도 기존 검증을 모두 통과해야 한다.

- schema allowlist
- dialect validation
- join-key validation
- analytics shape
- condition coverage
- delivery contract
- semantic verification receipt

최적화 적용 사실은 예를 들어 다음과 같은 별도 compiler receipt로 남길 수 있다.

```json
{
  "optimization": "aggregate_membership_join",
  "source": "campaign_contact_success",
  "preserved_expression_fingerprint": "..."
}
```

### 7.7 성능 구조 가드 추가

결정론적 컴파일러가 최적화 가능하다고 판정한 경우 다음 구조가 최종 SQL에 남지 않도록 검사한다.

- 바깥 subject에 상관된 scalar aggregate
- aggregate 내부의 casted subject correlation
- subject 행마다 반복되는 `COUNT(DISTINCT ...)`

일반 LLM SQL을 무조건 차단하는 광범위 정규식이 아니라, compiler receipt와 AST를 결합한 좁은 가드로 구현한다. 적용 가능성이 없거나 의미 동치를 증명하지 못한 경우에는 unsupported로 바꾸지 말고 기존 경로를 유지하되 성능 진단을 명시적으로 남긴다.

### 7.8 테스트 매트릭스

필수 단위/통합 테스트:

- 최근 5일 성공 발송 3회 이상
- 최근 캠페인 0건
- 정확히 2회/3회/4회
- 동일 캠페인 실행의 중복 발송 행
- 동일 `CAMP_ID`의 서로 다른 `CAMP_EXEC_NO`
- 숫자가 아닌 `MBR_NO`
- 앞자리 0이 있는 `MBR_NO`
- 취소 캠페인
- `CAMP_SDATE` null
- 성공 여부 null
- threshold 0/1 경계
- 다른 회원 조건과 `AND`
- `OR` 및 `NOT`에서 최적화 미적용 확인
- 여러 집계 조건에서 기간이 같음/다름
- Event IR JSON round-trip
- expression fingerprint/query identity 불변
- 기존 condition token/coverage receipt 불변
- unsupported expression 보존
- aggregate가 없는 단순 조건에서 optimizer가 호출되지 않음
- `subject_column` fast-path에서 기존 SQL이 바이트 수준으로 동일함
- 이미 집합형인 계획을 다시 lowering하지 않음
- 스킵된 쿼리에서 shadow DB 실행이 발생하지 않음
- `TOP`/`LIMIT`이 있다는 이유만으로 위험한 상관 집계를 스킵하지 않음

DB shadow 동치 검증은 고정 기준 시각을 사용하고 다음 두 차집합이 모두 0건인지 확인한다.

```sql
original_sql EXCEPT optimized_sql
optimized_sql EXCEPT original_sql
```

실데이터를 로그에 남기지 말고 건수와 해시/차집합 존재 여부만 기록한다.

### 7.9 단계적 rollout

1. 기능 플래그를 명시적으로 주입한다. 전역 mutable state로 두지 않는다.
2. `campaign_contact_success + positive COUNT threshold`에만 우선 적용한다.
3. shadow 모드에서 기존/최적화 결과 집합을 비교한다.
4. 불일치 시 자동으로 기존 SQL을 사용하고 최적화 실패 receipt를 남긴다.
5. 충분한 동치 증거 후 최적화 SQL을 primary로 전환한다.
6. 이후 같은 물리 패턴의 다른 이벤트 집계로 확대한다.

## 8. 완료 기준

기능 기준:

- Core Event IR 및 직렬화 변경 없음
- 최적화 전후 Event IR/fingerprint/query identity 동일
- 기존 의미 및 condition coverage 유지
- 모든 zero/null/negation 경계 테스트 통과
- 명백한 저비용 쿼리는 최적화 및 shadow 단계를 건너뛰고 기존 SQL을 그대로 생성
- fast-path 판정은 DB 조회 없이 IR 단일 순회 수준으로 완료

SQL 구조 기준:

- `Z_CAMP_MBR` 스캔 최대 한 번인 집합형 계획
- 바깥 회원 수만큼 반복되는 inner aggregate 제거
- scalar correlated `COUNT` 제거
- 동일 기간/성공/취소/distinct 실행키 의미 보존

성능 기준:

- 예상계획에서 약 69,000회 rebind/rewind 제거
- 원본 비용 `19118.6` 대비 집합형 비교 계획 수준으로 대폭 감소
- 애플리케이션의 기존 15초 timeout 안에서 완료
- timeout 증가는 완료 조건이나 우회책으로 인정하지 않음

운영 기준:

- DB DDL/DML 없음
- 인덱스/통계 변경 없음
- 민감한 회원 데이터 로깅 없음
- feature flag와 안전한 fallback 제공
- shadow 결과 동치 증거와 compiler receipt 확보

## 9. 다음 세션에서 먼저 볼 파일

- `event_compiler.py`
  - `compile_scalar()`의 `Aggregate` 처리
  - `_aggregate_expression()`
  - `_aggregate_subquery()`
  - `compile_condition()`의 aggregate comparison 처리
- `docs/data/runtime/semantics/audience_catalog.json`
  - `campaign_contact_success`
  - `campaign_contact_success.execution_id`
- `audience_runtime.py`, `resolved_semantic_catalog.py`
  - 카탈로그 compiler binding 로딩 및 revision/cache 정책
- `sql_dialect.py`
  - T-SQL 날짜 및 cast 렌더링
- `sql_guard.py`
  - AST/schema/analytics/performance 구조 검증
- `graph_rag.py`
  - canonical Event IR candidate 선택과 최종 SQL validation/target connection
- `tests/test_canonical_audience_path.py`
- `tests/test_event_ir.py`
- `tests/test_db_swap_preflight_gate.py`
- `tests/test_semantic_verification_receipts.py`

다음 세션은 위 파일과 기존 테스트를 먼저 읽은 후, 테스트를 작성하고 lowering을 구현해야 한다. 원본 scalar subquery를 단순 삭제하거나 모든 aggregate를 일괄 join으로 바꾸지 않는다.
