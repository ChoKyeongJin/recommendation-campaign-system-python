> **2026-08-05 폐기 고지.** 아래에서 legacy 슬롯 writer 로 열거된 `legacy_plan_compiler` ·
> `semantic_pipeline` · `requirement_ledger` 는 SemanticPlanV2 중간표현과 함께 **삭제**됐다
> (writer 목록에서 셋이 빠진다). `event_expression.source` 표식 집합
> `{audience_requirement, semantic_plan}` 은 **그대로 유지**된다 — `semantic_plan` 은 생산자 없는
> 저장 페이로드 호환 값이고, 빼면 그 표식을 가진 저장분의 라우팅이 조용히 뒤집힌다.

# 작업 노트 — legacy 오디언스 슬롯 → Event IR strangler 이행 (2026-08-03)

목표는 legacy 슬롯을 한 번에 걷어내는 것이 **아니다**. Event IR 을 오디언스 조건의 단일 실행 권위로
세우고, 기존 자산을 변환·검증·대조·점진 전환한다.

```
legacy slot asset → legacy_slot_to_event_ir → plan.event_expression → event_compiler → SQL
```

| 웨이브 | 무엇을 세웠나 | 상태 |
|---|---|---|
| 1 — 변환 | 실행 권위 상태 기계 · 지문 3축 · A그룹 어댑터 · dry-run 배치 | 완료 |
| 2 — 검증 골격 | shadow 6단계 모델 · divergence 분류 · 승인 계약 · cut-over 게이트 · 단계 ①~④ | 완료 |
| 3 — 실측 | 단계 ⑤(실DB 회원 집합) · ⑥(실행 비용, 벽시계 축) · 시계 고정 | 완료 |
| 4 — 이관 | 이행 상태 저장소 · CAS cut-over · 명시 rollback · 감사 로그 | 완료 |
| 5 — 정합·조회 | 판정↔스탬프 대상 대조 · 감사 로그 조회 명령 · 스탬프 사후조건 테스트 | 진행 중(§10) |

**지금 어디까지 왔나.** 여섯 단계가 모두 돌게 되면서 자산 13개 중 **1개**
(`aud-convertible-lifetime-absence`)가 게이트를 통과했고, 나머지 12개는 **사유와 함께** 차단돼
있다(§4). 웨이브 4로 그 1개를 **실제로 옮기는 경로**가 생겼다 — 명령 하나 = 상태 전이 하나,
상태 행과 플랜 행은 한 트랜잭션, 되돌리기는 보존 payload 로만.

**아직 옮기지는 않았다.** `campaign_target_audiences` 는 여전히 행 0건이라 게이트를 통과한 그
자산이 저장소에 없고(코퍼스 fixture 다), cut-over 는 저장된 플랜이 없으면 `PLAN_ROW_MISSING` 으로
막힌다 — 그게 맞다(파일 코퍼스는 실행 경로가 아니다). 그래서 웨이브 4가 실측으로 고정한 것은
**기계**다: 전이·CAS·원자성·rollback 이 주입 저장소 위에서 돌고, cut-over 후 플랜을 **실제 실행기와
플랜 검증기에 물어보며**, 실DB 에서는 `record` 의 DDL·INSERT·CAS 까지 돌려 봤다(§4-2).
첫 실자산 cut-over 는 자산이 저장되는 날이다.

**웨이브 5는 그 경로의 구멍 하나를 막았다.** 권위를 옮길 때 **판정한 자산과 스탬프할 자산이 같은지**
대조하지 않던 자리가 있었다(§9-12) — 파일 코퍼스로 판정하고 저장 행에 얹으므로 실자산 이관에서
실제로 갈릴 수 있는 지점이다. 함께 `history` 조회 명령이 생겼고(막힌 시도까지 읽힌다),
"권위를 스탬프한 뒤 실행기에 다시 묻는다"는 사후 조건을 테스트가 고정한다.

| 찾는 것 | 어디 |
|---|---|
| 지금까지 한 작업 | §2(웨이브별로 무엇을 세웠나) · §4(그중 무엇이 실측됐나) |
| 내린 결정과 이유 | §3(웨이브 1·2 / 3 / 4 세 표) |
| 수정·추가한 파일 | §6 |
| 남은 할 일 | §10(무엇을) · §9(왜 아직 못 하나) · §11(밟으면 안 되는 함정) |

---

## 1. 조사로 확정한 사실 — 전제와 실제

착수 전 가정과 실제 코드가 다른 지점이 넷 있었고, 그중 **둘이 슬롯 셋**(`recent_login` ·
`inactivity_period` · `aggregate_conditions`)**의 분류를 바꿨다**.

| 착수 전 가정 | 실제(실측) |
|---|---|
| 실행 권위는 `plan.event_expression` 의 **존재**가 정한다 | 존재가 아니라 `source` 표식이 정했다(`{audience_requirement, semantic_plan}`). 그런데 같은 판정을 **두 곳이 각자** 하고 있었다(`_has_canonical_audience_authority`, `build_event_expression_sql_candidate`) |
| `recent_login`·`inactivity_period` 는 A그룹 후보 | 둘 다 아니다. legacy 술어에 `LEN(col)=8` 유효성 가드와 `IS NOT NULL`·`<=` 경계가 붙어 있어 Event IR 과 **다른 집합**이다(§3) |
| `aggregate_conditions` 는 통째로 A그룹 | 연산자 방향이 갈린다. legacy 집계는 `INNER JOIN + GROUP BY … HAVING` 이라 그 사건이 0건인 회원을 **평가조차 하지 않는다**. `≥`·`>` 에 임계 > 0 일 때만 두 해석이 같다 |
| 저장 자산은 revision 을 갖는다 | `campaign_target_audiences` 에 revision 컬럼이 **없다**(그리고 현재 행 0건). cut-over 는 revision 만으로 걸 수 없어 `source_fingerprint` + `source_schema_checksum` 을 함께 저장한다 |

**인벤토리** — 전체 표는 `docs/plans_legacy_audience_strangler.md` §2

- legacy 슬롯 writer 6: `legacy_plan_compiler` · `semantic_pipeline` · `behavior_demotion` ·
  `conceptual_targeting` · `graph_rag`(상품 슬롯) · `campaign_plan_v4`(스켈레톤)
- legacy 슬롯 reader: `_sql_target_builder_registry()` 빌더 20종 + `compile_member_target_conditions`
  + `confidence` · `requirement_ledger` · `ir_snapshot`
- Event IR writer 3: `campaign_plan_v4._derive_audience_execution`(ingress) ·
  `semantic_receipts.merge_into_event_expression`(의미 노드 lowering) · **신규 이행 어댑터(실행 권위 없음)**
- Event IR reader: `build_event_expression_sql_candidate` → `event_compiler.compile_expression`

---

## 2. 무엇을 했나

### 웨이브 1 — 변환

**① 실행 권위를 명시 상태로 뽑았다.** `audience_authority.py` 가 `AudienceAuthority`(legacy|event_ir),
`MigrationStatus` 12종, 허용 전이표, `resolve_authority(plan)` 를 단독 소유한다. 실행기의 판정 지점
두 곳이 이 모듈에 위임한다. 저장(`event_expression` 이 플랜에 있다)과 실행(권위)이 **분리**됐다.

**② 지문을 세 축으로 나눴다.** `migration_fingerprint.py` — `source`(원본이 바뀌었나) /
`semantic`(뜻이 바뀌었나) / `binding`(같은 뜻이 다른 SQL 이 되나). 별도로 `source_schema_checksum`
(값이 아니라 **모양**)이 스키마 변화를 잡는다.

**③ A그룹 어댑터.** `legacy_audience_migration.py` — 순수 함수. 슬롯 이름은 이 파일 안에서만 산다.
경로 회계는 전수다: 오디언스 컨테이너의 모든 키가 consumed / ignored / unmapped / invalid **정확히
하나**에 들어간다.

**④ dry-run 배치 CLI.** `tools/migrate_legacy_audience.py` — 읽기만 한다. `--apply` 는 의도적으로 거부.
checkpoint/resume · batch · asset-id/slot-type 필터 · JSON+CSV 리포트 · offline replay.

### 웨이브 2 — 검증 골격

**⑤ shadow 검증 계층.** `audience_shadow.py` + `tools/verify_legacy_audience_shadow.py`.
여섯 단계 모델 · divergence 분류 · 승인 계약 · cut-over 게이트를 세우고 단계 ①~④ 를 구현했다.
legacy 쪽 산출물은 어댑터를 거치지 않고 `graph_rag` 실행 헬퍼로 **독립 렌더**해 대조한다 — 같은
코드에서 나온 두 값을 비교하면 검증이 동어반복이 된다.

### 웨이브 3 — 실측(단계 ⑤⑥ 배선)

**⑥ 검증 계층을 실데이터에 붙였다.** 아래 셋이 한 벌이다 — 스냅샷 대조 · 실행 비용 · 그 둘이
같은 시점을 보게 하는 시계 고정.

**스냅샷 회원 집합(단계 ⑤).** `audience_shadow.run_snapshot_comparison` + `--snapshot-db`.
두 산출물의 **행수·회원수·양방향 차집합**을 한 문장으로 세고(차집합은 엔진의 `EXCEPT` 가 한다 —
회원 id 수백만을 파이썬으로 실어 오지 않는다), 차이가 나올 때만 표본과 안정성 probe 를 더 돌린다.
연결은 도구가 만들고 검증 계층은 실행자를 **주입받는다** — 검증기가 접속 정보를 알기 시작하면
"확인만 해보려던 실행"이 어느 DB 에 붙을지 검증기 안에서 정해진다.

