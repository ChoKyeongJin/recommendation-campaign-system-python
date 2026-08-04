# 실패 진단 — "이 요청은 어디서 끝났나"를 읽는 법

SQL 이 안 나온 요청 앞에서 운영자가 답해야 하는 질문은 둘이다.

1. **사용자에게 뭐라고 하나** → 응답의 `message` · `clarification_questions` · `failure_stage`
2. **코드의 어디를 고치나** → 응답의 `audience_diagnosis`

이 문서는 2번을 다룬다.

---

## 1. `stage` 라는 말은 이 저장소에서 다섯 개의 다른 축이다

같은 이름이 다섯 군데에 있고 값이 전부 다르다. **한 번이라도 섞으면 그 뒤의 모든 집계가 거짓**이
되므로, 표를 먼저 읽는다.

| 축 | 소유자 | 값의 예 | 무엇을 답하나 | 어디로 나가나 |
|---|---|---|---|---|
| **UI 스텝퍼 단계** | `rag/failure_stage.py` `_FAILURE_STAGE_SEQUENCE` | `condition_recognition` · `real_db_mapping` · `sql_safety_validation` | 사용자에게 "어디까지 갔다"고 보여줄까 | `api_response.failure_stage` (BFF 가 배지·스텝퍼로 렌더) |
| **종착 레인** | `audience_failure.py` `LANE_STAGES` | `event_ir_compile` · `semantic_resolution` · `plan_validation` | **코드의 어느 소유자가** 이 요청을 끝냈나 | `api_response.audience_diagnosis.stage` |
| **엔드포인트 단계** | `api.py` `_target_sql_failure_payload` | `sql_generation` · `database_execution` · `message_generation` | 요청의 어느 구간에서 실패했나(SQL 생성 / 실행 / 메시지) | 실패로그 컬럼 `failure_stage` |
| **Event IR 컴파일 내부 단계** | `query_pipeline.QueryPipelineError.stage` | `ir_schema` · `legacy_event_expression_adapter` · `sql_compilation` | 컴파일 계층 안 어디서 터졌나 | 좌표 항목의 `stage` → `audience_diagnosis.stage_detail` |
| **검색 트레이스 단계** | `rag/trace.build_stage_log` | `1. Query Planning` · `6. SQL Template / Guard` | 시연·디버깅용 파이프라인 진행 로그 | `debug.stage_log[].stage` |

가장 흔한 혼동은 위 두 줄이다. 예를 들어 `audience_authority_invalid` 는

- 스텝퍼에서 **첫 단계**(`condition_recognition`)다 — 사용자 눈에는 조건을 못 읽은 것으로 보인다.
- 레인으로는 **`audience_authority`** 다 — 소유자는 조건을 읽지도 않은 진입 게이트이고, 고칠 곳은
  요청 문장이 아니라 그 플랜을 만든 쪽이다.

둘을 하나로 합치면 한쪽이 반드시 거짓말을 한다. 그래서 두 값은 **같은 응답에 함께** 나간다.

> 세 번째 줄(엔드포인트 단계)의 값 `sql_generation` 은 두 번째 줄(레인)의 값 `sql_generation` 과
> **글자만 같다.** 전자는 "DB 실행이 아니라 SQL 생성 구간"이고, 후자는 "조건은 다 갔는데 생성·검증
> 관문에서 떨어졌다"다. 두 컬럼을 조인하지 마라.

---

## 2. 좌표(`audience_diagnosis`) 의 모양

```jsonc
{
  "stage":        "event_ir_compile",        // LANE_STAGES 의 값 하나
  "stage_label":  "Event IR 컴파일",
  "stage_detail": "sql_compilation",         // 레인 안의 하위 좌표(없으면 null)
  "code":         "event_ir_compile_failed", // 항상 닫힌 코드. 한국어 문장은 여기 안 온다
  "evidence":     [{"path": "...", "code": "...", "detail": "..."}],
  "message":      "조건을 실DB 술어로 컴파일하지 못했습니다: ...",
  "sources":      ["query_plan.unresolved_source_conditions"]
}
```

성공한 요청은 `null` 이다. **실패인데 `null` 인 경우는 없다** — 어느 종착 상태에도 안 걸리면
`code: "unclassified"` 로 나가고, 그때 `sources` 가 "값이 있던 입력"의 목록이다(§5).

판정 규칙 한 줄: **`failure_reason` 이 종착 게이트를 이름으로 지목하면 그 레인이고, 지목하지
못하는 거친 사유일 때만 플랜에 남은 잔여물로 정밀화한다.** 반대로 하면 앞 단계가 남긴 좌표가
실제 종착 게이트를 덮는다.

---

## 3. 레인별로 무엇을 하나

레인 목록과 "왜 이 레인이 따로 있는가"는 **코드가 소유한다**(`audience_failure.LANE_STAGES` 의
`distinct_because`). 이 문서는 **다음 행동**만 소유한다 — 표를 두 벌로 적으면 갈라진다.

```bash
docker exec recommendation-campaign-system-python-python-1 python -c \
  "import audience_failure, json; print(json.dumps(audience_failure.LANE_STAGES, ensure_ascii=False, indent=2))"
```

