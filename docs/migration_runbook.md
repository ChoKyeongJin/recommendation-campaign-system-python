# 파서 이행 운영 절차

"새 표현이 나올 때마다 파이썬을 고치는" 구조를 "주 1회 분류하는" 구조로 바꾸는 프로그램의 운영 문서다.

## 원칙 하나

새 표현이 들어오면 셋 중 하나다. **C 만 코드다.**

| | 무엇 | 어디에 | 배포 |
|---|---|---|---|
| **A 어휘** | 기존 능력의 새 표면어 | `docs/data/runtime/language/parser_lexicon.json` 의 `vocabularies` | 불필요 |
| **B 파라미터** | 기존 팩트의 새 조건 모양 | `targeting_ir.CONDITION_SPECS` / `_FilterSpec` 레지스트리 | 필요 |
| **C 능력** | 새 grain·조인이 필요 | 새 ConditionSpec + 빌더 | 필요 |

A 가 코드로 가는 것이 원래 문제였다. C 가 코드인 것은 정상이다.

### A 를 사람이 다 못 적을 때 — LLM 보완

A(어휘)는 끝이 없다. 사전에 없는 말투는 규칙이 조용히 침묵하므로, 그 **빈칸만** LLM 이 채운다
(`docs/prompts/condition_slot_extract_system.txt`). 대신 채우는 범위가 묶여 있다 — 이 경계가
"LLM 이 스키마를 지어내지 않는다"의 실질이다.

> 이 보완을 끄던 스위치 `CONDITION_SLOT_LLM_FALLBACK` 은 **더 이상 코드에서 읽히지 않는다**
> (2026-08-04 실측). 끄고 싶으면 스위치가 아니라 호출부를 봐야 한다.

| | LLM 이 정한다 | JSON 이 정한다 |
|---|---|---|
| 회원 상태 플래그 | 문장이 어느 canonical 을 말하는가 | 어떤 canonical 이 존재하는가(`attribute_token_groups.json` ∩ 컴파일 가능) |
| 쿠폰 임계 | 연산자와 값(`세 번 넘게` → `gt 3`) | 그 조건을 실제로 지원하는가(`segment_metrics.json` 의 capability) |
| 표면 신호 | 문장이 어느 뜻을 말하는가(채널 발송·판매·재활성·재구매·구매이력·집계 함수 …) | 어떤 뜻이 존재하는가(`surface_concepts.json` 의 닫힌 개념 집합) |
| 회원 지표 | 문장이 어느 지표의 크기를 말하는가(`돈이 많아 보이는 고객` → `total_buy_amt`) | 어떤 지표가 존재하는가(`member_metrics.json` ∩ 랭킹 빌더가 컴파일 가능). **개수·퍼센트는 JSON 도 LLM 도 아닌 문장이 정한다** — 결정론 파서가 읽고, 없으면 `default_top_n` |

지표 행이 왜 필요했는지가 이 표의 사용법을 잘 보여준다. `돈이 많아 보이는 고객`은 능력(랭킹 빌더)도
파라미터 모양(지표×방향×개수)도 이미 있는 **순수 A**였다. 그런데 `member_metrics.json` 의 `synonyms`
는 닫히지 않는 A 다(큰손·씀씀이 큰·여유 있는·형편이 넉넉한 …). 동의어를 한 줄 더하는 것은 두더지
한 마리를 잡는 것이고, 판정을 뜻으로 옮기는 것이 그 부류 전체를 끝내는 것이다. 새 표현 앞에서
던질 질문은 **"이 낱말의 형제를 다 적을 수 있나?"** 다 — 적을 수 있으면 사전, 못 적으면 개념/선택지다.

호출 비용은 게이트 둘로 묶여 있다. 회원 단위 표현(`granularity_tokens`)이 있고, 표면 개념
`member_magnitude_ranking` 이 참일 때만 지표 선택 호출이 한 번 더 나간다. 개념 판정은 질의당 한 번
도는 표면 신호 해석을 그대로 읽으므로, 평범한 질의(`서울 사는 30대 여성`)의 추가 비용은 0 이다.

