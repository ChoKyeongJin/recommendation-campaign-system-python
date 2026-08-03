# 작업 노트 — 닫힌 문형 어댑터의 범용화 (2026-08-04)

대상은 커밋 `9429e99` 감사에서 남은 것으로 판정한 **부채 2종**이다.

| # | 부채 | 증상 |
|---|---|---|
| ① | 문장 템플릿 정규식 4종 | 커버리지 단위가 **문장**이라 어미 하나만 달라져도 무효. 확장이 선형(케이스마다 정규식 추가) |
| ② | `graph_rag` 2곳의 케이스 전용 예외 주입 | 드롭 경고 로직이 특정 어댑터 모듈을 직접 호출 — 결합이 감지기 밖으로 퍼짐 |

**지금 어디까지 왔나.** 실측·설계까지 끝났고 **코드 변경은 0건**이다(§5). 작업 디렉터리는 깨끗하다.
가장 값을 한 것은 실측이다 — 부채 ①과 ②가 서로 다른 문제가 아니라 **같은 원인**의 두 증상이라는
것이 드러났고(§3-1), 부채 ②의 정답은 이미 저장소 안에 문서로 적혀 있었다(§2-2).

| 찾는 것 | 어디 |
|---|---|
| 지금까지 한 작업 | §1(감사 범위) · §2(실측으로 알아낸 것) |
| 내린 결정과 이유 | §3(설계 결정 7종) |
| 수정·추가한 파일 | §5(현재 0건 + 계획 표) |
| 남은 할 일 | §6(순서) · §7(깨면 안 되는 불변식) · §8(검증 명령) |

---

## 1. 감사 범위 — 무엇을 읽었나

커밋 `9429e99`(+30,713줄, 151파일)에서 **닫힌 문형**에 해당하는 자리를 전수로 찾아 읽었다.