**실행 비용(단계 ⑥).** `run_performance_comparison` + `--cost-repetitions`(기본 0 = 미실행).
두 술어를 **교대로** 반복 실행해 p50/p95 를 비교한다. 몰아서 돌리면 캐시 적재·부하 변동이
한쪽에만 몰린다. 기본이 꺼짐인 이유는 이것이 실DB 에 부하를 만드는 유일한 자리이기 때문이다.

**시계 고정(양 단계 공통).** 롤링 컷오프는 두 산출물 모두 실행 시점 함수(`GETDATE()`)로 렌더된다.
`pin_both_sides` 가 엔진 시계에서 앵커를 한 번 읽어 **양쪽에 같은 값**으로 치환한다. 고정하지 못하면
대조를 시작하지 않는다(예외 → `NOT_RUN`).

### 웨이브 4 — 이관(권위가 실제로 움직인다)

**⑦ 이행 상태 저장소.** `campaign_audience_migration`(+ append-only `..._log`) 신설.
자산 저장 테이블에 revision 컬럼이 없으므로(웨이브 1의 실측) 이행 상태는 이 표가 소유하고
revision · 지문 3축 · **보존 legacy payload** · 검증 근거(보고서 지문 + 검증 시각) · `row_version` 을
함께 든다. 실행 권위 자체는 여전히 `query_plan.audience_authority` 하나다 — 실행기가 읽는 값이
둘이 되면 그 순간 "무엇이 실행됐는가"의 답이 둘이 된다.

**⑧ 명령 하나 = 상태 전이 하나.** `tools/cutover_legacy_audience.py`:

```
record   → CONVERTED | BLOCKED_* | INVALID_LEGACY_ASSET   변환 저장(권위 불변, 보존 payload 확보)
promote  → SHADOW_VERIFIED       shadow 보고서를 근거로 승격(근거 지문·검증 시각 저장)
cutover  → EVENT_IR_PRIMARY      상태 행 + 플랜 행을 같은 트랜잭션에서
rollback → LEGACY_ONLY           권위만 되돌린다(역변환 없음, 표현은 남는다)
```

보고 명령 둘은 전이가 아니다 — `status`(지금 어디에 있고 왜 막혀 있나) · `history`(웨이브 5,
무슨 일이 있었나). 둘 다 `--apply` 가 붙어도 **읽기 전용 연결로 연다**.

**⑨ CAS.** 갱신은 판정이 읽은 상태 값 **일곱**을 WHERE 로 건다 — `asset_id`·`revision`·
`migration_status`·`source_fingerprint`·`source_schema_checksum`·`semantic_fingerprint`·`row_version`.
갱신 행이 0이면 **중단**한다. `binding_fingerprint` 도 판정 입력이지만(불일치는
`BINDING_FINGERPRINT_MOVED` 로 cut-over 를 막는다, §3) 조건에는 넣지 않았다 — 그 값을 새로 쓰는
경로는 `record` 뿐이고 그 경로는 INSERT 아니면 `row_version` 을 올리는 CAS 라, 값이 움직이면
`row_version` 이 먼저 잡는다. 플랜 행은 `SELECT … FOR UPDATE` 로 읽고 같은 트랜잭션에서 쓴다.
판정(`audience_cutover`, 순수) · 저장/CAS(`audience_migration_store`) · 연결/트랜잭션(도구)이 각각
다른 파일에 산다 — shadow 계층에 실행자를 주입한 것과 같은 이유다.

### shadow 6단계 — 현재 상태

| 단계 | 상태 | 방법 |
|---|---|---|
| ① 의미 경로 소비 완전성 | 구현(W2) | `unmapped`/`invalid` 가 0인가 |
| ② canonical semantic 지문 | 구현(W2) | 두 번 변환해 지문 동일(멱등) |
| ③ SQL 술어 구조 | 구현(W2) | sqlglot AST 서명 대조(술어·EXISTS 극성·조인·NULL·기간 경계). **문자열 일치 아님** |
| ④ adversarial fixture | 구현(W2) | 경계 회원을 만들어 두 SQL 을 **sqlite 인메모리로 실행**, 회원 집합·중복 비교 |
| ⑤ 스냅샷 회원 집합 | 구현(W3) | 읽기전용 실DB 에서 **같은 기준 시각**으로 행수·회원수·양방향 차집합 대조 |
| ⑥ 성능·실행 계획 | 구현(W3, 벽시계 축만) | 교대 반복 실행의 p50 비교. **재지 못한 축은 이름으로 남긴다**(§9-1) |

세 규칙이 여섯 단계 전체에 걸린다. **미실행은 통과가 아니다**(필수 단계 하나라도 `NOT_RUN` 이면
차단) · **대조할 수 없으면 '다르다'가 아니라 '대조하지 않았다'** · **공집합끼리의 일치는 증거가
아니다**(§5).

---

## 3. 내린 결정과 이유

가장 중요한 것부터. **이유가 없는 결정은 다음 사람이 되돌린다.**

**웨이브 1·2 에서 내린 것**

| 결정 | 이유 |
|---|---|
| **존재가 아니라 선언이 권위다** — 실행기는 `audience_authority` 만 본다 | 이행기에는 변환만 되고 검증 전인 IR 이 같은 플랜에 저장된다(dual-storage). 존재를 권위로 읽으면 **저장이 곧 실행**이 되고, rollback 이 '표현을 지우는 일'로 변질된다 |
| **이행 변환물은 ingress 표식을 달지 않는다**(`source="legacy_migration"`) | dual-storage ≠ dual-execution 의 물리적 실체가 이것이다. 표식을 달면 저장하는 순간 실행된다 |
| **`EVENT_IR_PRIMARY` 로 들어오는 선행 상태는 `SHADOW_VERIFIED` 하나** | 표를 넓히면 검증을 건너뛴 cut-over 가 가능해진다. 그것이 이 이행에서 가장 비싼 사고다 |
| **rollback 목적지는 항상 `LEGACY_ONLY`** | rollback 은 IR→슬롯 역변환이 아니라 **보존된 legacy payload 로 권위를 되돌리는 것**이다. 역변환 함수를 production rollback 경로로 쓰지 않는다 |
| **부분 변환은 실행 가능이 아니다** | 변환된 조각은 관찰 가능하되 `is_executable=False`. 반쯤 변환된 오디언스는 실패보다 나쁘다 — 조용히 넓거나 좁은 대상이 성공으로 나간다 |
| **근거(evidence)를 지어내지 않는다** | legacy 슬롯에는 원문 스팬이 없다. 라벨을 IR `evidence` 로 승격하면 검증 계층이 '검증된 근거'로 오인한다. 원문 흔적은 **영수증**에 보존한다 |
| **집계 레시피를 어댑터가 정하지 않는다** | 함수·표현 필드·distinct 는 카탈로그 `metric` 선언에서 읽는다. 어댑터에는 `order_count → purchase_count` 라는 **이름 대응**만 있다(물리 바인딩은 catalog/compiler registry 소유) |
| **롤링 창을 일 단위로 정규화** | legacy 실행 술어는 `min_days` 만 쓴다(`char8_cutoff`). '3개월'과 '90일'은 legacy 에서 **이미 같은 조건**이라, 지문이 둘을 다르게 보면 같은 뜻이 두 지문을 갖는다 |
| **8자리 아닌 날짜 토큰은 변환하지 않는다** | legacy 실행기는 그 토큰의 술어를 **아예 만들지 않는다**(조건이 조용히 사라진다). IR 구간으로 승격하면 legacy 가 만들지 않던 조건이 생긴다 — 무손실 변환이 아니라 승인된 버그 수정이다 |
| **빈 슬롯을 지문에서 뺀다** | 플랜 스켈레톤이 모든 슬롯을 `None`/`[]` 로 깔아 둔다. 기본값이 지문에 들어가면 스켈레톤에 슬롯 하나가 추가되는 것만으로 **모든 자산이 한꺼번에 stale** 이 되고, 그때 stale 은 신호가 아니라 잡음이다 |
| **범용 `strip_provenance` 를 의미 지문에 쓰지 않는다** | 그 정규형은 키 이름이 `source` 면 무조건 지우는데, Event IR 의 `EventReference` 는 **어느 사건인가**를 그 이름으로 싣는다. 쓰면 서로 다른 사건 사이의 시간 관계가 같은 지문을 갖는다 |
| **조건 0개 자산은 '변환 완료'가 아니다** | 조건 없는 표현은 전체 회원을 뜻하게 되는데 Event IR 대수에는 그 뜻을 담는 노드가 없다(항진 노드를 만들지 않는 것이 설계다) → `NO_AUDIENCE_CONDITION`(업무 결정) |
| **대조할 수 없으면 "다르다"가 아니라 "대조하지 않았다"** | legacy 집계는 조인 문장, IR 은 스칼라 서브쿼리 술어 — 같은 뜻의 다른 구조라 서명이 다른 게 정상이다. 차이로 보고하면 보고서가 잡음으로 차고 진짜 차이가 묻히며, 통과시키면 검증하지 않은 것을 검증했다고 말하게 된다 → `NOT_RUN`(사유 기록) |
| **미실행은 통과가 아니다** | 필수 6단계 중 하나라도 `NOT_RUN` 이면 cut-over 는 그대로 막힌다. 접근이 없다·렌더가 안 된다·데이터가 비었다는 **전부 미실행이지 통과가 아니다**. 이 규칙이 실제로 무는지는 웨이브 3에서 확인됐다 — 여섯 단계가 다 돌게 된 뒤에도 13개 중 12개가 사유와 함께 차단됐다(§4) |
| **승인 없이 결과 차이를 허용하지 않는다** | `APPROVED_LEGACY_BUG_FIX` 는 여섯 항목(변경 전/후 의미·영향 회원 수·승인자·승인 시각·문서·rollback 기준)이 **전부** 있어야 한다. 하나라도 비면 분류 이름과 무관하게 계속 차단 — 그 경로가 없으면 발견된 모든 차이가 '아마 버그였을 것'으로 정리된다 |
| **게이트는 관측과 판정을 나눈다** | 단계가 차이를 찾았다는 사실(`failed_stages`)은 관측이고, 막을지는 분류가 정한다. 안 그러면 승인 계약이 장식이 된다(승인된 버그 수정은 정의상 결과가 다르다). 대신 **사유 없는 실패**(`unaccounted_stages`)는 무조건 막는다 |
| **fixture 기준일은 선언 `as_of` 가 아니라 엔진의 오늘** | 롤링 컷오프는 두 SQL 모두 실행 시점 함수로 렌더된다. 행을 다른 시계로 만들면(선언 TZ 는 서울, 엔진은 UTC) 경계에 놓은 회원이 하루 어긋나 **경계 검증이 조용히 경계를 안 보게** 된다 |
| **경계 fixture 날짜는 절대값이 아니라 `offset_days`** | 절대값으로 적으면 오늘이 지날 때마다 경계가 이동해 검증이 조용히 무의미해진다 |
| **어댑터는 DB 를 쓰지 않는다 / CLI 는 `--apply` 를 거부한다** | dry-run 도구가 쓰기를 할 수 있으면 "확인만 해보려던 실행"이 운영 상태를 바꾼다 |