채택 조건 셋: **닫힌 집합에서 고르기만**(목록 밖 값은 버림), **근거는 원문에 그대로**(회원 명사 포함,
규칙이 이미 읽은 조각과 겹치면 거부), **빈칸만**(규칙이 채운 슬롯은 안 덮음). 채택분은
`plan_decisions` 에 `source: llm` 으로 남는다. 이 셋을 강제하던 두 계약 테스트
(`test_condition_slot_llm` · `test_surface_lexicon_llm`)는 규칙 계층 철거와 함께 삭제됐다 —
**지금 이 경계를 지키는 테스트는 없다.**

### 표면 신호는 이미 A 를 졸업했다

의도·목적·문맥 신호어(채널 발송·판매 아웃리치·재활성·재구매·장바구니 이탈·구매이력·집계 함수어)는
낱말 목록이 아니라 **뜻**으로 판정한다. 표면어 소유권은 `lexicon_llm.py` + `surface_concepts.json`
(개념 선언)에 있고, 데이터 파일에서는 그 낱말이 빠졌다. 즉 **이 부류에는 A 가 없다** — 새 말투가
나와도 사전에 한 줄 추가할 일이 없고, 새 *개념*이 필요할 때만 `surface_concepts.json` 에 항목을 더한다.

코드에 남은 낱말 목록(`graph_rag._DEFAULT_TARGETING_LEXICON` 의 LLM 소유 그룹,
`analytical_intent._AGGREGATE_FUNCTION_BACKSTOP`)은 **동결 백스톱**이다. 키가 없거나
`SURFACE_LEXICON_LLM=off` 인 환경(테스트 포함)에서 이관 전 결정론 동작을 재현하는 것이 유일한
역할이고, 손으로 늘리지 않는다. 이것을 강제하던 래칫(`test_surface_lexicon_llm`)은 규칙 계층
철거와 함께 삭제됐다 — **지금은 규약일 뿐 강제되지 않는다.**

### 불리언으로는 부족한 뜻 — 의미 신호(status)

표면 개념은 "이 문장이 그 얘기인가"라는 **불리언**이다. 그런데 어떤 뜻은 불리언으로 접는 순간 반드시
틀린다. 구매가 그렇다 — `샀다`·`살까 고민 중`·`사지 않았다`·`구매 방법을 알려줘`는 전부 "구매 얘기"
지만 오디언스는 완전히 다르다. 이 부류는 `semantic_signal.py` + `docs/data/runtime/semantics/semantic_signals.json` 이
소유하고, 불리언 대신 **상태(status)** 를 돌려준다.

| | 무엇 | 어디에 |
|---|---|---|
| 상태 집합 | `completed`/`history`/`ongoing`/`intent`/`hypothetical`/`mentioned`/`denied`/`none`/`unknown` | 코드(구조) |
| 어떤 상태가 '실제 발생'인가 | `detected_statuses` | `semantic_signals.json`(정책) |
| 그 뜻을 어떻게 말하는가 | — | **어느 목록도 소유하지 않는다**(추출기가 읽는다) |

규약 넷이 이 계층의 실질이다.

1. **한 번만 판정한다.** 원문에서 한 번 구조화하고 게이트·필터·재작성 비교가 같은 값을 읽는다.
   재작성본 문자열을 같은 키워드로 다시 검사하는 자리가 조건이 사라지던 자리였다.
2. **boolean 을 뭉개지 않는다.** `detected` 는 `semantic_signal.detected_for` 한 곳만 계산한다.
   문맥 게이트(`_has_purchase_history_signal`)와 발생 판정은 같은 status 에서 나온 **다른** 질문이다.
3. **폴백은 우선순위지 OR 이 아니다.** 구조화 결과 → 형태 판정(`purchase_lexicon`) → 보수적 낱말
   (문맥만, 발생으로 승격 안 함) → `unknown`. 상위가 답하면 하위는 보지 않는다.
