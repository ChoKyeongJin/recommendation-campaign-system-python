# 작업 노트 — 닫힌 문형 어댑터의 범용화 (2026-08-04)

대상은 커밋 `9429e99` 감사에서 남은 것으로 판정한 **부채 2종**이다.

| # | 부채 | 증상 |
|---|---|---|
| ① | 문장 템플릿 정규식 4종 | 커버리지 단위가 **문장**이라 어미 하나만 달라져도 무효. 확장이 선형(케이스마다 정규식 추가) |
| ② | `graph_rag` 2곳의 케이스 전용 예외 주입 | 드롭 경고 로직이 특정 어댑터 모듈을 직접 호출 — 결합이 감지기 밖으로 퍼짐 |

**지금 어디까지 왔나.** 설계대로 **구현까지 끝났다**(§5의 17개 파일). 부채 ①·② 모두 종료됐고
전량 검증(§8)은 그린이다. 가장 값을 한 것은 여전히 실측이다 — 부채 ①과 ②가 서로 다른 문제가
아니라 **같은 원인**의 두 증상이라는 것이 드러났고(§3-1), 그래서 공용 모듈 하나가 둘을 함께
갚았다. 부채 ②의 정답은 이미 저장소 안에 문서로 적혀 있었다(§2-2).

| 찾는 것 | 어디 |
|---|---|
| 지금까지 한 작업 | §1(감사 범위) · §2(실측으로 알아낸 것) |
| 내린 결정과 이유 | §3(설계 결정 7종) · §4(모듈 설계) |
| 수정·추가한 파일 | §5(17개 + 계획과 갈린 것 2가지) |
| 끝난 일과 남은 후속 | §6 |
| 성패 기준과 검증 | §7(불변식 7줄) · §8(실행 결과) |

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

**17개(신설 3 · 삭제 1 · 수정 13).** 계획과 같고, 실행하며 갈린 두 가지는 표 아래에 적었다.

| 파일 | 한 일 |
|---|---|
| `audience_frame.py` | **신설** — §4 의 primitive(+ `surface_spans`·`alias_stems`·`residue_pieces`) |
| `docs/data/runtime/language/parser_lexicon.json` | 어휘 4종 추가(`audience_frame_noun`·`request_directive`·`frame_particle`·`temporal_recency_marker`) |
| `lexicon_patterns.py` | `_CODE_FALLBACK` 동일 추가 |
| `docs/data/runtime/semantics/audience_catalog.json` | `active_cart.selected_by`, `login.absence_restatement_terms` 선언 |
| `event_state_selection.py` | **신설**(= `cart_abandonment_claims.py` 대체, 카탈로그 선언 구동) |
| `cart_abandonment_claims.py` | **삭제** |
| `canonical_signal_coverage.py` | 증거 스팬 단위 소유권 API(`covered_signal_spans`·`owns_signal_span`·`owns_all_signal_spans`) |
| `graph_rag.py` | 어댑터 직접 호출 2곳 → 위 API 로 교체, import 제거, `_purchase_absence_source_spans` 추가 |
| `query_structurer/campaign_plan_v4.py` | 카트 호출부 2곳을 새 모듈로, 폴백 호출부 1곳을 새 이름으로 |
| `rolling_absence_claims.py` | 닫힌 정규식·`login` 하드코딩 제거, 국소 부정 스캔을 `audience_frame` 에 위임 |
| `open_text_scope_claims.py` | 접미 정규식·접두 공백 강제 제거 → 프레임 잔여물 검사 |
| `campaign_metric_claims.py` | `_MEMBER_SUFFIX`·문두 앵커 제거, 지표 id 하드코딩 → `claim_synthesis` 선언 지표 순회. 2026-08-05 이후 이 모듈은 **합성기가 아니라 모순 탐지기**다(캠페인당 평균 축 폐기 — 같은 문장이 조용히 행당 평균으로 바뀌는 것을 fail-close 시킨다) |
| ~~`profile_metric_claims.py`~~ | `_MEMBER_SUFFIX`·문두 앵커 제거 — **파일은 2026-08-05 삭제됨**(프로필 스칼라 지표 축 폐기). 아래 §1 표의 링크도 같은 이유로 실재하지 않는 경로다 |
| `tests/test_audience_frame.py` | **신설** — primitive 단위 + 부정 케이스 + 문형 복귀 래칫 |
| `tests/test_cart_abandonment_replay.py` | 새 모듈명·API 로 갱신(§7 불변식 7줄 보존, 일반화 긍정 케이스 추가) |
| `tests/test_rolling_absence_claims.py` | 새 이름으로 갱신 + 일반화 긍정 케이스(아래 참조) |
| `pyproject.toml` | 신규 모듈 2종과 그 테스트를 ruff `include` 에 |

