# 작업 노트 — canonical Event IR 단일 권위: 조용한 소실 제거 (2026-08-04)

계획서는 `docs/plans_event_ir_only.md`. 이 노트는 **그중 실제로 구현한 것**과 구현하면서 계획이
틀렸음을 확인한 지점을 기록한다.

목표는 legacy 슬롯 제거가 아니다. **권위가 `event_ir` 일 때 조건이 사유 없이 사라지는 경로를
없애고, 사라졌다는 사실이 이름을 갖게 하는 것**이다.

| 단계 | 무엇을 세웠나 | 상태 |
|---|---|---|
| 1-1 | 무방비 회원 슬롯 6개의 필수조건 + 검증 토큰 | 완료 |
| 1-2 | Event IR 실패 좌표(`stage`+`code`)와 차단 결과 전달 | 완료 |
| 1-3 | 권위 값 오류를 명명된 실패로 | 완료 |
| 2-1 | 사유→단계 매핑 총체성(미매핑 11건) + 곱집합 가드 | 완료 |
| 2-2 | `audience_failure.diagnose` 파생 좌표 모듈 | 완료 |
| 2-3 | 좌표를 응답·debug·실패로그에 배선 + 운영 문서 | 완료 |
| 3~5 | 권위 단일화 · 근거 좌표계 · 카탈로그 게이트 | **미착수** |

**테스트**: 착수 기준선 2033 passed / 6 failed → Phase 1·2-1 뒤 2062 / 5 →
현재 **2095 passed / 5 failed / 26 skipped**(9분 44초, 실측). 실패 5건은 §6 의 선재 red 그대로이고
**신규 red 0** 이다.

> 실행 도중에 파일을 고치면 pytest 가 이미 import 한 옛 모듈을 재므로, 그런 실행 두 번은 무효로
> 버리고 최종 코드 기준으로 다시 돌렸다. 그 사이 한 번은 **신규 red 1건**(`test_doc_claims`)이
> 실제로 나왔다 — 개별 테스트만 보고 "전량도 그럴 것"이라고 적었으면 놓쳤을 것이다(§3).

---

## 1. 무엇을 했나

### 1-1. 필수조건이 없던 회원 슬롯 6개 (`required_sql_conditions`)

`birthday_target` · `signup_target` · `coupon_usage_thresholds` · `cart_quantity_missing` ·
`age_exclude_ranges` · `member_metric_selection`. 이 여섯은 `compile_member_target_conditions`
(또는 전용 빌더)가 소비하는데 필수조건이 없었다. 권위가 `event_ir` 이면
`build_event_expression_sql_candidate` 가 회원 컴파일러를 통째로 건너뛰므로,
**조건이 빠진 SQL 이 커버리지 게이트까지 통과해 '성공'으로 출고**됐다. 조건이 사라진 사실이
SQL 모양에도 응답에도 남지 않는 유일한 부류였다.

실측으로 확인한 실효: `audience_authority='event_ir'` + `event_expression.source='legacy_migration'`
+ `birthday_target` 플랜에서 변경 전 커버리지 True(생일 조건이 증발한 SQL 이 성공) → 변경 후 False,
end-to-end `failure_reason='query_plan_conditions_missing'`.

### 1-2. Event IR 실패의 좌표 (`stage` + `code`)

`_record_event_ir_unresolved` 가 항목 모양을 단독 소유하고, `IrSchemaError`(`stage="ir_schema"`,
`code="event_ir_schema_invalid"`)와 `QueryPipelineError`(`stage=exc.stage`,
`code="event_ir_compile_failed"`) 두 분기가 그것을 쓴다. 좌표는 차단 결과 두 곳
(`_plan_validation_blocking_sql_result` · 의미 IR 차단)과 최종 반환 dict 에 실린다.

### 1-3. 권위 값 오류 (`_audience_authority_blocking_sql_result`)