4. **메타데이터는 의미가 아니다.** 출처·모델·소요시간은 `canonical_form` 에 들어가지 않는다.

새 표현이 들어와도 여기는 고칠 것이 없다. 새 *뜻*이 필요할 때만 `semantic_signals.json` 에 항목
하나와 그 뜻을 소비하는 코드를 더한다. 표현형 전수를 갖던 `test_semantic_signal` 은 규칙 계층
철거와 함께 삭제됐다 — **지금 이 계층의 표현형을 재는 테스트는 없다.**

대체되지 **않은** 어휘도 있다. 문장에 있는가가 아니라 **어디에 있는가**로 판정하는 것들이다:
대상 지향 표지(절 분리 지점), 장바구니 어휘(금액·수량 인접성), `parser_lexicon.json` 어휘(교대
정규식으로 합성), `normalization_rules` 의 동의어(매칭 스팬 `matched_text` 를 하위가 소비),
`segment_lexicon.json` 의 별칭·연산자·표지(`_metric_ending`/`_metric_leading` 이 접두·접미 위치로
비교 방향을 정한다). 불리언이 아니라 위치라서 개념 판정으로 못 바꾼다.

## 주간 루틴 (15분)

> **2026-08-04 재작성.** 이전 판은 `weekly_triage` 도구와 `unresolved.jsonl` 큐, 그리고
> `weekly_triage`·`slot_policy` 문서를 현재형으로 안내했다. **넷 다 저장소에 없다.** 존재하지 않는 안전망을 현재형으로 광고하는 것이 이 저장소의 알려진 재발 사고 모드라,
> 아래는 전부 실재하는 장치로만 적었고 `tests/test_runbook_paths_exist.py` 가 그것을 강제한다.

실패는 이제 **한 곳에 쌓인다** — 메타데이터 DB 의 `campaign_query_failure_logs` 다. 요청마다 종착
레인 좌표(`audience_diagnosis`)가 `context_metadata` JSONB 안에 함께 저장된다.

### 1. 레인별 분포를 본다 (무엇이 제일 많이 막혔나)

```sql
SELECT context_metadata->'audience_diagnosis'->>'stage' AS lane,
       context_metadata->'audience_diagnosis'->>'code'  AS code,
       count(*)
  FROM campaign_query_failure_logs
 WHERE created_at >= now() - interval '7 days'
 GROUP BY 1, 2 ORDER BY 3 DESC;
```

레인이 곧 **소유자**다. 어느 레인의 일인지와 첫 행동은
`docs/operations/failure_diagnosis.md` §3 이 소유한다.

- `structuring` 이 상위면 파서·어휘 문제가 아니라 **모델·인프라** 문제다. 사전에 낱말을 더해도
  안 풀린다. `logs/rag_llm/<날짜>/` 의 해당 요청 로그부터 본다.
- `unclassified` 가 하나라도 있으면 그것부터 본다. 요청의 결함이 아니라 **진단 배선의 결함**이고,
  이 루프의 사각지대다(§5 of `docs/operations/failure_diagnosis.md`).
- `execution_capability` 는 A/B/C 판정 대상이 아니다 — 컴파일러가 "못 한다"고 선언한 것이라
  `docs/plans_event_ir_only.md` §5 의 분류로 나눈다.

### 2. `source_coverage` 를 A/B/C 로 가른다

이 레인만이 위 §원칙의 A/B/C 판정 대상이다. `evidence[].path` 가 원문의 어느 구절이 어디로 못
갔는지를 말한다.

```sql
SELECT prompt,
       jsonb_pretty(context_metadata->'audience_diagnosis'->'evidence')
  FROM campaign_query_failure_logs
 WHERE context_metadata->'audience_diagnosis'->>'stage' = 'source_coverage'
   AND created_at >= now() - interval '7 days'
 ORDER BY created_at DESC LIMIT 30;
```

판정 질문은 하나다 — **"이 낱말의 형제를 다 적을 수 있나?"**

