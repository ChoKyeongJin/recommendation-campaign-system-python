# legacy 오디언스 슬롯 → Event IR strangler 이행

> 생성 2026-08-03. 1차(계약·A그룹 어댑터·권위·지문) / 2차(shadow 6단계 골격) / 3차(⑤ 실DB 스냅샷 ·
> ⑥ 실행 비용) / 4차(원자적 cut-over · 명시 rollback) 웨이브 구현 완료 시점 기록.
> 이 문서는 계획서가 아니라 **현재 코드가 무엇을 보장하는지**의 서술이다. 계획 부분은 §8 에만 있다.

목표는 legacy 슬롯을 한 번에 없애는 것이 아니라, Event IR 을 오디언스 조건의 **단일 실행 권위**로
세우고 기존 자산을 변환·검증·비교·점진 전환하는 것이다.

```
legacy slot asset → legacy_slot_to_event_ir → plan.event_expression → event_compiler → SQL
```

---

## 1. 현재 실행 권위가 결정되는 지점

| 지점 | 이행 전 | 이행 후(현재) |
|---|---|---|
| `graph_rag._has_canonical_audience_authority` | `event_expression.source ∈ {audience_requirement, semantic_plan}` 를 직접 읽음 | `audience_authority.executes_event_ir(plan)` 위임 |
| `graph_rag.build_sql_result` / 응답 조립 (2지점) | 위 함수 결과로 회원 속성 컴파일 여부 결정 | 동일(판정자만 교체) |
| `graph_rag.build_event_expression_sql_candidate` | `payload.source` 를 다시 읽어 canonical 여부 판정 | `audience_authority.executes_event_ir(plan)` 위임(`canonical_authority`) |
| `_compile_sql_template_candidate_validated` | `_plan_event_expression(...) is not None` 으로 배타 라우팅 | 동일. 이것은 "이 슬롯이 파손되지 않았는가"이지 권위 판정이 아니다 |
| 저장된 플랜의 `audience_authority` 필드 | (없음) | **cut-over 명령만** 이 값을 쓴다(웨이브 4, §7-2) — 실행기가 읽는 값이 하나여야 한다 |

권위의 단일 소유자는 `audience_authority.resolve_authority(plan)` 이고, 판정 입력은
① 명시 필드 `plan.audience_authority` ② (없으면) 기존 저장 페이로드의 ingress 표식이다.
**표현의 존재는 권위가 아니다** — 이행 어댑터가 세우는 표현은 `source="legacy_migration"` 이라
저장돼도 실행되지 않는다. 계약은 `tests/test_audience_authority.py` 가 고정한다.

## 2. 자산·생산자·소비자 인벤토리

**legacy 슬롯 writer**
- `legacy_plan_compiler.LegacyQueryPlanCompiler` (SemanticPlanV2 → 실행 슬롯; 슬롯 이름의 단일 소유자)
- `semantic_pipeline` (그 컴파일러의 배선)
- `behavior_demotion` (행동 → 존재/부재 슬롯 강등)
- `conceptual_targeting` (개념 접지 결과의 슬롯 착지)
- `graph_rag` 의 상품 슬롯 정리 지점 (`purchase_object(s)`)
- `query_structurer/campaign_plan_v4` 의 스켈레톤 채우기(`setdefault("target_user", {})`)

**legacy 슬롯 reader** — `graph_rag._sql_target_builder_registry()` 의 빌더 20종,
`compile_member_target_conditions`, `confidence`, `requirement_ledger`, `ir_snapshot`.

**Event IR writer** — `query_structurer/campaign_plan_v4._derive_audience_execution`(ingress),
`semantic_receipts.merge_into_event_expression` ← `semantic_plan_event_lowering`(의미 노드 lowering),
그리고 이번에 추가된 `legacy_audience_migration`(이행 어댑터, **실행 권위 없음**).

**Event IR reader/compiler** — `graph_rag.build_event_expression_sql_candidate` → `event_compiler.compile_expression`,
`aggregate_semantics`(분기 의미 검증), `canonical_audience_claims`, `canonical_signal_coverage`, `confidence`.

