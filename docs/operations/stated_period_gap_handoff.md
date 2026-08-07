# 명시 기간이 '기간 결핍'으로 닫히던 결함 — 작업 인계

작성 2026-08-07. **미커밋 상태로 작업 트리에 남아 있는 변경**의 인계 문서다.
다음 세션이 이 파일 하나로 이어받을 수 있게 쓴다: 무엇이 왜 들어갔는가, 무엇이 검증됐는가,
무엇이 남았는가, 어디서부터 손대야 하는가.

---

## 0. 한 줄 요약

`최근 30일 구매한 회원 수를 알려줘` 가 "'최근'에 기간이 없다"는 이유로 되묻기로 닫히던 결함을
**표면형 하나의 예외처리가 아니라 일반 규칙**으로 고쳤다. 실측 재현율은 약 18%(로그 22회 중 4회)였다.

---

## 1. 원인 (실측 로그 `logs/rag_llm/2026-08-07/054455-9826f6.jsonl`)

체인은 넷이었고, 넷 다 서로 다른 자리였다.

1. **구조화 1차** — 모델이 `expression=null` + `missing_argument(period)` 를 내고 근거로 `'최근'`[0,2] 만
   지목한다. `'30일'`[3,6] 은 그 구간 **밖**이다.
2. **즉시 재방출 판정 실패** — `canonical_audience_claims.missing_field_cause_records` 가 결핍 원인을
   **스팬 포함 관계**로만 판정해 `user_omission`(=사용자에게 되묻기)으로 떨어뜨린다. 구조화기 내부
   재시도가 걸리지 않는다.
3. **교정 라운드 실패** — `targeting_policy.resolve_stated_period` 는 `temporal_clause` 로 반박에
   성공하지만, 재구조화 3시도를 모두 태운다.
   - 시도1 `JSONDecodeError: Extra data`
   - 시도2 `TimeFilter(purchase.order_time, …)` → `purchase.order_time` 은 `data_type="string"` 이라
     `event_compiler.py:397` 에서 fail-close. **진짜 시간 필드 `purchase.occurred_at` 이 구조화
     프롬프트의 `[Fields]` 목록에 아예 없었다.**
   - 시도3 모델이 그것을 `unsupported_semantics` 로 선언 → 심볼 축 반박 → 예산 소진
4. **정답 후보 폐기** — 기본기간 정책 라운드가 **정확히 옳은 후보**(`purchase.occurred_at` + rolling
   30 day, issues 없음)를 만들었는데 `default_period_policy._admit` 이 "기본 창(5일)이 없다"
   (`candidate_ignored_the_default_window`)며 버렸다. 되묻기가 그대로 배송됐다.

추가로 검토 단계에서 드러난 **선행 결함**:

5. `targeting_policy.stated_period_instruction` 이 모델에게 `{"type":"rolling","value":30,"unit":"days"}`
   를 제시하는데 구조화 툴 스키마의 `rolling.unit` enum 은 `day|week|month|year` 다. 실측 로그에
   모델이 그 `"days"` 를 그대로 복사한 응답이 있다 — 교정 라운드 실패의 유력 원인이었다.

---

## 2. 들어간 변경 (6축)

모두 **미커밋**. `git diff` 로 읽을 수 있다.