- 적을 수 있다(A) → `docs/data/runtime/language/parser_lexicon.json` 에 낱말 추가 → `pytest tests/` → 끝(배포 불필요)
- 못 적는다(A′) → 사전이 아니라 개념이다. `docs/data/runtime/semantics/semantic_signals.json`
  또는 `surface_concepts.json` 에 **뜻** 하나를 더한다
- 조건 모양이 새것(B) → `targeting_ir.CONDITION_SPECS` 한 줄 + `tests/golden/cases.json` 케이스
- 능력이 없다(C) → `tests/golden/cases.json` 에 `known_gap` 으로 **먼저 기록**하고 별도로 설계

### 3. 회귀를 잰다

```bash
docker exec recommendation-campaign-system-python-python-1 python -m pytest tests -q
docker exec recommendation-campaign-system-python-python-1 python tools/live_prompt_baseline.py --help
```

`tools/live_prompt_baseline.py` 는 라이브 응답을 `logs/live_baseline_*.json` 으로 떠 전후를
비교한다. 어휘 한 줄을 더한 주에도 이 비교를 건너뛰지 않는다 — 어휘 추가가 다른 조건의 스팬을
가져가는 것이 이 계층에서 가장 흔한 회귀다.

## 환경 변수

실재하는 것만 적는다. 아래 표의 모든 변수가 코드에서 읽히는지는
`tests/test_runbook_paths_exist.py` 가 강제한다 — 2026-08-04 재작성 시점에 여섯 개
(`UNRESOLVED_LOG` · `PARSER_SHADOW_MODE` · `PARSER_SHADOW_LOG` · `SLOT_POLICY_PATH` ·
`TARGET_OBJECT_LLM_FALLBACK` · `CONDITION_SLOT_LLM_FALLBACK`)가 **아무도 읽지 않는 상태**로
남아 있었다. 끄고 켜도 아무 일이 안 생기는 스위치는 안내가 아니라 함정이다.

| 변수 | 값 | 뜻 |
|---|---|---|
| `PARSER_LEXICON_PATH` | 경로 | 파서 표면어 사전 |
| `SURFACE_LEXICON_LLM` | `true`(기본)/`off` | 표면 신호(의도·목적·문맥·집계 함수어)의 LLM 해석. **끄면 동결 백스톱 낱말만 읽으므로 처음 보는 말투가 조용히 침묵한다**(이관 전 동작). 켜져 있으면 질의당 빠른 모델 호출이 1회 추가되고, 그 결과는 질의 스코프 안에서 재사용된다(절 단위로 다시 부르지 않는다) |
| `SURFACE_CONCEPTS_PATH` | 경로 | 표면 개념(닫힌 집합) 선언 파일 |
| `SEMANTIC_SIGNAL_LLM` | `true`(기본)/`off` | 의미 신호(구매 등)의 구조화 판정. **끄면 형태 판정 폴백만 돌아 의향·가정·단순 언급이 발생과 구분되지 않는다**(이관 전 동작). 켜져 있으면 질의당 선언된 뜻 수만큼 빠른 모델 호출이 추가되고, 그 결과는 질의 스코프 안에서 재사용된다(절 단위로 다시 부르지 않는다) |
| `SEMANTIC_SIGNALS_PATH` | 경로 | 의미 신호(뜻 + detected 정책) 선언 파일 |
| `SEMANTIC_SIGNAL_LOG_EVIDENCE` | `off`(기본)/`on` | 관측 로그에 판정 근거(원문 조각)를 남길지. 원문은 개인정보일 수 있어 기본은 끔 |
| `SEMANTIC_AST_GATE` | `on`(기본)/`off` | 의미 AST 게이트(포함·제외 충돌 검사 + 생성 SQL 극성/구조 역검증). 조건을 만들지 않고 '조용한 의미 변형'만 차단하므로 켠 상태가 기본이다. `off` 는 이관 비교·사고 대응용 비상구 |

## 안전장치 (전부 `pytest tests/` 가 강제)