**저장 자산** — `campaign_target_audiences.query_plan`(JSONB). `audience_key` 가 asset id 이고
**revision 컬럼은 없다**(2026-08-03 실측: 행 0건). 그래서 어댑터는 revision 을 payload 선언값 또는 1 로
읽고, cut-over 는 revision 이 아니라 `source_fingerprint` + `source_schema_checksum` 으로도 막을 수 있게
두 값을 함께 저장한다(§6).

## 3. 슬롯 분류 (실측 근거 포함)

**A — 즉시 무손실 변환(구현됨)**

| 슬롯 | Event IR | 무손실 근거 |
|---|---|---|
| `purchase_date` (8자리 from/to, 시각 경계 없음) | `Exists(purchase, [start, end_exclusive))`, 나열은 `Or` | char8 일 단위 컬럼에서 `BETWEEN a AND b` ≡ `>= a AND < b+1일` |
| `purchase_membership` | `Exists(purchase[, Rolling N일])` | 컴파일 결과가 `_purchase_membership_predicate` 와 **별칭만 다른 문자열 동일** |
| `purchase_inactivity` | `Not(Exists(purchase, Rolling N일))` | 컴파일 결과가 `_purchase_inactivity_predicate` 와 **별칭만 다른 문자열 동일** |
| `behaviors:no_purchase` | `Not(Exists(purchase))` | 평생 anti-join 과 같은 모양 |
| `aggregate_conditions` (metric ∈ {order_count, purchase_amount}, 연산자 ∈ {≥, >}, 임계 > 0, scope/grain 없음) | `Comparison(Aggregate(catalog recipe), Literal)` | 카탈로그 `purchase_count` = COUNT(DISTINCT ORDER_ID), `purchase_amount` = SUM(PAYMENT_AMT) 로 legacy 지표 정의와 동일 |

`aggregate_conditions` 의 `≤`/`<` 와 임계 ≤ 0 이 A 에서 빠진 이유: legacy 집계는
**INNER JOIN + GROUP BY … HAVING** 이라 그 사건이 0건인 회원은 애초에 평가되지 않는다.
Event IR 은 스칼라 서브쿼리라 0 으로 평가돼 '이하'를 만족한다 — 무주문 회원의 포함 여부가 통째로
갈리므로 업무 결정 사안이다(`AGGREGATE_ZERO_EVENT_MEMBER_SEMANTICS`).

**B — 카탈로그 확인 필요**

| 슬롯 | 사유 |
|---|---|
| `recent_login` | legacy 는 `LEN(LAST_LOGIN_DATE)=8` 유효성 가드를 함께 걸지만 카탈로그 `login` 소스에 그 선언이 없다. 가드 없이 변환하면 형식이 깨진 값이 사전식 비교로 통과해 **대상이 넓어진다** |
| `balance_conditions` · `profile_date_conditions` · `cart_*` · `purchase_object(s)` · 회원 속성(`gender`/`age_*`/`lifecycle`) | 해당 필드·소스가 오디언스 카탈로그에 선언되어 있지 않다 |
| 집계 지표 `total_item_quantity` · `distinct_*_count` · `discount_amount` | 주문 상세/상품 마스터 소스가 카탈로그에 없다 |

**C — IR/lowering 확장 필요**: `metric_trend`, `member_metric_ranking`, `member_metric_selection`,
`entity_set_condition`, `purchase_count_ranking`, `group_ranking_target`, `region_density_target`,
`cell_rate_target`, `birthday_target`(MMDD 비교), `purchase_date.from_time/to_time`(시각 노드 부재),
`age_exclude_ranges`(구간 차집합), 집계 `average_order_amount`/`first|last_purchase_date`.

**D — 업무 결정 필요**: `inactivity_period`(아래), `signup_target`(anchor 정책), `exclude` 컨테이너,
캠페인 반응 슬롯 4종·`relational_operation`(canonical 경로가 이미 Event IR 을 직접 생산 — 슬롯에서
다시 변환하면 이중 생산자), 8자리가 아닌 날짜 토큰(legacy 는 술어를 만들지 않는다), 조건 0개 자산.