`plan['audience_authority']` 가 닫힌 어휘 밖이면 `AudienceAuthorityError`(ValueError 하위)가
generic except 에 흡수돼 '구조화 실패'로 뭉개졌다. 이제 `failure_reason='audience_authority_invalid'`
+ `interpretation_status='needs_clarification'` 로 끝난다. **판정자는 늘리지 않았다** — 값의
유효성만 보고 권위 자체는 여전히 `audience_authority.resolve_authority` 가 단독으로 읽는다.

### 2-1. 사유→단계 매핑 총체성

미매핑 사유가 **11건**이었다(조립형 8: `plan_validation_*` 4 · `semantic_ir_*` 2 ·
`event_compiler_*` 2 / 리터럴 3: `audience_authority_invalid` · `invalid_dimension_filters` ·
`semantic_condition_conflict`). 전부 `failure_stage=None` 이라 이 레인의 실패에는 스텝퍼가 없었다.

### 2-2. 종착 좌표 하나 (`audience_failure.py`)

"SQL 이 안 나왔다"는 아홉 갈래로 끝나는데, 흔적이 남는 자리도 이름도 갈래마다 달랐다
(`unsupported` · `semantic_ir.status` · `parser.fallback_reason` · `failure_reason` · 좌표의 `code`).
`diagnose(query_plan, sql_result)` 가 그 다섯 입력을 읽어 좌표 하나를 **파생**한다 — **생산자는
하나도 안 바꿨다.** 반환은 `{stage, stage_label, stage_detail, code, evidence[], message, sources[]}`
이고 성공이면 `None` 이다.

레인 어휘와 "왜 이 레인이 따로 존재하는가"는 `LANE_STAGES` 선언표가 소유한다. `distinct_because` 는
주석이 아니라 **데이터**다 — 레인을 합치려는 사람이 그 한 줄을 반증하지 못하면 합치면 안 된다.

### 2-3. 배선 — 응답 · debug · 실패로그

| 표면 | 무엇이 | 왜 거기인가 |
|---|---|---|
| `api_response.audience_diagnosis` | 요약 좌표 1개 | `include_debug` 없이 실려야 화면·BFF 가 읽는다 |
| `debug.unresolved_source_conditions` | 원본 항목 전량 | 내부 경로·코드가 있어 debug 밖으로 못 낸다 |
| 실패로그 `context_metadata.audience_diagnosis` | 요약 좌표 1개 | **DDL 0**. 기존 JSONB 컬럼으로 집계된다 |

새 로그 파일은 만들지 않았다. 요청별 흔적은 `logs/rag_llm/<날짜>/` 가 이미 갖고 있고, 소비자 없는
세 번째 로그가 생기면 운영자가 볼 곳이 다시 흩어진다.

문서 둘: `docs/operations/failure_diagnosis.md`(신규 — `stage` 어휘 5종이 서로 다른 축임을 표로
못박고, 레인별 첫 행동과 조회 SQL 을 준다) + `docs/migration_runbook.md` 주간 루틴 재작성(§3-4).

---

## 2. 내린 결정과 이유

**이유가 없는 결정은 다음 사람이 되돌린다.**