**웨이브 3에서 내린 것**

| 결정 | 이유 |
|---|---|
| **시계를 고정하지 못하면 대조하지 않는다** | 롤링 컷오프는 양쪽 다 실행 시점 함수다. 두 문장을 각각 실행하면 두 시계를 보게 되고, 자정이나 적재 시점에 걸리면 **뜻이 같은데도 다른 집합**이 나온다. 고정할 수 있는데 고정하지 않고 얻은 '같다'는 우연일 수 있고, 그 우연을 통과로 기록하면 검증이 검증이 아니게 된다 |
| **앵커는 문자열이 아니라 타입 있는 식**(`CAST('…' AS DATETIME)`) | `DATEADD` 자리에서는 문자열도 암묵 변환으로 통하지만 `to_char8(now())` 자리에서는 문자열을 문자열로 자르는 다른 뜻이 된다 — 오류가 아니라 **조용히 틀린 컷오프**다 |
| **앵커 표기는 '실행할 엔진'의 방언이 소유**(`SqliteVerificationDialect`) | 같은 앵커라도 T-SQL 표기를 sqlite 로 옮기면 숫자 캐스트가 되어 다른 날짜가 된다. 시계 어휘·앵커 표기를 검증기가 자기 소스에 적으면 방언이 바뀌는 날 검증기만 옛 문법으로 잰다 |
| **잔여 시계는 아는 어휘 전부로 센다**(`all_clock_functions`) | 치환은 대조를 실행할 연결의 시계 하나만 한다. 그 방언 어휘로만 훑으면 **다른 방언이 렌더한 시계**(legacy 빌더 방언 ≠ 연결 방언)를 '없다'고 세고, "고정할 시계가 없으니 고정 없이 대조해도 된다"는 잘못된 판정을 통과한다 — 그 SQL 은 실행하면 문법 오류로 죽지만 판정은 이미 내려진 뒤다 |
| **여섯 수치는 한 문장에서 나온다** | 행수·회원수·양방향 차집합을 나눠 실행하면 그 사이에 원천이 움직인 만큼이 '의미 차이'로 둔갑한다 |
| **차집합은 엔진이 세고, 파이썬으로는 표본만 가져온다** | 실데이터의 회원 집합은 수만~수백만 행이다. 전수 반출은 술어가 아니라 네트워크를 재는 일이고, 표본이 잘렸다는 사실은 **반드시** 함께 싣는다(조용한 절단은 '이게 전부'로 읽힌다) |
| **원천이 움직이면 FAIL 이 아니라 `NOT_RUN`** | 같은 문장이 두 번 다른 답을 냈다면 그 차이는 뜻의 차이일 수도, 그 사이 움직인 데이터일 수도 있다. FAIL 로 적으면 승인 계약이 잘못된 것을 승인하고, PASS 로 적으면 검증하지 않은 것을 검증했다고 말한다 |
| **불안정의 분류는 가릴 수 있는 만큼만** | 시계를 고정했는데도 움직였으면 원인이 시계는 아니다 → `NONDETERMINISTIC_SOURCE`. 고정할 시계가 애초에 없었으면 남는 원인은 데이터뿐이다 → `DATA_VOLATILITY`. 데이터 변동과 질의 비결정성을 가르려면 원천 워터마크가 필요한데, 그것이 없는 지금 둘을 가르는 척하지 않는다 |
| **공집합끼리의 일치는 통과가 아니다** | 뜻이 완전히 다른 두 조건도 대상이 없으면 똑같이 0명이다. 통과로 세면 **데이터가 비어 있는 자산일수록 검증을 쉽게 통과**하게 된다 — 정확히 거꾸로다(§5. 실DB 첫 실행이 잡았다) |
| **비용은 교대로 재고, 첫 실행은 표본에서 뺀다** | 몰아서 재면 캐시 적재·부하 변동이 한쪽에만 몰리고, 첫 실행에는 계획 컴파일이 들어 있다 — 빼지 않으면 이 단계는 **실행 순서**를 재는 장치가 된다 |
| **퇴행 판정은 비율 + 절대 바닥값 둘 다** | 비율만 보면 2ms→4ms 가 '2배 퇴행'이 되어 보고서가 잡음으로 차고, 절대 차이만 보면 느린 질의의 배수 퇴행을 놓친다 |
| **재지 못한 비용 축을 이름으로 남긴다** | 읽기 전용 SELECT 경로로는 `SET STATISTICS IO`·`SET SHOWPLAN_XML` 을 낼 수 없다(그 가드가 이 이행의 안전장치라 뚫지 않는다). 목록이 비면 보고서가 "성능이 모든 축에서 같다"로 읽힌다 |
| **비용 퇴행에는 승인 경로를 만들지 않았다** | 만들면 그 경로가 기본이 된다. 의미 차이 승인(`Approval`)을 재사용하면 감사 기록이 'legacy 버그 수정'이라는 **거짓 라벨**을 갖는다 — 필요해지는 날 전용 분류와 승인 스키마를 함께 만든다 |
| **비용 측정은 기본이 꺼짐**(`--cost-repetitions 0`) | 자산마다 (반복+1)×2 회 질의가 실DB 에서 돈다. 검증 도구가 만드는 유일한 부하이므로 켜는 것이 명시적 선택이어야 한다 |

**웨이브 4에서 내린 것**