> `inactivity_period` 가 A 가 아닌 이유(**추론이 아니라 실측**):
> legacy = `LAST_LOGIN_DATE IS NOT NULL AND LAST_LOGIN_DATE <= 컷오프` (한 번도 접속 안 한 회원 **제외**, 경계일 **포함**)
> Event IR = `NOT(접속이 컷오프 이후에 있음)` (NULL **포함**, 경계일 **제외**)
> 경계 fixture 실행 결과: `only_in_legacy={경계일 정확히 접속한 회원}`, `only_in_event_ir={한 번도 접속 안 한 회원}`.
> `tests/test_audience_shadow.py` 가 이 차이를 계약으로 고정한다 — 차이가 사라지면 그때가 A 승격 시점이다.

**미지원**: `interests`, `preferred_channels`, `price_sensitivity` — 실DB 원천이 없다.

## 4. 실행 권위와 이행 상태

```
audience_authority: legacy | event_ir            (실행하는 계층 — 한 실행에 하나)
migration_status:   LEGACY_ONLY → CONVERTED → SHADOW_VERIFIED → EVENT_IR_PRIMARY
                    → ROLLBACK_ELIGIBLE → RETIRED
                    (막힘) QUARANTINED · STALE · BLOCKED_CATALOG · BLOCKED_IR_EXTENSION
                           · BLOCKED_DOMAIN_DECISION · INVALID_LEGACY_ASSET
```

상태 기계가 강제하는 것 넷:
1. `EVENT_IR_PRIMARY` 로 들어오는 유일한 선행 상태는 `SHADOW_VERIFIED` 다(검증 생략 cut-over 차단).
2. `STALE` 은 `CONVERTED` 를 거쳐야 다시 검증으로 갈 수 있다.
3. rollback 목적지는 항상 `LEGACY_ONLY` 다 — **IR → 슬롯 역변환은 존재하지 않는다**.
4. `RETIRED` 는 종착지다(legacy payload 폐기 후이므로 되돌릴 대상이 없다).

권위는 상태에서 **파생**한다(`authority_for_status`) — 따로 저장하면 둘이 갈라진다.

## 5. 지문 세 축

| 축 | 입력 | 무엇을 답하는가 |
|---|---|---|
| `source_fingerprint` | 정규화된 오디언스 컨테이너(비의미 키·빈 슬롯 제거, 키 정렬) | 원본이 바뀌었나 → 변환이 stale 인가 |
| `semantic_fingerprint` | Event IR 직렬화에서 `evidence` 와 구간의 파생 표기(`from`/`to`) 제거 | 뜻이 바뀌었나 |
| `binding_fingerprint` | 위 + 참조 심볼의 물리 선언 + `catalog_version` + `compiler_version` | 같은 뜻이 다른 SQL 이 되나 |

범용 `semantic_fields.strip_provenance` 를 쓰지 않는 이유: 그 정규형은 키 이름이 `source` 면 무조건
지우는데 Event IR 의 `EventReference` 는 **어느 사건인가**를 그 이름으로 싣는다. 그걸 쓰면 서로 다른
사건 사이의 시간 관계가 같은 지문을 갖는다(`tests/test_legacy_audience_migration.py` 가 고정).

`source_schema_checksum` 은 값이 아니라 **모양**(키 경로 + 타입)의 해시다. 스키마에 키가 하나 늘면
어댑터의 경로 회계가 더 이상 완전하지 않은데, 값 지문만으로는 그 사실이 보이지 않는다.

## 6. 저장 전략(dual-storage ≠ dual-execution)

```
legacy payload        그대로 보존 — 어댑터는 입력을 변형하지 않는다(계약 테스트로 고정)
event_expression      추가(envelope: schema_version / expression / source="legacy_migration" / receipts / migration{...})
audience_authority    초기값 legacy — 배치는 권위를 옮기지 않는다
```

envelope 은 기존 `event_expression` 페이로드의 세 키(`expression`/`source`/`receipts`)를 같은 자리에
두고 이행 metadata 를 `migration` 하나에 모은다 — 예전 스키마 역직렬화가 그대로 통과한다.

이행 상태는 신설한 `campaign_audience_migration`(+ append-only `campaign_audience_migration_log`)이
소유한다(DDL: `docs/data/metadata_ddl.sql`, 도구 쪽 `CREATE TABLE IF NOT EXISTS` 와의 동치는 테스트가
고정한다). 그 표가 revision·지문 3축·**보존 legacy payload**·검증 근거(보고서 지문 + 검증 시각)·
`row_version` 을 든다. 실행 권위 자체는 여전히 `campaign_target_audiences.query_plan` 의
`audience_authority` 필드에 있다 — 실행기가 읽는 값이 하나여야 하기 때문이다.