| 레인 | 이건 누구 일인가 | 첫 행동 |
|---|---|---|
| `audience_authority` | 플랜 **생산자** | 저장된 플랜의 `audience_authority` 값을 본다. 닫힌 어휘(`audience_authority.AudienceAuthority`) 밖이면 그 플랜을 만든 경로를 고친다. 사용자에게 되물을 것이 없다 |
| `semantic_resolution` | 사용자 또는 **의미 레지스트리** | `stage_detail` 이 `structurer`/`system` 이면 사용자 일이 아니다(구조화기·실행설정). 그 외에는 `evidence[].path` 의 필드를 사용자가 지정하면 풀린다 |
| `source_coverage` | **파서/슬롯** | `evidence[].path` 가 원문의 어느 구절이 어디로 못 갔는지다. 어휘 추가로 풀리는지, 슬롯이 없는지를 먼저 가른다 |
| `plan_validation` | **해석 산출물** | `evidence[].code` 가 위반한 계약이다. `internal_invalid` 는 능력 부재가 아니라 산출물 불량 — 재방출·재시도 대상이다 |
| `event_ir_compile` | **컴파일러 능력** | `stage_detail` 이 컴파일 계층 중 어디인지다. `ir_schema` 면 저장 표현이, `sql_compilation` 이면 컴파일러가 원인이다 |
| `execution_capability` | **카탈로그 또는 IR** | 컴파일러가 "못 한다"고 선언했다. `docs/plans_event_ir_only.md` §5 의 분류(BLOCKED_CATALOG / BLOCKED_IR_EXTENSION / BLOCKED_DOMAIN_DECISION)로 나눈다 |
| `structuring` | **모델·인프라** | LLM 호출 실패·스키마 위반이다. 사용자에게 조건을 다시 쓰게 하면 같은 곳에서 또 막힌다. `logs/rag_llm/<날짜>/` 의 해당 요청 로그를 본다 |
| `sql_generation` | **생성·검증 관문** | 조건은 다 갔다. `evidence` 와 `sql_result.selected` 의 관문별 판정을 본다. 여기 쌓이는 사유가 다음에 구현할 것의 목록이다 |
| `unclassified` | **이 배선** | §5 |

---

## 4. 어디서 읽나

### 응답 (운영 화면·BFF)

`include_debug` 없이도 최상위에 실린다.

```bash
curl -s localhost:8000/api/target-sql -H 'Content-Type: application/json' \
  -d '{"prompt":"지난달 구매한 회원","execute_sql":false}' | jq '.audience_diagnosis'
```

### debug 블록 (좌표 **원본** 전량)

`audience_diagnosis` 는 항목 하나를 요약한다. 좌표가 여럿이면 전량은 여기서만 보인다.
내부 경로·코드가 그대로 들어 있어 debug 밖으로는 내보내지 않는다.

```bash
... -d '{"prompt":"...","include_debug":true}' | jq '.debug.unresolved_source_conditions'
```

### 실패로그 (누적 집계)

새 컬럼도 새 로그 파일도 만들지 않았다. 좌표는 기존 `context_metadata` **JSONB 안**에 들어간다.

```sql
-- 최근 7일, 레인별 실패 분포
SELECT context_metadata->'audience_diagnosis'->>'stage'   AS lane,
       context_metadata->'audience_diagnosis'->>'code'    AS code,
       count(*)
  FROM campaign_query_failure_logs
 WHERE created_at >= now() - interval '7 days'
 GROUP BY 1, 2
 ORDER BY 3 DESC;

-- 특정 레인의 실제 프롬프트
SELECT created_at, prompt,
       context_metadata->'audience_diagnosis'->>'stage_detail' AS detail
  FROM campaign_query_failure_logs
 WHERE context_metadata->'audience_diagnosis'->>'stage' = 'event_ir_compile'
 ORDER BY created_at DESC LIMIT 20;
```

요청별 LLM 왕복은 `logs/rag_llm/<날짜>/` 에 이미 있다. **세 번째 로그 파일을 만들지 않는 것이
판정이다** — 소비자 없는 로그가 생기면 운영자가 볼 곳이 다시 흩어진다.

---

## 5. `unclassified` 가 나오면

이것은 요청의 결함이 아니라 **이 배선의 결함**이다. 실패했는데 아홉 레인 어디에도 안 걸렸다는 뜻이고,
조용한 `null` 로 지우면 그 결함은 영영 관측되지 않으므로 레인으로 남겼다.

1. `sources` 를 본다 — 값이 있던 입력의 목록이다. 비어 있으면 종착 상태를 **아무도 안 남긴** 것이다.
2. `sources` 에 이름이 있는데 미분류라면, 그 입력의 값이 이 모듈이 아는 모양이 아니다
   (예: `semantic_ir.status` 가 새 상태값).
3. 고칠 곳은 `audience_failure.py` 의 해당 레인 함수 하나다. 고친 뒤
   `tests/test_audience_failure_coordinate.py` 에 그 갈래를 추가한다 — 그 파일이 "선언만 있고
   도달 못 하는 레인"을 금지한다.

---

## 6. 이 문서가 거짓이 되지 않게 하는 것

| 주장 | 무엇이 강제하나 |
|---|---|
| 아홉 레인이 전부 실제로 도달된다 | `tests/test_audience_failure_coordinate.py::test_every_declared_lane_is_reachable` |
| 이 문서의 레인 목록이 코드와 일치한다 | `tests/test_audience_diagnosis_wiring.py::test_this_document_lists_exactly_the_declared_lanes` |
| 좌표가 응답·debug·실패로그에 실제로 나간다 | `tests/test_audience_diagnosis_wiring.py` |
| 실패 사유가 전부 스텝퍼 단계를 갖는다 | `tests/test_failure_stage_totality.py` |
| 진단 계층이 코어 로딩 실패에 안 묶인다 | `tests/test_module_layering.py::test_leaf_diagnostic_modules_import_only_stdlib` |