| 결정 | 이유 |
|---|---|
| **명령 하나 = 상태 전이 하나**(record/promote/cutover/rollback 을 붙이지 않는다) | `record` 하면서 검증까지 통과시키는 명령이 있으면 그것이 기본 경로가 되고, 검증은 다시 형식이 된다 |
| **판정 기준선은 저장값이 아니라 '지금 다시 변환한 값'** | 저장값끼리 비교하면 **둘 다 낡았을 때 일치**한다. 지문 3축을 그때그때 재계산해 대조해야 stale 이 신호가 된다 |
| **`binding_fingerprint` 불일치도 cut-over 를 막는다** | 뜻은 같은데 카탈로그·컴파일러가 바뀌면 같은 의미가 다른 SQL 이 된다 — 검증이 잰 SQL 이 아니다. 지문을 세 축으로 나눈 값이 여기서 나온다(다시 검증하면 해소된다) |
| **플랜에 얹는 것은 저장된 산출물 그대로**(다시 만들지 않는다) | 다시 만들면 '검증한 것'과 '실행하는 것'이 미세하게 갈린다(최소한 `converted_at` 이 다르다). 대신 저장본의 의미 지문이 지금 변환과 같은지 확인한다 — 아니면 '검증된 것을 얹는다'가 '언젠가 저장된 것을 얹는다'가 된다 |
| **CAS 갱신 행 0 = 중단, 재시도 없음** | 재시도는 우리가 읽지 않은 상태 위에 판정을 다시 쓰는 일이다. `row_version` 을 따로 둔 이유도 같다 — 지문이 다 같아도 그 사이 상태가 한 번 왕복했으면 우리가 읽은 행은 이미 다른 행이다 |
| **상태 행과 플랜 행은 같은 트랜잭션** | 하나만 바뀌면 그 자산은 "상태는 Event IR 인데 실행은 legacy"가 되어 **어느 쪽으로도 설명되지 않는다**. 플랜 갱신이 1행이 아니면 예외로 빠져나가 전부 되돌린다 |
| **cut-over 는 보존 payload 없이는 못 한다** | 되돌릴 재료를 확보하지 못한 채 옮기면 rollback 에 남는 길이 '역변환'뿐이다 — 그 함수를 production rollback 경로로 쓰지 않기로 한 것이 웨이브 1의 결정이다 |
| **cut-over 후에도 legacy 슬롯을 지우지 않는다** | 지우면 rollback 이 역변환이 된다. 실행 경로에서 두 해석이 동시에 도는 일은 권위 판정이 막는다(Event IR 권위에서는 회원 속성 컴파일러가 호출되지 않는다) — 그리고 그 모양이 플랜 검증을 통과하는지 실제로 물어봤다(§4-2) |
| **rollback 은 표현을 지우지 않는다** | 지우면 rollback 이 '표현을 삭제하는 일'이 되고, 되돌린 뒤 무엇을 검증했었는지가 저장소에서 사라진다(저장 ≠ 실행) |
| **rollback 차단 조건은 '되돌린 결과가 조용히 틀리는 경우'만** | 보존 payload 부재·체크섬 불일치·revision 불일치·**legacy 컴파일러가 잃어버린 슬롯**. 마지막 것이 핵심이다 — 되돌린 결과가 '조건 하나가 조용히 사라진 오디언스'면 실패보다 나쁘다 |
| **cut-over 이후 원본 표류는 rollback 에서 경고(차단 아님)** | cut-over 에서는 차단이다(검증하지 않은 것을 실행하게 된다). 그러나 rollback 에서 막으면 **잘못된 cut-over 에서 빠져나오는 유일한 길**을 무관한 편집 하나가 잠근다. 대신 기록에 남긴다 |
| **`--apply` 없으면 연결 자체가 읽기 전용** | 애플리케이션 규칙은 코드 한 줄로 우회되지만 read-only 트랜잭션은 우회되지 않는다. dry-run 이 "쓰지 않기로 되어 있다"가 아니라 "쓸 수 없다"여야 한다 |
| **`--apply` 는 `--actor`, rollback 은 `--reason` 을 요구한다** | 권위를 옮긴 사람과 되돌린 이유가 없으면 그 표는 감사 기록이 아니라 상태 덤프다. 차단된 시도도 로그에 남긴다 — 시도했다는 사실이 사라지면 사고 후 재구성이 안 된다 |
| **권위는 자산 하나씩 옮긴다**(record 만 배치) | 여러 자산의 권위가 한 명령으로 움직이면 사고의 반경이 명령 한 줄이 된다 |
| **`record` 는 실행 권위가 이미 Event IR 인 자산을 거부한다**(`AUTHORITY_ALREADY_EVENT_IR`) | 덮어쓰면 실행 중인 표현과 저장된 근거가 갈라지고, 그 상태에서 rollback 은 무엇으로 되돌릴지 모른다. `record` 만 배치이므로 이 차단이 없으면 전체 재실행 한 번이 cut-over 된 자산의 보존 payload 와 산출물을 갈아치운다. 되돌리려면 rollback 이 먼저다 |
| **`record` 는 돌 때마다 저장된 검증 근거를 폐기한다**(차단이 아니라 경고 `VERIFICATION_EVIDENCE_DISCARDED`) | 근거는 '그때 그 산출물'에 붙어 있어서, 원본이 그대로여도 다시 변환하면 그 근거를 남길 수 없다. 조용히 지우면 운영자는 승격이 왜 다시 필요해졌는지 모른 채 promote 를 다시 돌린다. **부작용을 알고 써라** — `record --apply` 전체 재실행 한 번이 `SHADOW_VERIFIED` 자산을 전부 `CONVERTED` 로 되돌린다(전이표가 그 되돌림을 허용한다) |
| **권위를 스탬프한 직후 `executed_authority` 로 다시 묻는다**(어긋나면 커밋하지 않는다) | 우리가 **쓴 것**과 실행기가 **읽는 것**이 갈라지면 "권위를 옮겼다"가 거짓이 되는데, 그 어긋남은 저장 형식이나 권위 판정 규칙이 바뀌는 날 조용히 생긴다. §4-2 의 일회 실측과 **별개로** 매 이관마다 트랜잭션 안에서 확인한다(cut-over 는 dry-run 에서도 본다). **(웨이브 5)** 이 사후 조건을 테스트가 고정한다 — 스탬프가 빠진 산출물을 주입해 cut-over·rollback 양쪽에서 커밋되지 않는 것을 본다(이전에는 방어선이 코드 한 줄뿐이었다) |
| **rollback 은 자산 파일 없이도 성립한다**(`--revision` 만으로) | 운영 사고 중에 코퍼스 파일이 손에 없을 수 있다. 되돌리기의 재료는 저장된 상태 안에 전부 있어야 한다 |
| **legacy 슬롯 지원 여부는 레지스트리에서 파생**(`targeting_ir.SLOT_SHAPES` + `member_filters_config`) | 도구가 슬롯 목록을 자기 소스에 적으면 소유자가 하나 더 생기고, 슬롯이 사라지는 날 이 판정만 "지원한다"고 답한다 |
| **차단 사유에 코드를 붙인다** | 자유 문장만 남기면 "무엇이 몇 건 막혔나"를 사람이 눈으로 세게 되고, 그때 목록은 읽히지 않는다 |

**웨이브 5에서 내린 것**

| 결정 | 이유 |
|---|---|
| **판정한 payload 와 스탬프할 플랜 행이 같은 자산인지 대조한다**(`PLAN_PAYLOAD_MISMATCH`) | 판정은 `--assets`/`--source` 로 읽은 것을 지금 다시 변환해서 하고 스탬프는 `audience_key` 로 찾은 저장 행에 한다 — 둘이 다르면 **파일 코퍼스에서 검증한 IR 이 다른 자산의 실행 권위**가 된다(§9-12 가 적어 둔 결함) |
| **대조 재료를 넘기지 않은 호출도 차단**(`PLAN_PAYLOAD_UNVERIFIED`) | 인자를 선택으로 두면 대조를 **빼먹은** 호출이 조용히 통과하는데, 그것이 애초에 이 결함의 모양이었다. 저장 플랜을 변환하지 못한 경우(`""`)도 같다 — "대조하지 못한 것은 '같다'가 아니다" |
| **같은 대조가 cut-over 는 차단, rollback 은 경고** | 방향이 반대다. 옮기는 길은 검증한 바로 그 자산에 대해서만 열려야 하고, 되돌아오는 길은 무관한 편집 하나로 잠기면 안 된다(웨이브 4의 `LEGACY_PAYLOAD_DRIFTED` 결정이 그대로 산다) |
| **`status` 도 cut-over 와 같은 재료로 판정한다** | status 에서만 대조를 빼면 "통과"라고 말해 놓고 cut-over 는 막히는 자산이 생긴다 — 그때 운영자는 원인을 자산에서 찾는다 |
| **조회 명령(`history`)은 `--apply` 가 붙어도 읽기 전용으로 연다** | 조회가 쓰기 연결을 열 수 있으면 "확인만 해보려던 실행"이 다시 운영 상태를 바꿀 수 있는 자리가 된다 — dry-run 을 read-only 트랜잭션으로 막은 것과 같은 이유다 |
| **조회는 성공만 거르지 않는다** — 막힌 시도를 `blocked` 로 구분해 함께 싣는다 | 사고 후 재구성에서 필요한 것은 대개 "무엇이 막혔나"다. 성공만 남기면 "권위가 두 번 움직였다"는 나오지만 "그 사이 열두 번 막혔다"는 사라진다 |
| **`--limit` 은 최신 쪽을 남기고, 잘렸다는 사실을 목록 안에 싣는다** | 조용한 절단은 "이게 전부"로 읽힌다(웨이브 3이 표본에 건 규칙). 요약에만 적으면 목록만 복사해 가는 순간 그 사실이 사라져서 `truncated_before` 를 첫 행에 붙인다 |
| **`history` 의 `--asset-id` 는 하나까지** | 여러 자산을 섞으면 시간순 목록이 자산 사이를 오가고, 그때 그것은 사건의 순서가 아니라 그냥 섞인 줄이다 |

---

## 4. 측정된 것 — 분류가 추론에서 실측으로

**A그룹 등가(같은 집합)**

- `purchase_membership` · `purchase_inactivity` → 컴파일 SQL 이 legacy 헬퍼 출력과 **별칭만 다른
  문자열 동일**. `tests/test_legacy_audience_migration.py` 가 그 등식을 고정한다.
- 경계 fixture 실행(④): `purchase_inactivity`+`purchase_membership`(복합 자산, 1명) ·
  `no_purchase`(2명) · `aggregate_conditions(≥3)`(2명) 모두 **회원 집합 완전 일치**.
  경계 3인(-90/-91/-89일)이 실제로 판정 대상이 됐는지도 함께 고정했다.

**D그룹 비등가(다른 집합) — `inactivity_period`**

```
legacy  : (B.LAST_LOGIN_DATE IS NOT NULL AND B.LAST_LOGIN_DATE <= 컷오프)
EventIR : NOT (CASE WHEN (B.LAST_LOGIN_DATE IS NOT NULL AND >= 컷오프) THEN 1 ELSE 0 END = 1)

only_in_legacy   = (1009,)   # 마지막 접속이 컷오프 정확히 그날 → 경계일 포함/제외 차이
only_in_event_ir = (1004,)   # 한 번도 접속하지 않은 회원(NULL) → NULL 포함/제외 차이
```

`tests/test_audience_shadow.py` 가 이 차이를 계약으로 고정한다. **차이가 사라지면 실패가 아니라
분류를 다시 보라는 신호**다(A 승격 시점).

**실DB 대조 결과 (CRMDW · 회원 69,609 / 주문 230,442행 · `--cost-repetitions 5`)**

| 자산 | ⑤ 회원 집합 | ⑥ p50 legacy → Event IR | 판정 |
|---|---|---|---|
| `aud-convertible-lifetime-absence`(평생 무구매) | **8,397명 완전 일치** | 300.8ms → 278.9ms (0.93×) | **cut-over 허용** |
| `aud-convertible-aggregate-only`(주문 3건 이상) | **19,953명 완전 일치** | 232.1ms → 254.9ms (1.10×) | ③ 미실행으로 차단 |
| `aud-convertible-absence`(90일 미구매 + 365일 구매) | 양쪽 0명 → **미실행** | 138.0ms → 99.8ms (0.72×) | ⑤ 미실행으로 차단 |

세 가지가 여기서 확인됐다. ① **A그룹 등가가 실데이터에서 성립한다** — fixture 12명이 아니라
회원 2만 명 규모에서 집합이 완전히 같다. ② **③이 대조하지 못한 자산을 ⑤가 판정했다**(집계는
조인 문장 ↔ 스칼라 서브쿼리라 서명이 다른 게 정상이고, 결과 집합이 판정한다 — 설계 의도대로다).
③ **Event IR 이 더 비싸지 않다** — 세 자산 모두 임계 안이고 둘은 오히려 빨랐다.