## 7. shadow 검증(2·3차 웨이브 — 여섯 단계 모두 구현)

여섯 단계로 대조하고, **미실행 단계는 통과가 아니다**(필수 단계가 `NOT_RUN` 이면 cut-over 는 그대로 막힌다).

| 단계 | 상태 | 방법 |
|---|---|---|
| ① 의미 경로 소비 완전성 | 구현 | 어댑터의 `unmapped`/`invalid` 경로가 0인가 |
| ② canonical semantic 지문 | 구현 | 같은 입력을 두 번 변환해 지문 동일(멱등) |
| ③ SQL 술어 구조 | 구현 | `sql_semantics`(sqlglot AST) 서명 대조 — 술어·EXISTS 극성·조인·NULL·기간 경계. **문자열 일치 아님** |
| ④ adversarial fixture 결과 | 구현 | 경계 회원을 만들어 두 SQL 을 **sqlite 인메모리로 실행**해 회원 집합·중복 비교 |
| ⑤ 스냅샷 회원 집합 | 구현(웨이브 3) | 읽기전용 실DB 에서 **같은 기준 시각**으로 행수·회원수·양방향 차집합을 한 문장으로 센다. 차이가 있을 때만 안정성 probe + 표본 |
| ⑥ 성능·실행 계획 | 구현(웨이브 3, 벽시계 축만) | 두 술어를 **교대로** 반복 실행해 p50 비교(첫 실행 제외). 재지 못한 축은 `UNMEASURED_COST_AXES` 로 결과에 이름이 남는다 |

**⑤⑥의 시계 고정**: 롤링 컷오프는 양쪽 다 실행 시점 함수(`GETDATE()`)로 렌더된다. `pin_both_sides`
가 엔진 시계에서 앵커를 한 번 읽어 양쪽을 같은 값으로 치환하고, 고정하지 못하면 대조를 시작하지
않는다(→ `NOT_RUN`). 앵커는 **타입 있는 식**이어야 한다(`SqlDialect.datetime_anchor`) — 문자열이면
`to_char8(now())` 자리에서 조용히 다른 뜻이 된다.

**⑤가 통과하지 않는 두 경우**: ① 두 번 실행의 수치가 다르면(원천이 움직였다) `NOT_RUN` —
분류는 시계를 고정했는지에 따라 `NONDETERMINISTIC_SOURCE`/`DATA_VOLATILITY` 로 갈린다.
② **양쪽 0명이면 `NOT_RUN`** — 뜻이 다른 두 조건도 대상이 없으면 똑같이 0명이므로 공집합끼리의
일치는 아무것도 증명하지 않는다(같은 규칙이 ④에도 선다).

**⑥이 재지 않는 것**: 논리·물리 읽기, 계획 모양, sort/temp. 읽기 전용 SELECT 경로로는
`SET STATISTICS IO`·`SET SHOWPLAN_XML` 을 낼 수 없고, 그 가드는 이 이행의 안전장치라 뚫지 않는다.
비용 퇴행에는 **승인 경로를 두지 않았다** — 의미 차이 승인을 재사용하면 감사 기록이 거짓 라벨을 갖는다.

**③이 대조하지 않는 경우를 선언한다**: legacy 집계는 `INNER JOIN (GROUP BY … HAVING)` 이고 Event IR 은
스칼라 서브쿼리 술어다. 같은 뜻의 다른 구조라 서명이 다른 것이 정상이므로, 모양이 다르면 '차이'가 아니라
**`NOT_RUN`(사유 기록)** 이다 — 잡음 divergence 로 보고서를 채우면 진짜 차이가 묻히고, 통과시키면
검증하지 않은 것을 검증했다고 말하게 된다. 판정은 결과 집합 대조(④⑤)가 한다.

fixture 행의 기준일은 선언된 `as_of` 가 아니라 **검증 엔진이 보는 오늘**이다(`engine_anchor_date`).
롤링 컷오프는 두 SQL 모두 실행 시점 함수로 렌더되므로, 행을 다른 시계로 만들면 경계에 놓은 회원이
하루 어긋나 경계 검증이 조용히 무의미해진다.

