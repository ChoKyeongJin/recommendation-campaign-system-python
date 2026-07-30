# 규칙 인벤토리 (이행 작업 목록)

총 333개 — 어휘형 11 / 낱말집합 45 / 업무의미형 216 / 문법형 36

래칫 대상: lexical, wordlist, domain (문법형은 구조라 상한 없음).

판정 기준: **이 규칙이 열거하는 것이 단어 목록인가, 구조인가?** 단어 목록이면 데이터로 옮긴다
(`lexicon_patterns` + `docs/data/parser_lexicon.json`). `decision` 은 사람이 채운다.

`source` 열은 규칙이 코드에 담긴 형태다: `constant`(이름 붙은 정규식) / `collection`(튜플·딕트에
담긴 정규식) / `inline`(이름 없는 호출) / `wordlist`(한글 낱말집합 상수).

| 분류 | 형태 | 파일:줄 | 이름 | 교대수 | 패턴 | 자동 판정 사유 |
|---|---|---|---|---:|---|---|
| lexical | constant | build_dimension_catalog.py:42 | `_NOISE_LABEL` | 13 | `팝업\|콤보\|셀렉트\|체크\|check\|popup\|text\|combo\|multiselect\|버전업\|테스트\|test\|::op::` | 메타문자 없는 표면어 교대 13개 — 렉시콘으로 옮길 수 있다 |
| lexical | constant | graph_rag.py:9379 | `_BALANCE_DEFER_PATTERN` | 13 | `가장\|제일\|상위\|하위\|최상위\|랭킹\|순위\|top\|톱\|퍼센트\|프로\|%\|평균` | 메타문자 없는 표면어 교대 13개 — 렉시콘으로 옮길 수 있다 |
| lexical | constant | graph_rag.py:17912 | `_THRESHOLD_CUE_RE` | 12 | `이상\|이하\|미만\|초과\|이내\|같은\|동일\|>=\|<=\|>\|<\|=` | 메타문자 없는 표면어 교대 12개 — 렉시콘으로 옮길 수 있다 |
| lexical | constant | analytical_intent.py:65 | `_OUTPUT_ACTION_RE` | 6 | `알려\|보여\|조회\|계산\|구해\|집계` | 메타문자 없는 표면어 교대 6개 — 렉시콘으로 옮길 수 있다 |
| lexical | constant | analytical_intent.py:86 | `_MEMBER_METRIC_COMPARISON_RE` | 6 | `보다\|초과한\|미만인\|이상인\|이하인\|같은` | 메타문자 없는 표면어 교대 6개 — 렉시콘으로 옮길 수 있다 |
| lexical | constant | graph_rag.py:9383 | `_BALANCE_PRESENCE_PATTERN` | 5 | `보유\|가지고\|가진\|있는\|있으신` | 메타문자 없는 표면어 교대 5개 — 렉시콘으로 옮길 수 있다 |
| lexical | inline | graph_rag.py:9452 | `<inline re.search>` | 5 | `가장\|제일\|상위\|하위\|최상위` | 메타문자 없는 표면어 교대 5개 — 렉시콘으로 옮길 수 있다 |
| lexical | constant | graph_rag.py:11459 | `_TREND_ORDER_MARKER_RE` | 5 | `대비\|보다\|에서\|→\|->` | 메타문자 없는 표면어 교대 5개 — 렉시콘으로 옮길 수 있다 |
| lexical | inline | set_expression_engine.py:135 | `<inline re.search>` | 4 | `포함\|남기고\|그중\|중에서` | 메타문자 없는 표면어 교대 4개 — 렉시콘으로 옮길 수 있다 |
| lexical | constant | graph_rag.py:9384 | `_BALANCE_METRIC_NOUN_PATTERN` | 3 | `보유액\|보유금액\|보유량` | 메타문자 없는 표면어 교대 3개 — 렉시콘으로 옮길 수 있다 |
| lexical | inline | graph_rag.py:11069 | `<inline re.search>` | 2 | `수량\|개수` | 메타문자 없는 표면어 교대 2개 — 렉시콘으로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:7765 | `_VALUE_TAIL_TOKENS` | 45 | `{특별자치시, 특별자치도, 특별시, 광역시, 도, 시, 권, 지역…}` | 코드에 박힌 한글 낱말집합 45개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | query_semantics.py:36 | `NON_ENTITY_TERMS` | 36 | `{가장, 제일, 많이, 적게, 높게, 낮게, 큰, 작은…}` | 코드에 박힌 한글 낱말집합 36개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | set_expression_engine.py:280 | `_NOISE_OPERAND_TOKENS` | 32 | `{고객, 사용자, 사람, 세그먼트, 집합, 대상, 추천, 캠페인…}` | 코드에 박힌 한글 낱말집합 32개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | ingest.py:11 | `KOREAN_PARTICLES` | 28 | `{으로부터, 에서부터, 에게서, 이면서, 이고, 이며, 에서, 에게…}` | 코드에 박힌 한글 낱말집합 28개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:13390 | `_SCHEMA_QUERY_VALUE_UNIT` | 27 | `{개월, 주차, 주간, 년도, 분기, 시간, 달, 주…}` | 코드에 박힌 한글 낱말집합 27개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | set_expression_engine.py:273 | `_QUERY_TAIL_VERBS` | 26 | `{찾아줘, 찾아, 조회해줘, 조회, 보여줘, 알려줘, 추천해줘, 추천해…}` | 코드에 박힌 한글 낱말집합 26개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:1654 | `_CATEGORY_VALUE_STOPWORDS` | 21 | `{구매, 구입, 주문, 판매, 결제, 인기, 동일, 같은…}` | 코드에 박힌 한글 낱말집합 21개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10451 | `_AGG_WINDOW_ANCHOR_TERMS` | 16 | `{구매, 구입, 주문, 결제, 구매액, 매출, 객단가, 금액…}` | 코드에 박힌 한글 낱말집합 16개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:1662 | `_PURCHASE_SIGNAL_STOPWORDS` | 15 | `{이상, 이하, 미만, 초과, 회, 번, 건, 원…}` | 코드에 박힌 한글 낱말집합 15개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:1666 | `_PURCHASE_VALUE_QUALIFIERS` | 14 | `{고액, 소액, 고가, 저가, 고금액, 저금액, 금액, 구매금액…}` | 코드에 박힌 한글 낱말집합 14개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:9509 | `_PERIOD_TOKENS` | 12 | `{지난달, 저번달, 전월, 이번달, 금월, 당월, 지난주, 이번주…}` | 코드에 박힌 한글 낱말집합 12개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10270 | `_SCOPE_PLACEHOLDER_VALUES` | 12 | `{특정, 어떤, 모든, 해당, 일부, 각, 그, 이…}` | 코드에 박힌 한글 낱말집합 12개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:13267 | `_CONSENT_FILLER_TERMS` | 12 | `{활용, 이용, 수집, 제공, 처리, 정보, 광고성, 광고…}` | 코드에 박힌 한글 낱말집합 12개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10498 | `_SHARED_WINDOW_FOREIGN_TERMS` | 11 | `{접속, 로그인, 가입, 캠페인, 반응, 발송, 장바구니, 카트…}` | 코드에 박힌 한글 낱말집합 11개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11380 | `_NON_ORDER_DATE_ANCHORS` | 11 | `{가입, 등록, 생일, 생년, 로그인, 접속, 방문, 발송…}` | 코드에 박힌 한글 낱말집합 11개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11143 | `_CART_RETENTION_MARKERS` | 10 | `{담, 유지, 방치, 넣어, 보관, 남아있, 남겨, 그대로…}` | 코드에 박힌 한글 낱말집합 10개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11146 | `_CART_RETENTION_BENEFIT_WORDS` | 10 | `{쿠폰, 할인, 유효, 기한, 배송, 증정, 적립, 이벤트…}` | 코드에 박힌 한글 낱말집합 10개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | query_semantics.py:45 | `_COMPARISON` | 10 | `{이상, 이하, 초과, 미만, 보다, 같은, 높은, 낮은…}` | 코드에 박힌 한글 낱말집합 10개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:7156 | `_PURCHASE_SCOPE_TIME_WORDS` | 9 | `{상반기, 하반기, 올해, 작년, 금년, 지난달, 이번달, 전월…}` | 코드에 박힌 한글 낱말집합 9개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | query_semantics.py:48 | `_METRICS` | 9 | `{구매금액, 결제금액, 구매액, 구매건수, 구매횟수, 로그인횟수, 적립금, 예치금…}` | 코드에 박힌 한글 낱말집합 9개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10274 | `_SCOPE_DISTINCT_MODIFIERS` | 8 | `{다른, 여러, 다양, 다양한, 각기, 각각, 가지각색, 서로}` | 코드에 박힌 한글 낱말집합 8개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11148 | `_CART_RETENTION_MIN_WORDS` | 8 | `{이상, 넘게, 넘은, 지난, 지났, 째, 동안, 이후}` | 코드에 박힌 한글 낱말집합 8개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11622 | `_SIGNUP_SIGNALS` | 8 | `{신규가입, 신규회원, 신규유저, 신규고객, 새가입, 새로가입, 새가입자, 가입한지}` | 코드에 박힌 한글 낱말집합 8개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11997 | `_RECENT_LOGIN_NEG_SIGNALS` | 8 | `{미접속, 미로그인, 접속하지, 접속안, 로그인하지, 로그인안, 휴면, 비활성}` | 코드에 박힌 한글 낱말집합 8개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | query_semantics.py:46 | `_AGGREGATION` | 8 | `{합계, 총합, 총액, 평균, 평균값, 건수, 개수, 횟수}` | 코드에 박힌 한글 낱말집합 8개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | query_semantics.py:47 | `_DIMENSIONS` | 8 | `{회원, 고객, 사용자, 상품, 브랜드, 카테고리, 지역, 매장}` | 코드에 박힌 한글 낱말집합 8개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:9492 | `_ZERO_AMOUNT_CONTEXT` | 7 | `{구매, 결제, 구매액, 구매금액, 구매 금액, 주문 금액, 주문금액}` | 코드에 박힌 한글 낱말집합 7개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:13570 | `_POLARITY_CORRECTION_MARKERS` | 6 | `{지만, 그러나, 그런데, 이번에는, 정정, 대신}` | 코드에 박힌 한글 낱말집합 6개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10280 | `_GENERIC_COUNT_UNITS` | 5 | `{개, 가지, 종, 종류, 품목}` | 코드에 박힌 한글 낱말집합 5개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10497 | `_SHARED_WINDOW_PURCHASE_TERMS` | 5 | `{구매, 구입, 주문, 결제, 샀}` | 코드에 박힌 한글 낱말집합 5개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10842 | `_CART_MULTIPLE_WORDS` | 5 | `{여러, 복수, 중복, 2개이상, 두개이상}` | 코드에 박힌 한글 낱말집합 5개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11152 | `_CART_RETENTION_STRONG_MIN_WORDS` | 5 | `{이상, 넘게, 넘은, 지난, 지났}` | 코드에 박힌 한글 낱말집합 5개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11156 | `_CART_RECENT_EVENT_MARKERS` | 5 | `{생성, 담, 등록, 추가, 만들}` | 코드에 박힌 한글 낱말집합 5개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10809 | `_PURCHASE_COUNT_VERB_SIGNS` | 4 | `{구매, 구입, 주문, 샀}` | 코드에 박힌 한글 낱말집합 4개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10835 | `_CART_AMOUNT_PURCHASE_WORDS` | 4 | `{구매, 결제, 주문, 누적}` | 코드에 박힌 한글 낱말집합 4개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11129 | `_RECENCY_MARKERS` | 4 | `{최근, 요즘, 근래, 최근에}` | 코드에 박힌 한글 낱말집합 4개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11149 | `_CART_RETENTION_MAX_WORDS` | 4 | `{이내, 이하, 미만, 안에}` | 코드에 박힌 한글 낱말집합 4개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11154 | `_CART_RECENT_WORDS` | 4 | `{최근, 새로, 방금, 갓}` | 코드에 박힌 한글 낱말집합 4개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | segment_semantics.py:473 | `_COUNT_NOUN_SUFFIXES` | 4 | `{횟수, 회수, 건수, 개수}` | 코드에 박힌 한글 낱말집합 4개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10275 | `_SCOPE_GROUPING_MODIFIERS` | 3 | `{같은, 동일, 동일한}` | 코드에 박힌 한글 낱말집합 3개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:10810 | `_PURCHASE_COUNT_CONTEXT_YIELDS` | 3 | `{장바구니, 카트, 반응}` | 코드에 박힌 한글 낱말집합 3개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:11322 | `_PURCHASE_DATE_SIGNALS` | 3 | `{구매, 구입, 주문}` | 코드에 박힌 한글 낱말집합 3개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:13239 | `_CHILDREN_TERMS` | 3 | `{자녀, 아이, 키즈}` | 코드에 박힌 한글 낱말집합 3개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:19795 | `_GRADE_DIMENSION_CANONICALS` | 3 | `{vip등급, 등급, 회원등급}` | 코드에 박힌 한글 낱말집합 3개 — 렉시콘/레지스트리로 옮길 수 있다 |
| wordlist | wordlist | graph_rag.py:19796 | `_REGION_DIMENSION_CANONICALS` | 3 | `{지역, 시도, 시군구}` | 코드에 박힌 한글 낱말집합 3개 — 렉시콘/레지스트리로 옮길 수 있다 |
| domain | constant | graph_rag.py:4296 | `_ENUM_EXCLUSION_TAIL_RE` | 31 | `^(?:(?:[·ㆍ‧・/,]\|와\|과\|및\|또는\|이나\|랑)[가-힣A-Za-z]{1,8}?)+(?:상태\|중\|인\|한\|된)*(?:회원\|고객\|사용자\|유저\|이용자\|대상)?(?:은\|는\|이\|가\|을\|를\|만)?(?:모두\|전부\|다)?(?:제외\|배제\|제거\|아닌\|아니\|않은\|않는\|않았)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8814 | `_METRIC_SCOPING_PERIOD_RE` | 27 | `최근\s*\d+\s*(?:일\|주\|주간\|개월\|달\|년\|년간\|개월간\|분기)\|지난\s*(?:달\|주\|해\|분기\|주간)\|지난달\|저번\s*달\|저번달\|전월\|당월\|이번\s*달\|이번달\|올해\|금년\|작년\|지난해\|재작년\|\d{4}\s*년(?!령)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | analytical_intent.py:66 | `_TARGETING_COMPARISON_RE` | 25 | `(?:\d[\d,]*(?:\.\d+)?\s*(?:원\|건\|회\|개\|명\|일\|주\|주일\|개월\|달\|년)?\s*(?:이상\|이하\|초과\|미만\|이내\|이전\|이후\|같\|넘)\|상위\|하위\|높은\|낮은\|많은\|적은)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:11818 | `_CART_ABSENCE_PATTERN` | 25 | `장바구니(?:생성\|생성한\|담긴\|담은\|상품\|물건\|제품\|아이템\|이력)?(?:이나\|나\|또는\|랑\|이랑)?(?:구매이력\|주문이력\|구매내역\|구매\|주문\|상품)?(?:이\|가\|을\|를\|은\|는\|도)?(?:없\|않)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:13548 | `_EXCLUSION_CUE_RE` | 25 | `(?:포함\s*하지\s*(?:않는\|않은\|않고\|말아\s*줘\|말아줘\|말아\|마)?\|제외(?:해\s*주고\|해주고\|해\s*줘\|해줘\|하고\|해\s*달라(?:고)?\|해달라(?:고)?\|할)?\|빼(?!\s*지\s*말)(?:\s*주고\|주고\|\s*줘\|줘\|\s*달라(?:고)?\|달라(?:고)?\|고)?\|말고\|아닌\|아니고)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:24524 | `_LOGIC_TAIL_RE` | 24 | `\s*(?:인\|한\|하는\|이신\|된)?\s*(?:회원\|고객\|사람\|유저\|이용자\|분\|대상\|명단)(?:\s*(?:을\|를\|들)?\s*(?:찾아\|보여\|추출\|조회\|알려\|뽑아\|골라\|선정\|선별\|검색\|리스트업?)\S*)?\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:5879 | `_CAMPAIGN_GENERIC_RESPONSE_RE` | 22 | `캠페인(?:에\|에서\|을\|를\|의)?(?:는\|은\|도)?(?:반응\|응답)(?:을\|를\|이\|가\|은\|는\|도)?(?P<negative>하지않\|안한\|안했\|않은\|없)?(?:했\|한\|자\|회원\|고객)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:6916 | `_AGE_EXCLUSION_TAIL` | 21 | `^(?:인\|한\|된)?\s*(?:회원\|고객\|사용자\|유저\|이용자\|분\|명)?\s*(?:은\|는\|을\|를\|이\|가)?\s*(?:모두\|전부\|다)?\s*(?:제외\|제거\|빼\|제하\|아닌\|아니)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12686 | `_CAMPAIGN_BUY_NEG_PATTERN` | 21 | `캠페인(?:에서\|에\|을\|를)?(?:는\|은\|도)?(?:보고\|통해\|후)?(?:구매(?:이력\|내역)?(?:를\|은\|는\|도\|이\|가)*(?:반응)?(?:이\|가\|은\|는)?(?:하지않\|안하\|안한\|없)\|미구매)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12820 | `_ADDITIONAL_PURCHASE_ABSENCE_PATTERN` | 20 | `(?:추가로\|추가\|더이상\|더)(?:의)?(?:구매\|구입\|주문)(?:를\|은\|는\|가\|도\|한)?(?:없\|안했\|안한\|않았\|않은\|않는\|하지않\|안함\|못했\|못한)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12852 | `_ZERO_PURCHASE_COUNT_PATTERN` | 20 | `(?:구매\|구입\|주문)\s*(?:횟수\|건수\|건\|회\|번)?(?:가\|이\|은\|는\|도)?\s*(?:(?<![\d,.])0\s*(?:회\|건\|번)(?!\s*(?:이상\|이하\|초과\|미만\|넘\|보다))\|(?<![\d,.])0(?![\d,.회건번])\|없)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:17351 | `_NEGATION_CUE_RE` | 19 | `없\|않\|못[한했하받]\|아닌\|아니\|제외\|미사용\|미구매\|미접속\|미반응\|미결제\|미가입\|미방문\|비동의\|취소\|해지\|중단\|안\s*[한함했하샀]\|\bNOT\b` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | aggregation_requirements.py:650 | `_RELATIVE_FILTER_NOTE_RE` | 18 | `(?=.*(?:filters?(?:\[\d+\])?\.value\|relative\|상대\|from\s*/\s*to\|system\s+date\|past\s+\d+\s+days\|requires\s+concrete\|today\|current\s+date\|현재\s*날짜\|실행\s*시점))(?=.*(?:date\|from\|to\|today\|날짜\|기간\|변환\|실행))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:5849 | `_COMPLETED_BEHAVIOR_RE` | 18 | `(?:구매\|구입\|주문)(?:했\|한\|했던)\|장바구니.{0,10}(?:담\|보관\|있)\|캠페인.{0,12}(?:반응\|응답)(?:했\|한\|없는\|않)\|(?:로그인\|방문)(?:했\|한\|하지않\|없는)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10230 | `_AGG_CLAUSE_SPLIT_RE` | 18 | `이지만\|하지만\|지만\|반면에\|반면\|그리고\|이면서\|면서\|동시에\|이고\|이며\|했고\|았고\|었고\|하고\|또는\|(?<!\d),\|,(?!\d)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:5846 | `_COUNT_OUTPUT_SIGNAL_RE` | 17 | `(?:몇\s*(?:명\|건\|개\|곳)\|(?:회원\|고객\|사용자\|가입자\|구매자\|상품\|제품\|주문\|구매\|반응)\s*(?:수\|인원\|개수\|건수))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:5853 | `_OUTREACH_ACTION_RE` | 17 | `추천(?:해\|하\|안)\|캠페인(?:을)?\s*(?:생성\|만들\|기획)\|발송(?:해\|하\|할)\|보내(?:줘\|고\|기)\|알리(?:고\|기)\|홍보\|유도\|판매하고\s*싶` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:7174 | `_PURCHASE_SCOPE_ACTION_RE` | 17 | `^(?:잘)?(?:구매\|구입\|주문\|결제\|판매\|팔리\|팔린\|팔리는\|팔렸\|판매된\|판매되는\|판매량\|매출)(?:한\|한것\|된\|되는\|에서)?$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | unresolved_triage.py:155 | `_PARAMETER_HINT_RE` | 17 | `\d[\d,]*\s*(?:회\|번\|개\|건\|명\|원\|일\|주\|개월\|달\|년\|%)\|이상\|이하\|초과\|미만\|이내` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7087 | `<inline re.sub>` | 16 | `(?:으로부터\|로부터\|에서\|에게\|부터\|으로\|이나\|나\|이\|가\|은\|는\|을\|를\|의\|로)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:11807 | `_CART_PRESENCE_PATTERN` | 16 | `장바구니(?:에\|에는\|를\|을\|가\|이)?(?:상품\|물건\|제품\|아이템)?(?:이\|가\|을\|를)?(?:들어)?(?:있(?!지)\|담(?!지)\|보유(?!하지)\|보관(?!하지)\|가지(?!지))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12702 | `_CAMPAIGN_BUY_ZERO_COUNT_PATTERN` | 16 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)(?:건수\|횟수)(?:가\|이\|은\|는)?(?:없\|(?<![\d,.])0\s*건)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:13562 | `_NEGATED_EXCLUSION_RE` | 16 | `(?:빼\s*지\s*말(?:아\s*줘\|아줘\|아\|라)?\|제외\s*할\s*필요(?:는\|가)?\s*없(?:어\|다\|어요)?\|빼\s*달라(?:고\|는\|라는)?(?:\s*뜻\|\s*말)?(?:은\|는)?\s*아니(?:야\|다\|에요\|고)?)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1590 | `_PURCHASE_OBJECT_PATTERN` | 15 | `(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*(?:(?:을\|를)\s*\|\s+)(?:구매\|구입)\s*(?:한\|했\|했던\|하신\|하였\|이력\|내역\|경험\|고객\|회원\|유저\|구매자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9579 | `_MESSAGE_RECEIVED_COUNT_RE` | 15 | `(?:메시지\|문자\|알림\|톡\|dm)(?:를\|을\|이\|은\|는)?\s*\d+\s*(?:회\|번\|건)\s*(?:이상\|이하\|초과\|미만)?\s*(?:받\|수신)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12695 | `_CAMPAIGN_BUY_ZERO_AMOUNT_PATTERN` | 15 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)한?금액(?:이\|은\|는\|가)?(?:(?<!\d)0원\|없)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | semantic_requirements.py:95 | `_JOSA_TAIL_RE` | 15 | `(을\|를\|이\|가\|은\|는\|인\|의\|와\|과\|도\|만\|에게\|에서\|에)$` | 자동 판정 불가 — 사람이 본다 |
| domain | constant | analytical_intent.py:87 | `_MEMBER_NUMERIC_PREDICATE_RE` | 14 | `\d[\d,]*(?:\.\d+)?\s*(?:원\|건\|회\|번\|개\|명)?[^.!?]{0,30}(?:이상\|이하\|초과\|미만\|사이\|범위\|인\s*(?:회원\|고객\|사용자))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9590 | `_OR_OPERAND_THRESHOLD_RE` | 14 | `\d[\d,]*\s*(?:회\|원\|개\|건\|명\|번\|종\|일\|장\|점\|%)\s*(?:이상\|이하\|초과\|미만)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:11006 | `_CART_AGGREGATE_DOMAIN_BREAK_RE` | 14 | `구매(?:금액\|액\|횟수\|건수\|수량\|상품\|제품\|품목\|이력)\|구입\|주문\|결제\|캠페인반응\|쿠폰` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:11994 | `_RECENT_LOGIN_SIGNAL_RE` | 14 | `(?:로그인\|접속)(?:은\|는\|을\|를\|이\|도)?(?:한\|했\|하신\|하였\|함\|이력\|기록)\|loggedin` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12713 | `_GENERIC_BUY_NEG_PATTERN` | 14 | `구매(?:이력\|내역)?(?:를\|은\|는\|도\|이\|가)*(?:반응)?(?:이\|가\|은\|는)?(?:하지않\|안하\|안한\|없)\|미구매` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:13276 | `_CONSENT_EXCLUSION_TAIL_RE` | 14 | `^[^.。!?\n,]{0,40}?(?:회원\|고객\|사용자\|유저\|이용자\|대상)?(?:은\|는\|을\|를\|만)?(?:모두\|전부\|다)?(?:제외\|배제\|제거)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | semantic_requirements.py:98 | `_QUANTITY_VALUE_RE` | 14 | `^\d[\d,]*\s*(?:개\|종\|종류\|가지\|품목\|건\|회\|번\|명\|점\|장\|원\|%\|퍼센트)?$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | aggregation_requirements.py:641 | `_OPTIONAL_FILTER_ABSENCE_NOTE_RE` | 13 | `(?=.*(?:\bfilter\b\|\blimit\b\|\brestrict\w*\b\|필터\|제한\|범위))(?=.*(?:\bnot\s+(?:specified\|provided\|requested)\b\|\bunspecified\b\|명시되지\|지정되지\|제공되지\|요청되지))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1672 | `_QUANTITY_COUNT_TOKEN` | 13 | `^\d+(?:개\|회\|번\|건\|원\|명\|장\|종\|가지\|종류\|품목\|매\|권)?$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10093 | `_EXACT_AMOUNT_PATTERN` | 13 | `(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<mag>억\|천만\|백만\|만\|천)?\s*(?:원\|건\|회\|명\|개\|장\|번\|건수\|회수)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12923 | `_PURCHASE_EXISTS_ASSERT_RE` | 13 | `(?:구매\|구입\|주문)(?:이력\|내역)?(?:은\|는\|이\|가\|를\|도)?(?:있\|했지만\|했으나\|했는데\|하였)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:224 | `<inline re.sub>` | 13 | `^\s*(?:,\|그중\|중에서\|그리고\|또\|또한\|하되\|하고\|있는\|있고\|대상으로\|고객만\|사용자만)\s*` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1686 | `_PURCHASE_OBJECT_CHAIN_PATTERN` | 12 | `(?P<chain>[0-9A-Za-z가-힣_+\-]{1,40}(?:(?:(?<=[가-힣])(?:와\|과\|랑\|이랑)\s+\|\s*(?:및\|그리고)\s+\|\s*[,、]\s*)[0-9A-Za-z가-힣_+\-]{1,40}){1,4})\s*(?:을\|를)(?![0-9A-Za-z가-힣])(?=[^을를]{0,15}?(?:구매\|구입\|주문\|샀\|산(?=\s)))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:6570 | `_DATE_WINDOW_UNRESOLVED_RE` | 12 | `(?:\b(?:date\|from\|to\|today\|window\|yyyy(?:mmdd)?)\b\|order_date\|purchase_date\|날짜\|기간\|현재일\|실행\s*시점)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12486 | `_BALANCE_SUM_RE` | 12 | `합계\|합산\|합쳐\|합친\|합한\|더한\|더하면\|더해\|의\s*합\b\|합이\b\|합은\b\|합으로` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:12732 | `<inline re.compile>` | 12 | `(?:오퍼\|혜택\|제안)(?:에\|에는\|을\|를\|이\|가\|은\|는\|도)?(?:반응\|응답)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:12734 | `<inline re.compile>` | 12 | `(?:발송\|전송\|접촉\|도달)(?:은\|는\|이\|가\|에\|에는\|도\|을\|를)?성공` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:13110 | `_CAMPAIGN_BUY_COUNT_METRIC_PATTERN` | 12 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)(?:건수\|횟수)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | aggregation_requirements.py:637 | `_SCHEMA_EXISTENCE_NOTE_RE` | 11 | `(?:exist(?:s\|ence)?\|present\|available\|schema\|column\|field\|존재\|스키마\|컬럼\|필드)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | analytical_intent.py:82 | `_MEMBER_VALUE_PREDICATE_RE` | 11 | `\d[\d,]*(?:\.\d+)?\s*(?:원\|건\|회\|번\|개\|명)?\s*(?:인\|인\s*회원\|인\s*고객\|이거나\|거나\|없거나)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8886 | `_MEMBER_COUNT_SIGNAL_RE` | 11 | `(?:회원\|고객\|가입자)\s*수\|(?:회원\|고객\|가입자)\s*(?:이\|가\|은\|는)\s*(?:가장\s*\|제일\s*)?(?:많\|적)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:13039 | `_CAMPAIGN_BUY_AMOUNT_METRIC_PATTERN` | 11 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)한?금액` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:17908 | `_POST_PROCESSING_REQUEST_CUE_RE` | 11 | `(?:캠페인\|타겟리스트\|타겟\s*리스트\|타깃리스트\|세그먼트\|셀)[^.\n]{0,24}?(?:생성\|만들\|설정\|등록\|저장\|발행)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:84 | `<inline re.search>` | 11 | `(?:제외(?:한\|하고\|하여\|해서\|해(?:줘\|주세요)?\|하세요\|한다)\|빼고)\s*(?:고객\|회원\|사용자)?\s*[.!?。]*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:153 | `<inline re.finditer>` | 11 | `(?:빼고\|제외(?:하고\|한\|하여\|해서\|해(?:줘\|주세요)?\|하세요\|한다)?)(?=\s\|[,\.!?。]\|$)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:173 | `<inline re.finditer>` | 11 | `(?:포함(?:하고\|해서\|하되\|하며\|하여\|한\|해\s*줘\|해줘)?\|남기고)(?=\s\|[,\.!?。]\|$)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:268 | `<inline re.sub>` | 11 | `(?:의\|을\|를\|은\|는\|이\|가\|만\|으로\|로\|인)\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1625 | `_BRAND_COPULA_PATTERN` | 10 | `브랜드(?:가\|는\|명이\|명은)\s*(?P<object>[0-9A-Za-z가-힣_+\-]{1,40}?)(?:이면서\|이거나\|인데\|이고\|이며\|면서\|인)(?![0-9A-Za-z가-힣])` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9389 | `_DATA_MISSING_PATTERN` | 10 | `정보\S*\s*없\|값\S*\s*없\|입력\s*(?:되지\|하지)?\s*(?:않\|안\|못)\|미입력\|기재\S*\s*않\|미기재\|누락` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:5845 | `_MEMBER_OUTPUT_RE` | 9 | `회원\|고객\|사용자\|가입자\|대상\|몇\s*명\|인원\s*수\|회원\s*수\|고객\s*수` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:7150 | `_AMBIGUOUS_PURCHASE_SCOPE_PATTERN` | 9 | `(?P<object>[0-9A-Za-z가-힣_+\-]+(?:\s+[0-9A-Za-z가-힣_+\-]+){0,4})\s*(?:을\|를)\s*(?:(?:가장\|제일)\s*)?(?:(?:많이\|자주\|최다)\s*)?(?:구매\|구입\|주문\|샀\|산(?!책))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9415 | `_BALANCE_HIGH_TERMS` | 9 | `가장\s*많\|제일\s*많\|가장\s*높\|제일\s*높\|많은\|높은\|큰\|상위\|최상위` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10277 | `_DISTINCT_INTENT_RE` | 9 | `서로\s*다른\|각기\s*다른\|각각\s*다른\|여러\s*가지\|여러\|다양한\|가짓수\|종류별\|서로다른` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:12733 | `<inline re.compile>` | 9 | `쿠폰(?:을\|를\|이\|은\|는\|도)?(?:사용\|이용\|쓰\|쓴)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:13430 | `_PURCHASE_OBJECT_PARTICLE_RE` | 9 | `(?:으로부터\|로부터\|에서\|에게\|부터\|으로\|에\|의\|로)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | aggregation_requirements.py:646 | `_MISSING_OUTPUT_NOTE_RE` | 8 | `(?:outputcolumns?\|output_columns?).*(?:missing\|unspecified\|not\s+(?:specified\|provided)\|미지정\|누락\|없음)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8940 | `_PURCHASE_QUANTITY_RANK_PATTERN` | 8 | `(?P<sup>가장\s*\|제일\s*)?(?:많이\|자주\|최다)\s*(?:구매\|구입\|주문\|샀\|산(?!책))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9416 | `_BALANCE_LOW_TERMS` | 8 | `가장\s*적\|제일\s*적\|가장\s*낮\|제일\s*낮\|적은\|낮은\|작은\|하위` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10257 | `_AGG_SCOPE_PER_ORDER_RE` | 8 | `한\s*주문\|한\s*번에\|한번에\|주문당\|주문\s*당\|주문별\|주문\s*별\|1회\s*주문` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12002 | `_CUMULATIVE_DAYS_THRESHOLD_RE` | 8 | `\d+일(?:을\|를\|이\|가)?(?:이상\|이하\|초과\|미만\|미달)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12402 | `_ACTION_METRIC_DATE_GATE` | 8 | `\d+\s*(?:일\|주\|주일\|개월\|달\|년\|시간\|분)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12710 | `_BUY_RSPN_NEG_PATTERN` | 8 | `구매반응(?:이\|가\|은\|는\|도)?(?:없\|하지않\|안하\|안한)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:13557 | `_INCLUSION_CUE_RE` | 8 | `(?:포함(?!\s*하지)(?:해\s*주고\|해주고\|해\s*줘\|해줘\|하고\|하라고\|해\s*달라(?:고)?\|해달라(?:고)?)?)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:17905 | `_PRESENTATION_REQUEST_CUE_RE` | 8 | `산출\|표시\|출력\|보여\|노출\|함께\s*보\|요약\|정렬해\s*보` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:19843 | `<inline re.sub>` | 8 | `(?:특별자치시\|특별자치도\|특별시\|광역시\|자치도\|시\|도\|지역)\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | analytical_intent.py:75 | `_RECENT_WINDOW_RE` | 7 | `최근\s*\d+\s*(?:일\|주\|개월\|달\|년)(?:간\|동안\|이내)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1678 | `_PRODUCT_CONJUNCTION_RE` | 7 | `(?:(?<=[가-힣])(?:와\|과\|랑\|이랑)\s+\|\s*(?:및\|그리고)\s+\|\s*[,、]\s*)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:5883 | `_WHOLE_MEMBER_RE` | 7 | `(?:전체\|모든\|전부\|모두의?)\s*(?:회원\|고객\|사용자\|가입자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9532 | `_LATEST_PURCHASE_REF_RE` | 7 | `최근\s*구매\|마지막\s*구매\|최종\s*구매\|최근\s*주문\|마지막\s*주문\|최종\s*주문\|최근\s*결제` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:11169 | `_CART_PURCHASE_ABSENCE_RE` | 7 | `구매하지\|구입하지\|주문하지\|주문이?없\|사지않\|안\s*샀\|미구매` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:13378 | `<inline re.match>` | 7 | `^\d{1,4}(?:년\|년도\|월\|일\|분기\|주\|주차)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | query_semantics.py:43 | `_TOKEN_RE` | 7 | `\d+(?:\.\d+)?(?:원\|건\|회\|번\|개\|명)?\|[0-9A-Za-z가-힣_+\-]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | calendar_window.py:564 | `NUMERIC_DURATION_PATTERN` | 6 | `(?P<num>\d+)\s*(?P<unit>주일\|개월\|일\|주\|달\|년)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | entity_set.py:252 | `<inline re.fullmatch>` | 6 | `(?:중(?:에서\|의)?\|가운데\|내에서\|에서\|중에)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:5884 | `_ACTIVE_MEMBER_RE` | 6 | `정상\s*(?:회원\|고객\|사용자)\|활성\s*상태\s*(?:회원\|고객\|사용자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:6905 | `<inline re.search>` | 6 | `(?P<age>\d{1,3})\s*세(?!\s*(?:이상\|이하\|미만\|초과\|부터\|까지))(?![~\-\d])` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7197 | `<inline re.sub>` | 6 | `(?:을\|를\|이\|가\|은\|는)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:7500 | `_MEMBER_NOUN_RE` | 6 | `(?:회원\|고객\|유저\|사용자\|멤버\|가입자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:7915 | `_REGION_CITY_SUFFIX` | 6 | `(?:특별자치시\|특별자치도\|특별시\|광역시\|시\|군)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8889 | `_MEMBER_COUNT_HIGH_RE` | 6 | `많은\|높은\|상위\|가장\s*많\|제일\s*많\|많은\s*순` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8890 | `_MEMBER_COUNT_LOW_RE` | 6 | `적은\|낮은\|하위\|가장\s*적\|제일\s*적\|적은\s*순` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9394 | `_BALANCE_ZERO_MARKER` | 6 | `(?<![\d,.])0\s*(?:원\|회\|건\|개\|번\|명)?(?![\d,.])` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9488 | `_AVERAGE_COMPARISON_MARKER` | 6 | `평균\s*(?:보다\|대비\|이상\|이하\|초과\|미만)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9531 | `_FIRST_PURCHASE_REF_RE` | 6 | `첫\s*구매\|첫구매\|최초\s*구매\|첫\s*주문\|최초\s*주문\|첫\s*결제` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10252 | `_CUMULATIVE_WINDOW_MARKER_RE` | 6 | `누적\|누계\|평생\|통산\|역대\|전체\s*기간` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12404 | `_ACTION_ZERO_PATTERN` | 6 | `한\s*번도\|전혀\|이력이?\s*없\|기록이?\s*없\|한\s*적이?\s*없\|없` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | member_policy.py:22 | `_ALL_MEMBER_RE` | 6 | `(?:전체\|모든\|전부)\s*(?:회원\|고객\|사용자\|가입자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | query_semantics.py:44 | `_QUANTITY_RE` | 6 | `^\d+(?:\.\d+)?(?:원\|건\|회\|번\|개\|명)?$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:199 | `<inline re.split>` | 6 | `(?:을\|를)?\s*대상으로\s*(?:하되\|하고\|해서\|하여\|삼되)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | entity_set.py:34 | `_COUNT_AFTER_RE` | 5 | `^(\d{1,4})\s*(?:개\|종\|가지\|건\|위)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | entity_set.py:36 | `_COUNT_BEFORE_RE` | 5 | `(\d{1,4})\s*(?:개\|종\|가지\|건\|위)\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | entity_set.py:37 | `_COUNT_SEARCH_RE` | 5 | `(\d{1,4})\s*(?:개\|종\|가지\|건\|위)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:7497 | `_DIGIT_WITH_UNIT_RE` | 5 | `\d[\d,]*\s*(?:회\|건\|개\|장\|번)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:8139 | `<inline re.match>` | 5 | `(?:\s\|별\|단위\|마다\|지역)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8809 | `_UNSUPPORTED_GROUP_AXIS_RE` | 5 | `등급\s*별\|회원등급\s*별\|채널\s*별\|브랜드\s*별\|카테고리\s*별` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9382 | `_BALANCE_ABSENCE_PATTERN` | 5 | `없\|미보유\|보유하지\s*않\|보유\s*안\|보유하지\s*못` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10088 | `_RECENT_WINDOW_PATTERN` | 5 | `최근\s*(\d+)\s*(일\|주\|개월\|달\|년)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:215 | `<inline re.sub>` | 5 | `\s*(?:와\|과\|및\|하고\|그리고)\s*` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:228 | `<inline re.search>` | 5 | `(?P<left>.+?)(?:와\|과\|및\|하고)\s*(?P<right>.+?)(?:의)?\s*(?P<op>합집합\|교집합)\b` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | analytical_intent.py:77 | `_RANKING_HIGH_RE` | 4 | `가장\s*(?:많이\|많은)\|최다\|최고` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | analytical_intent.py:78 | `_RANKING_LOW_RE` | 4 | `가장\s*(?:적게\|적은)\|최소\|최저` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | calendar_window.py:191 | `<inline re.search>` | 4 | `(?:최근\|지난\|향후\|앞으로)\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:6867 | `<inline re.finditer>` | 4 | `(?P<decade>[1-9]\d)\s*대\s*(?P<op>이상\|이하\|초과\|미만)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7047 | `<inline re.split>` | 4 | `.*(?:에게\|한테\|께\|대상으로)\s*` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8257 | `_RANKING_PERCENT_PATTERN` | 4 | `(?P<dir>상위\|하위)?\s*(?P<pct>\d+(?:\.\d+)?)\s*(?:%\|퍼센트\|프로)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8803 | `_GROUP_PER_COUNT_RE` | 4 | `([\d,]+)\s*명\s*씩\|(?:상위\|하위)\s*([\d,]+)\s*명\|([\d,]+)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8806 | `_PER_GROUP_SUFFIX_RE` | 4 | `([\d,]+)\s*(?:명\|개\|곳)?\s*씩\|명씩` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9417 | `_BALANCE_PERCENT_PATTERN` | 4 | `(?P<dir>상위\|하위)?\s*(?P<pct>\d+(?:\.\d+)?)\s*(?:%\|퍼센트\|프로)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:9436 | `<inline re.search>` | 4 | `평균\s*이상\|평균\s*보다\s*(?:크\|많\|높)거나\s*같` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:9438 | `<inline re.search>` | 4 | `평균\s*이하\|평균\s*보다\s*(?:작\|적\|낮)거나\s*같` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10094 | `_EXACT_COUNT_PATTERN` | 4 | `(?P<num>\d+)\s*(?:개\|번\|회\|건)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10186 | `_SINO_KOREAN_AMOUNT_RE` | 4 | `(?P<num>[영공일이삼사오육칠팔구십백천]+)(?P<mag>억\|천만\|백만\|만)?원` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10265 | `_AGG_SCOPE_PER_BRAND_RE` | 4 | `동일한?\s*브랜드\|같은\s*브랜드\|브랜드별\|브랜드\s*별` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10917 | `_CART_TOTAL_QTY_SIGNAL` | 4 | `수량\|총\s*개수\|총\s*\d+\s*개\|총\s*\d` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12605 | `_RATIO_METRIC_PREFIX_RE` | 4 | `(?:하루\|1일\|매일\|일)\s*평균\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:12723 | `_CAMPAIGN_TAIL_NEG_RE` | 4 | `없\|않\|못[한했하받]\|안[한함했하]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:248 | `<inline re.search>` | 4 | `(?P<left>.+?)(?:와\|과\|및\|하고)\s*(?P<right>.+?)(?:의)?\s*차집합\b` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | analytical_intent.py:74 | `_RECENT_DAYS_RE` | 3 | `최근\s*(\d+)\s*일(?:간\|동안\|이내)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | calendar_window.py:192 | `<inline re.match>` | 3 | `\s*(?:동안\|간\|연속)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:6852 | `<inline re.search>` | 3 | `(?P<min>[1-9]\d)\s*(?:~\|-\|부터)\s*(?P<max>[1-9]\d)\s*대` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:6881 | `<inline re.search>` | 3 | `(?P<min>\d{1,3})\s*(?:세)?\s*(?:~\|-\|부터)\s*(?P<max>\d{1,3})\s*세?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7033 | `<inline re.search>` | 3 | `(?P<object>.+?)\s*(?:을\|를)\s*(?:팔\|판매)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8160 | `_REGION_DENSITY_TOP_N_PATTERN` | 3 | `상위\s*([\d,]+)\|(?:top\|톱)\s*([\d,]+)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8253 | `_RANKING_HIGH_DIRECTIVE` | 3 | `상위\s*[\d,]*\s*명?\|높은\s*순\|top\s*[\d,]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8255 | `_RANKING_DIRECTIVE_TOP_N` | 3 | `(?:상위\|하위\|top)\s*([\d,]+)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8801 | `_PER_GROUP_COUNT_RE` | 3 | `(?:상위\s*)?([\d,]+)\s*(?:명\|개\|곳)?\s*씩` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8891 | `_REGION_COUNT_TOP_N_RE` | 3 | `([\d,]+)\s*(?:개\|곳\|군데)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:19842 | `<inline re.sub>` | 3 | `\s*(?:에\s*)?(?:거주(?:하는)?\|사는\|살고\s*있는)\s*` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | segment_semantics.py:54 | `_AMOUNT_RE` | 3 | `(\d[\d,]*)\s*(억\|만\|천)?\s*원` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:166 | `<inline re.search>` | 3 | `(?P<left>.+?)(?:중에서\|에서\|중)\s*(?P<right>.+)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:240 | `<inline re.search>` | 3 | `(?P<left>.+?)에서\s*(?P<right>.+?)(?:을\|를)?\s*(?:제외\|빼고)\b` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | api.py:1535 | `<inline re.search>` | 2 | `\d[\d,]*\s*원\|%` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7265 | `<inline re.fullmatch>` | 2 | `(?:상\|하)반기` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8254 | `_RANKING_LOW_DIRECTIVE` | 2 | `하위\s*[\d,]*\s*명?\|낮은\s*순` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:8947 | `_PURCHASE_RANK_OBJECT_BRIDGE_RE` | 2 | `^\s*(?:을\|를)?\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9418 | `_BALANCE_TOPN_PATTERN` | 2 | `상위\s*(?P<a>[\d,]+)\|(?P<b>[\d,]+)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:13438 | `<inline re.sub>` | 2 | `(?:을\|를)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:24494 | `<inline re.split>` | 2 | `(?:을\|를)?\s*대상으로` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:24735 | `<inline re.split>` | 2 | `(?:을\|를)?\s*대상으로` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | member_policy.py:23 | `_INCLUDE_DORMANT_RE` | 2 | `휴면\s*(?:회원\s*)?(?:도\s*)?(?:포함\|포괄)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | member_policy.py:24 | `_INCLUDE_WITHDRAWN_RE` | 2 | `탈퇴\s*(?:회원\s*)?(?:도\s*)?(?:포함\|포괄)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:206 | `<inline re.finditer>` | 2 | `(?P<required>[^,]+?)(?:만)?\s*(?:포함\|남기고)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:216 | `<inline re.sub>` | 2 | `\s*(?:또는\|혹은)\s*` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | aggregation_requirements.py:1244 | `<inline re.sub>` | 1 | `[^0-9a-z가-힣]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | analytical_intent.py:91 | `_EXPLICIT_MEMBER_LIMIT_RE` | 1 | `\b(\d[\d,]*)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | api.py:1541 | `<inline re.sub>` | 1 | `\(?\s*무료\s*수신\s*거부\s*\)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | api.py:1542 | `<inline re.sub>` | 1 | `\(?\s*수신\s*거부\s*\)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | build_member_value_index.py:43 | `_CLEAN_NAME` | 1 | `^[\x20-\x7E가-힣ㄱ-ㅎㅏ-ㅣ·]+$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | calendar_window.py:43 | `_ANY_YEAR_RE` | 1 | `(\d{4})\s*년` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | calendar_window.py:44 | `_QUARTER_RE` | 1 | `([1-4])\s*(?:사)?분기` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | condition_reconciliation.py:63 | `_TOKEN_SPLIT` | 1 | `[^0-9A-Za-z가-힣]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1555 | `_CHANNEL_SUFFIX_PATTERN` | 1 | `\n?\s*발송\s*채널\s*:.*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1739 | `_BRAND_ADJACENT_BEFORE` | 1 | `(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*브랜드` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:1740 | `_BRAND_ADJACENT_AFTER` | 1 | `브랜드\s+(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:6569 | `_GROUP_AXIS_OBJECT_RE` | 1 | `^\s*[0-9A-Za-z가-힣_+\-]+\s*별\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7070 | `<inline re.findall>` | 1 | `[0-9A-Za-z가-힣_+\-]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7193 | `<inline re.findall>` | 1 | `[0-9A-Za-z가-힣_+\-]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:7255 | `<inline re.findall>` | 1 | `[0-9A-Za-z가-힣_+\-]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:7762 | `_HANGUL_SYLLABLE` | 1 | `[가-힣]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:8296 | `<inline re.search>` | 1 | `([\d,]+)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:8531 | `<inline re.search>` | 1 | `([\d,]+)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:8997 | `<inline re.search>` | 1 | `[\d,]+\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:9029 | `<inline re.search>` | 1 | `(\d+)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:9491 | `_ZERO_AMOUNT_MARKER` | 1 | `(?<!\d)0\s*원` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10267 | `_BRAND_SCOPE_RE` | 1 | `(?P<val>[가-힣A-Za-z0-9]+)\s*브랜드` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | graph_rag.py:10268 | `_CATEGORY_SCOPE_RE` | 1 | `(?P<val>[가-힣A-Za-z0-9]+)\s*카테고리` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:13437 | `<inline re.findall>` | 1 | `[0-9A-Za-z가-힣_+\-]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:13510 | `<inline re.sub>` | 1 | `[^0-9a-z가-힣]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | graph_rag.py:14355 | `<inline re.findall>` | 1 | `[0-9A-Za-z가-힣_]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | ingest.py:251 | `<inline re.search>` | 1 | `[가-힣]$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | product_master_resolver.py:47 | `_TERM_RE` | 1 | `[0-9A-Za-z가-힣_+\-]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | product_master_resolver.py:53 | `<inline re.sub>` | 1 | `[^0-9a-z가-힣]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | rag_index.py:26 | `SPECIAL_CHARACTER_PATTERN` | 1 | `[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ\s.!?%+\-_,:/]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | semantic_requirements.py:119 | `<inline re.sub>` | 1 | `[^0-9a-z가-힣]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | semantic_resolution.py:49 | `<inline re.sub>` | 1 | `[^0-9a-z가-힣]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | inline | set_expression_engine.py:409 | `<inline re.finditer>` | 1 | `(?P<decade>[1-9]\d)\s*대` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | unresolved_triage.py:153 | `_TOKEN_RE` | 1 | `[가-힣]{2,}` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | constant | calendar_window.py:72 | `_CAL_TOKEN_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | calendar_window.py:158 | `_YEAR_ANCHOR_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | calendar_window.py:160 | `_ADJACENT_YEAR_ANCHOR_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | calendar_window.py:575 | `WORD_DURATION_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | calendar_window.py:752 | `RELATIVE_PAST_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:1641 | `_CATEGORY_COPULA_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:1648 | `_CATEGORY_ADJACENT_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:2180 | `pattern` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:8156 | `_REGION_DENSITY_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:8159 | `_REGION_DENSITY_ALT_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:9263 | `range_p` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:9264 | `op_p` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:9265 | `eq_p` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:9738 | `_METRIC_THRESHOLD_TAIL_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:10166 | `_KOREAN_COUNT_NUMERAL_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:10807 | `_PURCHASE_COUNT_THRESHOLD_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | collection | graph_rag.py:11660 | `_RESULT_LIMIT_PATTERNS[0]` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | collection | graph_rag.py:11660 | `_RESULT_LIMIT_PATTERNS[1]` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | collection | graph_rag.py:11660 | `_RESULT_LIMIT_PATTERNS[2]` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | collection | graph_rag.py:11660 | `_RESULT_LIMIT_PATTERNS[3]` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:12095 | `chain` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:12171 | `_SIGNUP_ONLINE_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:12172 | `_SIGNUP_OFFLINE_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:13047 | `_CAMPAIGN_BUY_VERB_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | graph_rag.py:13397 | `_SCHEMA_QUERY_VALUE_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | segment_semantics.py:439 | `pattern` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | semantic_requirements.py:91 | `_ENTITY_QUALIFIER_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | set_expression_engine.py:288 | `_QUERY_TAIL_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | set_expression_engine.py:291 | `_NOISE_OPERAND_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | sql_guard.py:54 | `_TABLE_ALIAS_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | sql_guard.py:299 | `_AGG_CALL_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | constant | sql_guard.py:303 | `_INVALID_AGG_ARG_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| composed | constant | aggregate_spans.py:115 | `pattern` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | aggregate_spans.py:182 | `pattern` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | aggregate_spans.py:220 | `pattern` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | calendar_window.py:100 | `_ENUM_LINK_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | calendar_window.py:105 | `_RANGE_SEP_LINK_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | calendar_window.py:106 | `_RANGE_OPEN_LINK_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | calendar_window.py:109 | `_RANGE_CLOSER_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | collection | condition_evaluation_ir.py:54 | `_SAME_PRODUCT_PATTERNS[0]` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | collection | condition_evaluation_ir.py:54 | `_SAME_PRODUCT_PATTERNS[1]` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | condition_evaluation_ir.py:62 | `_MEMBER_COUNT_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:97 | `_OR_BOUNDARY_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:101 | `_AND_BOUNDARY_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:106 | `_NEGATION_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:107 | `_POLARITY_END_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:109 | `_COUNT_THRESHOLD_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:110 | `_AMOUNT_THRESHOLD_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:111 | `_SUM_CONTEXT_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:114 | `_OTHER_DATE_ANCHOR_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | event_parser.py:119 | `_TEMPORAL_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | graph_rag.py:5874 | `_PURCHASE_POSITIVE_MEMBERSHIP_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | graph_rag.py:8945 | `_PURCHASE_RANK_PRODUCT_PATTERN` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | graph_rag.py:8948 | `_PURCHASE_RANK_TIME_BRIDGE_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | graph_rag.py:9073 | `_PURCHASE_NEG_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | graph_rag.py:10262 | `_AGG_SCOPE_PER_PRODUCT_RE` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| composed | constant | graph_rag.py:10840 | `_CART_SAME_PRODUCT_PATTERN` |  | `(동적 조립)` | 사전 어휘를 끼워 넣어 조립한 구조 — 이행 완료 형태(낱말은 데이터에 있다) |
| grammar | constant | sql_guard.py:429 | `_PERF_FUNC_ON_COLUMN_RE` | 13 | `\b(LEN\|SUBSTRING\|LEFT\|RIGHT\|UPPER\|LOWER\|YEAR\|MONTH\|DAY\|DATEPART\|ISNULL\|LTRIM\|RTRIM)\s*\(\s*(\w+\.\w+)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | graph_rag.py:17846 | `_TARGET_MEMBER_PROJECTION_RE` | 7 | `(?i)(?:\b[A-Za-z_][\w$]*\s*\.\s*)?(?:\[\s*MEMBER_NO\s*\]\|'MEMBER_NO'\|\"MEMBER_NO\"\|MEMBER_NO)\s+AS\s+(?:\[\s*CUST_ID\s*\]\|'CUST_ID'\|\"CUST_ID\"\|CUST_ID)(?![\w$])` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | sql_ast.py:24 | `_ALIAS_PATTERN` | 6 | `\b(?:FROM\|JOIN)\s+([A-Za-z_][A-Za-z0-9_\.]*)\s+(?!ON\b\|WHERE\b\|GROUP\b\|ORDER\b\|HAVING\b)([A-Za-z_][A-Za-z0-9_]*)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | sql_guard.py:423 | `_PERF_CAST_JOIN_RE` | 6 | `(?:TRY_CAST\|CAST\|CONVERT)\s*\(\s*(\w+\.\w+)\b[^)]*\)\s*=\|=\s*(?:TRY_CAST\|CAST\|CONVERT)\s*\(\s*(\w+\.\w+)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | aggregation_requirements.py:655 | `_RELATIVE_WINDOW_PLACEHOLDER_RE` | 5 | `\bwindow_(?:start\|end)_\d+(?:d\|w\|m\|y)\b` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | rag_index.py:23 | `HTML_SCRIPT_STYLE_PATTERN` | 2 | `<\s*(script\|style)\b[^>]*>.*?<\s*/\s*\1\s*>` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | aggregation_requirements.py:635 | `_PHYSICAL_FIELD_IN_NOTE_RE` | 1 | `\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | aggregation_requirements.py:636 | `_RELATIVE_PERIOD_RE` | 1 | `^P(?=\d)(?:\d+[YMWD])+$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | analytical_intent.py:120 | `_COMPACT_DROP_RE` | 1 | `[\s.,!?·_\-/]` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | build_member_value_index.py:41 | `_CODE_VALUE` | 1 | `^[A-Z0-9_]+\.[^.].*$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | build_rag_knowledge.py:329 | `pattern` | 1 | `--\s*(?P<number>\d+)\.\s*(?P<title>.+?)\n(?P<sql>.*?;)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | common_utils.py:25 | `_DEFAULT_COMPACT_DROP_RE` | 1 | `\s` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | condition_evaluation_ir.py:40 | `_DATE_RE` | 1 | `^\d{8}$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | db_swap_preflight.py:41 | `_COLUMN_RE` | 1 | `^(?:([A-Za-z]\w*)\.)?([A-Z][A-Z0-9_]{1,})$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | entity_set.py:179 | `_COMPACT_DROP_RE` | 1 | `[\s.,!?·_\-/'\"()]` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | event_ir.py:108 | `_YMD8` | 1 | `^\d{8}$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | graph_rag.py:7763 | `_ASCII_ALNUM` | 1 | `[0-9A-Za-z]` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | graph_rag.py:7879 | `_PLAIN_NUMERIC_VALUE` | 1 | `^[\d.\-/:%\s]+$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | graph_rag.py:13547 | `_POLARITY_CLAUSE_BOUNDARY_RE` | 1 | `[.!?;。！？；\n\r]` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | graph_rag.py:17742 | `prefix` | 1 | `\bnot\s+exists\s*\(` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | graph_rag.py:17914 | `_CONSTANT_PROJECTION_RE` | 1 | `N?'([^']*)'\s+AS\s+([A-Za-z_]\w*)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | logical_expression.py:231 | `_PLACEHOLDER_RE` | 1 | `@([A-Za-z0-9_]+)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | rag_index.py:22 | `HTML_TAG_PATTERN` | 1 | `<[^>]+>` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | rag_index.py:24 | `WHITESPACE_PATTERN` | 1 | `\s+` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | rag_index.py:25 | `SENTENCE_PATTERN` | 1 | `[^.!?。！？]+[.!?。！？]*` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | schema_extract.py:9 | `CREATE_TABLE_RE` | 1 | `CREATE\s+TABLE\s+(?P<table>[\w.]+)\s*\((?P<body>.*?)\);` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | schema_extract.py:13 | `CREATE_INDEX_RE` | 1 | `CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?P<name>\w+)\s+ON\s+(?P<table>[\w.]+)\s*\((?P<columns>[^)]+)\);` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | schema_extract.py:17 | `CREATE_VIEW_RE` | 1 | `CREATE\s+VIEW\s+(?P<view>[\w.]+)\s+AS\s+(?P<body>.*?);` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | schema_extract.py:21 | `COLUMN_RE` | 1 | `^(?P<name>\w+)\s+(?P<type>[A-Z][A-Z0-9_]*(?:\([^)]*\))?)(?P<constraints>.*)$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | schema_extract.py:22 | `REFERENCES_RE` | 1 | `REFERENCES\s+(?P<table>[\w.]+)\s*\((?P<column>\w+)\)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | schema_extract.py:23 | `FOREIGN_KEY_RE` | 1 | `FOREIGN\s+KEY\s*\((?P<columns>[^)]+)\)\s*REFERENCES\s+(?P<table>[\w.]+)\s*\((?P<ref_columns>[^)]+)\)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | segment_semantics.py:53 | `_NUMBER_RE` | 1 | `\d[\d,]*` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | sql_guard.py:59 | `_EQUI_JOIN_PATTERN` | 1 | `\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\s*=\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | sql_guard.py:300 | `_BARE_COLUMN_PATTERN` | 1 | `\b[A-Za-z_]\w*\.[A-Za-z_]\w*` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | sql_guard.py:307 | `_NONE_LITERAL_PATTERN` | 1 | `\bNone\b` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | constant | sql_guard.py:422 | `_PERF_LEADING_WILDCARD_RE` | 1 | `(\w+(?:\.\w+)?)\s+LIKE\s+N?'%` | 표면어 없이 구조만 — 코드에 남는다 |