`aud-convertible-lifetime-absence` 가 여섯 단계를 모두 통과해 `blocking_reasons` 가 비었다.
**게이트가 처음으로 열렸고, 열린 자산은 정확히 하나다.**

### 4-2. 웨이브 4가 실측한 것 — 권위가 실제로 움직이는가

이관 기계의 판정·트랜잭션·CAS **계약**은 주입 저장소 위에서 검증했다(`record` 만 실DB 에서 한 번 더
돌렸다 — 이 절 끝의 스모크). 그러나 **"권위가 옮겨졌다"는 주장만은 흉내로 확인하지 않았다** —
cut-over 가 만든 플랜을 진짜 소비자에게 물어본다:

```
graph_rag._has_canonical_audience_authority(plan)          → True
graph_rag.build_event_expression_sql_candidate(plan)["sql"] → NOT EXISTS … (Event IR 산출물)
plan_validation.validate_executable_plan(plan).status       → executable
```

마지막 줄이 이번에 확인한 비자명한 사실이다. cut-over 한 플랜은 `audience_authority=event_ir`
이면서 `target_user` 슬롯이 **그대로 살아 있는** 모양인데(보존이 rollback 의 재료다), 플랜 검증에는
"canonical 표현 옆에 legacy 오디언스가 차 있으면 두 번째 실행 해석"이라는 hybrid 차단이 있다.
그 차단은 ingress 표식(`audience_requirement`/`semantic_plan`)에만 걸리므로 이행 표식
(`legacy_migration`)을 단 산출물은 통과한다 — 즉 **이행기의 dual-storage 는 그 규칙의 대상이 아니다.**
표식이 아니라 권위로 그 규칙을 바꾸면 cut-over 한 자산이 통째로 실행 불가가 된다(§9-11).

rollback 이후도 같은 방식으로 확인한다 — 권위는 legacy 로 돌아오고 `event_expression` 은 남는다.

**실DB(로컬 메타데이터 postgres) 스모크**: `record --apply` 를 자산 하나로 두 번 돌렸다.
① DDL 이 실제로 생성되고 ② 첫 실행이 INSERT(`row_version=1`) ③ 두 번째가 **진짜 CAS UPDATE**
(`row_version=2`) ④ 보존 payload 가 JSONB 로 그대로 왕복 ⑤ 감사 로그에 두 사건이
`LEGACY_ONLY→CONVERTED`, `CONVERTED→CONVERTED` 로 남았다. 확인 후 그 fixture 행은 지웠다(표는 남긴다).
남은 미검증은 **cut-over/rollback 트랜잭션**이다 — 저장된 `query_plan` 행이 있어야 `FOR UPDATE` 와
두 테이블 동시 갱신이 도는데, 자산 저장소가 아직 비어 있다(§9-9).

---

## 5. 작업 중 잡은 결함 — 여섯 다 내가 만든 것

| 결함 | 증상 | 수정 |
|---|---|---|
| 결과 집합 대조가 **공집합끼리 통과** | ⑤ 첫 실DB 실행에서 `aud-convertible-absence` 가 "회원 0명 집합 동일"로 **PASS** 했다. 그 자산은 '최근 365일 구매'를 요구하는데 실데이터가 2019년까지라 양쪽 다 0명이었다 — 뜻이 완전히 다른 두 조건도 대상이 없으면 똑같이 0명이므로 그 통과는 아무것도 증명하지 않는다. ③의 '빈 서명끼리 비교'와 **같은 종류의 퇴화**이고, 데이터가 비어 있는 자산일수록 검증을 쉽게 통과하게 만든다 | 양쪽 0명이면 `NOT_RUN`(사유 기록). ④에도 같은 규칙을 걸었다(경계 코퍼스라도 그 경계가 조건에 걸리지 않으면 0명끼리 같아진다 — 코퍼스에 경계를 더하라는 신호이지 통과가 아니다) |
| shadow 러너가 **부분 렌더를 대조** | legacy 쪽 조건 하나가 빠진 채 비교 → 무의미한 divergence 생산. 자산의 성질이 아니라 렌더러의 한계였다 | 렌더 불가 슬롯이 하나라도 있으면 `NOT_RUN`(사유 기록) |
| 구조 서명이 **빈 값끼리 비교** | `getattr(item, "column", "")` 기본값 때문에 모든 필드가 빈 문자열이었고, 단계가 항상 통과하는 장식이었다 | 실제 필드명 직접 접근(기본값 금지) + "술어 0개면 예외" 가드 |
| 승인 계약이 **장식** | `cutover_allowed` 가 `failed_stages` 도 막아 완전한 승인이 결코 통과하지 못했다 | 관측(`failed_stages`)과 판정(`blocking_divergences`) 분리 + `unaccounted_stages` 신설 |
| 어댑터에 **숨은 전역** | 카탈로그를 모듈 전역으로 주입하며 주석에는 "스레드 안전"이라 적었다(사실이 아니다) | 문맥 인자로 전달 |
| (웨이브 4) rollback 이 **움직인 적 없는 권위를 되돌린다** | 전이표는 `CONVERTED → LEGACY_ONLY` 를 허용한다(변환 취소). 그래서 아직 cut-over 하지 않은 자산에 rollback 을 걸면 그대로 통과했고, 감사 로그에는 "권위를 되돌렸다"가 남는데 **움직인 권위가 없다** — 사고 후 기록을 읽으면 있지도 않았던 cut-over 를 되돌린 것으로 보인다 | rollback 은 현재 권위가 Event IR 일 때만 성립(전이표와 별개 조건). 테스트가 계약으로 고정 |

추가로 `test_physical_binding_ratchet` 이 `audience_shadow.py` 의 하드코딩 2건을 잡았다.
**기준선을 올리지 않고** `subject_binding()`(카탈로그 파생)으로 제거했다 — 검증 계층이 테이블
이름을 자기 소스에 적으면 물리 바인딩 소유자가 하나 더 생기고, 이름이 바뀌는 날 검증기만 옛
이름으로 대조한다.

---

## 6. 수정·추가 파일

**수정(4)**

| 파일 | 변경 |
|---|---|
| `graph_rag.py` | 권위 판정 두 지점을 `audience_authority` 로 위임(`_has_canonical_audience_authority`, `build_event_expression_sql_candidate` 의 `canonical_authority`) + import |
| `plan_schema.py` | `audience_authority` 를 DERIVED 로 선언(사용자가 말한 조건이 아니라 이행 라우팅 판정) |
| `event_compiler.py` | `COMPILER_VERSION` 상수(바인딩 지문 입력) |
| `sql_dialect.py` | (웨이브 3) 방언별 `clock_functions()`(실행 시점 시계 어휘) · `datetime_anchor()`(타입 있는 앵커 표기) · `all_clock_functions()`(잔여 검사용 전체 어휘 — 다른 방언이 렌더한 시계를 '없다'고 세지 않기 위해) |

**신규 — 소스(6)**

| 파일 | 역할 |
|---|---|
| `audience_authority.py` | 권위 enum · 이행 상태 12종 · 허용 전이표 · `resolve_authority` (순수) |
| `migration_fingerprint.py` | canonical 직렬화 · 지문 3축 · schema checksum (순수) |
| `legacy_audience_migration.py` | A그룹 어댑터 · 경로 회계 · provenance · fail-close · envelope · manifest (순수) |
| `audience_shadow.py` | divergence 분류 · 승인 계약 · 6단계 모델 · cut-over 게이트 · sqlite 경계 비교기 · **(웨이브 3)** 시계 고정 · 스냅샷 집합 대조 · 실행 비용 대조 |
| `audience_cutover.py` | **(웨이브 4)** cut-over/rollback 판정 · 차단 사유 코드 · shadow 보고서 판독 · 플랜 권위 스탬프 (순수) · **(웨이브 5)** 판정↔스탬프 대상 대조(`_plan_row_blockers`) |
| `audience_migration_store.py` | **(웨이브 4)** 이행 상태 DDL · CAS(`StateGuard`) · 감사 로그 · 플랜 행 읽기/쓰기(커서 주입) · **(웨이브 5)** 감사 로그 읽기(`LogRecord`·`read_log`) |

**신규 — 도구(3)**

| 파일 | 역할 |
|---|---|
| `tools/migrate_legacy_audience.py` | dry-run 이행 배치 + offline replay |
| `tools/verify_legacy_audience_shadow.py` | shadow 검증 실행기(+ 스냅샷·비용 실행기 배선, 연결 소유) |
| `tools/cutover_legacy_audience.py` | **(웨이브 4)** record/promote/cutover/rollback — 연결·트랜잭션 소유, `--apply` 없으면 읽기 전용 · **(웨이브 5)** `history` 조회 명령 |

**신규 — 테스트(8) · fixture(2)**

`tests/test_audience_authority.py`(13) · `tests/test_legacy_audience_migration.py`(42) ·
`tests/test_migrate_legacy_audience_cli.py`(10) · `tests/test_audience_shadow.py`(21) ·
`tests/test_audience_shadow_snapshot.py`(32 — ⑤⑥) ·
`tests/test_verify_legacy_audience_shadow_cli.py`(8) ·
`tests/test_audience_cutover.py`(37 — 웨이브 4 판정 + 웨이브 5 플랜 행 대조) ·
`tests/test_cutover_legacy_audience_cli.py`(32 — 웨이브 4 명령·저장 계약 + 웨이브 5 조회·사후조건) ·
`tests/fixtures/legacy_audience_assets.json`(자산 13) ·
`tests/fixtures/audience_shadow_fixture.json`(경계 회원 12)

