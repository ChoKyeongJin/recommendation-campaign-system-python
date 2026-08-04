# canonical Event IR 단일 권위 — 배선 계획

목표는 legacy 슬롯을 걷어내는 것이 아니다. **"누가 오디언스를 실행하는가"의 판정자를 하나로 만들고,
그 판정이 `event_ir` 일 때 조건이 조용히 사라지는 경로를 전부 없애는 것**이다.

끝났을 때 성립하는 상태:

- 권위 판정자는 `audience_authority` 하나이고, 권위가 `event_ir` 인데 legacy 오디언스 조건이 남아 있으면
  조용히 사라지지 않고 **이름 있는 실패**로 드러난다.
- Event IR 레인이 "SQL 이 안 나온다"로 끝나는 모든 갈래가 **하나의 좌표**(stage+code+evidence)로
  응답·실패로그에서 읽히고, 모든 사유가 UI 단계로 사상된다.
- 카탈로그가 선언한 물리 바인딩이 배포 전 **종료코드 게이트**에서 실컬럼과 대조된다.
- "아직 못 하는 축"이 산문이 아니라 **기계로 반증 가능한 원장** 하나에서 파생한다.

이 계획을 다 해도 Event IR 로 표현 못 하는 축은 남는다(§5). 그 목록이 분류·소유자와 함께 명시되고,
축 하나를 여는 절차가 실사례로 검증되는 것이 이 계획의 종료 조건이다.

---

## 1. 지금 무엇이 깨져 있나

조사에서 `file:line` 으로 확인되고 적대적 검증에서 살아남은 것만.

| # | 문제 | 근거 | 조용한가 |
|---|---|---|---|
| 1 | hybrid 차단 가드가 권위가 아니라 `event_expression.source` **리터럴**로 걸려 있다. `plan_validation` 은 `audience_authority` 를 import 조차 하지 않고 같은 집합을 복제한다. 사유코드 `canonical_legacy_audience_conflict` 는 저장소 전체에서 정의 1줄뿐(테스트 0건) | `plan_validation.py:699-703` vs `audience_authority.py:71`; 실행 분기는 `graph_rag.py:14154` → `14161-14163` 삼항 | **예** |
| 2 | `required_sql_conditions` 가 `compile_member_target_conditions` 소비 슬롯을 다 덮지 않는다. `birthday_target` / `signup_target` / `coupon_usage_thresholds` / `cart_quantity_missing` / `age_exclude_ranges` / `member_metric_selection` 은 커버리지 게이트에도 안 걸려 **조건이 빠진 SQL 이 성공으로 출고**된다 | 소비: `graph_rag.py:13349·13404·13517·13532·13553`, 전용 빌더 `12434`. `required_sql_conditions`(16870-) 본문에 이 6개 이름 0회 | **예** |
| 3 | `query_pipeline` 실패가 붙인 `stage` 가 최종 `sql_result` 로 안 넘어간다. 그 dict 엔 `unresolved_source_conditions` 키 자체가 없고 `missing_input_conditions` 는 `[]` 하드코딩 | `graph_rag.py:14140` vs 반환 dict `11101-11125`(그중 `11113`) | **예** |
| 4 | Event IR 레인이 내는 사유(`semantic_ir_*`, `event_compiler_*`, `plan_validation_*`)가 사유→단계 매핑에 하나도 없다. 매핑이 없으면 `None` 이고 BFF 는 실패 단계 배지를 통째로 안 그린다 | `rag/failure_stage.py:77`(접두어 0건), `:169-171` | **예** |
| 5 | `coverage_exemptions` 선언 키가 카탈로그에 없어 면제 장치가 죽어 있고, 그걸 지키는 테스트는 빈 dict 에도 통과한다(공허한 가드). "안 적은 축"과 "표현 불가 축"이 45컬럼 차집합 안에서 안 갈린다 | `tools/canonical_coverage_inventory.py:132`; `audience_catalog.json` 최상위 11키에 그 키 없음; `tests/test_canonical_coverage_ratchet.py:78-86` | **예** |
| 6 | 좌표 계약 위반이 `CampaignQueryPlanValidationError` 로 승격돼 재시도 3회를 태우고, 폴백은 `intent='unknown'` 이라 플랜 병합에서 폐기된다. **배선 버그가 "조건을 못 찾았다"로 사용자에게 도착**한다 | `campaign_plan_v4.py:1230-1236` → `structurer.py:336-345`(광역 except, 분류 함수는 `85-92` 에서 provider 문자열 3종만) → `363-367` → `graph_rag.py:2313·2383·2920` | **예** |
| 7 | 권위 값이 어휘 밖이면 `plan_validation` 이 돌기 **전에** 예외가 터진다(HTTP 500) | `graph_rag.py:10507` → `11214/16776/16875` → `audience_authority.py:141-157`; `validate_executable_plan` 은 `10591` | 아니오 |
| 8 | 카탈로그 `fields[*].column` 이 자기 source 테이블에 실재하는지 검사하는 게이트가 **하나도 없다**. preflight 는 `table` 키가 없어 0건 검사, 테스트는 전 테이블 평면 집합이라 오귀속을 못 잡는다 | `db_swap_preflight.py:166-176`, `188-217`; `tools/physical_binding_inventory.py:54-70` | 아니오 |
| 9 | "표현할 수 없다"의 판정이 모델 산문에 남아 있다. 보정은 모델이 신고한 issue 에만 걸리고, 강등돼도 `unsupported_operations[].reason` 엔 모델 문장이 그대로 실린다 | `audience_execution.py:812-824`, `853-860`, `861-870`; 검증기 산 unsupported 는 `audience_validators.py:51-82·85-120` | 아니오 |
| 10 | 근거 좌표계 일치가 계약이 아니라 지역 규약이다. `run_audience_resolver` 는 payload 와 query 를 각각 받고 동일성을 검사하지 않으며, 같은 대조 규칙이 두 모듈에 각각 구현돼 있다 | `audience_execution.py:155-163` 과 `campaign_plan_v4.py:1592-1601`; 두 값을 묶는 유일한 지점은 `campaign_plan_v4.py:1361` | 아니오 |

