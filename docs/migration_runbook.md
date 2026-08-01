# 파서 이행 운영 절차

"새 표현이 나올 때마다 파이썬을 고치는" 구조를 "주 1회 분류하는" 구조로 바꾸는 프로그램의 운영 문서다.

## 원칙 하나

새 표현이 들어오면 셋 중 하나다. **C 만 코드다.**

| | 무엇 | 어디에 | 배포 |
|---|---|---|---|
| **A 어휘** | 기존 능력의 새 표면어 | `docs/data/parser_lexicon.json` 의 `vocabularies` | 불필요 |
| **B 파라미터** | 기존 팩트의 새 조건 모양 | `targeting_ir.CONDITION_SPECS` / `_FilterSpec` 레지스트리 | 필요 |
| **C 능력** | 새 grain·조인이 필요 | 새 ConditionSpec + 빌더 | 필요 |

A 가 코드로 가는 것이 원래 문제였다. C 가 코드인 것은 정상이다.

### A 를 사람이 다 못 적을 때 — LLM 보완

A(어휘)는 끝이 없다. 사전에 없는 말투는 규칙이 조용히 침묵하므로, 그 **빈칸만** LLM 이 채운다
(`CONDITION_SLOT_LLM_FALLBACK`, `docs/prompts/condition_slot_extract_system.txt`). 대신 채우는
범위가 묶여 있다 — 이 경계가 "LLM 이 스키마를 지어내지 않는다"의 실질이다.

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
`plan_decisions` 에 `source: llm` 으로 남는다. 계약은 `tests/test_condition_slot_llm.py` 와 (이 테스트는 규칙 계층 철거와 함께 삭제됨)
`tests/test_surface_lexicon_llm.py` 가 강제한다. (이 테스트는 규칙 계층 철거와 함께 삭제됨)

### 표면 신호는 이미 A 를 졸업했다

의도·목적·문맥 신호어(채널 발송·판매 아웃리치·재활성·재구매·장바구니 이탈·구매이력·집계 함수어)는
낱말 목록이 아니라 **뜻**으로 판정한다. 표면어 소유권은 `lexicon_llm.py` + `surface_concepts.json`
(개념 선언)에 있고, 데이터 파일에서는 그 낱말이 빠졌다. 즉 **이 부류에는 A 가 없다** — 새 말투가
나와도 사전에 한 줄 추가할 일이 없고, 새 *개념*이 필요할 때만 `surface_concepts.json` 에 항목을 더한다.

코드에 남은 낱말 목록(`graph_rag._DEFAULT_TARGETING_LEXICON` 의 LLM 소유 그룹,
`analytical_intent._AGGREGATE_FUNCTION_BACKSTOP`)은 **동결 백스톱**이다. 키가 없거나
`SURFACE_LEXICON_LLM=off` 인 환경(테스트 포함)에서 이관 전 결정론 동작을 재현하는 것이 유일한
역할이고, 손으로 늘리지 않는다 — `tests/test_surface_lexicon_llm.py` 의 래칫이 강제한다. (이 테스트는 규칙 계층 철거와 함께 삭제됨)

### 불리언으로는 부족한 뜻 — 의미 신호(status)

표면 개념은 "이 문장이 그 얘기인가"라는 **불리언**이다. 그런데 어떤 뜻은 불리언으로 접는 순간 반드시
틀린다. 구매가 그렇다 — `샀다`·`살까 고민 중`·`사지 않았다`·`구매 방법을 알려줘`는 전부 "구매 얘기"
지만 오디언스는 완전히 다르다. 이 부류는 `semantic_signal.py` + `docs/data/semantic_signals.json` 이
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
하나와 그 뜻을 소비하는 코드를 더한다. 표현형 전수는 `tests/test_semantic_signal.py` 가 갖는다. (이 테스트는 규칙 계층 철거와 함께 삭제됨)