**신규 — 문서(1)**: `docs/plans_legacy_audience_strangler.md`(인벤토리·분류 근거·계약·운영)

**수정 — 스키마(1)**: `docs/data/metadata_ddl.sql` 에 `campaign_audience_migration` ·
`campaign_audience_migration_log` 추가. 도구 쪽 `CREATE TABLE IF NOT EXISTS` 와 컬럼이 갈라지면
테스트가 빨개진다 — 두 벌을 둔 이유는 이행 명령이 처음 도는 환경에서 스키마 부재가
"cut-over 실패"로 나타나면 그 실패의 원인을 자산에서 찾게 되기 때문이다.

---

## 7. 테스트

| 시점 | 결과 |
|---|---|
| 착수 기준선 | 1359 passed, 24 skipped |
| 웨이브 1 이후 | 1424 passed, 24 skipped (신규 65) |
| 웨이브 2 이후 | 1453 passed, 24 skipped (신규 94) |
| 웨이브 3 이후 | 1485 passed, 24 skipped (신규 126) |
| 웨이브 4 이후 | 1539 passed, 24 skipped (신규 180) |
| 웨이브 5 이후 | **1554 passed, 24 skipped** (신규 195) |

**기존 테스트는 하나도 고치지 않았다.** 권위 판정 교체가 기존 계약과 동치임을
`tests/test_audience_authority.py` 가 양방향(ingress 표식 유지 / 이행 표식 거부)으로 고정한다.
물리 바인딩 래칫 184 불변.

⑤⑥ 테스트는 **실DB 없이** sqlite 인메모리 실행자로 돈다(실행자가 주입이라 가능하다). 실데이터
분포는 운영 실행이 보고, 테스트가 고정하는 것은 **합성한 SQL 이 실제로 도는가**와 **게이트가 닫히는가**다.

웨이브 4 테스트도 실DB 없이 돈다(저장소가 주입이라 가능하다). 대역이 흉내내는 것은 **계약**뿐이다 —
CAS 는 판정이 읽은 상태 값 일곱을 걸고(§2 ⑨), 트랜잭션은 예외로 빠져나가며 전부 되돌리고,
읽기 전용 연결에서는 쓰기가 실패한다. 흉내내지 **않은** 하나가 "권위가 옮겨졌다"는 주장이고, 그것만은 진짜 실행기와
플랜 검증기에 물어본다(§4-2).

**재현**(로컬에 파이썬 없음 — 컨테이너가 저장소를 `/app` 에 마운트한다):

```bash
# 전체 테스트
docker exec recommendation-campaign-system-python-python-1 python -m pytest -q

# ①~④ 만(실DB 접근 없음). ⑤⑥ 은 사유와 함께 미실행이라 cut-over 는 계속 막힌다
docker exec recommendation-campaign-system-python-python-1 python -m tools.verify_legacy_audience_shadow \
  --assets /app/tests/fixtures/legacy_audience_assets.json --output /tmp/shadow.json

# ⑤(실DB 회원 집합) + ⑥(비용). 비용은 자산마다 (반복+1)×2 회 질의를 실DB 에서 돌린다
docker exec recommendation-campaign-system-python-python-1 python -m tools.verify_legacy_audience_shadow \
  --assets /app/tests/fixtures/legacy_audience_assets.json \
  --snapshot-db CRMDW --cost-repetitions 5 --output /tmp/shadow.json
```

**이관**(웨이브 4). `--apply` 가 없으면 읽기 전용 트랜잭션으로 열려 **쓸 수 없다**:

```bash
# 지금 각 자산이 어디에 있고 cut-over 가 왜 막혀 있는가(쓰지 않는다)
docker exec recommendation-campaign-system-python-python-1 python -m tools.cutover_legacy_audience \
  status --assets /app/tests/fixtures/legacy_audience_assets.json

# ① 변환 저장(권위 불변) → ② 보고서를 근거로 승격 → ③ 권위 이관
# record 는 돌 때마다 저장된 검증 근거를 폐기한다 — 승격을 마친 자산이 있으면 --asset-id 로 좁혀라(§11)
docker exec recommendation-campaign-system-python-python-1 python -m tools.cutover_legacy_audience \
  record --assets /app/tests/fixtures/legacy_audience_assets.json --apply --actor "$(whoami)"
docker exec recommendation-campaign-system-python-python-1 python -m tools.cutover_legacy_audience \
  promote --assets /app/tests/fixtures/legacy_audience_assets.json \
  --asset-id aud-convertible-lifetime-absence --shadow-report /tmp/shadow.json --apply --actor "$(whoami)"
docker exec recommendation-campaign-system-python-python-1 python -m tools.cutover_legacy_audience \
  cutover --assets /app/tests/fixtures/legacy_audience_assets.json \
  --asset-id aud-convertible-lifetime-absence --apply --actor "$(whoami)"

# 되돌리기 — 자산 파일 없이 저장 상태만으로 성립한다(사고 중에 파일이 손에 없을 수 있다)
docker exec recommendation-campaign-system-python-python-1 python -m tools.cutover_legacy_audience \
  rollback --asset-id aud-convertible-lifetime-absence --revision 1 \
  --apply --actor "$(whoami)" --reason "대상 수가 예상보다 20% 적다"

# 무슨 일이 있었나(웨이브 5) — 막힌 시도까지 시간순. 자산 파일도 --apply 도 필요 없다
docker exec recommendation-campaign-system-python-python-1 python -m tools.cutover_legacy_audience \
  history --asset-id aud-convertible-lifetime-absence --limit 50
```

종료 코드: `0` 통과 · `2` 명령을 시작할 수 없음(인자·재료) · `3` 판정이 차단 · `1` 적용 중 중단
(CAS 0행·플랜 갱신 실패 — 트랜잭션은 되돌아갔다). 보고 명령(`status`·`history`)은 **항상 0** 이다.

---

## 8. 아직 변환하지 않은 슬롯

**A — 변환 구현됨**: `purchase_date`(8자리·시각 경계 없음) · `purchase_membership` ·
`purchase_inactivity` · `behaviors:no_purchase` ·
`aggregate_conditions`(metric ∈ {order_count, purchase_amount}, 연산자 ∈ {≥,>}, 임계 > 0, scope/grain 없음)

**B — 카탈로그 확인 필요**: `recent_login`(유효성 가드 미선언) · `balance_conditions` ·
`profile_date_conditions` · `cart_*` · `purchase_object(s)` · 회원 속성(`gender`/`age_*`/`lifecycle`) ·
집계 지표 `total_item_quantity`·`distinct_*_count`·`discount_amount`

**C — IR/lowering 확장 필요**: `metric_trend` · `member_metric_ranking` · `member_metric_selection` ·
`entity_set_condition` · `purchase_count_ranking` · `group_ranking_target` · `region_density_target` ·
`cell_rate_target` · `birthday_target`(MMDD) · `purchase_date.from_time/to_time`(시각 노드 부재) ·
`age_exclude_ranges` · 집계 `average_order_amount`·`first|last_purchase_date`

**D — 업무 결정 필요**: `inactivity_period`(NULL·경계) · `signup_target`(anchor 정책) · `exclude` 컨테이너 ·
캠페인 반응 4종·`relational_operation`(canonical 이 이미 IR 직접 생산 — 슬롯 변환은 이중 생산자) ·
8자리 아닌 날짜 토큰 · 조건 0개 자산 · `aggregate_conditions` 의 `≤`/`<`·임계 ≤ 0

**미지원**: `interests` · `preferred_channels` · `price_sensitivity`(실DB 원천 없음)

---

## 9. 알려진 위험

1. **⑥이 재는 비용 축이 하나다** — 벽시계 시간만 잰다. 논리·물리 읽기, 계획 모양, sort/temp 는
   읽기 전용 SELECT 경로로 낼 수 없어(`SET STATISTICS IO`·`SET SHOWPLAN_XML` 이 SELECT 가 아니다)
   **재지 않았다는 사실을 이름으로** 남긴다(`UNMEASURED_COST_AXES`). 계획 퇴행은 아직 못 잡는다.
2. **비용 표본이 적다** — 측정 자체가 기본은 **꺼짐**(`--cost-repetitions 0`)이고, §4 실측은 5회로 쟀다
   (`run_performance_comparison` 의 인자 기본값도 5지만 CLI 는 언제나 명시값을 넘기므로, 5는 우리가
   지정한 값이다). nearest-rank 백분위라 표본이 적으면 p95·p99 가 사실상 최대값이고, 보고서는 그래서
   `samples` 개수를 항상 함께 싣는다. 판단이 필요한 자산은 반복을 올려 다시 잰다.
3. **A그룹 5종 중 4종이 실행 대조됨** — legacy 산출물을 독립 렌더할 수 없는 슬롯은 `purchase_date`
   하나다(`RENDERABLE_SLOTS` = 변환 가능 5종 − `purchase_date`). 그 슬롯을 실은 자산
   (`aud-convertible-window`·`aud-multi-window`)만 ③④⑤ 가 모두 `NOT_RUN` 이고, 미실행 사유도
   `target_user.purchase_date` 하나로 찍힌다. **술어끼리의 복합 자산은 장애가 아니다** —
   `purchase_inactivity`+`purchase_membership` 복합인 `aud-convertible-absence` 는 ③ '구조 서명 동일' ·
   ④ '회원 1명 집합 동일' 로 통과한다(그 자산의 ⑤ 가 미실행인 이유는 렌더가 아니라 양쪽 0명이다, §9-4).
   렌더러가 실제로 거부하는 복합은 **집계 문장과 술어가 섞인 자산**과 **집계 조건 2건 이상**이다 —
   legacy 빌더의 조립 규칙을 흉내내야 하는 경우이고, 흉내내면 검증이 자기확인이 된다(현재 코퍼스에는
   그 모양이 없다). 진단은 그대로다: 막힌 곳이 대조 엔진이 아니라 **legacy 쪽 렌더러**이므로
   **⑤가 배선돼도 해소되지 않는다**(웨이브 2의 예상은 틀렸다).