가장 비싼 것은 **2번**이다. 1번은 조건이 사라진 사실이 최소한 SQL 모양으로는 남지만, 2번은 커버리지
게이트조차 통과해 **성공 응답**으로 나간다.

---

## 2. 단계 — 순서가 곧 의존성이다

### Phase 1 — 조용한 소실을 시끄럽게 (결합 0, red 창 0)

권위가 `event_ir` 일 때 사유 없이 사라지는 경우를 loud 실패로 바꾸고, 빌더가 만든 좌표가 `sql_result`
까지 살아남게 한다. **이 단계는 plan_schema·권위·cut-over 어디와도 결합하지 않고 어떤 테스트도 red 로
만들지 않는다.**

| 단계 | 파일 | 변경 | 크기 |
|---|---|---|---|
| 1-1 | `graph_rag.py`, `tests/test_member_slot_sql_coverage.py` | `required_sql_conditions` 에 무방비 슬롯 6개의 필수조건 추가. 토큰은 **생성부와 같은 함수**가 만든다(`_member_birthday_predicate` / `_member_signup_predicate` / `_coupon_usage_threshold_predicate` / `_cart_quantity_missing_predicate`) — 두 벌로 적으면 그 순간 갈라진다. 생일은 반대 분기를 `none_terms` 로 막아 월/일 뒤바뀜까지 잡는다 | S |
| 1-2 | `graph_rag.py`, `tests/test_query_pipeline_legacy_adapter.py` | (a) `IrSchemaError` 분기(14086-14098)에 `stage:"ir_schema"`, `code:"event_ir_schema_invalid"` (b) `QueryPipelineError` 분기(14131-14149)에 `code:"event_ir_compile_failed"` (c) 최종 `sql_result` 반환 dict(11101-11125)에 `unresolved_source_conditions` **추가**. `11113` 의 `missing_input_conditions: []` 는 손대지 않는다(사용자 문구 계약) | S |
| 1-3 | `graph_rag.py`, `tests/test_failure_honesty.py` | `build_sql_result` 진입부(10491 이전)에서 `coerce_authority` 를 한 번 시도하고 실패면 `failure_reason='audience_authority_invalid'` 로 즉시 종결. **판정자는 늘리지 않는다** — 값의 유효성만 보고 권위 자체는 여전히 `executes_event_ir` 가 읽는다 | S |

1-2 가 1-3 보다 먼저인 이유: Phase 2 의 진단 좌표가 `unresolved_source_conditions` 를 1순위 입력으로
읽는다. 좌표 모듈을 먼저 만들면 fixture 위에서만 그린이 된다.

**종료 조건** — 기준선 대비 신규 red 0. 권위 `event_ir` 에서 회원 슬롯이 빠진 SQL 이 커버리지
불만족. 좌표가 `stage`+`code` 를 갖고 차단 결과에 실림. 권위 오타 플랜이 예외 대신
`audience_authority_invalid` 로 종결.

#### 실측 기록 (구현 시점)

착수 기준선은 **2033 passed / 실패 6** 이었다(문서 인용 결함 1건 포함, 해소). 나머지 5건은
미커밋 작업·환경에서 온 선재 red 다: `test_canonical_event_ir_grounding` 1건,
`test_capability_discovery_cli` 1건, `test_module_size_ratchet`(graph_rag.py 가 상한 초과),
`test_physical_binding_ratchet` 2건(`.venv` 를 스캔 중).

계획이 틀렸던 것 둘을 구현 중에 확인했다.

**① 좌표의 소유자를 잘못 짚었다.** 계획은 "읽을 수 없는 저장 표현이 진입 가드에서 조용히
사라진다"고 적었지만, 그 실패의 소유자는 이미 `plan_validation` 이다
(`event_expression_schema_invalid`, 표현 부재는 `canonical_event_expression_missing`).
빌더가 좌표를 한 번 더 남기게 고쳤다가 `tests/test_query_pipeline_legacy_adapter.py` 의
"파손된 저장 표현은 빌더까지 내려가지 않는다"가 red 가 되어 되돌렸다 — 그 테스트가 계약이었고,
같은 실패에 소유자를 둘로 만드는 변경이었다. 경계는
`tests/test_event_ir_unresolved_coordinate.py` 가 좌표 쪽에서 함께 고정한다.

**② `stage`/`code` 의 실질은 진단 편의가 아니라 판정 입력이었다.** `plan_validation._marker` 는
`("code", "reason", ...)` 순으로 읽는다. `code` 가 없던 동안 Event IR 실패의 issue code 는
항목의 **한국어 문장**을 정규화한 값이었고, `_status_for_validation_code` 가 그 문자열에서
status 를 파생했다(`unsupported`/`missing`/`unresolved` 포함 여부). 두 새 코드는 기존과 같은
`INTERNAL_INVALID` 로 떨어져 status 는 불변이다.

**③ `unresolved_source_conditions` 는 공유 채널이다.** `build_sql_result` 초반이 이 리스트를
통째로 다시 계산하므로, 미리 심은 좌표는 지워진다. 좌표가 실리는 자리는 최종 반환 dict 가
아니라 **차단 결과**다 — 좌표가 남는 순간 `plan_validation` 이 internal_invalid 를 내기 때문이다.

---

### Phase 2 — 실패 좌표를 하나로 (관측만, 판정 기준 불변)

기존 종착 상태 생산자를 **하나도 바꾸지 않는다.** 파생 좌표만 만든다.