| 축 | 파일 | 내용 |
|---|---|---|
| **A** | `audience_runtime.py` | 구조화 프롬프트에 `[Event time fields]` 절 신설. `time_column` 을 선언한 소스의 파생 심볼 `<source>.occurred_at` 을 grain(day/month)과 함께 **선언에서 파생**해 렌더. `[Fixed wire shapes]` 에 TimeFilter.field 규칙 1줄. `_pins_its_own_time_column()` — `extra_predicates` 가 자기 time_column 을 한 값으로 고정한 스냅샷 소스(18개)는 심볼을 팔지 않고 "(기간 창 불가)" 이유를 말한다(§11 조용히 빼지 않기) |
| **B** | `default_period_policy.py`, `targeting_policy.py` | 기간 결핍을 **거짓 결핍**(원문이 이미 말함) / **진짜 결핍** 으로 가른다. 거짓 결핍만 있는 라운드는 기본 창을 지시하지도 요구하지도 않고 원문 값으로 해결된 후보를 채택한다. 채택해도 `audience_default_period` 영수증·`policy_applied` 는 붙이지 않는다(창의 출처가 사용자이므로). 혼합이면 기존 엄격 계약 유지 |
| **C** | `canonical_audience_claims.py` | `missing_field_cause_records` 가 period 결핍에 한해 스팬 포함 관계 실패 시 `temporal_clause.stated_period_for_issue` 로 재판정해 `model_omission`(구조화기 재방출)으로 승격 |
| **D** | `temporal_clause.py` | 한국어 어순 규칙 — duration 은 자기 표지 **뒤**에 올 때만 그 표지의 절이다. 검토가 찾은 신규 오탐(임계값 duration 이 뒤의 맨 표지를 수량화) 제거 |
| **E** | `query_structurer/semantic_ir.py`, `calendar_window.py`, `condition_normalizers.py` | 명시 기간의 **표면형 공백**. 단어형(`일주일`/`한 달`/`반년`/`보름`/`석달`/`한해`)을 `calendar_window.WORD_DURATION_SPECS` 에서 파생해 duration 리터럴로 emit + 낱말 경계 가드. `-간` 접미 불균일 제거(`30일간`은 되고 `3개월간`은 안 되던 것) |
| **F** | `temporal_clause.py`, `targeting_policy.py`, `default_period_policy.py` | (1) 지시문 단위를 `TemporalClause.wire_window` 가 계산하게 하고 `event_ir.canonical_unit` 으로 접는다. `WINDOW_UNITS` 밖이면 줄을 만들지 않는다. (2) **달력·절대 창**(`지난달`/`2026년 3월`/`작년 1월`)을 거짓 결핍 반박 재료로 인정 — `TemporalClause` 에 이미 선언돼 있던 `RELATION_ABSOLUTE` 의 생산자를 만들었다 |

### 단일 소유자 원칙 (깨지 말 것)

- **'원문이 기간을 말했는가'** 의 판정자는 `temporal_clause.stated_period_for_issue` 하나다.
- **목록 단위 판정**은 `targeting_policy.split_period_issues` 하나다(B·C 가 각자 루프를 만들지 않게 통합).
- **교정 지시문 문구**는 `targeting_policy.stated_period_instruction` 하나다(공개 승격).
- **기간 표현 문법**은 `calendar_window.py` 하나다. 두 번째 사전을 만들지 말 것.

---

## 3. 검증 상태

| 시점 | 전수 결과 |
|---|---|
| pristine(변경 전, 컨테이너 `/tmp/baseline`) | **5 failed / 3353 passed / 29 skipped** |
| A·B·C·D 완료 시점 | **5 failed / 3445 passed / 29 skipped** (`matches_baseline: true`) |
| **E·F 반영 후, 최종 트리 (확정)** | **5 failed / 3536 passed / 29 skipped** (11:27, 실패 집합 동일 → 회귀 0) |

pristine 대비 통과 **+183**, skipped 동일, 실패 집합 동일.

> **중간에 한 번 `10 failed / 3531 passed` 가 나왔던 건에 대해.** 코드 회귀가 아니었다. 서브에이전트가
> 라이브 코퍼스 대조용으로 저장소 루트에 만든 13MB HEAD 사본 `.baseline_head/` 를 지우지 않아서,
> `tools/unwired_symbol_inventory.py` · `tools/physical_binding_inventory.py` 스캐너가 그 안까지 훑고
> 래칫 5건(`test_physical_binding_ratchet` 2 + `test_unwired_symbol_ratchet` 3)이 빨개졌다. 실패 항목
> 이름이 전부 `.baseline_head/...` 접두어였다. 디렉터리를 지우자 래칫 16개가 즉시 통과했고, 최종 전수도
> 위 표대로 깨끗하다.
> **교훈 둘: (1) 비교용 사본은 저장소 밖(컨테이너 `/tmp`)에 만든다. (2) 낯선 실패가 나오면 실패 항목의
> 경로 접두어부터 본다 — 저장소 밖 경로가 섞여 있으면 코드가 아니라 남은 파일 문제다.**

실패 5건은 **전부 사전 존재**(Decimal→float 투영)이며 이번 변경과 무관하다:

```
tests/test_aggregation_decimal.py::test_multiply_by_is_decimal_internally_and_uses_the_exact_wire_projection[0.1-expected0-0.1]
tests/test_aggregation_decimal.py::test_multiply_by_is_decimal_internally_and_uses_the_exact_wire_projection[0.1-expected1-0.1]
tests/test_money_literal_bindings.py::test_fractional_money_is_exact_internally_and_json_round_trips_without_float
tests/test_money_literal_bindings.py::test_fractional_money_literal_uses_the_compatible_exact_json_projection
tests/test_semantic_literal_characterization.py::test_literal_kinds_values_and_exact_spans_are_characterized
```

테스트는 **컨테이너에서만** 돈다(호스트 venv 는 asyncio DLL 문제로 수집 불가):

```
docker exec recommendation-campaign-system-python-api-1 python -m pytest -q -p no:cacheprovider
```

### 라이브 코퍼스 파급 (E 측정)

`docs/data/test_baselines/live_prompts.json` 86개 프롬프트에 `extract_literal_bindings` 를 변경 전/후로
돌린 결과 **새로 생기는 바인딩 0건**. 즉 미소비 리터럴(`validation_mismatch`) 압력을 새로 받는
프롬프트는 없다. 오히려 실재 오탐 하나가 제거됐다 — `live:53` 의 `"모두 주문"` 에서 압축 텍스트
`모‖두 주‖문` 이 `두주`(=2주)로 읽혀 `parse_duration_window` 가 2주 창을 만들던 것.

---

## 4. 추가된 테스트

| 파일 | 내용 |
|---|---|
| `tests/test_audience_catalog_time_field_guidance.py` (신규) | 창 가능 소스 순회, grain 표기 파생, 광고 심볼의 컴파일 가능성, 핀 소스 18개가 "기간 창 불가"로 고지되는지, 핀 소스에 창을 걸면 자기모순 SQL 이 나온다는 사실 자체를 실컴파일로 고정 |
| `tests/test_stated_period_correction_replay.py` (신규) | 실측 실패 체인의 **LLM·DB 없는 종단 재생**. 변경 3개를 각각 `git checkout` 으로 되돌려 red 가 되는 것을 확인함(자기 증명 포함) |
| `tests/test_word_duration_literals.py` (신규) | 단어형 기간 전 항목 순회, 오탐 코퍼스, `최근 보름` vs `보름 전` 의 temporal_kind 차이 |
| `tests/test_missing_field_causes.py` (+) | 절 단위 판정, 단위별 규칙 불변, 표지 앞 duration 비결합, 임계값 duration 이 재시도 예산을 태우지 않음 |
| `tests/test_default_period_policy.py` (+) | 거짓 결핍 채택, 명시 기간 보존 거부, 혼합 라운드, 정책 꺼진 배포 불변 |
| `tests/test_temporal_clause.py` / `tests/test_targeting_policy.py` (+) | 어순 규칙, 달력 절 생산, 지시문 window dict 의 **스키마 유효성**(스키마를 손으로 베끼지 않고 `audience_schema.audience_expression_json_schema()` 에서 enum 을 읽어 대조) |

기존 단언의 삭제·skip·xfail·완화는 없다. `tests/test_missing_field_causes.py:118` 의 fixture 가
`'최근'` 1번째→2번째 등장으로 옮겨진 것 하나가 있는데, 원래 자리는 원문이 `'최근 3개월'` 로 기간을
실제로 말한 절이라 **옛 라벨이 오라벨**이었다. 형제 테스트가 새 계약을 명시적으로 고정한다.

---

## 5. 남은 작업 — 다음 세션은 여기서 시작

### 5.1 즉시 해야 할 것: 중단된 검증 단계

워크플로를 **F 완료 직후 중단**했다. 아래가 실행되지 않았다:

1. **통합 + 라이브 코퍼스 파급 측정** — E/F 결합 후 `combine_temporal_clauses` /
   `stated_period_for_issue` 판정이 달라지는 프롬프트를 코퍼스 전체로 재측정 (E 는 바인딩만,
   F 는 절 판정까지 바뀌었으므로 결합 후 측정이 아직 없다)
2. **적대적 검토 3렌즈** — 오탐 / 파급 / 가드 약화. 앞선 두 워크플로에서 이 단계가 매번 **진짜 결함을
   찾아냈다**(워크플로 2에서 4건). E/F 는 이 검증을 아직 받지 않았다.
3. **확정 결함 수정 + 전수 재검증**