대체되지 **않은** 어휘도 있다. 문장에 있는가가 아니라 **어디에 있는가**로 판정하는 것들이다:
대상 지향 표지(절 분리 지점), 장바구니 어휘(금액·수량 인접성), `parser_lexicon.json` 어휘(교대
정규식으로 합성), `normalization_rules` 의 동의어(매칭 스팬 `matched_text` 를 하위가 소비),
`segment_lexicon.json` 의 별칭·연산자·표지(`_metric_ending`/`_metric_leading` 이 접두·접미 위치로
비교 방향을 정한다). 불리언이 아니라 위치라서 개념 판정으로 못 바꾼다.

## 주간 루틴 (15분)

```bash
docker compose exec -e PYTHONPATH=/app -w /app api \
  python tools/weekly_triage.py --unresolved logs/unresolved.jsonl --shadow logs/parser_shadow.jsonl
```

`docs/weekly_triage.md` 를 열고 **`decision` 열만 채운다.**

1. **§1 이행 지표** — `조용한 소실 슬롯` 이 0 이 아니면 그것부터 본다. 그 슬롯은 값을 못 만들 때
   아무 표시 없이 사라지므로, 미해석 큐에 잡히지 않는다. 즉 루프의 사각지대다.
2. **§2 미해석 표현** — 빈도순. 초안(A/B/C)은 정렬용이지 판정이 아니다. 판정 후:
   - A → 사전 어휘에 낱말 추가 → `pytest tests/` → 끝 (배포 불필요)
   - B → 레지스트리 한 줄 + 골든 케이스 추가
   - C → `cases.json` 에 `known_gap` 으로 먼저 기록하고 별도로 설계
3. **§3 슬롯 승격 후보** — `막는 것` 이 비어 있는 슬롯만 `docs/data/slot_policy.json` 에서
   `owner: llm` 으로 바꾼다. 문턱을 손으로 낮추지 않는다.

## 환경 변수

| 변수 | 값 | 뜻 |
|---|---|---|
| `UNRESOLVED_LOG` | 경로 | 미해석 큐 적재 위치. **미설정이면 큐가 안 쌓여 루프가 돌지 않는다** |
| `PARSER_SHADOW_MODE` | `off`(기본)/`shadow`/`enforce` | shadow 는 관찰만, enforce 는 LLM 소유 슬롯만 채택 |
| `PARSER_SHADOW_LOG` | 경로 | shadow 관찰 누적 위치(승격 판단의 유일한 근거) |
| `PARSER_LEXICON_PATH` | 경로 | 파서 표면어 사전 |
| `SLOT_POLICY_PATH` | 경로 | 슬롯 소유권 정책 |
| `TARGET_OBJECT_LLM_FALLBACK` | `true`(기본)/`false` | 상품명 LLM 후보. **끄면 상품 조건이 조용히 사라진다** |
| `CONDITION_SLOT_LLM_FALLBACK` | `true`(기본)/`off` | 사전에 없는 말투를 조건 슬롯으로 채우는 LLM 보완(회원 상태 플래그·쿠폰 임계·**회원 지표 선택**). 끄면 동결 백스톱 표면어와 `segment_lexicon.json`·`member_metrics.json` 동의어만으로 동작한다(기존 동작). 켜져 있으면 회원 명사가 있고 규칙이 플래그를 못 올린 질의마다 빠른 모델 호출이 1회 추가되고, 지표 개념 신호가 참인 질의에 1회 더 추가된다 |
| `SURFACE_LEXICON_LLM` | `true`(기본)/`off` | 표면 신호(의도·목적·문맥·집계 함수어)의 LLM 해석. **끄면 동결 백스톱 낱말만 읽으므로 처음 보는 말투가 조용히 침묵한다**(이관 전 동작). 켜져 있으면 질의당 빠른 모델 호출이 1회 추가되고, 그 결과는 질의 스코프 안에서 재사용된다(절 단위로 다시 부르지 않는다) |
| `SURFACE_CONCEPTS_PATH` | 경로 | 표면 개념(닫힌 집합) 선언 파일 |
| `SEMANTIC_SIGNAL_LLM` | `true`(기본)/`off` | 의미 신호(구매 등)의 구조화 판정. **끄면 형태 판정 폴백만 돌아 의향·가정·단순 언급이 발생과 구분되지 않는다**(이관 전 동작). 켜져 있으면 질의당 선언된 뜻 수만큼 빠른 모델 호출이 추가되고, 그 결과는 질의 스코프 안에서 재사용된다(절 단위로 다시 부르지 않는다) |
| `SEMANTIC_SIGNALS_PATH` | 경로 | 의미 신호(뜻 + detected 정책) 선언 파일 |
| `SEMANTIC_SIGNAL_LOG_EVIDENCE` | `off`(기본)/`on` | 관측 로그에 판정 근거(원문 조각)를 남길지. 원문은 개인정보일 수 있어 기본은 끔 |
| `SEMANTIC_AST_GATE` | `on`(기본)/`off` | 의미 AST 게이트(포함·제외 충돌 검사 + 생성 SQL 극성/구조 역검증). 조건을 만들지 않고 '조용한 의미 변형'만 차단하므로 켠 상태가 기본이다. `off` 는 이관 비교·사고 대응용 비상구 |