| 단계 | 파일 | 변경 | 크기 |
|---|---|---|---|
| 2-1 ✅ | `rag/failure_stage.py`, `tests/test_failure_stage_totality.py` | `_FAILURE_REASON_TO_STAGE` 에 Event IR 레인 사유를 **리터럴로** 추가(`semantic_ir_*`/`event_compiler_*`/`plan_validation_*`/`audience_authority_invalid`/`canonical_legacy_audience_conflict` 등). 새 테스트가 `plan_validation.status` Literal·`CAPABILITY_*` 를 import 해 곱집합을 계산하고 3방향 단언. **런타임 모듈은 아무것도 import 하지 않는다** | S |
| 2-2 ✅ | `audience_failure.py`(신규), `tests/test_audience_failure_coordinate.py`(아직 없다 — 이 단계가 만든다), `tests/test_module_layering.py` | `diagnose(query_plan, sql_result) -> dict \| None`. shape = `{stage, stage_detail, code, evidence, message, sources}`. 어휘는 `LANE_STAGES` 선언 표가 소유(각 행에 "왜 이 단계가 따로 존재하는가"를 데이터로). 입력 우선순위: `unresolved_source_conditions`(source=event_ir) → `plan['unsupported']` → `plan['semantic_ir']` → `plan['parser'].fallback_reason` → `sql_result['failure_reason']`. 어디에도 안 걸리면 `code='unclassified'` + sources(**조용한 None 금지**). 성공이면 즉시 None | M |
| 2-3 ✅ | `graph_rag.py`, `api.py`, `docs/operations/failure_diagnosis.md`, `docs/migration_runbook.md` | 응답에 `audience_diagnosis` 추가, debug 블록에 `unresolved_source_conditions` 추가, `_capability_failure_context_metadata`(api.py:3519-3527)에 넣어 **DDL 변경 없이** JSONB 조회 가능. 새 문서가 `stage` 어휘 **5종이 서로 다른 축**임을 표로 못박는다. runbook 의 존재하지 않는 `logs/unresolved.jsonl`·`tools/weekly_triage.py` 안내를 실재 장치로 재작성 | M |

새 로그 파일을 만들지 않는 것이 판정이다 — 요청별 흔적은 `logs/rag_llm/<날짜>/` 가 이미 갖고 있고,
소비자 없는 세 번째 로그가 생기면 운영자가 볼 곳이 다시 흩어진다.

**종료 조건** — 모든 `failure_reason` 이 단계를 갖는다. 종착 상태 9갈래가 `audience_diagnosis.stage` 로
구분된다. `include_debug` 없이도 응답에 실리고 실패로그에서 조회된다. runbook 의 모든 경로가 실재한다.

#### 2-1 실측 기록

미매핑 사유는 **11건**이었다 — 조립형 8(`plan_validation_*` 4 · `semantic_ir_*` 2 ·
`event_compiler_*` 2)과 리터럴 3(`audience_authority_invalid` · `invalid_dimension_filters` ·
`semantic_condition_conflict`). 전부 `failure_stage=None` 이었으므로 이 레인의 실패에는 스텝퍼가
아예 없었다.

가드를 쓰면서 **가드 자체의 구멍 둘**을 실측으로 찾았다.

- `failure_reason` 은 sql_result 만의 어휘가 아니다. LLM 호출 결과 dict 도 같은 키를 쓰는데
  (`missing_openai_api_key`) 그것은 파이프라인 단계가 아니라 호출 실패다. dict 에 `"sql"` 키가
  함께 있는 경우만 센다 — 안 그러면 표가 서로 다른 축을 섞어 담는다.
- 사유를 **지역 변수에 먼저 담으면** AST 스캔이 놓친다. 역검증에서 `audience_authority_invalid` 를
  매핑에서 빼도 전부 green 이었다(이 저장소의 `_relational_ir_blocking_sql_result` 도 같은 형태다).
  `failure_reason = "<리터럴>"` 대입까지 세도록 넓혀 세 형태 모두 red 가 되는 것을 확인했다.

역검증은 매번 한다. 가드를 넣고 그 가드가 실제로 무는지 확인하지 않으면 남는 것은 가드가 아니라
가드가 있다는 **믿음**이다.

#### 2-2·2-3 실측 기록

**① 계획이 적은 입력 우선순위가 틀렸다.** 계획은 "잔여물 먼저"(`unresolved(event_ir)` → `unsupported`
→ `semantic_ir` → `parser` → `failure_reason`)로 적었는데, 그러면 **앞 단계가 남긴 좌표가 실제 종착
게이트를 덮는다.** 한 요청은 게이트 하나로 끝나고, `failure_reason` 은 대부분 그 게이트의 **이름**이다
(`semantic_ir_*`·`plan_validation_*`·`audience_authority_invalid`·`query_plan_required_conditions_missing`).
그래서 규칙을 뒤집었다 — **사유가 게이트를 지목하면 그 레인이고, 지목하지 못하는 거친 사유
(`no_sql_candidates`·`sql_guard_failed` …)일 때만 잔여물로 정밀화한다.** 실측: 좌표가 남은 플랜에
의미 게이트가 걸리면 원안은 `event_ir_compile`(이미 지나간 자리)을, 수정안은 `semantic_resolution`
(실제 종착)을 가리킨다. 역검증에서 `_named_gate` 를 끄니 4건이 red 가 됐다.

**② 레인은 9갈래인데 계획의 입력은 5개다.** 둘은 다른 축이다 — 입력 하나가 여러 레인을 낳고
(`failure_reason` → `audience_authority`/`plan_validation`/`source_coverage`/`sql_generation`),
레인 하나가 입력 둘에서 온다(`semantic_resolution` 은 사유와 `plan['semantic_ir']` 양쪽).
`LANE_STAGES` 는 **레인**을 소유하고, `INPUTS` 가 입력을 소유한다.

**③ `parser.fallback_reason` 은 실패가 아니다.** rules 로 떨어지고도 SQL 은 나온다. 항상 읽으면
"조건이 있었는데 관문에서 떨어진" 실패를 구조화 폴백으로 오귀속한다. `_STRUCTURING_EXPLAINS`
(= "플랜에 조건이 없다"는 사유 3종)일 때만 이것이 더 나은 설명이다. 역검증에서 그 집합에
`sql_guard_failed` 를 넣으니 곧바로 red.