**지금 존재하는 것만** 적는다. 규칙 계층 철거와 함께 사라진 장치 여덟은 "삭제됨" 꼬리표를 단 채
표에 남아 있었는데, 꼬리표는 사람만 읽고 기계는 안 읽는다 — 그 사이 이 표는 "우리에겐 이런
안전망이 있다"로 읽혔다. 목록에서 뺐고, 재발은
`tests/test_runbook_paths_exist.py` 가 막는다(적힌 경로가 실재하지 않으면 red).

| 장치 | 파일 | 막는 것 |
|---|---|---|
| 골든 IR 스냅샷 | `tests/golden/` | 파서 변경이 조건을 조용히 잃는 것 |
| `known_gap` 마커 | `tests/golden/cases.json` | 결함을 스냅샷으로 축복하는 것 (고쳐지면 마커를 지우라고 실패) |
| 이관 동등성 | `tests/test_lexicon_patterns.py` | 사전으로 옮기며 몰래 어휘를 넓히는 것 |
| 의미 AST 불변식 | `tests/test_semantic_ast.py` | 부정·AND/OR·owner 가 정규화 과정에서 뒤집히거나 사라지는 것 |
| 모듈 크기 래칫 | `tests/test_module_size_ratchet.py` | 분할해 놓은 모듈이 다시 한 파일로 자라는 것 |
| 실패 단계 총체성 | `tests/test_failure_stage_totality.py` | 새 실패 사유가 UI 단계 없이 나가는 것 |
| 종착 좌표 도달성 | `tests/test_audience_failure_coordinate.py` | 선언만 있고 도달 못 하는 진단 레인이 생기는 것 |
| 진단 배선 | `tests/test_audience_diagnosis_wiring.py` | 좌표가 파생만 되고 응답·debug·실패로그로 안 나가는 것 |
| 문서 경로 실재 | `tests/test_runbook_paths_exist.py` | 이 문서가 없는 도구·경로·env 를 현재형으로 광고하는 것 |

### 철거된 장치 (되살릴 때 이 목록에서 지운다)

`test_regex_inventory_ratchet` · `test_slot_policy` · `test_condition_slot_llm` ·
`test_segment_semantics` · `test_ir_golden_corpus` · `test_semantic_signal` ·
`test_plan_semantic_ast` · `regen_ir_goldens` · `regex_inventory`(도구·기준선·문서) ·
`method_mix_baseline`.

규칙 계층을 걷어내면서 함께 사라졌다. **이 중 무엇도 지금 회귀를 막고 있지 않다.** 정규식 상한과
방법 구성 기준선은 그 계층이 있을 때의 장치였고, 지금 같은 목적의 래칫은 위 표의 모듈 크기·단계
총체성·좌표 도달성 셋이다.

### 래칫이 세는 것 (2026-07-30 확대)

래칫이 `lexical` 만 보던 동안 `domain` 은 147 → 162 로 조용히 늘었다. 상한 없는 분류가 배출구가
되므로, 이제 **`lexical`·`wordlist`·`domain` 세 분류 모두**에 상한이 있다(`grammar` 는 구조라 제외).

더 중요한 것은 **스캔 범위**였다. 상한은 세는 것만 막을 수 있어서, 규칙이 세지지 않는 형태로 들어오면
상한이 있어도 늘어난다. 인벤토리는 이제 넷을 모두 본다 — 총 205 → 315개가 드러났다:

| `source` | 형태 | 이전 |
|---|---|---|
| `constant` | `NAME = re.compile("리터럴")` | 셌음 |
| `collection` | `NAME = (re.compile(...), ...)` — 튜플·딕트에 담긴 정규식 | **못 봄** |
| `inline` | `re.search("한글…", text)` — 이름 없는 호출 | **못 봄** |
| `wordlist` | `NAME = frozenset({"상품", "제품", …})` — 한글 낱말집합 상수 | **못 봄** |