## 안전장치 (전부 `pytest tests/` 가 강제)

| 장치 | 파일 | 막는 것 |
|---|---|---|
| 골든 IR 스냅샷 | `tests/golden/` | 파서 변경이 조건을 조용히 잃는 것 |
| `known_gap` 마커 | `tests/golden/cases.json` | 결함을 스냅샷으로 축복하는 것 (고쳐지면 마커를 지우라고 실패) |
| 코드 규칙 래칫 | `docs/data/regex_inventory_baseline.json` | 새 표면어를 또 코드로 받는 것 (어휘형·낱말집합·업무의미형 상한) |
| 래칫 스캔 범위 | `tests/test_regex_inventory_ratchet.py` | 규칙이 **세지지 않는 형태**로 들어오는 것(묶음·인라인 정규식·낱말집합) | (이 테스트는 규칙 계층 철거와 함께 삭제됨)
| rule 생산자 래칫 | `tests/golden/method_mix_baseline.json` | 조건 생산자가 정규식으로 늘어나는 것 |
| 조용한 소실 상한 | `tests/test_slot_policy.py` | 백스톱도 fail-close 도 없는 슬롯이 느는 것 | (이 테스트는 규칙 계층 철거와 함께 삭제됨)
| 이관 동등성 | `tests/test_lexicon_patterns.py` | 사전으로 옮기며 몰래 어휘를 넓히는 것 |
| LLM 슬롯 경계 | `tests/test_condition_slot_llm.py` | LLM 이 목록 밖 값·근거 없는 조건을 만들어내는 것 | (이 테스트는 규칙 계층 철거와 함께 삭제됨)
| 세그먼트 소유권 분리 | `tests/test_segment_semantics.py` | 표면어와 접지(소스·capability)가 한 파일로 다시 섞이는 것 | (이 테스트는 규칙 계층 철거와 함께 삭제됨)
| 미해석 오탐 | `tests/test_ir_golden_corpus.py` | 탐지기가 정상 프롬프트를 잡아 큐를 잡음으로 덮는 것 | (이 테스트는 규칙 계층 철거와 함께 삭제됨)
| 의미 AST 불변식 | `tests/test_semantic_ast.py` | 부정·AND/OR·owner 가 정규화 과정에서 뒤집히거나 사라지는 것 |
| 의미 신호 계약 | `tests/test_semantic_signal.py` | 발생·의향·부정·동음이의가 한 boolean 으로 뭉쳐지는 것, 폴백이 OR 로 퇴화하는 것, 메타데이터가 의미 비교에 섞이는 것, 재작성이 뜻을 지우거나 지어내는 것 | (이 테스트는 규칙 계층 철거와 함께 삭제됨)
| 의미 보존 계약 | `tests/test_plan_semantic_ast.py` | 제외가 포함으로 컴파일되는 것, OR 이 AND 로 축소되는 것, 포함/제외 충돌이 한쪽만 실행되는 것, rules/LLM 경로가 다른 의미로 갈라지는 것 | (이 테스트는 규칙 계층 철거와 함께 삭제됨)

## 재생성 명령