**④ runbook 의 거짓은 계획이 적은 2건이 아니라 12건이었다.** `tools/weekly_triage.py` ·
`logs/unresolved.jsonl` 외에 없는 도구·파일 10개(`regen_ir_goldens`·`regex_inventory` 도구/기준선/문서
·`method_mix_baseline`·`slot_policy.json`·`weekly_triage.md`·삭제된 테스트 4종)를 현재형으로 안내하고
있었고, env 표의 6개는 **아무도 읽지 않는 스위치**였다. 산문으로 고치면 다시 썩으므로
`tests/test_runbook_paths_exist.py` 를 만들어 기계가 읽게 했다 — 백틱 안 경로가 실재하지 않으면 red,
죽은 스위치가 표의 행으로 돌아오면 red, 그 스위치가 되살아났는데 목록에 남아 있어도 red.

**⑤ 개별 그린은 전량 그린이 아니다.** 이 작업이 세운 계약 테스트는 전부 개별로 green 이었는데,
전량 실행에서 **신규 red 1건**이 나왔다 — 새 테스트의 docstring 이 없는 `docs/data/*.json` 을
인용해 기존 문서 가드(`test_doc_claims`)를 깼다. 인정된 부재 표지(`없음`/`없다`/`삭제`/…)를 안 쓰고
"없는"이라고 적은 것이 원인이다. **가드가 옳고 내 문장이 틀렸으므로 가드를 고치지 않았다.**
종료 기준선: **2095 passed / 5 failed / 26 skipped**(선재 red 그대로, 신규 red 0).

**⑥ 좌표는 라이브에서 확인했다.** `장바구니에 담긴 상품의 색상이 파란색인 회원` →
`failure_stage.code='condition_recognition'`(사용자 축)과 `audience_diagnosis.stage='source_coverage'`
(소유자 축)가 **같은 응답에** 서로 다른 값으로 실렸고, 실패로그 행의
`context_metadata->'audience_diagnosis'->>'stage'` 로 조회됐다(DDL 변경 0).

---

### Phase 3 — 권위 단일화: **cut-over 를 먼저 이사시키고** 게이트를 켠다

> 적대적 검증이 원안(게이트 먼저)을 major 로 반증했다. `build_event_expression_sql_candidate` 는
> import 시점에 `_admitted_sql_builder` 로 감싸져 **직접 호출도** `validate_executable_plan` 을 먼저
> 태우고 EXECUTABLE 이 아니면 `None` 을 돌려준다(`graph_rag.py:12649-12662`, `17640-17655`).
> 게이트를 먼저 켜면 cut-over 자산이 5커밋 동안 **응답 경로·직접 호출 양쪽에서 SQL 을 못 낸다.**
> 순서를 뒤집어 red 창을 0으로 만들었다.

| 단계 | 파일 | 변경 | 크기 |
|---|---|---|---|
| 3-1 | `plan_schema.py` | `PlanKey` 에 `audience: bool \| None` 추가, 모든 CONDITION 키에 사유와 함께 채운다. **`semantic_plan` 과 `unresolved_source_conditions` 는 반드시 `audience=False`** — 전자는 canonical 레인에 상시 존재해 `True` 면 모든 canonical 요청이 충돌로 죽고, 후자는 Event IR 빌더 자신이 써서 자기참조 fail-close 가 된다. 컨테이너는 별도 상수 `AUDIENCE_CONTAINERS`. import 시점 assert | S |
| 3-2 | `plan_schema.py`, `audience_cutover.py`, `tools/cutover_legacy_audience.py`, 테스트 2종, `NOTES_migration.md` | DERIVED 키 `preserved_legacy_audience` 신설. `plan_after_cutover` 가 표면을 **실행 위치 → 보존 위치**로 이사. `plan_after_rollback(plan)` **시그니처 유지**하고 preserved 를 실행 위치로 복원. `audience_view(plan)` 을 모든 지문 계산 호출부가 쓴다. `rag/trace.py:308·401·798` 도 함께 옮긴다 | **L** |
| 3-3 | `audience_admission.py`(신규) | 순수 모듈(`plan_schema` + `audience_authority` + stdlib). API 3개: `legacy_audience_paths` / `has_empty_legacy_audience_surface` / `execution_conflicts`. `AudienceAuthorityError` 는 전파하지 않고 `('audience_authority',)` 경로로 접는다. `canonical_event_ir_grounding.has_empty_legacy_audience_surface` 는 **위임하지 않는다**(의미가 다른 술어다) | M |
| 3-4 | `plan_validation.py` | `699-709` 를 `execution_conflicts` 순회로 교체하고 리터럴 집합 제거. **이 한 줄이 축 A 의 원인이다.** 앞 단계에서 표면을 이미 비웠으므로 red 가 되는 테스트는 없다 | S |
| 3-5 | `graph_rag.py` | `14154` 권위 판정 직전에 fail-close. `14161-14163` 삼항은 **유지** — `compile_member_target_conditions` 는 `campaign_constraints.{category,offer_type,channels}` 도 unsupported 로 회계하므로 무조건 호출로 바꾸면 채널 있는 canonical 요청이 전부 탈락한다 | S |
| 3-6 | `audience_authority.py`, `audience_execution.py`, `graph_rag.py` | `declare_canonical_expression(plan, *, expression, source, receipts)` 단일 진입점. 호출부 3곳 전환(`audience_execution.py:776-783`, `graph_rag.py:10248-10251`, `10297-10305`). AST 가드는 **tests/ 를 제외**해야 한다(제외 안 하면 기존 직렬화 계약 테스트가 걸린다) | S |

3-2 가 L 인 이유와 축소 가능성은 §6 열린 질문 3을 보라.