**divergence 분류**: `UNEXPLAINED_DIVERGENCE`(기본 — cut-over 차단) / `APPROVED_LEGACY_BUG_FIX` /
`DATA_VOLATILITY` / `NONDETERMINISTIC_SOURCE` / `EXPECTED_BINDING_CHANGE`.
승인으로 차이를 허용하려면 여섯 항목(변경 전 의미·변경 후 의미·영향 회원 수·승인자·승인 시각·
관련 문서·rollback 기준)이 **전부** 있어야 하고, 하나라도 비면 분류 이름과 무관하게 계속 차단된다.
승인은 차이를 지우지 않는다 — 단계 실패 자체는 남는다.

## 7-1. 실DB 대조 결과 (2026-08-03 · CRMDW · 회원 69,609 / 주문 230,442행)

| 자산 | ⑤ 회원 집합 | ⑥ p50 legacy → Event IR | 판정 |
|---|---|---|---|
| `aud-convertible-lifetime-absence`(평생 무구매) | **8,397명 완전 일치** | 300.8ms → 278.9ms (0.93×) | **cut-over 허용** |
| `aud-convertible-aggregate-only`(주문 3건 이상) | **19,953명 완전 일치** | 232.1ms → 254.9ms (1.10×) | ③ 미실행으로 차단 |
| `aud-convertible-absence`(90일 미구매 + 365일 구매) | 양쪽 0명 → 미실행 | 138.0ms → 99.8ms (0.72×) | ⑤ 미실행으로 차단 |

A그룹 등가가 fixture 12명이 아니라 **회원 2만 명 규모의 실데이터에서** 성립했고, ③이 대조하지
못한 집계 자산(조인 문장 ↔ 스칼라 서브쿼리)을 ⑤가 판정했다 — 설계 의도대로다. 세 자산 모두
Event IR 이 더 비싸지 않았다.

## 7-2. 웨이브 4 — 원자적 cut-over 와 명시 rollback

권위를 옮기는 것은 `tools/cutover_legacy_audience.py` **하나뿐**이고, 판정은
`audience_cutover.py`(순수), 저장·CAS 는 `audience_migration_store.py` 가 소유한다.

**명령 하나 = 상태 전이 하나.**

```
record   LEGACY_ONLY → CONVERTED|BLOCKED_*   변환 저장(권위 불변, 보존 payload 확보)
promote  CONVERTED   → SHADOW_VERIFIED       shadow 보고서를 근거로 승격(근거 지문 저장)
cutover  SHADOW_VERIFIED → EVENT_IR_PRIMARY  상태 행 + 플랜 행을 같은 트랜잭션에서
rollback EVENT_IR_* → LEGACY_ONLY            권위만 되돌린다(역변환 없음)
```

붙여서 자동 진행하는 명령을 두지 않았다 — `record` 하면서 검증까지 통과시키는 명령이 있으면
그것이 기본 경로가 되고 검증은 다시 형식이 된다.

**cut-over 가 요구하는 것(하나라도 어긋나면 차단)**

| 검사 | 사유 |
|---|---|
| 선행 상태 `SHADOW_VERIFIED` | 검증 생략 cut-over 차단(상태 기계가 판정) |
| 저장 지문 3축 == 지금 다시 변환한 지문 | 저장값끼리 비교하면 **둘 다 낡았을 때 일치**한다 |
| `binding_fingerprint` 일치 | 뜻은 같은데 카탈로그·컴파일러가 바뀌면 검증이 잰 SQL 과 지금 나올 SQL 이 다르다 |
| 저장된 검증 근거(보고서 지문 + 검증 시각) 존재 | 상태만 올려 두고 넘어가는 길을 막는다 |
| (보고서 재제시 시) 지문 == 승격 근거 | 다른 보고서로 승격해 두고 또 다른 보고서로 cut-over 하는 경로 차단 |
| 저장된 산출물의 의미 지문 == 지금 변환 | 플랜에 얹는 것은 **검증된 그 산출물**이다(다시 만들지 않는다) |
| 보존 legacy payload 존재 | 되돌릴 재료 없이 권위를 옮기지 않는다 |
| 저장된 `query_plan` 존재 | 파일 코퍼스는 실행 경로가 아니다 |
| (웨이브 5) 저장 `query_plan` 의 오디언스 == 판정한 payload | 판정은 읽어 온 payload 로 하고 스탬프는 `audience_key` 로 찾은 행에 한다 — 다르면 **검증한 IR 이 다른 자산의 실행 권위**가 된다(`PLAN_PAYLOAD_MISMATCH`). 대조하지 못했거나 재료를 넘기지 않았으면 `PLAN_PAYLOAD_UNVERIFIED` 로 역시 차단이다 |