```bash
# 골든 스냅샷 + 방법 구성 기준선 (diff 를 눈으로 검토한 뒤 커밋)
python tools/regen_ir_goldens.py

# 규칙 인벤토리 (--set-baseline 은 상한을 내릴 때. 올릴 때는 --reason 이 필수다)
python tools/regex_inventory.py [--set-baseline] [--reason "..."]
```

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

## QueryPlan V3 LLM-first 전환

캠페인 API의 기본 파서는 `auto`이며, 원문을 재작성하거나 정규식으로 읽기 전에 strict
`CampaignQueryPlanV3` 구조화를 먼저 수행한다. V3는 채택한 슬롯의 원문 구간을
`semantic_evidence`에 남기고 표현할 수 없는 의미는 `unresolved`로 반환한다. LLM 출력은
SQL을 포함하지 않으며 기존 결정론 컴파일러와 SQL 검증기를 그대로 통과한다.

| 환경 변수 | 값 | 동작 |
|---|---|---|
| `QUERY_PARSER` | `auto`(기본) / `llm` / `rules` | `auto`와 `llm`은 V3 의미 구조화를 사용한다. `rules`는 명시적 레거시 경로다. |
| `QUERY_PLAN_AUTHORITY` | `llm_first`(기본) | V3가 충돌 슬롯을 소유하고 레거시 규칙은 빈 슬롯만 보완한다. |
| `QUERY_PLAN_AUTHORITY` | `shadow` | 기존 rules-first 실행을 유지하며 `PARSER_SHADOW_*`로 차이를 관측한다. |
| `QUERY_PLAN_AUTHORITY` | `rules_first` | 즉시 롤백용. 기존 규칙 우선순위와 지연 LLM 보완을 사용한다. |

LLM-first에서 원문 권위 규칙은 실행 플랜을 수정하지 않는다. 복사본에 적용해 V3와 비교하며,
차이가 있으면 `llm_legacy_semantic_disagreement` 미해결 조건으로 기록해 SQL 생성을 차단한다.

배포 순서:

1. `QUERY_PLAN_AUTHORITY=shadow`, `PARSER_SHADOW_MODE=shadow`로 슬롯별 일치율을 수집한다.
2. 위험 차이를 골든 코퍼스에 추가하고 V3 스키마·프롬프트를 수정한다. 새 문장별 정규식은 추가하지 않는다.
3. `QUERY_PLAN_AUTHORITY=llm_first`로 전환한다.
4. 장애 시 `QUERY_PLAN_AUTHORITY=rules_first` 또는 요청별 `query_parser=rules`로 되돌린다.

계약 테스트는 `tests/test_campaign_plan_v3.py`가 소유한다. (이 테스트는 규칙 계층 철거와 함께 삭제됨)

## 남은 결함 (2026-07-29 기준)

- **조용한 소실 2건** — `target_user.purchase_object` / `purchase_object_kind`. 결정론 백스톱이 없고
  fail-close 표시도 없다. 7단계의 종료 조건은 이 값이 0 이 되는 것이다.
- **부분 소실은 탐지 못 한다** — 미해석 탐지기는 "조건이 하나도 없는" 경우만 잡는다. `안양에 사는`
  처럼 일부만 빠지는 것은 shadow 비교의 `only_candidate` 판정이 담당하므로, shadow 를 켜야 보인다.
- **어휘형 11개 / 낱말집합 46개 / 업무의미형 218개 / 조립형 5개** (2026-07-30 확대 스캔 기준). 어휘형은
  `_BALANCE_*` 3, `_THRESHOLD_CUE_RE`, `_OUTPUT_ACTION_RE`, `_MEMBER_METRIC_COMPARISON_RE`,
  `_TREND_ORDER_MARKER_RE`, `_NOISE_LABEL`(빌드 스크립트) + 인라인 3. 우선순위는
  `docs/regex_inventory.md` 의 교대수 순서다. 낱말집합 상위는 `_VALUE_TAIL_TOKENS`(45),
  `query_semantics.NON_ENTITY_TERMS`(36), `_NOISE_OPERAND_TOKENS`(32).