**종료 조건** — 모든 커밋에서 전량 pytest 실패 0. 권위 `event_ir` + `target_user` 잔존 플랜이 응답
경로·빌더 직접 호출 **양쪽**에서 `canonical_legacy_audience_conflict` 로 종결. cut-over 자산은 표면이
비고 EXECUTABLE 이며 여전히 Event IR SQL 을 낸다. rollback 후 `target_user` byte 동일.
`plan_validation.py` 에 canonical source 리터럴이 없다.

#### 착수 전 재조사 (2026-08-04) — 위 표의 절반은 이미 참이거나 전제가 틀렸다

계획을 그대로 집행하기 전에 현재 코드로 여섯 단계를 하나씩 대조했다. **위 표를 지우지 않고 남긴다** —
무엇이 왜 바뀌었는지가 다음 사람에게 필요한 정보이기 때문이다. 실측 결과는 셋으로 갈린다.

**① 3-5 는 이미 구현돼 있다(작업 없음).** `_admitted_sql_builder`(`graph_rag.py:12953-12993`)가 등록된
모든 하위 빌더를 import 시점에 감싸고(`_install_sql_builder_admission_guards`, `18092-18107`),
`requires_event_ir(query_plan)` 이면 **`build_event_expression_sql_candidate` 가 아닌 빌더는 무조건
`None`** 이다(`12960-12962`). 응답 경로도 후보를 같은 id 하나로 이미 필터한다
(`graph_rag.py:10826-10835`). 즉 "canonical 요청에서 legacy 빌더가 돈다"는 상태는 응답 경로·직접 호출
**양쪽에서 이미 닫혀 있다.** 계획이 지목한 `14161-14163` 삼항(현재 `14486-14490`)도 유지가 맞다.

**② 3-2 의 전제가 틀렸다 — 이사할 자산이 0건이고, 이사는 현행 계약과 정면 충돌한다.**
`campaign_audience_migration` 실측 **0행**(2026-08-04, campaign_db). 그리고 `plan_after_cutover`
(`audience_cutover.py:702-714`)는 슬롯을 **의도적으로 남기며**, 그 이유가 docstring 에 적혀 있다
("슬롯을 지우지 않는 것이 rollback 을 '권위 되돌리기'로 유지하는 조건"). 표면을 `preserved_legacy_audience`
로 이사시키는 3-2 는 그 계약을 뒤집는 변경이지, 그 위에 얹는 변경이 아니다. §6 열린 질문 3 의 답이
"저장 자산 없음"으로 실측됐으므로 **L → S 축소가 성립한다.**

**③ 남은 실질은 3-4·3-6 과 그 테스트다.** 지금 `plan_validation.py:704-714` 의 hybrid 가드는 여전히
`event_expression.source ∈ {audience_requirement, semantic_plan}` **리터럴**로 걸린다. 권위로 걸리지
않으므로 표식 없는 페이로드(= cut-over 산출물, 지금은 0건)에서는 hybrid 가 안 걸리고,
`canonical_legacy_audience_conflict` 는 저장소 전체에서 **정의 1줄·테스트 0건**(재확인)이다.
표현 생산자는 4곳인데(`audience_execution.py:912`, `graph_rag.py:10467`·`3661`, `audience_cutover.py:712`)
그중 `graph_rag.py:3661` 은 표현만 쓰고 권위를 남기지 않는다.

**재조사 후 집행 표 — L 없음, 전부 S/M**

| 단계 | 파일 | 변경 | 크기 |
|---|---|---|---|
| 3-1 | `plan_schema.py` | 원안 유지(`PlanKey.audience`). 단 `AUDIENCE_CONTAINERS` 는 **신설이 아니라 이동**이다 — 이미 `legacy_audience_migration.py:48` 이 소유하고 `tools/cutover_legacy_audience.py:359` 가 그걸 읽는다. 두 벌로 적으면 그 순간 갈라진다 | S |
| 3-2′ | `audience_cutover.py`, 테스트 1종 | 이사하지 않는다. `plan_after_cutover` 산출 플랜이 **실행 경로에 들어오지 않는다**는 한 줄 계약 + 테스트로 대체. 저장 자산 0건이 근거이므로, 자산이 생기는 날 이 계약이 먼저 red 가 되도록 `campaign_audience_migration` 비어 있음을 테스트가 함께 고정한다 | S |
| 3-3 | `audience_admission.py`(신규) | 원안 유지. `canonical_event_ir_grounding.has_empty_legacy_audience_surface`(`:160-183`)는 **위임하지 않는다** — 그 술어는 `semantic_plan.nodes`·`set_expressions` 등 8개 표면을 함께 보고, admission 의 술어(컨테이너 2개)와 의미가 다르다 | M |
| 3-4 | `plan_validation.py` | `704-714` 를 `execution_conflicts` 순회로 교체하고 리터럴 집합 제거. **Phase 3 의 유일한 판정 변경이다.** 표면을 비우는 선행 단계가 없어졌으므로, 이 단계가 red 를 만드는지는 3-2′ 의 계약 테스트가 먼저 답한다 | S |
| 3-5 | — | **이미 구현됨(①).** 회귀 방지 테스트만 추가: 권위 `event_ir` 플랜에 legacy 빌더를 직접 호출하면 `None` | S |
| 3-6 | `audience_authority.py`, `audience_execution.py`, `graph_rag.py` | `declare_canonical_expression` 단일 진입점. 전환 대상은 3곳이 아니라 **4곳**이고, 그중 `graph_rag.py:3661` 은 권위를 안 남기는 호환 경로다 — 이 단계는 그 비대칭을 없애는 것이 목적이다. AST 가드는 tests/ 제외 | S |

**수정된 종료 조건** — 위 종료 조건에서 "cut-over 자산은 표면이 비고" 를 뺀다(이사하지 않으므로).
나머지는 그대로 두되, 다음을 추가한다: `canonical_legacy_audience_conflict` 에 테스트가 존재한다
(현재 0건). 권위 `event_ir` 인데 legacy 표면이 남은 플랜이 **source 표식 유무와 무관하게** 차단된다.

---

### Phase 4 — 근거 좌표계를 계약으로 (Phase 3 과 순서 의존 없음)