| 결정 | 이유 |
|---|---|
| **필수조건 토큰은 술어 생성부와 같은 함수로 만든다** | 문자열을 두 벌로 적으면 그 순간부터 갈라지고, 갈라진 검증부는 옳은 SQL 을 '조건 누락'으로 되돌린다. `_member_birthday_predicate` / `_member_signup_predicate` / `_coupon_usage_threshold_predicate` / `_cart_quantity_missing_predicate` 를 그대로 호출한다 |
| **생일은 반대 분기를 `none_terms` 로 막는다** | 월/일이 `SUBSTRING` 자릿수(2 vs 4)로만 갈린다. 두 문자열이 상호 배타임을 실렌더로 확인했고, 막지 않으면 '이달 생일'이 '오늘 생일'로 컴파일된 SQL 도 커버로 센다 |
| **`member_metric_selection` 은 모드 무관 고정 2토큰을 요구한다** | 모드별 구문(TOP/ORDER BY/AVG)으로 요구하면 빌더가 파라미터 불량으로 후보를 못 내고 다른 빌더가 이긴 경우를 놓친다. `IS NOT NULL` + `member_selection:balance_{mode}` 는 세 모드 공통이다 |
| **읽을 수 없는 저장 표현의 좌표는 빌더가 남기지 않는다** | 그 실패의 소유자는 이미 `plan_validation` 이다(`event_expression_schema_invalid`, 표현 부재는 `canonical_event_expression_missing`). 빌더가 한 번 더 기록하면 한 실패에 소유자가 둘이 된다 — 그렇게 고쳤다가 `tests/test_query_pipeline_legacy_adapter.py` 의 "파손된 저장 표현은 빌더까지 내려가지 않는다"가 red 가 되어 되돌렸다(§3) |
| **좌표는 최종 dict 가 아니라 차단 결과에 실어야 한다** | 좌표가 남는 순간 `plan_validation` 이 internal_invalid 를 내므로, 정상 흐름에서 최종 반환 dict 는 그 항목을 볼 일이 거의 없다 |
| **권위 게이트를 `build_sql_result` 첫 문장에 둔다** | 권위를 읽는 첫 소비자(당시 `_apply_semantic_plan_pipeline` — 2026-08-05 SemanticPlanV2 폐기와 함께 삭제됨)보다 앞이어야 그 안의 예외가 사라진다. 게이트 위치 결정은 그대로 유효하다. 다만 이 게이트의 실효 범위는 `build_sql_result` 로 들어오는 플랜뿐이다(§5-1) |
| **런타임 매핑 표는 리터럴로 두고 총체성은 테스트가 강제한다** | `rag/failure_stage.py` 는 "plain dict 입력만 받는다"는 순수 모듈 불변식을 스스로 선언한다. 곱집합을 런타임에서 계산하려면 `plan_validation`/`event_compiler` 를 끌어와야 하고, 그러면 렌더링 계층이 코어 스키마 로딩 실패에 묶인다(같은 계층의 `failure_messages` 가 정확히 이 이유로 지연 import 한다) |
| **집계 경로에서 조건이 빠진 성공을 실패로 바꾸는 것을 유지한다** | `build_analytical_aggregation_sql_candidate` 는 회원 컴파일러를 호출하지 않아 '이번 달 생일인 여성 회원 수' 류가 지금까지 **생일 조건을 흘린 채 성공**했다. 조용한 오답보다 시끄러운 실패가 낫다는 것이 이 작업의 전제다. 근본 해결(집계 계약이 이 슬롯들을 실제로 컴파일)은 §7-1 |
| **`output_contract` 인덱싱을 `.get(...) or {}` 로 바꾼다** | 그 줄은 오래 도달 불가였다가 필수조건이 늘면서 드러났다. KeyError → 처리되지 않는 500 은 어떤 명명된 실패보다 나쁘다 |
| **가드를 넣을 때마다 역검증한다** | 가드가 실제로 무는지 확인하지 않으면 남는 것은 가드가 아니라 가드가 있다는 **믿음**이다. 실제로 이 원칙이 §3-3 을 잡았다 |

### 2-2·2-3 에서 추가로 내린 결정