**계획과 갈린 것 1 — 함수 이름.** `rolling_absence_claims.synthesize_closed_dormant_login_absence`
를 `synthesize_closed_rolling_absence` 로 바꿨다. `login` 하드코딩을 뺀 뒤에도 이름이 login 을
부르면 모듈명이 거짓이 되는 것과 같은 문제다(§3-2 #6 의 근거를 함수에도 적용).

**계획과 갈린 것 2 — 의도한 동작 확대.** 문장 템플릿을 절 구조로 바꾸면 **같은 뜻의 변형이 함께
열린다**. 그래서 기존 거절 케이스 하나가 수용으로 뒤집혔다.

| 입력 | 전 | 후 | 왜 |
|---|---|---|---|
| `6개월 이상 접속하지 않은 고객` | 거절 | **수용** | 문형이 `휴면` 을 요구했을 뿐이다. 영수증(기간·단위·`>=`·소스·국소 부정·원장 전량 소비)은 이미 전부 증명돼 있고, 잔여물 `고객` 은 조건이 아니다 |

같은 이유로 `6개월 이상 구매하지 않은 회원`(로그인이 아닌 소스)도 폴백이 복구한다. 반면
`6개월 이상 구매하지 않은 휴면 고객` 은 계속 거절이다 — `휴면` 은 **login 의** 부재 동어반복으로만
선언돼 있어서 구매 부재 옆에서는 프레임이 아니다(선언이 실제로 일을 한다는 증거).

---

## 6. 진행 — 전부 완료

1. ✅ `audience_frame.py` 신설 + 어휘 4종(JSON·폴백 양쪽).
2. ✅ 카탈로그 선언 2종(`selected_by`·`absence_restatement_terms`).
3. ✅ `event_state_selection.py` 신설 → `campaign_plan_v4` 호출부 교체 → `cart_abandonment_claims.py` 삭제.
4. ✅ `canonical_signal_coverage` 스팬 소유권 API → `graph_rag` 2곳 교체(**부채 ② 종료** — 이제
   `graph_rag` 에 어댑터 모듈 import 가 없다).
5. ✅ 나머지 문형 3종(휴면 로그인 · 단일 보완 상품 · 지표 2종) 프레임으로 일반화(**부채 ① 종료**).
6. ✅ 래칫 — `tests/test_audience_frame.py::test_no_module_regex_matches_the_whole_request`.
   6개 모듈을 AST 로 훑어 "모듈 상수 정규식을 원문(또는 그 접두/접미)에 `fullmatch`/`match`" 하는
   자리가 생기면 실패한다. 인라인 `re.fullmatch(r"\s+", query[a:b])` 같은 절 **안쪽** 접착부는
   대상이 아니다 — 문형과 구조 검사를 구분하는 선이 그것이다.
7. ✅ 전량 검증(§8) 그린.

**후속(이 작업 범위 밖).**

- `결제`가 구매 존재/부재 어휘에서 빠져 있는 것(§2-1). 여전히 별건이다.
- **띄어 쓴 부정 부사**(`결제 안 한`)를 `local_negation_spans` 가 못 읽는다. `generic_negation` 에
  공백형이 없고(`event_negation_marker` 의 `안한` 은 공백 제거 문자열용이다), 그 어휘를 넓히면
  구매 존재/부재 판정 전체가 흔들린다 — `결제` 와 같은 성격의 별건이다. 구조가 아니라 **어휘**의
  한계라는 것을 드러내려고 `tests/test_cart_abandonment_replay.py` 의 거절 목록에
  `spaced-negation-adverb-lexicon-gap` 으로 이름을 붙여 고정해 두었다.

---

## 7. 깨면 안 되는 불변식 — 범용화 뒤에도 전부 거절이어야 한다

[tests/test_cart_abandonment_replay.py](tests/test_cart_abandonment_replay.py)가 고정한 목록이고,
**이 작업의 성패 기준**이었다. 일곱 줄 모두 거절로 통과한다(각 케이스에 거절 사유를 이름으로 붙였다).
오른쪽이 새 구조에서 무엇이 막는지다.

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

## 8. 검증 명령 — 실행 결과

로컬에 파이썬이 없어 컨테이너에서 돈다(`docker compose exec -T python …`). `ruff`/`mypy` 는 이미지에
없어 컨테이너에 임시 설치했다(CI 는 `ruff==0.16.1` 로 `F821` 만 본다).

```bash
# 당시 목록에 있던 캠페인/프로필 지표 합성 테스트 2건은 2026-08-05 폐기 축 이행에서
# 삭제됐다(그 계약이 사라졌다). 아래는 그 이전 시점의 실행 기록이다.
python -m pytest tests/test_audience_frame.py tests/test_cart_abandonment_replay.py \
  tests/test_rolling_absence_claims.py tests/test_product_complement_replay.py \
  tests/test_canonical_signal_coverage_drift.py tests/test_doc_claims.py -q
                                        # → 140 passed
python -m pytest -q                     # → 2024 passed / 24 skipped, 실패 0
python -m mypy                          # → Success: no issues found in 28 source files
python -m ruff check                    # → All checks passed!
```

**기준선 주의.** 착수 시점 전량은 `1 failed, 1983 passed, 26 skipped` 였다. 그 하나는 코드가 아니라
**이 문서**였다 — `test_doc_claims` 가 §5 의 `tests/test_audience_frame.py` 인용을 "없는 테스트를
근거로 든 문장"으로 잡았다. 파일이 생기면서 함께 해소됐다. 노트 작성 시점의 옛 수치(`1701 passed`)는
현재 값으로 갱신했다. skip 수는 실행마다 24~26으로 흔들린다(착수 전에도 그랬다) — 실패 0이 기준이다.

라이브 코퍼스는 회귀 게이트가 아니다(같은 코드로 두 번 돌려 77종 중 43종이 갈린 실측이 있다).
동치 증명은 `git stash` 차등으로 한다.