| 단계 | 파일 | 변경 | 크기 |
|---|---|---|---|
| 4-1 | `audience_execution.py`, `campaign_plan_v4.py`, **`structurer.py`**, 테스트 2종 | (a) 좌표계 선언(=`payload['original_query']`)을 상수/함수로 두고 진입에서 동일성 검사 → `CoordinateSystemConflict` (b) 원자 근거 대조 구현을 한 곳으로(`campaign_plan_v4.py:1592-1601` 은 호출만) (c) 실패에 코드 4종 부여 (d) **`structurer.py:342` 광역 except 앞에 전용 절**을 두어 즉시 break, `parser.fallback_reason='coordinate_system_conflict'` 고정 | M |

(d) 가 없으면 이 단계의 핵심 주장이 성립하지 않는다: 새 예외 타입은 `campaign_plan_v4.py:1230-1236`
(`AudienceValidationError` 만 잡는다)을 통과해 광역 except 에 잡히고, 재시도 분류 함수는 provider 스키마
문자열 3종만 매칭하므로 **재시도 3회를 태운 뒤 `intent='unknown'` 폴백**이 되어 오늘과 똑같이
"조건을 못 찾았다"로 도착한다.

**종료 조건** — 다른 문장을 넘기면 `coordinate_system_conflict` 로 즉시 종결하고 재시도 0회.
근거 대조 구현이 저장소에 하나. 채널 접미어 프롬프트 왕복 테스트가 존재하고 그린(현재 0건).

---

### Phase 5 — 카탈로그 접지 게이트와 축 개방 절차

| 단계 | 파일 | 변경 | 크기 |
|---|---|---|---|
| 5-1 | `db_swap_preflight.py` | `fields` 항목의 `source` 로 소유 테이블을 정해 `(table, column)` 을 검사. 제외 2개를 사유와 함께 명시: `expression`/`search_expressions` 가 있으면 조인 별칭 소유(현재 해당 6개, 전부 제외 조건에 걸림을 실측 확인), source 미상은 문제로 보고. **착수 첫 행동은 dry-run** — 처음부터 red 면 사람이 게이트를 끈다 | M |
| 5-2 | `db_swap_preflight.py`, `.github/workflows/tests.yml` | projection 의 `severity=='error'` issue 를 problems 로 승격. 축 C·E 테스트를 **fast gate** 에 등재(지금은 suite 잡에서만 돌아 타임아웃 시 축 C 가 전혀 검증되지 않는다). **기동 시점 강제는 하지 않는다** | S |
| 5-3 | `expressibility_ledger.json`(신규), `expressibility.py`, `capability_validation.py` | 행 = `gap_id`. 필드: `classification`(`BLOCKED_CATALOG`/`BLOCKED_IR_EXTENSION`/`BLOCKED_DOMAIN_DECISION` — `MigrationStatus` 이름 재사용) / `reason` / **`falsifier`** / `decision_owner+question` / `consumers`. **lru_cache 지연 로드, import 시점 assert 금지.** 무결성은 무인자 `validate_capabilities` 묶음 → preflight 종료코드 | M |
| 5-4 | `tools/canonical_coverage_inventory.py`, 기준선 | `exemptions()` 가 원장의 `consumers.columns` 를 읽게 한다. 기준선 재기록(기록 53/19, 실측 45/18)하되 `exempt_columns` 를 함께 기록해 "줄어든 것"과 "면제된 것"을 나눠 읽게 | S |
| 5-5 | `audience_execution.py` | 원장·자산 대조를 **모델 신고 여부와 무관하게** 먼저 돌린다. `unsupported_operations[].reason` 에 모델 산문을 넣지 않고 원장 문장을 넣되 모델 문장은 `model_claim`(미검증)으로 내린다. **원장에도 없으면 기존 고정 문구로 종결** — 재시도로 강등하지 않는다 | M |
| 5-6 | `audience_catalog.json`, `docs/operations/new_event_axis_runbook.md` | **축 하나를 실제로 연다**(기본 후보 `cart.cart_type`) + runbook 한 장. 원칙은 "새 사건 축은 셋 중 하나다, C 만 코드다" — A 선언 / B 값·어휘 / C 능력. 판별 질문은 **"기존 선언 키만으로 적을 수 있나?"** 이고 실사례로 못박는다(`active_cart`=A 커밋 `9429e99`; `match_mode`/`search_expressions`=C 같은 커밋; `selected_by`=C 커밋 `f01e53e`) | M |

5-6 이 필요한 이유: 세 설계 중 어느 것도 실제로 축을 열지 않아 "카탈로그 한 줄이면 열린다"가 증명되지
않은 채 절차만 문서화된다. 골든 단계는 **"expect_slots 를 단언하는 테스트는 없다"** 를 정직하게 표기해야
한다 — 존재하지 않는 안전망을 현재형으로 광고하는 것이 이 저장소의 알려진 재발 사고 모드다.

**종료 조건** — `python db_swap_preflight.py` 가 fields 컬럼 소유 테이블과 projection error 를 함께
검사하고 종료코드 0. 커버리지 면제가 원장에서 파생하고 사유 없는 면제가 red. 사용자에게 나가는 미지원
사유에 모델 산문이 없다. 축 하나가 선언만으로 열렸고 차집합 기준선이 그만큼 하향 재기록됐다.

---

## 3. 첫 커밋

**`required_sql_conditions` 에 무방비 슬롯 6개의 필수조건을 추가한다.**

이 6개는 `compile_member_target_conditions` 가 실제로 소비하지만 필수조건이 없어 권위가 `event_ir` 인
분기에서 커버리지 게이트에도 걸리지 않고 **성공 SQL 이 조건 없이 그대로 출고된다** — 축 A 에서 유일하게
완전히 무성인 실패 모드다. `plan_schema`·`audience_admission`·cut-over·권위 판정 어디와도 결합하지 않고,
어떤 기존 테스트도 red 로 만들지 않으며, 이후 어떤 단계도 이것 없이는 "조용한 소실이 남아 있는 상태"에서
시작한다.