| 결정 | 이유 |
|---|---|
| **사유가 게이트를 지목하면 그 레인이다** (계획의 "잔여물 먼저"를 뒤집었다) | 한 요청은 게이트 **하나**로 끝나고 `failure_reason` 은 대부분 그 게이트의 이름이다. 잔여물을 항상 먼저 읽으면 앞 단계가 남긴 좌표가 실제 종착 게이트를 덮어, 진단이 **이미 지나간 자리**를 가리킨다. 잔여물은 거친 사유(`no_sql_candidates`·`sql_guard_failed` …)를 정밀화할 때만 쓴다 |
| **`parser.fallback_reason` 은 조건부로만 읽는다** | 폴백은 실패가 아니다 — rules 로 떨어지고도 SQL 은 나온다. 항상 읽으면 "조건이 있었는데 관문에서 떨어진" 실패가 구조화 폴백으로 오귀속된다. "플랜에 조건이 없다"는 사유 3종(`_STRUCTURING_EXPLAINS`)일 때만 이것이 더 나은 설명이다 |
| **`unclassified` 도 레인이다** | 실패했는데 아홉 갈래 어디에도 안 걸린 것 자체가 결함이다. 조용한 `None` 으로 지우면 그 결함은 영영 관측되지 않는다. `sources`(값이 있던 입력의 목록)가 "어디를 열어야 하는지"를 함께 준다 |
| **`audience_failure` 는 stdlib 만 import 한다** | 이 좌표는 실패 응답을 만드는 경로에서 무조건 호출된다. 코어 스키마를 로드하는 모듈을 하나라도 끌어오면 **실패 진단이 실패**하고, 남는 것은 처리되지 않은 500 이다. 대가로 키·사유를 리터럴로 들고 드리프트는 계약 테스트가 잡는다(같은 계층의 `failure_messages` 가 지연 import 를 쓰는 이유와 같다) |
| **`stage` 어휘 5종을 합치지 않고 문서로 갈랐다** | UI 스텝퍼(사용자에게 어디까지 갔다고 보여줄까)와 종착 레인(코드의 누가 끝냈나)은 다른 질문이다. 합치면 한쪽이 반드시 거짓말한다 — `audience_authority_invalid` 는 스텝퍼로는 첫 단계지만 소유자는 조건을 읽지도 않은 진입 게이트다. 두 값은 **같은 응답에** 나간다 |
| **실패로그는 새 컬럼도 새 파일도 안 만든다** | 기존 `context_metadata` JSONB 에 넣으면 DDL 0 으로 집계된다. 새 로그 파일을 만들면 요청별 흔적이 `logs/rag_llm/` 과 나뉘어 운영자가 볼 곳이 다시 흩어진다 |
| **문서의 거짓을 산문으로 고치지 않는다** | runbook 은 "(이 테스트는 삭제됨)" 꼬리표를 단 채 안전장치 8종을 표에 남기고 있었다. 꼬리표는 사람만 읽고 기계는 안 읽어서, 그 사이 표는 "우리에겐 이런 안전망이 있다"로 읽혔다. `tests/test_runbook_paths_exist.py` 가 백틱 안 경로의 실재를 강제한다 |

---

## 3. 작업 중 잡은 결함 — 셋 다 내가 만든 것

| 결함 | 증상 | 어떻게 드러났나 | 수정 |
|---|---|---|---|
| **1) 필수조건만 늘려 정상 요청을 500 으로 만들었다** | 6개 중 5개는 `build_verified_condition_tokens` 가 토큰을 안 만드는 슬롯인데, `build_sql_result` 에 "필수조건은 있는데 토큰이 0개면 후보 생성 전에 종료" 게이트가 있다. 그 줄이 `query_plan["output_contract"]` 를 대괄호로 인덱싱해 **"이번 달 생일인 고객"이 `KeyError: 'output_contract'`** 가 됐다 | 전량 테스트 2059개가 **초록**이었다. 이 슬롯들을 end-to-end 로 태우는 테스트가 하나도 없었기 때문이다. 적대적 검토가 BEFORE/AFTER 를 실행 재현해 잡았다 | 같은 5슬롯의 검증 토큰 추가(같은 생성 함수 사용) + `output_contract` 방어 + **슬롯별 end-to-end 성공 테스트** |
| **2) 한 실패에 소유자를 둘로 만들었다** | 진입 가드가 읽을 수 없는 표현에 좌표를 남기게 고쳤는데, 그 실패는 이미 `plan_validation` 이 `event_expression_schema_invalid` 로 소유하고 있었다 | `tests/test_query_pipeline_legacy_adapter.py::test_malformed_stored_expression_is_rejected_before_any_builder_runs` 가 red. 그 테스트가 계약이었다 | 진입 가드를 원복하고, 경계를 `tests/test_event_ir_unresolved_coordinate.py` 가 좌표 쪽에서 함께 고정 |
| **3) 총체성 가드에 구멍이 있었다** | 사유를 **지역 변수에 먼저 담으면** AST 스캔이 놓친다. 역검증에서 `audience_authority_invalid` 를 매핑에서 빼도 전부 green 이었다(내가 방금 추가한 사유인데도) | 가드를 넣고 곧바로 "한 줄 빼면 red 인가"를 돌렸다 | `failure_reason = "<리터럴>"` 대입까지 세도록 확대. 세 형태 모두 red 확인 |