> **E/F 는 각자 단위 테스트와 전수는 통과했지만 적대적 검토를 받지 않았다.** 커밋 전에 2번을 돌리는
> 것을 권장한다. 스크립트가 남아 있다:
> `~/.claude/projects/.../workflows/scripts/stated-period-surface-coverage-wf_c4e5f341-022.js`

### 5.2 알려진 결함 — 우선순위 순

**(1) 프롬프트가 아직 모델에게 비legal 단위를 보여 준다 [높음, 작음]**
F 가 지시문 경로는 막았지만 프롬프트 경로가 남았다. `query_structurer/prompt.py:277` 이
`literal_bindings` 를 통째로 `json.dumps` 하므로 `normalized.semantic_unit="days"` 가 프롬프트에 그대로
있고, `audience_runtime.py:1069` 는 "rolling/relative 기간은 binding의 값·단위를 사용" 이라고 지시한다.
→ 고칠 자리: (a) 바인딩에 canonical 단위를 함께 싣거나 (b) 그 지시 문장을 "binding 의 값 + event_ir
canonical 단위"로 바꾼다. **1번과 같은 결함의 두 번째 인스턴스이므로 값이 크다.**

**(2) `graph_rag._duration_days_signals` 의 단어형 오탐 [중간]**
`WORD_DURATION_PATTERN` 을 압축 텍스트에 직접 돌려 원문 경계를 볼 수 없다. `live:53` 의
`"모두 주문"` → 14일 신호가 그 경로에는 **그대로 남아 있다**. E 가 리터럴 경로는 가드로 막았지만
`graph_rag.py` 는 소유 밖이라 손대지 않았다. 고치려면 그 호출부를 `calendar_window` 의 원문 인자
경로로 옮겨야 한다.

**(3) `semantic_normalizers.py:506` 의 세 번째 기간 표면 목록 [중간]**
`_RELATIVE_SURFACE_RE = ^최근\s*(\d+)\s*(일|주|주일|개월|달|년|년간)$` 가
`numeric_duration_unit_semantics()` 에서 파생하지 않는 자기 목록이다. E 가 없앤 `-간` 불균일이
이 계층에는 그대로 남아 있다(`최근 3개월간` 문자열이 그쪽으로 오면 못 읽는다).

**(4) 관형절을 건너뛰는 결합 [낮음, 상수 조정 위험]**
`최근 접속한 30일 이내 신규 가입 회원`(gap 5) 처럼 표지와 duration 사이에 관형형 어미로 끝나는
술어가 끼면 `_ADJACENCY_BUDGET=6` 이 절 경계로 보지 못해 잘못 결합한다. 예산을 좁히면(6→3)
`최근 약 30일` 류 정상 표현을 잃고 **사용자 명시값을 기본값으로 덮는 더 나쁜 실패**가 되므로
근거 없이 상수를 조정하지 말 것. 라이브 코퍼스 189개 프롬프트에서 정방향 결합은 전부 gap=1 이라
현재 노출은 코퍼스 밖 표현에 한정된다.

**(5) `_admit` 이 창의 결속을 보지 않는다 [중간, 재설계]**
채택 검사가 **값 집합만** 보고 어느 사건에 붙었는지를 보지 않는다. 구매 조건을 통째로 잃고 login 에
30일을 붙인 후보가 채택될 수 있다. `targeting_policy._admit_stated_period` 와
`default_period_policy._admit` **두 단계 공통** 문제이며 선행 결함이다.

**(6) 정책 영수증 소실 [낮음]**
`resolve_stated_period` 가 교정 실패 시 CLARIFY 영수증을 **버려지는 플랜**에 적고,
`apply_default_period` 는 새 candidate 를 돌려주므로 그 영수증이 사라진다. B 가 채택 경로에는
영수증을 추가했지만 원장을 이어 나르는 일반 해법은 아니다.

**(7) 달력 교정의 verbatim 복사 계약 [낮음, 관측 필요]**
달력 교정은 모델이 `event_ir_window` 를 **그대로 복사**하기를 요구한다. 의미상 동등한 다른 표기
(`작년` → `{"type":"relative","value":1,"unit":"year","direction":"past"}`)로 답하면
`candidate_dropped_stated_period` 로 거부되고 되묻기로 간다. 실사용 로그에서 relative 로 답하는
빈도가 높으면 `window_key` 에 등가 판정을 넣는 편이 낫다.

### 5.3 의도적으로 고치지 않기로 한 것