**CAS** — 갱신은 판정이 읽은 상태 값 **일곱**(`asset_id`·`revision`·`migration_status`·
`source_fingerprint`·`source_schema_checksum`·`semantic_fingerprint`·`row_version`)을 WHERE 로 걸고,
갱신 행이 0이면 **중단**한다(재시도하지 않는다 — 재시도는 우리가 읽지 않은 상태 위에 판정을 다시
쓰는 일이다). `binding_fingerprint` 는 판정 입력이지만 조건에는 없다 — 그 값을 새로 쓰는 경로가
`record` 뿐이고 그 경로는 INSERT 아니면 `row_version` 을 올리는 CAS 라 값이 움직이면 `row_version`
이 먼저 잡는다. `row_version` 이 따로 있는 이유는 지문이 모두 같아도 그 사이 상태가 한 번
왕복했다면 우리가 읽은 행이 이미 다른 행이기 때문이다.

**원자성** — 상태 행과 플랜 행은 같은 트랜잭션에서 바뀐다(플랜 행은 `SELECT … FOR UPDATE`). 하나만
바뀌면 그 자산은 "상태는 Event IR 인데 실행은 legacy"가 되어 어느 쪽으로도 설명되지 않는다.
플랜 갱신이 1행이 아니면 예외로 빠져나가 트랜잭션 전체가 되돌아간다.

**rollback 이 보는 것** — ① 보존 payload 존재 ② 그 payload 의 체크섬(저장 무결성 — 의미 지문과 다른
질문이다) ③ revision 일치 ④ 그 슬롯을 **legacy 컴파일러가 지금도 실행하는가**(`targeting_ir.SLOT_SHAPES`
+ `member_filters_config` 어휘에서 파생). ④가 어긋나면 되돌린 결과가 '조건 하나가 조용히 사라진
오디언스'이므로 차단한다. cut-over 이후 원본이 편집된 경우는 **경고이지 차단이 아니다** — 잘못된
cut-over 에서 빠져나오는 유일한 길을 무관한 편집 하나가 잠그면 안 된다.

**되돌린 뒤에도 표현은 남는다.** rollback 은 `audience_authority` 만 legacy 로 되돌리고
`event_expression` 을 지우지 않는다(저장 ≠ 실행). 지우면 rollback 이 '표현을 삭제하는 일'이 되고,
무엇을 검증했었는지가 저장소에서 사라진다.

**쓰기는 `--apply` 에서만.** 그 플래그가 없으면 연결 자체가 읽기 전용 트랜잭션으로 열려 **서버가**
쓰기를 거부한다(애플리케이션 규칙은 코드 한 줄로 우회되지만 read-only 트랜잭션은 우회되지 않는다).
`--apply` 는 `--actor` 를, `rollback` 은 `--reason` 을 요구한다 — 권위를 옮긴 사람과 되돌린 이유가
기록에 없으면 그것은 감사가 아니다. 차단된 시도도 로그에 남는다.

**어디까지 실측했나.** 판정·전이·CAS·원자성·rollback 은 주입 저장소 위 테스트가 고정하고,
"권위가 옮겨졌다"는 주장만은 진짜 소비자에게 묻는다(`graph_rag._has_canonical_audience_authority`
· `build_event_expression_sql_candidate` · `plan_validation.validate_executable_plan`).
로컬 메타데이터 postgres 에서는 `record` 의 DDL·INSERT·**실제 CAS UPDATE**(row_version 1→2)까지
확인했다. cut-over/rollback 트랜잭션(`FOR UPDATE` + 두 테이블 동시 갱신)은 저장된 자산이 생기는
날 처음 돈다 — `campaign_target_audiences` 는 아직 행 0건이다.