분류가 하나 늘었다: **`composed`** — 사전 어휘를 끼워 넣어 조립한 구조
(`rf"(?:{alternation('identity_same')})\s*(?:{...})"`). 이행 **완료** 형태라 상한을 걸지 않는다.
걸면 손으로 쓴 `domain` 정규식을 사전으로 옮길 때 지표가 그대로여서 이관이 손해가 된다 —
이제 옮기면 `domain` 이 내려가고 `composed` 가 올라간다.

확대 즉시 이관 회귀 1건이 잡혔다: `member_noun_basic`(이미 사전으로 옮긴 어휘)이
`graph_rag._has_entity_ranking_source_signal` 안에서 인라인 정규식으로 되살아나 있었다. 이름이 없어서
기존 래칫의 `test_migrated_patterns_are_gone_from_the_inventory` 도 못 보던 자리다.

낱말집합은 **규칙 상수만** 센다(상수 이름 규약 `^_?[A-Z][A-Z0-9_]*$`) — LLM 프롬프트 문구를 조립하는
지역 리스트까지 세면 문구 수정이 래칫을 깨고, 그러면 래칫이 신뢰를 잃는다.

## 상품 조건의 접지 — 무추측 폴백 (2026-07-30)

상품 자유텍스트(`target_user.purchase_object`)는 닫힌 어휘가 없어 사전으로 못 옮긴다. 대신 **실DB
접지**로 판정한다: `product_master_resolver` 가 `CRM_CM_PRODUCT` 를 조회해 같은 상품 행에서
상품명/브랜드/카테고리 facet 이 우열(신뢰도+마진)로 결정될 때만 `resolved` 로 확정한다.

근거를 못 찾았을 때(`not_found`)의 정책이 **무추측 폴백**이다 — `no_guess_fallback` 이 단일 소유자다.

| 상태 | 실행 | 추측 | 결과 |
|---|---|---|---|
| `resolved` | O | — | facet 별 술어(같은 행 AND). `grounded=True` |
| `not_found` | O | **없음** | 종류·값 분해·컬럼 선택을 모두 비운 채 원문 그대로 광역 LIKE. `grounded=False` + 신뢰도 경고 |
| `ambiguous` | X | — | 근거는 있는데 우열이 없음 → clarification |
| `unavailable` | X | — | 마스터 조회 실패 → clarification |

폴백이 clarification 이 **아닌** 이유: 오탈자·신규 등록 상품이 요청을 통째로 막으면 안 된다. 폴백이
추측을 **하지 않는** 이유: 종류를 하나 고르는 순간 실DB 근거 없이 지어낸 술어가 되고, 그때부터 다시
부정 목록으로 막아야 한다. 실행은 하되 근거 없음을 드러내는 것이 이 설계다(`grounded` 플래그 →
`confidence` 근거·경고).

**남은 것**: 접지 판정은 폴백을 정리했지만 **입력 게이트를 대체하지 못한다.** `'고액'`·`'다'`·`'3월'`
같은 잡토큰은 `not_found` 가 아니라 마스터에서 *많이* 매칭되므로, 구체 식별어 게이트
(`_is_concrete_purchase_scope_phrase` + `_GENERIC_PRODUCT_NOUNS` 계열 46개 낱말집합 중 일부)는
그대로 필요하다. 그 목록의 데이터 이관은 낱말집합 래칫이 관리하는 별건이다.

## QueryPlan V4 LLM-first 전환

캠페인 API의 기본 파서는 `auto`이며, 원문을 재작성하거나 정규식으로 읽기 전에 strict
`CampaignQueryPlanV4` 구조화를 먼저 수행한다. V4는 실행기가 그대로 소비하는 실행 슬롯
계약(구 V2)과 의미 계약(구 V3)을 한 IR로 합친 버전이다: 채택한 슬롯의 원문 구간을
`semantic_evidence`에 남기고 표현할 수 없는 의미는 `unresolved`로 반환하며, 날짜·숫자
리터럴은 애플리케이션 소유 `literal_bindings`로 봉인된다. LLM 출력은 SQL을 포함하지
않으며 기존 결정론 컴파일러와 SQL 검증기를 그대로 통과한다.