부수로 잡은 것 둘:

- **한국어 문장이 issue code 로 승격되고 있었다.** `plan_validation._marker` 는
  `("code", "reason", ...)` 순으로 읽는데 Event IR 실패 항목에 `code` 가 없어서, issue code 가
  `"조건을_실db_술어로_컴파일하지_못했습니다:_..."` 였다. `_status_for_validation_code` 가 그
  문자열에서 status 를 파생하므로 판정 입력이기도 했다. 두 새 코드는 기존과 같은
  `INTERNAL_INVALID` 로 떨어져 status 는 불변이다(실행 확인).
- **`unresolved_source_conditions` 는 공유 채널이다.** `build_sql_result` 초반이 이 리스트를
  통째로 다시 계산하므로 미리 심은 좌표는 지워진다. 좌표는 refresh **뒤에** 기록될 때만 산다.

### 2-2·2-3 에서 잡은 것

| 결함 | 어떻게 드러났나 | 수정 |
|---|---|---|
| **죽은 상수를 내가 만들었다** | 우선순위 구조를 뒤집으면서 `_BLOCKING_SEMANTIC_STATUSES` 가 아무도 안 읽는 채로 남았다. "죽은 선언 금지"가 이 모듈의 논지인데 모듈 자신이 위반했다 | 상수 제거 + `test_no_declaration_in_the_module_is_dead`(AST) 신설. 가짜 상수를 넣어 red 를 확인 |
| **새 문서가 기존 문서 가드를 깼다** | 전량 실행에서 `test_doc_claims::test_no_source_claims_a_missing_config_json_as_authority` 가 red. 없는 `docs/data/*.json` 을 인용하면서 인정된 부재 표지를 안 썼다 — "없는"은 표지 목록(`없음`/`없다`/`삭제`/…)에 없다 | docstring 을 인정된 표지로 재작성. **가드가 옳고 내 문장이 틀렸다** — 가드를 고치지 않았다 |
| **계획의 입력 우선순위가 잘못돼 있었다** | 원안(잔여물 먼저)으로 구현하면, Event IR 좌표가 남은 플랜에 의미 게이트가 걸릴 때 진단이 `event_ir_compile`(이미 지나간 자리)을 가리킨다. 역검증에서 `_named_gate` 를 꺼 보고 4건 red 를 확인했다 | "사유가 게이트를 지목하면 그 레인" 규칙으로 뒤집고, 계약 테스트 2건(`test_named_gate_wins_over_leftover_coordinates` / `test_leftover_coordinate_refines_a_coarse_reason`)이 양방향을 고정 |
| **폴백을 항상 읽으면 실패를 오귀속한다** | `_STRUCTURING_EXPLAINS` 에 `sql_guard_failed` 를 넣어 보니 곧바로 red — 조건이 있었던 실패가 "구조화기가 못 돌았다"로 나갔다 | 그 집합을 "플랜에 조건이 없다"는 사유 3종으로 제한 |
| **runbook 의 거짓이 계획이 적은 2건이 아니라 12건이었다** | 경로 스캐너를 붙이자마자 12개가 한 번에 나왔다(없는 도구 3 · 없는 파일 5 · 삭제된 테스트 4). env 표의 6개는 별도로 코드 전수 grep 으로 확인 | 실재 장치로 재작성 + `tests/test_runbook_paths_exist.py` 신설(경로 실재 · 죽은 스위치가 표의 행으로 못 돌아옴 · 스위치가 되살아나면 목록이 red) |
| **`_semantic_ir_blocking_sql_result` 은 자기 `semantic_ir` 사본을 싣는다** | 좌표가 플랜만 읽으면, 저장된 sql_result blob 만으로 사후 진단할 때 의미 레인이 통째로 안 잡힌다 | `_semantic_ir` · `_unresolved` 가 **플랜과 결과 양쪽**을 읽는다 |