세 설계 어디에도 이 단계가 없었다. 적대적 검증이 "가장 싼 첫 커밋"으로 지목한 것이다.

---

## 4. 불변식과 강제 위치

이 저장소는 계약 테스트가 일괄 정리 커밋에 **두 번**(`ce39f68`, `8ba50b6`) 삭제된 전력이 있다
(`capability_validation.py:337-343`). 그래서 모든 불변식에 테스트 + 기동 경로 이중 방어를 건다.

| 불변식 | 테스트 | 기동 경로 |
|---|---|---|
| 권위를 읽는 함수는 `resolve_authority` 하나 | `test_audience_authority.py:143-169` AST | capability_validation 새 축 → preflight 종료코드 |
| 권위 EVENT_IR ⇒ legacy 오디언스 컨테이너 비어 있음 | `test_plan_validation.py` + `test_canonical_audience_path.py` | ① `validate_executable_plan`(응답 경로 + admission wrapper — 우회 없음) ② 빌더 진입 fail-close |
| `required_sql_conditions` 가 모든 소비 슬롯을 덮음 | 소비 집합 ⊆ 생산 집합 AST 대조 | capability_validation 축 → preflight |
| legacy 표면 목록의 소유자는 `plan_schema` 하나 | `test_plan_schema_registry.py` | 모듈 최하단 **import 시점 assert** |
| canonical 생산자는 표식과 권위를 한 호출로 남긴다 | AST 가드(tests/ 제외) | `declare_canonical_expression` 단일 진입점 |
| cut-over 후 실행 위치 표면은 비고, rollback 은 preserved 복원 | 갱신된 5단언 | `plan_after_*` 사후 조건 + `PRESERVED_PAYLOAD_MISSING` |
| 실패한 응답은 반드시 단계를 갖는다 | `test_failure_stage_totality.py` 곱집합 3방향 | 그 테스트를 **fast gate** 에 등재 |
| 모든 종착 상태가 `audience_diagnosis` 하나로 관측 | fixture 전수 + LANE_STAGES 역방향(죽은 선언 금지) | 응답 조립이 무조건 호출 |
| 권위 오타는 500 이 아니라 명명된 실패 | `test_failure_honesty.py` | `build_sql_result` 진입부(권위 첫 판독보다 앞) |
| 좌표계는 `original_query` 하나, 불일치는 재시도 0회 | 재시도 0회 단언 + AST 가드 | 진입 동일성 검사 + `structurer` 전용 except |
| `fields[*].column` 은 자기 source 테이블에 실재 | `test_db_swap_preflight_gate.py` | preflight 종료코드(CI) |
| "표현할 수 없다"는 애플리케이션만 선언 | `test_expressibility_ledger.py` 반증기 전수 | 원장 조회로만 사유 생성 + preflight 무결성(**import assert 금지**) |

---

## 5. 이 계획을 다 해도 못 하는 것

Event IR 단일 권위로 가면 아래는 여전히 안 된다. **분류를 숨기지 않는 것이 이 절의 목적이다.**

**`BLOCKED_CATALOG`** — 선언 한 줄로 열린다:
장바구니 유형(`cart.cart_type`, 컬럼 실재) · 지역 시군구/동/우편번호(`SIGUNGU` 실재) ·
가치등급·예치금/적립금·누적 로그인·가입채널·자녀/SNS 등록(전부 "등록되지 않은 필드") ·
브랜드 축(카탈로그 fields 에 0개 — `event_semantic_registry` 엔 있어 rules 파서가 만들면 항상 unsupported) ·
`active_cart` 결합 상품·수량(선언 필드 0개)

**`BLOCKED_IR_EXTENSION`** — 컴파일러/대수를 넓혀야 한다:
생일 MMDD 비교 · **"기준시점보다 오래된" 창**(장바구니 N일 이상 유지, N일 이상 미접속 — 창 3종이 전부
"창 안" 방향만 컴파일된다) · `within_before`/hour Duration/`any`·`subsequent` selector(**IR·LLM 스키마는
허용하는데 컴파일러가 거부** — 모델이 계약을 지켜도 반드시 실패) · 결과 grain 이 회원이 아닌 조건
(셀 비율·그룹별 Top-N·지역 밀집 — Event IR 출력은 `SELECT DISTINCT + WHERE` 뿐) ·
`member_metric_selection` · rules 파서로 새 카탈로그 소스 부르기(파서는 코드 폴백 4개만 순회)

**`BLOCKED_DOMAIN_DECISION`** — 사람이 정해야 한다:
조건 0개(전체 회원) · 등급 이력 다월 연산(데이터가 2017년 1월뿐) ·
**사건 소스별 적재 구간 미선언** — `data_coverage` 가 12개 소스 전부 `unknown` 이고 실행 레인은 그것을
읽지도 않아, "최근 30일 구매"가 capability=supported / semantic=CONSISTENT 로 통과해 **"조건에 맞는 회원
0명"으로 보고**된다. *이 계획의 어느 단계도 이것을 잡지 않는다.*

---

## 6. 결정이 필요한 것

1. **표면 확대의 처리 방식.** Phase 3 은 가드를 `target_user`/`exclude` 두 컨테이너로 유지한다. plan 최상위
   조건 키까지 넓히면 그 조건을 가진 canonical 요청은 SQL 이 안 나오는데, 그 축은 Event IR 로 표현할 수도
   없다. (a) fail-close(정직하지만 기능 축소) (b) **표면이 비지 않으면 EVENT_IR 권위를 주지 않는다** —
   그러면 회원 컴파일러가 다시 돌아 Event IR 술어와 AND 합성된다(`graph_rag.py:14156-14159` 가 명시한
   현행 동작) (c) 축을 먼저 열고 좁힌다. **결정 전에는 넓히지 않는다.**