| 환경 변수 | 값 | 동작 |
|---|---|---|
| `QUERY_PARSER` | `auto`(기본) / `llm` / `rules` | `auto`와 `llm`은 V4 의미 구조화를 사용한다. `rules`는 명시적 레거시 경로다. |
| `QUERY_PLAN_AUTHORITY` | `llm_first`(기본) | V4가 충돌 슬롯을 소유하고 레거시 규칙은 빈 슬롯만 보완한다. |
| `QUERY_PLAN_AUTHORITY` | `shadow` | 기존 rules-first 실행을 유지하며 `PARSER_SHADOW_*`로 차이를 관측한다. |
| `QUERY_PLAN_AUTHORITY` | `rules_first` | 즉시 롤백용. 기존 규칙 우선순위와 지연 LLM 보완을 사용한다. |

V4 통합 이후 구조화기는 하나뿐이므로 `QUERY_PLAN_AUTHORITY`는 구조화기 종류를 바꾸지
않는다(모든 값이 V4 의미 계약 — 근거 스팬 검증·unresolved fail-close — 을 따른다).
authority는 구조화 시점(즉시/지연)과 후보 우선순위만 바꾸며, 비의미(구 V2) 레인은 없다.
LLM 의미 구조화를 완전히 우회하는 유일한 경로는 요청별 `query_parser=rules`다.

LLM-first에서 원문 권위 규칙은 실행 플랜을 수정하지 않는다. 복사본에 적용해 V4와 비교하며,
차이가 있으면 `llm_legacy_semantic_disagreement` 미해결 조건으로 기록해 SQL 생성을 차단한다.

배포 순서:

1. `QUERY_PLAN_AUTHORITY=shadow`, `PARSER_SHADOW_MODE=shadow`로 슬롯별 일치율을 수집한다.
2. 위험 차이를 골든 코퍼스에 추가하고 V4 스키마·프롬프트를 수정한다. 새 문장별 정규식은 추가하지 않는다.
3. `QUERY_PLAN_AUTHORITY=llm_first`로 전환한다.
4. 장애 시 `QUERY_PLAN_AUTHORITY=rules_first` 또는 요청별 `query_parser=rules`로 되돌린다.

(V2·V3 구현은 2026-08-01 V4로 통합되며 삭제됐다. 계약 테스트였던 `test_campaign_plan_v3` 도
규칙 계층 철거와 함께 삭제됐다.)

## 남은 결함 (2026-07-29 기준)

- **조용한 소실 2건** — `target_user.purchase_object` / `purchase_object_kind`. 결정론 백스톱이 없고
  fail-close 표시도 없다. 7단계의 종료 조건은 이 값이 0 이 되는 것이다.
- **부분 소실은 탐지 못 한다** — 미해석 탐지기는 "조건이 하나도 없는" 경우만 잡는다. `안양에 사는`
  처럼 일부만 빠지는 것은 shadow 비교의 `only_candidate` 판정이 담당하므로, shadow 를 켜야 보인다.
- **어휘형 11개 / 낱말집합 46개 / 업무의미형 218개 / 조립형 5개** (2026-07-30 확대 스캔 기준). 어휘형은
  `_BALANCE_*` 3, `_THRESHOLD_CUE_RE`, `_OUTPUT_ACTION_RE`, `_MEMBER_METRIC_COMPARISON_RE`,
  `_TREND_ORDER_MARKER_RE`, `_NOISE_LABEL`(빌드 스크립트) + 인라인 3. 낱말집합 상위는
  `_VALUE_TAIL_TOKENS`(45), `query_semantics.NON_ENTITY_TERMS`(36), `_NOISE_OPERAND_TOKENS`(32).
  (우선순위를 교대수 순으로 적어 두던 `regex_inventory` 문서와 도구는 규칙 계층 철거와 함께
  삭제됐다 — **이 수치는 2026-07-30 스냅샷이고 지금 갱신하는 장치가 없다.**)