---

## 4. 수정·추가 파일

**수정(6)**

| 파일 | 변경 |
|---|---|
| `graph_rag.py` | `required_sql_conditions` 회원 슬롯 6종 · `build_verified_condition_tokens` 같은 5종 토큰 · `_record_event_ir_unresolved` 신설 및 두 분기 전환 · 차단 결과 2곳 + 최종 dict 에 `unresolved_source_conditions` · `_audience_authority_blocking_sql_result` 신설 및 진입 배선 · `output_contract` 방어 · 응답에 `audience_diagnosis`(+5줄) |
| `rag/failure_stage.py` | `_FAILURE_REASON_TO_STAGE` 에 Event IR 레인 사유 11건 (+25줄) |
| `api.py` | debug 블록에 `unresolved_source_conditions` · `_capability_failure_context_metadata` 에 좌표(+11줄, **DDL 0**) |
| `tests/test_failure_honesty.py` | 권위 오류 계약 3건 (+54줄) |
| `tests/test_module_layering.py` | 진단 리프의 stdlib-only 계약 (+27줄) |
| `docs/migration_runbook.md` | 주간 루틴을 실재 장치로 재작성 · env 표에서 죽은 스위치 6개 제거 · 안전장치 표를 실재하는 것만으로 · "철거된 장치" 절 신설 |

**신규 — 테스트(5)**

| 파일 | 무엇을 고정하나 |
|---|---|
| `tests/test_member_slot_sql_coverage.py` (12) | 슬롯↔필수조건↔토큰 3자 일치, 생성부 SQL 이 자기 조건을 만족, 회원 술어 없는 SQL 은 불만족, 생일 월/일 뒤바뀜, **슬롯별 end-to-end 성공** |
| `tests/test_event_ir_unresolved_coordinate.py` (8) | 좌표의 `stage`+`code`, 한국어 문장이 코드로 승격되지 않음, 두 분기의 항목 모양 동일, 소유권 경계(읽을 수 없는 표현·표현 부재는 plan_validation 소유), 차단 결과 전달 |
| `tests/test_failure_stage_totality.py` (5) | 조립형·리터럴 사유가 전부 매핑됨, 스테일 매핑 없음, 매핑이 가리키는 단계가 실재, 미매핑 시 `None` 은 계약 |
| `tests/test_audience_failure_coordinate.py` (14) | 9개 레인이 **전부 실행에서 도달**됨(죽은 선언 금지), 좌표 모양 하나, `code` 가 한국어 문장이 아님, 게이트 지목이 잔여물을 이김·거친 사유는 잔여물이 정밀화함(양방향), 폴백이 실제 실패를 안 덮음, 조건 IR 게이트 둘이 plan_validation 레인 공유, 리터럴 키가 소유 모듈과 일치, **모듈에 죽은 상수 없음** |
| `tests/test_audience_diagnosis_wiring.py` (8) | 좌표가 `include_debug` 없이 응답에 실림, 성공엔 없음, 두 축이 같은 응답에 다른 값으로 나감, 실패로그 JSONB 에 실림·좌표 없으면 키 자체가 없음, debug 가 원본을 노출, 운영 문서의 레인 목록이 코드와 일치, 새 로그 장치 없음 |
| `tests/test_runbook_paths_exist.py` (9) | 문서가 적은 경로가 전부 실재, 죽은 env 스위치가 표의 행으로 못 돌아옴, 그 스위치가 되살아나면 목록이 red |

**신규 — 모듈(1)**: `audience_failure.py` — `LANE_STAGES` 선언표 + `diagnose()`. stdlib 만 import.

**신규 — 문서(3)**: `docs/plans_event_ir_only.md`(5단계 계획·불변식·남는 것·열린 질문),
`docs/operations/failure_diagnosis.md`(`stage` 5축 구분 · 레인별 첫 행동 · 조회 SQL), 이 노트.

---

## 5. 확인했지만 고치지 않은 것