| 자리 | 무엇이 닫혀 있나 |
|---|---|
| [cart_abandonment_claims.py:19](cart_abandonment_claims.py#L19) | `_RECENT_ACTIVE_CART_RE.fullmatch(query)` — 문장 전체가 한 문형과 같아야 함 |
| [rolling_absence_claims.py:13](rolling_absence_claims.py#L13) | `_CLOSED_DORMANT_LOGIN_ABSENCE_RE.fullmatch` + `source == "login"` 하드코딩 |
| [open_text_scope_claims.py:36](open_text_scope_claims.py#L36) | `_SINGLE_COMPLEMENT_AUDIENCE_SUFFIX_RE` + `query[:claim.start]` 공백 강제 |
| [campaign_metric_claims.py:32](campaign_metric_claims.py#L32) · [:115](campaign_metric_claims.py#L115) | `_MEMBER_SUFFIX` + `semantic_metric_id = "campaign_purchase_amount"` 하드코딩 |
| [profile_metric_claims.py:36](profile_metric_claims.py#L36) | `_MEMBER_SUFFIX`(위와 **거의 같은 목록의 복제본**) |
| [graph_rag.py:8228](graph_rag.py#L8228) · [:8453](graph_rag.py#L8453) | 드롭 경고·불변식 검사가 `cart_abandonment_claims`를 직접 호출 |

호출부도 함께 읽었다 — [campaign_plan_v4.py:1122](query_structurer/campaign_plan_v4.py#L1122)(단일 보완 상품),
[:1190](query_structurer/campaign_plan_v4.py#L1190)(active_cart), [:1380](query_structurer/campaign_plan_v4.py#L1380)(휴면 로그인 폴백).

---

## 2. 실측으로 알아낸 것

### 2-1. 부정 표면 감지기가 `결제`를 구매 동사로 보지 않는다

`purchase_lexicon.NEGATIVE_MEMBERSHIP_RE`(= `graph_rag._PURCHASE_NEG_RE`)를 실제 문장에 돌렸다.

| 문장 | NEG 매치 |
|---|---|
| 최근 30일 장바구니에 담아두고 **결제**하지 않은 회원 | **없음** |
| 최근 30일 장바구니에 담아두고 **구매**하지 않은 회원 | `구매하지않` |
| 최근 30일 장바구니에 담아두고 구매 안 한 회원 | `구매안한` |
| 30일간 장바구니에 담아두고 **미구매**한 회원 | `미구매` |

원인은 어휘 분리다 — `purchase_membership_verb`(구매·구입·주문·샀)에 `결제`가 없고, `결제`는
`event_alias_purchase`에만 있다. **결과**: 라이브 프롬프트 #11 원문("결제하지 않은")에서는
`purchase_absence_mentioned`가 False라 `graph_rag`의 cart 예외가 애초에 발동하지 않는다.
그 예외는 `구매하지 않은`/`미구매` 계열 어미일 때만 실효가 있다.

→ 부채 ②를 "예외를 일반 규칙으로 바꾸는 일"로 볼 수 있는 근거다. 지금 그 자리는 **한 문형을 위한
예외인데 정작 그 문형에서는 안 쓰인다**.

### 2-2. 부채 ②의 정답은 이미 저장소가 적어 두었다

[canonical_signal_coverage.py](canonical_signal_coverage.py) docstring, 저자 본인의 기록:

> 입도 주의(알려진 한계): 판정은 **family 단위**다. … 정확한 최종형은 **근거 스팬 단위(IR 원자의
> Evidence 가 그 신호의 원문 구간을 덮는가)**이지만, `_prompt_signal_signature`의 신호 추출기
> 대부분이 스팬을 남기지 않아 선행 작업이 필요하다.

`cart_abandonment_claims.owns_recent_active_cart_claim` 호출은 그 "근거 스팬 단위 소유권"의
**케이스 전용 대역**이다. 즉 부채 ②는 새 발명이 아니라 이미 식별된 최종형의 구현이다.

### 2-3. 리터럴 바인딩 실측 (`extract_literal_bindings`)

| 입력 | 바인딩 |
|---|---|
| 최근 30일 … | `duration "30일" [3,6) rolling_duration` — **`최근`은 바인딩 밖**(잔여물) |
| 30일 전 … | `duration "30일" [0,3) past_point` — 롤링 창 아님 |
| 3개월 이상 접속하지 않은 휴면 고객 | `duration "3개월" rolling_duration` + `comparison_operator "이상" >=` |

→ 잔여물 검사기는 `최근`·`지난` 같은 **최근성 표지**를 프레임 어휘로 알아야 하고, `30일 전`은
`temporal_kind`만으로 자동 거절된다(문형 정규식이 하던 일을 원장이 대신함).

### 2-4. 어휘·카탈로그 자산 실측

- `lexicon_patterns`가 이미 "어휘=데이터, 구조=코드"의 단일 소스다. 필요한 축 대부분이 이미 있다 —
  `member_noun`(회원·고객·사용자) / `member_noun_role`(사람·구매자·소비자) / `bound_particle` /
  `clause_scope_marker`(중·가운데·중에서) / `and_connective` / `or_connective` /
  `generic_negation`(없·않·미구매…) / `product_noun` / `source_entity_domain`.
- **없는 것 3종**: 요청 지시어(찾아줘·추출해줘…), 처소격 조사(에·에서·의·로), 최근성 표지(최근·지난).
  지금은 이 낱말들이 4개 모듈에 정규식으로 **복제**돼 있다(`_MEMBER_SUFFIX` 2벌 +
  `_SINGLE_COMPLEMENT_AUDIENCE_SUFFIX_RE` + cart 정규식 꼬리 + [set_expression_engine.py:281](set_expression_engine.py#L281)).
- 파리티 테스트([tests/test_lexicon_patterns.py:110](tests/test_lexicon_patterns.py#L110))는 **patterns만** 강제한다.
  vocabularies 는 강제 대상이 아니지만 JSON 과 `_CODE_FALLBACK` 양쪽을 갱신하는 것이 모듈 관례다.
- 카탈로그 `cart` 별칭에 `담기`가 이미 있다 → 어간 `담`을 데이터에서 파생할 수 있다("담아두고"를
  코드에 적지 않아도 된다).
- `member_noun_role`에 `구매자`가 들어 있다 → **프레임 명사로 그대로 쓰면 안 된다**(구매 조건이
  프레임으로 위장해 통과함). 프레임 전용 어휘를 따로 선언해야 한다.

---

## 3. 내린 결정과 이유

### 3-1. 결정 1 — 부채 ①과 ②를 하나의 primitive 로 함께 푼다

두 부채의 원인이 같다. 문장 템플릿이 하고 있던 일은 사실 **세 가지 구조 판정**이다.

1. 두 표면(소스 별칭 · 부정 표지)이 **같은 절**에 있는가
2. 그 밖의 나머지 문자열이 조건을 담지 않는 **잉여어(프레임)** 뿐인가
3. 추출된 **리터럴 원장이 전량 소비**되는가

부채 ②의 "과대 증거 차단"도 정확히 1번이다 — 아래 §7의 적대 케이스(IR 증거가 문장 전체)를
막는 것은 "증거 스팬이 부정 표면을 덮는가"가 아니라 "그 둘이 같은 절인가"이기 때문이다.
그래서 공용 모듈 `audience_frame.py` 하나가 두 부채를 함께 갚는다.

### 3-2. 결정 표

| # | 결정 | 이유 |
|---|---|---|
| 2 | 문형 정규식 → (같은 절 + 프레임 잔여물 + 원장 전량 소비) 3조건 | 커버리지 단위가 문장에서 **절 구조**로 바뀐다. 어미·요청어·어순이 달라져도 같은 판정 |
| 3 | 소스 변형 선택을 카탈로그 선언으로: `active_cart.selected_by = {source: cart, negated_event: purchase}` | "카트 표면 + 구매 부정이 같은 절 ⇒ KEEP_YN='Y' 소스"는 도메인 사실이지 파서 사정이 아니다. 유사 축(미사용 쿠폰 등)은 JSON 한 항목으로 는다 |
| 4 | 드롭 감지기 소유권을 family → **증거 스팬** 단위로 (`canonical_signal_coverage`가 소유) | §2-2 의 명시된 최종형. `graph_rag`에서 어댑터 모듈 import 가 사라진다 |
| 5 | `휴면` 같은 동어반복 상태어는 카탈로그 `absence_restatement_terms` 선언으로 프레임 허용 | 지금은 문형 정규식이 `휴면`을 조용히 삼킨다. 삼키는 근거를 **데이터로 드러내면** 드리프트 가드가 볼 수 있다 |
| 6 | `cart_abandonment_claims.py` → `event_state_selection.py` 로 이름·소유 재정의 | 일반화되면 모듈명이 거짓이 된다(카트 전용이 아니다). 저장소 관례는 "모듈 = 한 소유자" |
| 7 | 새 어휘 3종은 lexicon JSON + `_CODE_FALLBACK` 양쪽에 | 파일이 폴백보다 좁아지면 조용히 옛 어휘로 돌아간다(모듈 docstring 의 경고) |

### 3-3. 명시적으로 **하지 않기로** 한 것

- **어댑터를 LLM 에 맡기지 않는다.** 부정 목록·정규식 원자·좌표 산술은 결정론이 소유해야 한다
  (`~/.claude` 메모리의 "LLM 에 넘기면 안 되는 5종").
- **감지기를 canonical 이라는 이유로 면제하지 않는다.** `canonical_signal_coverage` docstring 이
  기록한 2026-08-02 실측(면제 시 등급·수신동의 조건이 경고 없이 증발)이 있다. 소유권은 **좁히는**
  방향으로만 바꾼다.
- **`purchase_membership_verb`에 `결제`를 넣지 않는다**(§2-1). 지금 고치면 이 작업과 무관한 곳의
  구매 존재/부재 판정이 함께 흔들린다. 별건으로 분리한다(§6 후속).

---

## 4. 설계 — `audience_frame.py` (신설 예정)

```
is_frame_only(query, owned_spans, *, extra_terms=())   잔여물이 잉여어뿐인가
in_same_clause(query, left, right, *, stems, content_terms)   두 스팬이 한 절인가
local_negation_spans(query, aliases)                   별칭이 국소 부정된 구간들
compact_to_source_span(query, start, end)              compact 좌표 → 원문 좌표
```

- **프레임 어휘**: 신규 `audience_frame_noun`(회원·고객·사용자·유저·고객님·사람) + 신규
  `request_directive` + 신규 `frame_particle` + 기존 `bound_particle`/`object_particle`.
  `member_noun_role`은 통째로 쓰지 않는다(§2-4 의 `구매자` 함정).
- **절 경계어**(같은 절 판정에서 거절): 프레임 명사 · `clause_scope_marker` · `and_connective` ·
  `or_connective` · `enum_connective` · 쉼표.
  → 프레임 명사는 **잔여물에서는 허용, 절 사이에서는 거절**이다(같은 낱말, 다른 자리, 다른 뜻).
- **어간 활용**: 소유 소스의 별칭에서 어간 파생(`담기`→`담`) 후 `담아두고`·`담은`을 활용형으로 인정.
- **compact 좌표 변환**: `graph_rag`의 부정 감지기는 공백 제거 문자열 위에서 돈다. 길이가 달라지면
  (`casefold` 확장) `None` 으로 fail-close.

---

## 5. 수정·추가한 파일

**현재 0건.** `git status` 깨끗하고 커밋도 없다. 아래는 **계획**이다.

| 파일 | 예정 작업 |
|---|---|
| `audience_frame.py` | **신설** — §4 의 4개 primitive |
| `docs/data/runtime/language/parser_lexicon.json` | 어휘 3종 추가(`audience_frame_noun`·`request_directive`·`frame_particle`·`temporal_recency_marker`) |
| `lexicon_patterns.py` | `_CODE_FALLBACK` 동일 추가 |
| `docs/data/runtime/semantics/audience_catalog.json` | `active_cart.selected_by`, `login.absence_restatement_terms` 선언 |
| `event_state_selection.py` | **신설**(= `cart_abandonment_claims.py` 대체, 카탈로그 선언 구동) |
| `cart_abandonment_claims.py` | **삭제** |
| `canonical_signal_coverage.py` | 증거 스팬 단위 소유권 API 추가 |
| `graph_rag.py` | 2곳의 어댑터 직접 호출 → 위 API 호출로 교체, import 제거 |
| `query_structurer/campaign_plan_v4.py` | 3개 호출부를 새 모듈로 |
| `rolling_absence_claims.py` | 닫힌 정규식·`login` 하드코딩 제거 → 프레임 잔여물 검사 |
| `open_text_scope_claims.py` | 접미 정규식 제거 → 프레임 잔여물 검사 |
| `campaign_metric_claims.py` | `_MEMBER_SUFFIX` 제거, 지표 id 하드코딩 → `claim_synthesis` 선언 지표 순회 |
| `profile_metric_claims.py` | `_MEMBER_SUFFIX` 제거 |
| `tests/test_audience_frame.py` | **신설** — primitive 단위 + 부정 케이스 |
| `tests/test_cart_abandonment_replay.py` | 새 모듈명·API 로 갱신(불변식 assert 는 보존) |
| `pyproject.toml` | 신규 모듈 2종을 ruff `include` 에 |

---

## 6. 남은 할 일 (이 순서로)

1. `audience_frame.py` 신설 + 어휘 3종(JSON·폴백 양쪽). **먼저 §7 표를 통과시키고** 배선한다.
2. 카탈로그 선언 2종 추가(`selected_by`·`absence_restatement_terms`).
3. `event_state_selection.py` 신설 → `campaign_plan_v4` 호출부 교체 → `cart_abandonment_claims.py` 삭제.
4. `canonical_signal_coverage` 스팬 소유권 API → `graph_rag` 2곳 교체(부채 ② 종료).
5. 나머지 문형 3종(휴면 로그인 · 단일 보완 상품 · 지표 2종) 프레임으로 일반화(부채 ① 종료).
6. 드리프트 가드 추가 — "문장 전체 `fullmatch` 정규식이 소스에 다시 생기면 실패"하는 테스트.
   부채가 같은 모양으로 되돌아오는 것을 막는 유일한 안전망이다.
7. 전량 검증(§8).

**후속(이 작업 범위 밖).** `결제`가 구매 존재/부재 어휘에서 빠져 있는 것(§2-1)은 별건이다.
지금 고치면 이 작업의 회귀 판정과 섞인다.

---

## 7. 깨면 안 되는 불변식 — 범용화 뒤에도 전부 거절이어야 한다

[tests/test_cart_abandonment_replay.py:257](tests/test_cart_abandonment_replay.py#L257)이 고정한 목록이고,
**이 작업의 성패 기준**이다. 오른쪽이 새 구조에서 무엇이 막는지다.

| 입력 | 왜 거절인가 | 새 구조에서 막는 것 |
|---|---|---|
| **30일 전** 장바구니에 담아두고 결제하지 않은 회원 | 롤링 창이 아님 | 원장(`temporal_kind=past_point`) |
| 최근 30일 장바구니에 **담은** 회원 | 부정이 없음 | 국소 부정 부재 |
| 최근 30일 **결제하지 않은** 회원 | 카트 표면이 없음 | 소스 별칭 부재 |
| … 담아두고 **다른 상품을** 결제하지 않은 회원 | 상품 범위 조건이 추가됨 | 절 사이 내용어(`상품`) |
| … 결제하지 않은 **여성** 회원 | 성별 조건이 추가됨 | 잔여물 비프레임(`여성`) |
| … 회원 **또는 VIP 회원** | OR 조건이 추가됨 | 절 경계(`또는`) + 잔여물(`VIP`) |
| 최근 30일 장바구니에 담은 **회원 중** 구매 이력이 없는 회원 | 회원 전체 구매 부재는 별개 조건 | 절 경계(`회원`+`중`) |

마지막 줄이 부채 ②의 적대 케이스([tests/test_cart_abandonment_replay.py:305](tests/test_cart_abandonment_replay.py#L305))다.
여기서 IR 증거는 **문장 전체**라 "증거가 부정 표면을 덮는가"만 보면 통과해 버린다 — 반드시
**같은 절인가**로 판정해야 한다. 이 케이스는 `purchase_absence_dropped` 불변식 위반까지 확인한다.

그리고 라이브 #11 은 계속 SQL 이 나와야 한다 — `EAC.KEEP_YN = 'Y'`, `EAC.INS_DT >= DATEADD(DAY, -30, …)`,
`NOT EXISTS (` 없음, `CRM_SL_ORDER*` 없음.

---

## 8. 검증 명령

```bash
python -m pytest tests/test_cart_abandonment_replay.py tests/test_rolling_absence_claims.py \
  tests/test_product_complement_replay.py tests/test_campaign_metric_claims.py \
  tests/test_profile_metric_claims.py tests/test_canonical_signal_coverage_drift.py -q
python -m pytest -q                     # 전량(기준: 1701 passed / 24 skipped)
python -m mypy                          # query_pipeline strict, 기준 0
python -m ruff check                    # pyproject include 범위
```

라이브 코퍼스는 회귀 게이트가 아니다(같은 코드로 두 번 돌려 77종 중 43종이 갈린 실측이 있다).
동치 증명은 `git stash` 차등으로 한다.