4. **⑤가 의미 있으려면 대상이 있어야 한다** — 양쪽 0명이면 미실행이다(§5). 실데이터가 2019년까지라
   '최근 N일' 계열 자산은 대부분 0명이 되므로, 그 계열의 ⑤는 **기준 시각을 데이터 구간으로 옮겨**
   다시 재야 한다(현재 `--as-of` 는 IR 컴파일 기준일이고 롤링 컷오프는 엔진 시계를 쓴다 — 이 둘을
   잇는 것이 다음 웨이브의 작은 숙제다).
5. **자산 저장소가 비어 있다** — 분류·경로 회계는 adversarial fixture 로 실측했지만 실제 운영 자산의
   슬롯 분포는 아직 모른다. 첫 실자산 배치에서 `LEGACY_PATH_UNCLASSIFIED` 가 나오는 것이 **정상 신호**다.
6. **④의 검증 엔진이 SQLite** — T-SQL 을 sqlglot 로 옮긴다. 양쪽을 같은 방식으로 옮기므로 편향은 없지만
   방언 고유 의미(collation 정렬 등)는 재현되지 않는다. 그래서 ⑤(실DB 스냅샷)가 필수 단계다.
7. **`aggregate_semantics` 재검증 미수행** — 변환된 IR 이 실행 경로에 오르면 분기 의미 검증을
   통과해야 한다(현재는 컴파일까지만 확인).
8. **revision 컬럼 부재 — 부분 해소.** 이행 상태를 `campaign_audience_migration` 이 소유하면서 revision 을
   그 표가 든다(자산 테이블 DDL 은 손대지 않았다 — 그 표는 이행의 것이 아니다). 다만 그 값을
   **발급하는 자리는 아직 없다**: 자산 로더가 payload 의 `revision` 을 읽고 없으면 1 로 보는데,
   `query_plan` 스키마에 그 키가 없어(plan_schema 선언 없음, 생산자도 없음) 저장 자산은 전부 1 이다.
   그래서 지금 revision 은 **상수축**이고 stale 판정은 지문 3축이 전담한다. 결과 둘 — 구 revision 을
   `EVENT_IR_PRIMARY` 로 둔 채 새 revision 을 record 하는 병행 이관이 안 되고, cut-over 한 자산의
   원본이 편집되면 record 가 `AUTHORITY_ALREADY_EVENT_IR` 로 막혀 rollback 이 반드시 선행된다.
   revision 을 누가 발급하는지가 다음 결정 사안이다.
9. **cut-over/rollback 트랜잭션은 실DB 에서 아직 안 돌았다** — `record` 의 DDL·INSERT·CAS 는 실제
   postgres 에서 확인했다(§4-2 스모크). 남은 것은 **저장된 `query_plan` 행이 있어야 도는 부분**이다:
   `SELECT … FOR UPDATE`, 두 테이블 동시 갱신, 실패 시 트랜잭션 롤백. 자산이 저장되는 날 **한
   자산부터** 돌려라. **(웨이브 5)** `history` 의 `SELECT_LOG_SQL` 도 같은 상태다 — 테스트는
   저장소 대역 위에서 **계약**(최신 우선 · limit+1 · 시간순 반환)만 고정하고, 그 SQL 자체
   (`%s::INTEGER IS NULL` 널 필터, `occurred_at DESC` 정렬)는 실DB 에서 아직 안 돌았다.
   `record --apply` 를 한 번 돌린 환경이면 바로 확인할 수 있다(로그 행이 이미 있다).
10. **상태 12종 중 넷은 어떤 명령도 쓰지 않는다** — `RETIRED`·`ROLLBACK_ELIGIBLE` 은 폐기·유예 명령
    자체가 없고, `QUARANTINED` 는 dry-run 배치 리포트의 문자열로만 존재하며(저장 경로 없음),
    `STALE` 은 그것을 만드는 `migration_status_for`/`stale_against` 에 프로덕션 호출자가 없어
    원본 표류가 상태가 아니라 cut-over 차단 사유 `SOURCE_FINGERPRINT_MOVED` 로만 나타난다.
    명령이 실제로 쓰는 것은 record 가 어댑터 분류에서 오는 5종, promote 가 `SHADOW_VERIFIED`,
    cutover 가 `EVENT_IR_PRIMARY`, rollback 이 `LEGACY_ONLY` 뿐이다. 전이표는 넷을 다 허용하므로
    **'전이표에 있다'를 '도달할 수 있다'로 읽지 마라.**
11. **cut-over 후 플랜은 '권위 event_ir + legacy 슬롯 잔존' 모양이다** — 지금 플랜 검증은 이 모양을
    실행 가능으로 읽는다(hybrid 차단이 ingress 표식에만 걸리기 때문, §4-2). 그 규칙을 표식이 아니라
    권위로 바꾸면 cut-over 한 자산이 통째로 실행 불가가 된다. 그리고 그 **표식 어휘가 지금 두 곳에
    각자 적혀 있다** — `audience_authority.CANONICAL_EVENT_EXPRESSION_SOURCES`(늘리지 않기로 사유를
    적어 둔 자리)와 `plan_validation` 의 하드코딩 리터럴 `{"audience_requirement","semantic_plan"}`.
    한쪽만 넓히면 권위 판정과 hybrid 차단이 갈린다. 규칙을 손대기 전에 어휘 소유자를 하나로 합쳐라.
12. ~~**cut-over 는 판정한 payload 와 권위를 얹을 플랜 행이 같은 내용인지 확인하지 않는다**~~
    — **웨이브 5에서 해소.** `evaluate_cutover` 가 `plan_source_fingerprint` 를 판정 입력으로 받아
    판정 대상(읽어 온 payload)과 스탬프 대상(저장 `query_plan` 행)을 대조한다. 다르면
    `PLAN_PAYLOAD_MISMATCH`, 넘기지 않았거나 저장 플랜을 변환하지 못했으면
    `PLAN_PAYLOAD_UNVERIFIED` — **둘 다 차단**이다. `status` 도 같은 재료로 판정한다.
    남은 권고는 그대로다: 실자산 이관은 `--source db` 로 돌려 판정 대상과 스탬프 대상이 애초에
    같은 행에서 나오게 하라(§10-1). 이제는 그러지 않으면 대조가 막는다.
13. **③이 원리적으로 대조할 수 없는 자산 계열이 있다** — 집계는 legacy 가 조인 문장, Event IR 이
    스칼라 서브쿼리라 모양이 항상 달라 ③은 `NOT_RUN` 이고(설계대로다), 필수 단계 미실행은 곧 차단이라
    ⑤가 완전 일치를 내도 게이트는 계속 닫힌다. `aud-convertible-aggregate-only`(⑤ 19,953명 완전 일치 ·
    ④ 회원 2명 집합 동일)가 정확히 그 상태다 — **지금 설계로는 집계 계열이 영구히 cut-over 불가**다.
    면제 규칙을 열지 말지가 결정 사안이다(§10-8).

---

## 10. 남은 할 일

**끝난 것**: 스냅샷 대조(⑤) · 성능 비교(⑥, 벽시계 축만 — §9-1) — 웨이브 3.
**원자적 cut-over · 명시 rollback · 감사 로그** — 웨이브 4(§2, §3).
**9(감사 로그 조회) · 10(플랜 행 대조) · 스탬프 사후조건 테스트** — 웨이브 5.

**번호는 재사용하지 않는다.** 끝난 항목도 자리를 비워 두고 지우지 않는다 — 문서 곳곳이
`§10-1`·`§10-5`·`§10-8` 로 서로를 부르고 있어서, 번호를 당기면 그 참조들이 조용히 다른 일을
가리키게 된다.

1 은 **자산이 저장되는 날 하는 일**이고 나머지를 막지 않는다. 2·5 는 검증의 사각을 줄이는 일,
3 은 **카탈로그 선언 + 어댑터 변환기 등록**, 4·8·11 은 **지금 바로 물어볼 수 있는 결정**이다 —
답을 기다리는 동안 다른 것을 막지 않으므로 먼저 질문을 띄워 두는 편이 좋다.

1. **첫 실자산 cut-over** — `campaign_target_audiences` 에 행이 생기면 ① `--source db` 로 **그 저장
   자산을 읽어 shadow 를 다시 돌리고** ② `record → promote → cutover` 를 한 자산부터 돌린다.
   §7 의 fixture 보고서는 실자산 promote 의 근거가 될 수 없다 — 자산 id 가 보고서에 없으면 명령이
   시작조차 못 하고("shadow 보고서에 이 자산의 항목이 없다"), id 가 같아도 payload 가 다르면
   `SHADOW_REPORT_FINGERPRINT_MISMATCH` 로 차단된다. 여섯 단계를 통과한 것은 **코퍼스 자산이지 저장
   자산이 아니다.** 같은 이유로 판정과 스탬프가 같은 행에서 나오게 `--source db` 를 쓴다(§9-12).
   그때 처음으로 실DB CAS·`FOR UPDATE`·트랜잭션 롤백이 실제로 돈다(§9-9).
   실행 직후 확인할 것: ① 그 자산의 응답 SQL 이 Event IR 산출물인가 ② 대상 수가 ⑤가 센 수와
   같은가 ③ `aggregate_semantics` 분기 의미 검증을 통과하는가(§10-12) ④ `history` 가 그 전이를
   기록했는가. 판정 대상과 스탬프 대상이 다르면 이제 `PLAN_PAYLOAD_MISMATCH` 로 막힌다(§9-12) —
   그것이 뜨면 자산이 아니라 **`--source` 를 먼저 보라.**