1. **권위 게이트의 실효 범위는 `build_sql_result` 뿐이다.** 라이브 경로는 구조화 단계
   (`_grounded_canonical_event_ir_repair`)가 훨씬 앞서 `requires_event_ir` 를 부르고 거기서
   `AudienceAuthorityError` 가 그대로 올라간다. `plan_validation` · `_admitted_sql_builder` ·
   `compile_executable_plan` 도 여전히 raise 한다. 값 정규화를 `resolve_authority` 진입부로
   올리는 것이 근본 해결이다.
2. ~~**좌표가 `api_response` 로는 안 나간다.**~~ → 2-3 이 해소했다(응답 `audience_diagnosis` ·
   debug 원본 · 실패로그 JSONB). 라이브·DB 로 확인.
3. ~~**`_describe_sql_failure` 에 `audience_authority_invalid` 분기가 없다.**~~ → 이번에 닫았다.
   배지·좌표가 정직해도 본문이 "조건을 만족하는 검증된 SQL 이 없습니다"면 사용자는 본문을 따라
   조건을 고쳐 쓰고, 고칠 곳은 저장된 실행 설정이라 아무리 고쳐도 안 풀린다. 게이트가 이미 만든
   문구를 그대로 노출한다(`tests/test_failure_honesty.py` 계약 1건 추가).
4. **`missing_input_conditions[0]['label']` 이 내부 식별자다.** 사용자 문구가
   `'audience_authority_invalid' 조건을 …` 로 나간다(같은 관례가 `_plan_validation_blocking_sql_result`
   에도 있다). 사람이 읽는 이름으로 바꾸는 것이 맞다.
5. **`age_exclude_ranges` 와 `member_metric_selection` 은 토큰 문자열이 두 벌이다.** 나머지 4종과
   달리 공유 헬퍼가 없어 생성부/검증부가 각자 조립한다(`f"NOT ({col} BETWEEN …)"`,
   `f"member_selection:balance_{mode}"`). 헬퍼로 뽑는 것이 이 파일의 규칙이다.
6. **트레이스 화면은 자기 진단을 따로 갖는다.** `rag/trace._trace_failure_diagnosis` 가
   `failure_reason` 을 사람 문구로 다시 분류한다. `audience_diagnosis` 를 거기 얹으면 한 화면에
   진단이 두 벌이 되므로 손대지 않았다 — 통합은 트레이스 진단의 소비자를 먼저 세어야 한다.
7. **`stage_detail` 이 자주 `None` 이다.** 라이브 실측에서 `source_coverage` 좌표의 `stage_detail`
   이 비었다 — `_refresh_unresolved_source_conditions` 가 만드는 항목 일부에 `source` 키가 없다.
   좌표의 결함이 아니라 항목 생산자의 결함이고, 없는 값을 지어내지 않는 쪽을 택했다.

---

## 6. 선재 red 5건 (Phase 1·2-1 귀속 아님)

| 실패 | 원인 |
|---|---|
| `test_canonical_event_ir_grounding::test_canonical_semantic_lowering_failure_never_calls_legacy_bridge` | 미커밋 작업 소관 |
| `test_capability_discovery_cli::test_candidate_cli_writes_only_to_explicit_path_and_promotion_is_blocked` | 미커밋 작업 소관 |
| `test_module_size_ratchet::test_no_module_exceeds_its_ceiling` | `graph_rag.py` 가 상한 17,330 초과. HEAD 17,324(통과) → 미커밋 작업 +497 → Phase 1·2-1 +246 → 2-2·2-3 **+15**(신규 로직은 전부 `audience_failure.py` 로 뺐다) |
| `test_physical_binding_ratchet` 2건 | `.venv/Lib/site-packages/*` 를 스캔하는 환경 아티팩트 |

모듈 크기 래칫은 **결정이 필요하다**: 신규 함수를 `rag/` 하위로 빼거나, 상한을 올리고 커밋
메시지에 사유를 남긴다. 초과분의 다수가 미커밋 작업 쪽이라 임의로 올리지 않았다. 2-2·2-3 은
이 래칫을 의식해 새 로직을 `graph_rag.py` 에 넣지 않았다 — `graph_rag` 쪽 증분은 응답 한 줄과
사용자 문구 분기뿐이다.