2. **표면 확대의 측정 수단.** 골든 코퍼스는 `parser='rules'` 라 권위 EVENT_IR 플랜을 한 건도 못 만든다
   (LLM 경로는 `GOLDEN_LIVE_LLM=1` 옵트인). 메타데이터 DB 저장 플랜 JSONB / 라이브 1회 스냅샷 /
   측정 포기 후 env 스위치 중 택일.
3. ~~**저장된 legacy 자산을 실제로 cut-over 할 계획이 있는가.**~~ **결정됨(2026-08-04).** 하지 않는다 —
   "자산이 legacy 에만 연결돼 있으면 쓰지 않는다"가 소유자 판단이다. 실측이 그 판단과 일치한다
   (`campaign_audience_migration` 0행). 따라서 Phase 3-2 는 표면 이사(L)가 아니라 **"cut-over 산출 플랜은
   실행 경로에 들어오지 않는다"는 계약 + 테스트**(3-2′, S)로 집행한다. 이 결정이 뒤집히면 —
   즉 legacy 에만 있는 자산을 실행해야 하게 되면 — 3-2′ 의 테스트가 먼저 red 가 되고, 그때 원안 L 로
   되돌아가거나 cut-over 가 표면을 비우도록 바꾸는 것이 선택지다. **결정을 계약으로 고정하는 것이
   3-2′ 의 목적이다**(산문으로 적으면 다음 사람이 0행을 "아직 안 썼을 뿐"으로 읽는다).
4. **어느 축을 먼저 열 것인가.** `cart.cart_type`(가장 싸다) / `subject.sigungu`(빈도 높으나 시→구 확장
   앱 로직이 따라온다) / `subject.birthday`(MMDD 가 IR 확장이라 한 줄로 안 끝난다) / "older-than" 창
   (두 축을 한 번에 여나 창 어휘 + 기간 검증기 동시 변경). **요청 빈도 데이터가 있으면 그것으로 정한다.**
5. **"전 회원 대상"(조건 0개)을 canonical 레인에서 어떻게 끝낼 것인가.** legacy 에는 있던 기능이다.
   되묻기 유지 / 항진 노드 추가 / Event IR 밖 명시적 "전체" 경로 중 택일.
6. **새 fail-close 의 사용자 문구.** `issue.path` 는 내부 플랜 경로이고 이 저장소는 "내부 필드명을 노출하지
   않는다"를 계약으로 갖는다. 경로는 운영자용 좌표에만 남기고 사용자에겐 조건의 한국어 라벨로 낼지,
   고정 문구로 갈지.

---

## 7. 채택하지 않은 선택지

되돌리지 않도록 이유와 함께 남긴다.

| 선택지 | 왜 아닌가 |
|---|---|
| 게이트를 먼저 켜고 cut-over 이사는 나중 | 불변식을 켜는 단계가 그것을 위반하는 유일한 현행 생산자보다 앞이면, 저장된 cut-over 자산이 **5커밋 동안 SQL 을 못 낸다**. `known_failures` 우회는 cut-over CLI 회귀를 그동안 통째로 가린다 |
| `plan_after_rollback(plan, legacy_payload)` 로 시그니처 변경 | 호출자와 monkeypatch(인자 1개 lambda)가 TypeError. 그리고 `legacy_payload` 는 플랜 행이 아니라 **자산 payload** 라 그걸로 복원하면 rollback 이 "다른 사본을 덮어쓰는 일"이 된다 |
| `rag/failure_stage.py` 가 곱집합을 런타임 생성 + import assert | 그 모듈이 스스로 선언한 순수성 규약을 깬다. 같은 계층의 `failure_messages` 가 정확히 이 이유로 지연 import 한다 |
| expressibility 원장에 import 시점 assert | 기존 import assert 는 전부 in-code 표다. 외부 JSON 을 응답 경로 import 사슬에 넣으면 파일 누락 하나로 API 기동이 죽는다(`docs/plans_ir_decoupling.md:160` 이 같은 실패 모드를 "raise 금지"로 못박았다) |
| 좌표 예외 타입만 만들고 `structurer.py` 는 안 건드림 | 새 타입이 광역 except 에 잡혀 재시도 3회 → `intent='unknown'` 폴백이 그대로 재현된다 |
| 확인 안 된 unsupported 를 `structurer_failure` 로 강등해 재시도 | 원장은 정의상 불완전하다. **원천이 실제로 없는** 축이 정직한 미지원 대신 구조화 실패로 강등돼 "조건을 찾지 못했습니다"로 도착 — 없애려던 것과 같은 모양의 새 오귀속 |
| 종착 상태 자체를 통합(`plan['unsupported']` 폐기) | 소비자가 최소 5계층이고 `unresolved_source_conditions` 는 공유 채널이다. 독립 커밋으로 못 쪼개고 회귀 원인이 "좌표 통합"인지 "게이트 변경"인지 안 갈린다 |
| 회원 속성을 전부 Event IR 로 흡수(축을 먼저 다 연다) | 이 축의 **목적지**이지 수단이 될 수 없다. 차집합이 45컬럼/18축이고 IR_EXTENSION·DOMAIN_DECISION 항목은 선언으로 안 열린다 |
| `canonical_event_ir_grounding` 를 `audience_admission` 으로 위임 | 두 술어의 의미가 다르다(`semantic_plan.nodes` 요구 / `dimension_filters` 미검사). 위임하면 둘 중 하나가 조용히 바뀐다 |
| projection error 를 import 시점 강제 | 런타임 fail-close 가 이미 있고, projection 은 전 저장소 ast 스캔 + git subprocess 라 부팅 실패 모드가 된다 |
| `legacy_audience_migration` 정비 | 사용자 목표 밖이다. 라이브 canonical 레인은 그 표를 읽지 않는다 |
| 골든 코퍼스로 표면 확대 영향 측정 | `parser='rules'` 코퍼스라 EVENT_IR 플랜을 못 만든다. 기준선이 0 으로 기록돼 "영향 0건"이라는 **거짓 근거**가 된다 |