**아직 없는 것 둘**(자세히는 `NOTES_migration.md` §9-12·§9-13):
① cut-over 는 판정한 payload(`--assets`/`--source` 에서 읽어 다시 변환한 것)와 권위를 얹을
`query_plan` 행이 **같은 내용인지 확인하지 않는다** — 같은 계열의 대조가 rollback 에는 있다.
실자산 이관은 `--source db` 로 돌려 판정과 스탬프가 같은 행에서 나오게 하라.
② 집계 계열은 ③이 구조상 항상 `NOT_RUN` 이고 미실행은 곧 차단이라, ⑤가 완전 일치를 내도
**지금 설계로는 영구히 cut-over 불가**다(`aud-convertible-aggregate-only`). 면제 규칙을 열지
말지가 결정 사안이다.

## 8. 남은 웨이브

1. **⑤의 기준 시각을 데이터 구간에 맞추기** — 롤링 창 자산은 실데이터(2019년까지)에서 0명이라
   대조가 공허해진다. 앵커를 선언값으로 고정하는 모드를 열지가 결정 사안이다(열면 '운영과 다른
   시점'을 재게 되므로 그 사실이 보고서에 남아야 한다).
2. **legacy 렌더러 확장** — 독립 렌더할 수 없는 것은 `purchase_date` 슬롯과 '집계 문장+술어 혼합' ·
   '집계 조건 2건 이상' 자산이고, 그 자산들만 ③④⑤가 모두 미실행이다(술어끼리의 복합 자산은
   렌더된다 — `aud-convertible-absence` 는 ③④를 통과한다). 막힌 곳은 대조 엔진이 아니라
   렌더러다(흉내내면 검증이 자기확인이 된다).
3. **권위 판정 합류 완료** — 실행 경로의 두 판정 지점은 이미 `audience_authority` 로 합쳤다.
   남은 것은 `_plan_event_expression` 기반 배타 라우팅의 성격 정리(권위가 아니라 '슬롯 가용성'이라는 서술).
4. **B 그룹 해소** — `login` 시간 바인딩에 유효성 가드를 카탈로그로 선언하고, **그 선언을 읽는
   `recent_login` 변환기를 어댑터 `CONVERTERS` 에 등록**해야 A 로 승격된다. 지금 분류를 정하는 것은
   카탈로그가 아니라 코드의 정적 표(`CONVERTERS` 5종 + `BLOCKED_SLOTS`)라, 카탈로그만 고치면 그 키는
   '미등록 키'로 떨어져 같은 분류를 낸다 — "선언 한 줄이 분류를 바꾼다"는 설계 지향이지 현재 동작이 아니다.
5. **`RETIRED` 경로** — legacy payload 폐기는 아직 명령이 없다. 종착지이므로 되돌릴 대상이 사라진다 —
   만들 때 `ROLLBACK_ELIGIBLE` 유예 기간 정책과 `QUARANTINED` 저장 경로를 함께 정해야 한다
   (지금 그 셋과 `STALE` 은 전이표에만 있고 어떤 명령도 쓰지 않는다).
6. **③ 면제 규칙 결정** — 위 '아직 없는 것 ②'. 열면 승인자·기록 형식을 함께 정하고(의미 차이
   승인 계약을 재사용하지 말 것 — 그것은 'legacy 버그 수정' 라벨이다), 열지 않기로 하면 집계 계열이
   cut-over 대상이 아니라는 사실을 §3 분류에 적는다.
7. ~~**cut-over 판정에 플랜 행 대조 추가**~~ — **웨이브 5 완료**(위 §7-2 표 마지막 행).
8. ~~**감사 로그 조회 명령**~~ — **웨이브 5 완료.** `history` 명령 + 저장소 `read_log`/`LogRecord`.
   막힌 시도를 `blocked` 로 구분해 함께 싣는다(사고 후 재구성에서 필요한 것은 대개 그쪽이다).
9. **revision 발급 주체 미정** — 저장 자산에 revision 이 없어 전부 1이므로 지금 revision 은
   상수축이고 stale 판정은 지문 3축이 전담한다. 그 결과 병행 이관(구 revision 을 `EVENT_IR_PRIMARY`
   로 둔 채 새 revision 을 record)이 성립하지 않는다.

## 9. 운영

```bash
# shadow 검증(권위 변경 없음) — 어떤 자산을 SHADOW_VERIFIED 로 올려도 되는지의 근거와 차단 사유
# ①~④ 만: 실DB 접근 없이 돈다. ⑤⑥ 은 사유와 함께 미실행이므로 cut-over 는 계속 막힌다.
python -m tools.verify_legacy_audience_shadow \
  --assets tests/fixtures/legacy_audience_assets.json \
  --fixture tests/fixtures/audience_shadow_fixture.json \
  --approvals approvals.json \
  --output shadow-report.json

# ⑤ 실데이터 회원 집합 대조(읽기 전용 연결). 기준 시각은 엔진 시계에서 한 번 읽어 양쪽에 고정한다.
python -m tools.verify_legacy_audience_shadow \
  --assets tests/fixtures/legacy_audience_assets.json \
  --snapshot-db CRMDW --output shadow-report.json

# ⑤ + ⑥. 비용 측정은 자산마다 (반복+1)×2 회 질의를 실DB 에서 돌린다 — 켜는 것이 명시적 선택이다.
python -m tools.verify_legacy_audience_shadow \
  --assets tests/fixtures/legacy_audience_assets.json \
  --snapshot-db CRMDW --cost-repetitions 5 --output shadow-report.json
```

```bash
# dry-run(기본): 파일 코퍼스
python -m tools.migrate_legacy_audience \
  --assets tests/fixtures/legacy_audience_assets.json \
  --batch-size 100 --compile-sql \
  --output migration-report.json --csv-output migration-manifest.csv

# 저장된 자산(읽기 전용) + 체크포인트 재개
python -m tools.migrate_legacy_audience --source db --checkpoint .migration-checkpoint --limit 500

# 특정 자산/슬롯만 재실행
python -m tools.migrate_legacy_audience --assets assets.json --asset-id aud-123 --slot-type purchase_date
```

`migrate_legacy_audience --apply` 는 의도적으로 거부된다 — 저장·권위 이관은 아래 명령이 소유한다
(dry-run 도구가 쓰기를 할 수 있으면 "확인만 해보려던 실행"이 운영 상태를 바꾼다).

```bash
# 지금 각 자산이 어디에 있고 cut-over 가 왜 막혀 있는가(쓰지 않는다)
python -m tools.cutover_legacy_audience status --assets assets.json

# ① 변환 저장(권위 불변) — 배치가 허용되는 유일한 명령
python -m tools.cutover_legacy_audience record --assets assets.json --apply --actor "$(whoami)"

# ② shadow 보고서를 근거로 승격 → ③ 권위 이관. 자산 하나씩이다.
python -m tools.cutover_legacy_audience promote --assets assets.json \
  --asset-id aud-convertible-lifetime-absence --shadow-report shadow-report.json --apply --actor "$(whoami)"
python -m tools.cutover_legacy_audience cutover --assets assets.json \
  --asset-id aud-convertible-lifetime-absence --apply --actor "$(whoami)"

# 되돌리기 — 자산 파일 없이 저장 상태만으로 성립한다(사고 중에 파일이 손에 없을 수 있다)
python -m tools.cutover_legacy_audience rollback --asset-id aud-convertible-lifetime-absence \
  --revision 1 --apply --actor "$(whoami)" --reason "대상 수가 예상보다 20% 적다"

# 무슨 일이 있었나(웨이브 5) — 막힌 시도까지 시간순. --limit 은 **최신** 쪽을 남긴다.
# 자산 파일도 --apply 도 필요 없고, --apply 를 붙여도 읽기 전용으로 열린다.
python -m tools.cutover_legacy_audience history --asset-id aud-convertible-lifetime-absence --limit 50
```

종료 코드: `0` 통과 · `2` 명령을 시작할 수 없음(인자·재료) · `3` 판정이 차단 ·
`1` 적용 중 중단(CAS 0행·플랜 갱신 실패 — 트랜잭션은 되돌아갔다).
보고 명령(`status`·`history`)은 **항상 0** 이다 — 막혀 있다는 사실·막힌 시도가 쌓여 있다는 사실이
그 명령의 정상 출력이라, 실패 코드로 내면 조회를 파이프라인에 넣을 수 없다.