---

## 7. 남은 할 일

### 7-1. 지금 결정된 것의 뒷정리

- **집계 계약이 회원 슬롯을 컴파일하게 만든다.** 현재는 '요구는 하는데 어느 컴파일러도 낼 수 없는'
  상태라 `birthday_target`/`signup_target`/`age_exclude_ranges` + 집계 요청이 실패한다.
  이것이 옳은 방향(조용한 오답 제거)이지만, 실패로 두는 것과 컴파일되게 하는 것은 다르다.
- §5 의 5건.

### 7-2. 계획서의 남은 단계 (`docs/plans_event_ir_only.md`)

| 단계 | 내용 |
|---|---|
| 3 | 권위 단일화. **cut-over 표면 이사가 게이트보다 먼저** — 순서를 뒤집으면 저장된 cut-over 자산이 5커밋 동안 SQL 을 못 낸다. 사용자 미커밋 작업이 이미 `requires_event_ir` 를 만들어 두어 `audience_admission` 신설이 불필요해졌다(계획보다 싸다) |
| 4 | 근거 좌표계를 진입 계약으로 + `structurer.py` 재시도 분류 |
| 5 | 카탈로그 접지 게이트 · expressibility 원장 · 축 하나 실제 개방 |

### 7-3. 진행 전 결정이 필요한 것

계획서 §6 에 여섯 개가 있다. 그중 Phase 3 을 막고 있는 둘:

1. **표면 확대 처리 방식** — plan 최상위 조건 키까지 가드를 넓힐지. 넓히면 그 요청은 SQL 이
   안 나오는데 Event IR 로 표현할 수도 없다. 대안: 표면이 비지 않으면 EVENT_IR 권위를 주지 않는다.
2. **저장된 legacy 자산을 실제로 cut-over 할 계획이 있는가** — 없다면 Phase 3 의 유일한 L 단계가
   한 줄 계약 + 테스트로 축소된다(L → S).

---

## 8. 재현

```bash
# 전량 (약 10분)
docker exec recommendation-campaign-system-python-python-1 python -m pytest tests -q

# 이 작업이 세운 계약만
docker exec recommendation-campaign-system-python-python-1 python -m pytest \
  tests/test_member_slot_sql_coverage.py \
  tests/test_event_ir_unresolved_coordinate.py \
  tests/test_failure_stage_totality.py \
  tests/test_audience_failure_coordinate.py \
  tests/test_audience_diagnosis_wiring.py \
  tests/test_runbook_paths_exist.py \
  tests/test_module_layering.py \
  tests/test_failure_honesty.py -q
```

좌표가 실제로 나가는지(라이브):

```powershell
docker restart recommendation-campaign-system-python-api-1
$body  = @{ prompt = "장바구니에 담긴 상품의 색상이 파란색인 회원"; execute_sql = $false; query_parser = "rules" } | ConvertTo-Json -Compress
$bytes = [System.Text.Encoding]::UTF8.GetBytes($body)   # 한글은 UTF8.GetBytes 로 보낸다
(Invoke-RestMethod -Uri "http://localhost:8000/target-sql" -Method Post -Body $bytes `
  -ContentType "application/json; charset=utf-8").audience_diagnosis | ConvertTo-Json -Depth 5
```

실패로그(메타데이터 DB)에서:

```sql
SELECT failure_stage,                                          -- 엔드포인트 축
       context_metadata->'audience_diagnosis'->>'stage' AS lane -- 소유자 축(이 작업)
  FROM campaign_query_failure_logs ORDER BY created_at DESC LIMIT 5;
```

호스트 `python` 은 Windows Store 스텁이라 pytest 는 반드시 컨테이너 경유
(실측: `python - <<EOF` 가 조용히 아무것도 안 하고 종료해 역검증이 **거짓 green** 으로 보였다).
코드는 볼륨 마운트라 `python-1` 재시작은 불필요하지만, **API 로 재현하려면
`docker restart …-api-1` 이 필수**다. 엔드포인트는 `/target-sql` 이다(`/api/target-sql` 은 404).