**`active_cart.time_column = INS_DT`** — `ODS_MALL_OMS_CART` 38,133행 전부
`INS_DT='2020-02-03 14:23:14.850'`(distinct 1)인 ETL 적재 시각이다. 담은 시점은 `UPD_DT`(distinct 62일)이고
형제 소스 `cart` 는 이미 `UPD_DT` 를 쓴다. 따라서 `최근 N일 장바구니에 담아둔 회원` 은 컴파일에
성공하면서 **항상 0명**이 나온다(fail-close 아님, 조용한 왜곡). `audience_catalog.json` 의 `_comment`
에도 "발생 시각은 담은 시점 INS_DT다"라고 잘못 적혀 있다.

**2026-08-07 사용자 결정: 고치지 않는다.** 이미 출고되던 SQL 의 뜻이 바뀌고
`tests/test_cart_abandonment_replay.py` 가 `INS_DT` 를 계약으로 고정하고 있기 때문이다.
자동으로 고치거나 다시 제기하지 말 것. 되살리려면 그 테스트 계약 변경이 먼저다.

### 5.4 추측하지 않기로 하고 남긴 표면형

E 가 근거를 대고 **의도적으로 제외**한 것들이다. 다시 넣으려면 그 근거부터 반박해야 한다.

| 표면형 | 제외 근거 |
|---|---|
| 조사가 붙은 단어형 (`한 달은`, `일주일이`) | `live:35` "최근 6개월 중 **적어도 한 달은** 골드 이상" 의 `한 달` 은 창이 아니라 **세는 달 수**다. 허용하면 그 자리에 1개월 롤링 창이 근거까지 갖춘 채 SQL 로 나간다 |
| `세달째`, `일주일째` (서수 접미) | `째` 가 기산점과 현재 칸 포함 여부를 정하지 않아 창을 확정할 수 없다 (§12 정의 없이 구현 금지) |
| `오랫동안`, `한동안` | 어휘가 이미 모호 정도어로 선언돼 있다 |
| `한 분기`, `반기` | `unit_conversions` 에 환산만 있고 값이 없다 |

---

## 6. 재현·측정 방법 (다음 세션용)

```bash
# 전수 (컨테이너 필수)
docker exec recommendation-campaign-system-python-api-1 python -m pytest -q -p no:cacheprovider

# pristine 기준선을 다시 만들려면
docker exec recommendation-campaign-system-python-api-1 sh -c \
  "rm -rf /tmp/baseline && mkdir -p /tmp/baseline && \
   tar -C /app -cf - --exclude=.git --exclude=logs --exclude=__pycache__ --exclude=artifacts . \
   | tar -C /tmp/baseline -xf -"
# (그 뒤 /tmp/baseline 에서 git 없이 변경분만 되돌려 비교)

# 실측 로그 — 요청 하나가 파일 하나
ls logs/rag_llm/<YYYY-MM-DD>/
# 관심 이벤트: campaign_query_plan_v4_response / _attempt_failed / _success / _fallback,
#              stated_period_correction, audience_default_period_policy, rag_context_assembly

# API 로 실제 재현하려면 코드 변경 후 컨테이너 재시작이 필요하다
docker restart recommendation-campaign-system-python-api-1
```

**아직 실행하지 않은 검증**: 코드 변경 후 API 컨테이너를 재시작해 실제 프롬프트를 N회 돌려
되묻기 비율이 실제로 떨어지는지 재는 라이브 측정. LLM 호출 비용이 들고 컨테이너 재시작이
필요해 사용자 확인 전까지 하지 않았다. 이번 결함이 **확률적**(약 18%)이므로 그 측정 없이는
"고쳐졌다"를 수치로 말할 수 없다 — 오프라인 replay 로 각 고리가 끊어진 것만 증명돼 있다.

---

## 7. 관련 파일

- 실측 로그 발췌: `logs/rag_llm/2026-08-07/054455-9826f6.jsonl`, `043940-ef79e5.jsonl`
- 라이브 코퍼스: `docs/data/test_baselines/live_prompts.json` (#14 가 이 프롬프트)
- 카탈로그: `docs/data/runtime/semantics/audience_catalog.json`
- 파이프라인 배선: `graph_rag.py` 의 `structure_campaign_query_plan_once`
  (`resolve_stated_period` → `apply_default_period` 순서를 여기서 읽는다)