2. **⑤의 기준 시각을 데이터 구간에 맞추기**(작은 숙제, §9-4) — 롤링 창 자산이 실데이터에서 0명이라
   대조가 공허해진다. `--as-of` 가 IR 컴파일 기준일만 정하고 롤링 컷오프는 엔진 시계를 쓰는 지금
   구조에서, **앵커를 선언값으로 고정하는 모드**를 열지가 결정 사안이다(열면 '운영과 다른 시점'을
   재게 되므로 그 사실이 보고서에 남아야 한다).
3. **B그룹 해소** — 카탈로그 `login` 소스에 8자리 유효성 가드를 선언하고, **그 선언을 읽는
   `recent_login` 변환기를 어댑터 `CONVERTERS` 에 등록해야** A 로 승격된다. 지금 분류를 정하는 것은
   카탈로그가 아니라 코드의 정적 표다 — `CONVERTERS` 는 다섯 슬롯뿐이고 `recent_login` 은
   `BLOCKED_SLOTS` 에 (`NEEDS_CATALOG_BINDING`, `BINDING_VALIDITY_GUARD_MISSING`) 로 선언돼 있어,
   카탈로그에 가드가 생겨도 그 키는 경로 회계의 '미등록 키'로 떨어져 같은 분류를 낸다.
   **"선언 한 줄이 분류를 바꾼다"는 설계 지향이지 현재 동작이 아니다**(웨이브 1~3 노트의 그 표현은
   과장이었다). 변환기를 등록하면 그 `BLOCKED_SLOTS` 항목은 죽은 선언이 된다.
4. **D그룹 결정 묶음** — 업무 담당자 확인 필요: ① 미접속의 NULL·경계 정책 ② 집계 `≤`/`<` 에서
   무주문 회원 포함 여부 ③ `exclude` 컨테이너의 Not 결합 규칙 ④ 조건 0개 자산의 취급.
5. **legacy 렌더러 확장**(§9-3) — `purchase_date` 슬롯과 '집계 문장+술어 혼합'·'집계 조건 2건 이상'
   자산의 legacy 산출물을 독립 렌더할 수 있어야 ③④⑤가 그 자산들에 대해 돈다. 빌더에서 술어
   조립부를 떼어낼 수 있는지가 관건이다.
6. **문서 canonical authority 통일** — `docs/overview/structure.md` 등에서 "표현이 있으면 canonical"
   식 서술을 권위 기준 서술로 교체. 표식 어휘 이중 소유(§9-11)도 이때 함께 정리한다.
7. **`RETIRED` 경로**(§9-10) — legacy payload 폐기 명령. `ROLLBACK_ELIGIBLE` 유예 기간 정책과
   `QUARANTINED` 저장 경로를 함께 정한다(지금은 셋 다 전이표에만 있다).
8. **③ 면제 규칙 결정**(§9-13) — 서명 대조가 **원리적으로** 불가하다고 사유가 기록된 단계를 ⑤ 통과로
   대체할 수 있는가. 열면 승인자·기록 형식을 함께 정하고(승인 계약을 재사용하지 말 것 — 그것은
   'legacy 버그 수정' 라벨이다), 열지 않기로 하면 **집계 계열은 cut-over 대상이 아니라는 사실**을
   §8 분류에 적는다. 지금은 어느 쪽도 적혀 있지 않아 "언젠가 열리겠지"로 남아 있다.
9. ~~**감사 로그 조회 명령**~~ — **웨이브 5 완료.** `history` 명령 + 저장소 `read_log`/`LogRecord`.
   막힌 시도를 `blocked` 로 구분해 함께 싣고, `--limit` 은 최신 쪽을 남기며 잘린 사실을 목록 안에
   `truncated_before` 로 표시한다. `--apply` 가 붙어도 읽기 전용으로 연다.
10. ~~**cut-over 판정에 플랜 행 대조 추가**~~ — **웨이브 5 완료**(§9-12).
    `PLAN_PAYLOAD_MISMATCH`/`PLAN_PAYLOAD_UNVERIFIED`, `status` 도 같은 재료로 판정.
11. **revision 을 누가 발급하는가**(§9-8) — 웨이브 4가 "다음 결정 사안"으로 적어 두고 이 목록에는
    올리지 않았던 항목이다. 지금 revision 은 **상수축**(저장 자산은 전부 1)이라 stale 판정을 지문
    3축이 전담하고, 그 결과 ① 구 revision 을 `EVENT_IR_PRIMARY` 로 둔 채 새 revision 을 record 하는
    **병행 이관이 안 되고** ② cut-over 한 자산의 원본이 편집되면 record 가 막혀 rollback 이 반드시
    선행된다. 발급 주체를 정하기 전에는 이 둘이 제약이 아니라 **모르는 채로 밟는 함정**이다.
12. **`aggregate_semantics` 재검증**(§9-7) — 변환된 IR 이 실행 경로에 오르면 분기 의미 검증을
    통과해야 하는데 지금은 컴파일까지만 확인했다. 첫 실자산 cut-over(§10-1) 직후 확인 목록에
    **넣어야 하는 항목**이고, 지금 §10-1 의 "실행 직후 확인할 것"에는 빠져 있다.

---

## 11. 함정 — 다음 사람이 밟을 것

- **`_plan_event_expression(...) is not None` 배타 라우팅은 권위 판정이 아니다.** 그것은 "슬롯이
  파손되지 않았는가"이고, 권위와 같은 이름으로 부르면 둘이 곧 갈라진다.
- **`sql_semantics` 필드명이 바뀌면 구조 서명이 조용히 퇴화한다.** 그래서 `getattr` 기본값을
  쓰지 않고, "술어를 하나도 못 읽으면 예외"를 넣어 뒀다. 이 가드를 지우지 마라.
- **fixture 에 절대 날짜를 추가하지 마라.** `offset_days` 가 아닌 경계 행은 다음 날부터 경계가 아니다.
- **자산 코퍼스에 항목을 더해도 테스트 개수는 안 고쳐도 된다** — 개수는 코퍼스에서 파생한다.
  개수를 박아 두면 다음 사람이 경계 자산을 추가하는 대신 추가하지 않는 쪽을 고른다.
- **`tools/migrate_legacy_audience.py --apply` 는 여전히 거부된다.** 저장·권위 이관은 그 도구가 아니라
  `tools/cutover_legacy_audience.py` 가 소유한다(dry-run 도구가 쓰기를 할 수 있으면 "확인만 해보려던
  실행"이 운영 상태를 바꾼다). 그 거부를 살리지 말고 이관 명령을 써라.
- **CAS 가 0행을 돌려주면 재시도하지 마라.** 재시도는 우리가 읽지 않은 상태 위에 판정을 다시 쓰는
  일이다. 다시 읽고 **판정부터** 다시 하라 — 그래서 그 자리에 `require_swapped` 가 예외를 던진다.
- **`record --apply` 를 전체 코퍼스에 습관적으로 돌리지 마라.** 그 명령은 돌 때마다 저장된 검증
  근거를 폐기하고 `SHADOW_VERIFIED` 를 `CONVERTED` 로 되돌린다(경고 `VERIFICATION_EVIDENCE_DISCARDED`,
  §3). 승격을 마친 자산이 있으면 `--asset-id` 로 범위를 좁혀라.
- **cut-over 후 legacy 슬롯을 '정리'하지 마라.** 그 슬롯이 rollback 의 재료다. 지우는 순간 되돌리는
  길이 IR→슬롯 역변환뿐이 되고, 그 함수를 production rollback 경로로 쓰지 않기로 한 결정이 무너진다.
- **rollback 이 `event_expression` 을 지우게 만들지 마라.** 지우면 무엇을 검증했었는지가 저장소에서
  사라진다. 되돌아가는 것은 **권위 값 하나**다.
- **`evaluate_cutover` 의 `plan_source_fingerprint` 를 '선택'으로 되돌리지 마라.** 넘기지 않은 호출이
  차단되는 것이 그 인자의 요점이다 — 기본값을 '통과'로 두면 대조를 **빼먹은** 호출이 조용히 지나가고,
  그것이 §9-12 결함의 원래 모양이었다. `PLAN_PAYLOAD_UNVERIFIED` 가 뜨면 자산이 아니라 **호출자**를 보라.
- **`history --limit` 은 오래된 쪽을 버린다.** 앞에서 N개가 아니라 **최신 N개**다(사고 조사는 방금
  무슨 일이 있었는지부터 본다). 잘리면 첫 행에 `truncated_before` 가 붙으므로, 목록만 떼어 옮길 때
  그 표시를 함께 옮겨라.
- **판정 계층(`audience_cutover`)에 연결·시계를 넣지 마라.** 지금 그 모듈은 순수라서 판정만 보고
  테스트할 수 있다. I/O 가 들어오면 "이 판정이 어느 DB 를 보고 내려졌나"가 판정 안에서 정해진다.
- **"0명 집합 동일"을 통과로 읽지 마라.** 그것은 `NOT_RUN` 이고, 그렇게 만든 이유가 §5에 있다.
  대조 결과에서 회원 수가 0으로 보이면 자산이 아니라 **기준 시각·데이터 구간**을 먼저 의심하라.
- **비용 측정을 켜는 것은 실DB 에 부하를 만드는 유일한 스위치다.** `--cost-repetitions N` 은
  자산마다 (N+1)×2 회 질의를 돌린다. 자산 수 × 반복을 곱해 보고 켜라.
- **시계 고정 치환을 '문자열이면 충분하다'로 단순화하지 마라.** `DATEADD` 자리에서는 통하지만
  `to_char8(now())` 자리에서 문자열을 자르는 다른 뜻이 된다 — 오류가 아니라 틀린 컷오프로 나온다.
