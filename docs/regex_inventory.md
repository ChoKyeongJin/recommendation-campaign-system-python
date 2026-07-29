# 정규식 인벤토리 (이행 작업 목록)

총 190개 — 어휘형 8 / 업무의미형 151 / 문법형 31

판정 기준: **이 패턴이 열거하는 것이 단어 목록인가, 구조인가?** 단어 목록이면 데이터로 옮긴다
(`lexicon_patterns` + `docs/data/parser_lexicon.json`). `decision` 은 사람이 채운다.

| 분류 | 파일:줄 | 이름 | 교대수 | 패턴 | 자동 판정 사유 |
|---|---|---|---:|---|---|
| lexical | build_dimension_catalog.py:42 | `_NOISE_LABEL` | 13 | `팝업\|콤보\|셀렉트\|체크\|check\|popup\|text\|combo\|multiselect\|버전업\|테스트\|test\|::op::` | 메타문자 없는 표면어 교대 13개 — 렉시콘으로 옮길 수 있다 |
| lexical | graph_rag.py:7272 | `_BALANCE_DEFER_PATTERN` | 13 | `가장\|제일\|상위\|하위\|최상위\|랭킹\|순위\|top\|톱\|퍼센트\|프로\|%\|평균` | 메타문자 없는 표면어 교대 13개 — 렉시콘으로 옮길 수 있다 |
| lexical | graph_rag.py:15145 | `_THRESHOLD_CUE_RE` | 12 | `이상\|이하\|미만\|초과\|이내\|같은\|동일\|>=\|<=\|>\|<\|=` | 메타문자 없는 표면어 교대 12개 — 렉시콘으로 옮길 수 있다 |
| lexical | analytical_intent.py:47 | `_OUTPUT_ACTION_RE` | 6 | `알려\|보여\|조회\|계산\|구해\|집계` | 메타문자 없는 표면어 교대 6개 — 렉시콘으로 옮길 수 있다 |
| lexical | analytical_intent.py:68 | `_MEMBER_METRIC_COMPARISON_RE` | 6 | `보다\|초과한\|미만인\|이상인\|이하인\|같은` | 메타문자 없는 표면어 교대 6개 — 렉시콘으로 옮길 수 있다 |
| lexical | graph_rag.py:7276 | `_BALANCE_PRESENCE_PATTERN` | 5 | `보유\|가지고\|가진\|있는\|있으신` | 메타문자 없는 표면어 교대 5개 — 렉시콘으로 옮길 수 있다 |
| lexical | graph_rag.py:9195 | `_TREND_ORDER_MARKER_RE` | 5 | `대비\|보다\|에서\|→\|->` | 메타문자 없는 표면어 교대 5개 — 렉시콘으로 옮길 수 있다 |
| lexical | graph_rag.py:7277 | `_BALANCE_METRIC_NOUN_PATTERN` | 3 | `보유액\|보유금액\|보유량` | 메타문자 없는 표면어 교대 3개 — 렉시콘으로 옮길 수 있다 |
| domain | graph_rag.py:3382 | `_ENUM_EXCLUSION_TAIL_RE` | 31 | `^(?:(?:[·ㆍ‧・/,]\|와\|과\|및\|또는\|이나\|랑)[가-힣A-Za-z]{1,8}?)+(?:상태\|중\|인\|한\|된)*(?:회원\|고객\|사용자\|유저\|이용자\|대상)?(?:은\|는\|이\|가\|을\|를\|만)?(?:모두\|전부\|다)?(?:제외\|배제\|제거\|아닌\|아니\|않은\|않는\|않았)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6853 | `_METRIC_SCOPING_PERIOD_RE` | 27 | `최근\s*\d+\s*(?:일\|주\|주간\|개월\|달\|년\|년간\|개월간\|분기)\|지난\s*(?:달\|주\|해\|분기\|주간)\|지난달\|저번\s*달\|저번달\|전월\|당월\|이번\s*달\|이번달\|올해\|금년\|작년\|지난해\|재작년\|\d{4}\s*년(?!령)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | analytical_intent.py:48 | `_TARGETING_COMPARISON_RE` | 25 | `(?:\d[\d,]*(?:\.\d+)?\s*(?:원\|건\|회\|개\|명\|일\|주\|주일\|개월\|달\|년)?\s*(?:이상\|이하\|초과\|미만\|이내\|이전\|이후\|같\|넘)\|상위\|하위\|높은\|낮은\|많은\|적은)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:9554 | `_CART_ABSENCE_PATTERN` | 25 | `장바구니(?:생성\|생성한\|담긴\|담은\|상품\|물건\|제품\|아이템\|이력)?(?:이나\|나\|또는\|랑\|이랑)?(?:구매이력\|주문이력\|구매내역\|구매\|주문\|상품)?(?:이\|가\|을\|를\|은\|는\|도)?(?:없\|않)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:20173 | `_LOGIC_TAIL_RE` | 24 | `\s*(?:인\|한\|하는\|이신\|된)?\s*(?:회원\|고객\|사람\|유저\|이용자\|분\|대상\|명단)(?:\s*(?:을\|를\|들)?\s*(?:찾아\|보여\|추출\|조회\|알려\|뽑아\|골라\|선정\|선별\|검색\|리스트업?)\S*)?\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4831 | `_PURCHASE_POSITIVE_MEMBERSHIP_RE` | 22 | `(?:구매\|구입\|주문)(?:이력\|내역)?(?:을\|를\|은\|는\|이\|가\|도)*(?:했\|한\|했던\|있는\|있었\|있던\|있음)\|(?:구매\|구입\|주문)(?=(?:고객\|회원\|사용자\|유저))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4836 | `_CAMPAIGN_GENERIC_RESPONSE_RE` | 22 | `캠페인(?:에\|에서\|을\|를\|의)?(?:는\|은\|도)?(?:반응\|응답)(?:을\|를\|이\|가\|은\|는\|도)?(?P<negative>하지않\|안한\|안했\|않은\|없)?(?:했\|한\|자\|회원\|고객)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:5519 | `_AGE_EXCLUSION_TAIL` | 21 | `^(?:인\|한\|된)?\s*(?:회원\|고객\|사용자\|유저\|이용자\|분\|명)?\s*(?:은\|는\|을\|를\|이\|가)?\s*(?:모두\|전부\|다)?\s*(?:제외\|제거\|빼\|제하\|아닌\|아니)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10422 | `_CAMPAIGN_BUY_NEG_PATTERN` | 21 | `캠페인(?:에서\|에\|을\|를)?(?:는\|은\|도)?(?:보고\|통해\|후)?(?:구매(?:이력\|내역)?(?:를\|은\|는\|도\|이\|가)*(?:반응)?(?:이\|가\|은\|는)?(?:하지않\|안하\|안한\|없)\|미구매)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10556 | `_ADDITIONAL_PURCHASE_ABSENCE_PATTERN` | 20 | `(?:추가로\|추가\|더이상\|더)(?:의)?(?:구매\|구입\|주문)(?:를\|은\|는\|가\|도\|한)?(?:없\|안했\|안한\|않았\|않은\|않는\|하지않\|안함\|못했\|못한)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10588 | `_ZERO_PURCHASE_COUNT_PATTERN` | 20 | `(?:구매\|구입\|주문)\s*(?:횟수\|건수\|건\|회\|번)?(?:가\|이\|은\|는\|도)?\s*(?:(?<![\d,.])0\s*(?:회\|건\|번)(?!\s*(?:이상\|이하\|초과\|미만\|넘\|보다))\|(?<![\d,.])0(?![\d,.회건번])\|없)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7033 | `_PURCHASE_NEG_RE` | 19 | `(?:구매\|구입\|주문)(?:이력\|내역)?(?:을\|를\|은\|는\|이\|가\|도)*(?:없\|않\|하지않\|안함\|안한\|안했\|안하)\|미(?:구매\|구입\|주문)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:14668 | `_NEGATION_CUE_RE` | 19 | `없\|않\|못[한했하받]\|아닌\|아니\|제외\|미사용\|미구매\|미접속\|미반응\|미결제\|미가입\|미방문\|비동의\|취소\|해지\|중단\|안\s*[한함했하샀]\|\bNOT\b` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | aggregation_requirements.py:650 | `_RELATIVE_FILTER_NOTE_RE` | 18 | `(?=.*(?:filters?(?:\[\d+\])?\.value\|relative\|상대\|from\s*/\s*to\|system\s+date\|past\s+\d+\s+days\|requires\s+concrete\|today\|current\s+date\|현재\s*날짜\|실행\s*시점))(?=.*(?:date\|from\|to\|today\|날짜\|기간\|변환\|실행))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4812 | `_COMPLETED_BEHAVIOR_RE` | 18 | `(?:구매\|구입\|주문)(?:했\|한\|했던)\|장바구니.{0,10}(?:담\|보관\|있)\|캠페인.{0,12}(?:반응\|응답)(?:했\|한\|없는\|않)\|(?:로그인\|방문)(?:했\|한\|하지않\|없는)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8121 | `_AGG_CLAUSE_SPLIT_RE` | 18 | `이지만\|하지만\|지만\|반면에\|반면\|그리고\|이면서\|면서\|동시에\|이고\|이며\|했고\|았고\|었고\|하고\|또는\|(?<!\d),\|,(?!\d)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8145 | `_AGG_UNIT_TOKEN_RE` | 18 | `\d[\d,]*\s*(?:억\|천만\|백만\|만\|천)?\s*(종류\|종수\|품목\|가지\|건수\|회수\|종\|개\|건\|회\|번\|원\|점\|장)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4809 | `_COUNT_OUTPUT_SIGNAL_RE` | 17 | `(?:몇\s*(?:명\|건\|개\|곳)\|(?:회원\|고객\|사용자\|가입자\|구매자\|상품\|제품\|주문\|구매\|반응)\s*(?:수\|인원\|개수\|건수))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4816 | `_OUTREACH_ACTION_RE` | 17 | `추천(?:해\|하\|안)\|캠페인(?:을)?\s*(?:생성\|만들\|기획)\|발송(?:해\|하\|할)\|보내(?:줘\|고\|기)\|알리(?:고\|기)\|홍보\|유도\|판매하고\s*싶` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | unresolved_triage.py:155 | `_PARAMETER_HINT_RE` | 17 | `\d[\d,]*\s*(?:회\|번\|개\|건\|명\|원\|일\|주\|개월\|달\|년\|%)\|이상\|이하\|초과\|미만\|이내` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:9543 | `_CART_PRESENCE_PATTERN` | 16 | `장바구니(?:에\|에는\|를\|을\|가\|이)?(?:상품\|물건\|제품\|아이템)?(?:이\|가\|을\|를)?(?:들어)?(?:있(?!지)\|담(?!지)\|보유(?!하지)\|보관(?!하지)\|가지(?!지))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10438 | `_CAMPAIGN_BUY_ZERO_COUNT_PATTERN` | 16 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)(?:건수\|횟수)(?:가\|이\|은\|는)?(?:없\|(?<![\d,.])0\s*건)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1332 | `_PURCHASE_OBJECT_PATTERN` | 15 | `(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*(?:(?:을\|를)\s*\|\s+)(?:구매\|구입)\s*(?:한\|했\|했던\|하신\|하였\|이력\|내역\|경험\|고객\|회원\|유저\|구매자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7472 | `_MESSAGE_RECEIVED_COUNT_RE` | 15 | `(?:메시지\|문자\|알림\|톡\|dm)(?:를\|을\|이\|은\|는)?\s*\d+\s*(?:회\|번\|건)\s*(?:이상\|이하\|초과\|미만)?\s*(?:받\|수신)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10431 | `_CAMPAIGN_BUY_ZERO_AMOUNT_PATTERN` | 15 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)한?금액(?:이\|은\|는\|가)?(?:(?<!\d)0원\|없)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | semantic_requirements.py:92 | `_JOSA_TAIL_RE` | 15 | `(을\|를\|이\|가\|은\|는\|인\|의\|와\|과\|도\|만\|에게\|에서\|에)$` | 자동 판정 불가 — 사람이 본다 |
| domain | analytical_intent.py:69 | `_MEMBER_NUMERIC_PREDICATE_RE` | 14 | `\d[\d,]*(?:\.\d+)?\s*(?:원\|건\|회\|번\|개\|명)?[^.!?]{0,30}(?:이상\|이하\|초과\|미만\|사이\|범위\|인\s*(?:회원\|고객\|사용자))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7483 | `_OR_OPERAND_THRESHOLD_RE` | 14 | `\d[\d,]*\s*(?:회\|원\|개\|건\|명\|번\|종\|일\|장\|점\|%)\s*(?:이상\|이하\|초과\|미만)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8742 | `_CART_AGGREGATE_DOMAIN_BREAK_RE` | 14 | `구매(?:금액\|액\|횟수\|건수\|수량\|상품\|제품\|품목\|이력)\|구입\|주문\|결제\|캠페인반응\|쿠폰` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:9730 | `_RECENT_LOGIN_SIGNAL_RE` | 14 | `(?:로그인\|접속)(?:은\|는\|을\|를\|이\|도)?(?:한\|했\|하신\|하였\|함\|이력\|기록)\|loggedin` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10449 | `_GENERIC_BUY_NEG_PATTERN` | 14 | `구매(?:이력\|내역)?(?:를\|은\|는\|도\|이\|가)*(?:반응)?(?:이\|가\|은\|는)?(?:하지않\|안하\|안한\|없)\|미구매` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:11012 | `_CONSENT_EXCLUSION_TAIL_RE` | 14 | `^[^.。!?\n,]{0,40}?(?:회원\|고객\|사용자\|유저\|이용자\|대상)?(?:은\|는\|을\|를\|만)?(?:모두\|전부\|다)?(?:제외\|배제\|제거)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | semantic_requirements.py:95 | `_QUANTITY_VALUE_RE` | 14 | `^\d[\d,]*\s*(?:개\|종\|종류\|가지\|품목\|건\|회\|번\|명\|점\|장\|원\|%\|퍼센트)?$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | aggregation_requirements.py:641 | `_OPTIONAL_FILTER_ABSENCE_NOTE_RE` | 13 | `(?=.*(?:\bfilter\b\|\blimit\b\|\brestrict\w*\b\|필터\|제한\|범위))(?=.*(?:\bnot\s+(?:specified\|provided\|requested)\b\|\bunspecified\b\|명시되지\|지정되지\|제공되지\|요청되지))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1414 | `_QUANTITY_COUNT_TOKEN` | 13 | `^\d+(?:개\|회\|번\|건\|원\|명\|장\|종\|가지\|종류\|품목\|매\|권)?$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7984 | `_EXACT_AMOUNT_PATTERN` | 13 | `(?P<num>[\d,]+(?:\.\d+)?)\s*(?P<mag>억\|천만\|백만\|만\|천)?\s*(?:원\|건\|회\|명\|개\|장\|번\|건수\|회수)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10659 | `_PURCHASE_EXISTS_ASSERT_RE` | 13 | `(?:구매\|구입\|주문)(?:이력\|내역)?(?:은\|는\|이\|가\|를\|도)?(?:있\|했지만\|했으나\|했는데\|하였)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1428 | `_PURCHASE_OBJECT_CHAIN_PATTERN` | 12 | `(?P<chain>[0-9A-Za-z가-힣_+\-]{1,40}(?:(?:(?<=[가-힣])(?:와\|과\|랑\|이랑)\s+\|\s*(?:및\|그리고)\s+\|\s*[,、]\s*)[0-9A-Za-z가-힣_+\-]{1,40}){1,4})\s*(?:을\|를)(?![0-9A-Za-z가-힣])(?=[^을를]{0,15}?(?:구매\|구입\|주문\|샀\|산(?=\s)))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:5177 | `_DATE_WINDOW_UNRESOLVED_RE` | 12 | `(?:\b(?:date\|from\|to\|today\|window\|yyyy(?:mmdd)?)\b\|order_date\|purchase_date\|날짜\|기간\|현재일\|실행\s*시점)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10222 | `_BALANCE_SUM_RE` | 12 | `합계\|합산\|합쳐\|합친\|합한\|더한\|더하면\|더해\|의\s*합\b\|합이\b\|합은\b\|합으로` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10846 | `_CAMPAIGN_BUY_COUNT_METRIC_PATTERN` | 12 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)(?:건수\|횟수)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | semantic_requirements.py:83 | `_ENTITY_QUALIFIER_RE` | 12 | `(브랜드명\|브랜드\|상품명\|카테고리명\|카테고리\|제품명\|품목명)(?:이\|가\|은\|는\|:\|=)\s*([가-힣A-Za-z0-9][가-힣A-Za-z0-9]+)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | aggregation_requirements.py:637 | `_SCHEMA_EXISTENCE_NOTE_RE` | 11 | `(?:exist(?:s\|ence)?\|present\|available\|schema\|column\|field\|존재\|스키마\|컬럼\|필드)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | analytical_intent.py:64 | `_MEMBER_VALUE_PREDICATE_RE` | 11 | `\d[\d,]*(?:\.\d+)?\s*(?:원\|건\|회\|번\|개\|명)?\s*(?:인\|인\s*회원\|인\s*고객\|이거나\|거나\|없거나)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6925 | `_MEMBER_COUNT_SIGNAL_RE` | 11 | `(?:회원\|고객\|가입자)\s*수\|(?:회원\|고객\|가입자)\s*(?:이\|가\|은\|는)\s*(?:가장\s*\|제일\s*)?(?:많\|적)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10775 | `_CAMPAIGN_BUY_AMOUNT_METRIC_PATTERN` | 11 | `캠페인(?:을\|를\|에서\|으로\|에\|의)?(?:통해\|통한\|보고\|반응\|후)?(?:한)?(?:구매\|결제)한?금액` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:15141 | `_POST_PROCESSING_REQUEST_CUE_RE` | 11 | `(?:캠페인\|타겟리스트\|타겟\s*리스트\|타깃리스트\|세그먼트\|셀)[^.\n]{0,24}?(?:생성\|만들\|설정\|등록\|저장\|발행)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1367 | `_BRAND_COPULA_PATTERN` | 10 | `브랜드(?:가\|는\|명이\|명은)\s*(?P<object>[0-9A-Za-z가-힣_+\-]{1,40}?)(?:이면서\|이거나\|인데\|이고\|이며\|면서\|인)(?![0-9A-Za-z가-힣])` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7282 | `_DATA_MISSING_PATTERN` | 10 | `정보\S*\s*없\|값\S*\s*없\|입력\s*(?:되지\|하지)?\s*(?:않\|안\|못)\|미입력\|기재\S*\s*않\|미기재\|누락` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4808 | `_MEMBER_OUTPUT_RE` | 9 | `회원\|고객\|사용자\|가입자\|대상\|몇\s*명\|인원\s*수\|회원\s*수\|고객\s*수` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7308 | `_BALANCE_HIGH_TERMS` | 9 | `가장\s*많\|제일\s*많\|가장\s*높\|제일\s*높\|많은\|높은\|큰\|상위\|최상위` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8159 | `_DISTINCT_INTENT_RE` | 9 | `서로\s*다른\|각기\s*다른\|각각\s*다른\|여러\s*가지\|여러\|다양한\|가짓수\|종류별\|서로다른` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:11163 | `_PURCHASE_OBJECT_PARTICLE_RE` | 9 | `(?:으로부터\|로부터\|에서\|에게\|부터\|으로\|에\|의\|로)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | aggregation_requirements.py:646 | `_MISSING_OUTPUT_NOTE_RE` | 8 | `(?:outputcolumns?\|output_columns?).*(?:missing\|unspecified\|not\s+(?:specified\|provided)\|미지정\|누락\|없음)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6979 | `_PURCHASE_QUANTITY_RANK_PATTERN` | 8 | `(?P<sup>가장\s*\|제일\s*)?(?:많이\|자주\|최다)\s*(?:구매\|구입\|주문\|샀\|산(?!책))` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7309 | `_BALANCE_LOW_TERMS` | 8 | `가장\s*적\|제일\s*적\|가장\s*낮\|제일\s*낮\|적은\|낮은\|작은\|하위` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8147 | `_AGG_SCOPE_PER_ORDER_RE` | 8 | `한\s*주문\|한\s*번에\|한번에\|주문당\|주문\s*당\|주문별\|주문\s*별\|1회\s*주문` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:9738 | `_CUMULATIVE_DAYS_THRESHOLD_RE` | 8 | `\d+일(?:을\|를\|이\|가)?(?:이상\|이하\|초과\|미만\|미달)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10138 | `_ACTION_METRIC_DATE_GATE` | 8 | `\d+\s*(?:일\|주\|주일\|개월\|달\|년\|시간\|분)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10446 | `_BUY_RSPN_NEG_PATTERN` | 8 | `구매반응(?:이\|가\|은\|는\|도)?(?:없\|하지않\|안하\|안한)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:15138 | `_PRESENTATION_REQUEST_CUE_RE` | 8 | `산출\|표시\|출력\|보여\|노출\|함께\s*보\|요약\|정렬해\s*보` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | analytical_intent.py:57 | `_RECENT_WINDOW_RE` | 7 | `최근\s*\d+\s*(?:일\|주\|개월\|달\|년)(?:간\|동안\|이내)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1420 | `_PRODUCT_CONJUNCTION_RE` | 7 | `(?:(?<=[가-힣])(?:와\|과\|랑\|이랑)\s+\|\s*(?:및\|그리고)\s+\|\s*[,、]\s*)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4840 | `_WHOLE_MEMBER_RE` | 7 | `(?:전체\|모든\|전부\|모두의?)\s*(?:회원\|고객\|사용자\|가입자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7425 | `_LATEST_PURCHASE_REF_RE` | 7 | `최근\s*구매\|마지막\s*구매\|최종\s*구매\|최근\s*주문\|마지막\s*주문\|최종\s*주문\|최근\s*결제` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8148 | `_AGG_SCOPE_PER_PRODUCT_RE` | 7 | `동일\s*상품\|같은\s*상품\|동일한\s*상품\|상품별\|상품\s*별\|동일\s*제품\|같은\s*제품` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8905 | `_CART_PURCHASE_ABSENCE_RE` | 7 | `구매하지\|구입하지\|주문하지\|주문이?없\|사지않\|안\s*샀\|미구매` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | query_semantics.py:43 | `_TOKEN_RE` | 7 | `\d+(?:\.\d+)?(?:원\|건\|회\|번\|개\|명)?\|[0-9A-Za-z가-힣_+\-]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | calendar_window.py:434 | `NUMERIC_DURATION_PATTERN` | 6 | `(?P<num>\d+)\s*(?P<unit>주일\|개월\|일\|주\|달\|년)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:4841 | `_ACTIVE_MEMBER_RE` | 6 | `정상\s*(?:회원\|고객\|사용자)\|활성\s*상태\s*(?:회원\|고객\|사용자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:5844 | `_MEMBER_NOUN_RE` | 6 | `(?:회원\|고객\|유저\|사용자\|멤버\|가입자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6229 | `_REGION_CITY_SUFFIX` | 6 | `(?:특별자치시\|특별자치도\|특별시\|광역시\|시\|군)$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6928 | `_MEMBER_COUNT_HIGH_RE` | 6 | `많은\|높은\|상위\|가장\s*많\|제일\s*많\|많은\s*순` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6929 | `_MEMBER_COUNT_LOW_RE` | 6 | `적은\|낮은\|하위\|가장\s*적\|제일\s*적\|적은\s*순` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7287 | `_BALANCE_ZERO_MARKER` | 6 | `(?<![\d,.])0\s*(?:원\|회\|건\|개\|번\|명)?(?![\d,.])` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7381 | `_AVERAGE_COMPARISON_MARKER` | 6 | `평균\s*(?:보다\|대비\|이상\|이하\|초과\|미만)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7424 | `_FIRST_PURCHASE_REF_RE` | 6 | `첫\s*구매\|첫구매\|최초\s*구매\|첫\s*주문\|최초\s*주문\|첫\s*결제` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8143 | `_CUMULATIVE_WINDOW_MARKER_RE` | 6 | `누적\|누계\|평생\|통산\|역대\|전체\s*기간` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8576 | `_CART_SAME_PRODUCT_PATTERN` | 6 | `(동일\|같은\|똑같은)(상품\|제품\|품목\|것)` | 자동 판정 불가 — 사람이 본다 |
| domain | graph_rag.py:10140 | `_ACTION_ZERO_PATTERN` | 6 | `한\s*번도\|전혀\|이력이?\s*없\|기록이?\s*없\|한\s*적이?\s*없\|없` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | member_policy.py:22 | `_ALL_MEMBER_RE` | 6 | `(?:전체\|모든\|전부)\s*(?:회원\|고객\|사용자\|가입자)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | query_semantics.py:44 | `_QUANTITY_RE` | 6 | `^\d+(?:\.\d+)?(?:원\|건\|회\|번\|개\|명)?$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | entity_set.py:33 | `_COUNT_AFTER_RE` | 5 | `^(\d{1,4})\s*(?:개\|종\|가지\|건\|위)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | entity_set.py:35 | `_COUNT_BEFORE_RE` | 5 | `(\d{1,4})\s*(?:개\|종\|가지\|건\|위)\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | entity_set.py:36 | `_COUNT_SEARCH_RE` | 5 | `(\d{1,4})\s*(?:개\|종\|가지\|건\|위)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:5841 | `_DIGIT_WITH_UNIT_RE` | 5 | `\d[\d,]*\s*(?:회\|건\|개\|장\|번)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6848 | `_UNSUPPORTED_GROUP_AXIS_RE` | 5 | `등급\s*별\|회원등급\s*별\|채널\s*별\|브랜드\s*별\|카테고리\s*별` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7275 | `_BALANCE_ABSENCE_PATTERN` | 5 | `없\|미보유\|보유하지\s*않\|보유\s*안\|보유하지\s*못` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7979 | `_RECENT_WINDOW_PATTERN` | 5 | `최근\s*(\d+)\s*(일\|주\|개월\|달\|년)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | analytical_intent.py:59 | `_RANKING_HIGH_RE` | 4 | `가장\s*(?:많이\|많은)\|최다\|최고` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | analytical_intent.py:60 | `_RANKING_LOW_RE` | 4 | `가장\s*(?:적게\|적은)\|최소\|최저` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6534 | `_RANKING_PERCENT_PATTERN` | 4 | `(?P<dir>상위\|하위)?\s*(?P<pct>\d+(?:\.\d+)?)\s*(?:%\|퍼센트\|프로)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6842 | `_GROUP_PER_COUNT_RE` | 4 | `([\d,]+)\s*명\s*씩\|(?:상위\|하위)\s*([\d,]+)\s*명\|([\d,]+)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6845 | `_PER_GROUP_SUFFIX_RE` | 4 | `([\d,]+)\s*(?:명\|개\|곳)?\s*씩\|명씩` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7310 | `_BALANCE_PERCENT_PATTERN` | 4 | `(?P<dir>상위\|하위)?\s*(?P<pct>\d+(?:\.\d+)?)\s*(?:%\|퍼센트\|프로)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7985 | `_EXACT_COUNT_PATTERN` | 4 | `(?P<num>\d+)\s*(?:개\|번\|회\|건)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8077 | `_SINO_KOREAN_AMOUNT_RE` | 4 | `(?P<num>[영공일이삼사오육칠팔구십백천]+)(?P<mag>억\|천만\|백만\|만)?원` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8653 | `_CART_TOTAL_QTY_SIGNAL` | 4 | `수량\|총\s*개수\|총\s*\d+\s*개\|총\s*\d` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10341 | `_RATIO_METRIC_PREFIX_RE` | 4 | `(?:하루\|1일\|매일\|일)\s*평균\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:10459 | `_CAMPAIGN_TAIL_NEG_RE` | 4 | `없\|않\|못[한했하받]\|안[한함했하]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | analytical_intent.py:56 | `_RECENT_DAYS_RE` | 3 | `최근\s*(\d+)\s*일(?:간\|동안\|이내)?` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6437 | `_REGION_DENSITY_TOP_N_PATTERN` | 3 | `상위\s*([\d,]+)\|(?:top\|톱)\s*([\d,]+)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6530 | `_RANKING_HIGH_DIRECTIVE` | 3 | `상위\s*[\d,]*\s*명?\|높은\s*순\|top\s*[\d,]+` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6532 | `_RANKING_DIRECTIVE_TOP_N` | 3 | `(?:상위\|하위\|top)\s*([\d,]+)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6840 | `_PER_GROUP_COUNT_RE` | 3 | `(?:상위\s*)?([\d,]+)\s*(?:명\|개\|곳)?\s*씩` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6930 | `_REGION_COUNT_TOP_N_RE` | 3 | `([\d,]+)\s*(?:개\|곳\|군데)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | segment_semantics.py:54 | `_AMOUNT_RE` | 3 | `(\d[\d,]*)\s*(억\|만\|천)?\s*원` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6531 | `_RANKING_LOW_DIRECTIVE` | 2 | `하위\s*[\d,]*\s*명?\|낮은\s*순` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7311 | `_BALANCE_TOPN_PATTERN` | 2 | `상위\s*(?P<a>[\d,]+)\|(?P<b>[\d,]+)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | member_policy.py:23 | `_INCLUDE_DORMANT_RE` | 2 | `휴면\s*(?:회원\s*)?(?:도\s*)?(?:포함\|포괄)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | member_policy.py:24 | `_INCLUDE_WITHDRAWN_RE` | 2 | `탈퇴\s*(?:회원\s*)?(?:도\s*)?(?:포함\|포괄)` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | analytical_intent.py:73 | `_EXPLICIT_MEMBER_LIMIT_RE` | 1 | `\b(\d[\d,]*)\s*명` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | build_member_value_index.py:43 | `_CLEAN_NAME` | 1 | `^[\x20-\x7E가-힣ㄱ-ㅎㅏ-ㅣ·]+$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | calendar_window.py:37 | `_ANY_YEAR_RE` | 1 | `(\d{4})\s*년` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | calendar_window.py:38 | `_QUARTER_RE` | 1 | `([1-4])\s*(?:사)?분기` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1297 | `_CHANNEL_SUFFIX_PATTERN` | 1 | `\n?\s*발송\s*채널\s*:.*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1481 | `_BRAND_ADJACENT_BEFORE` | 1 | `(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})\s*브랜드` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:1482 | `_BRAND_ADJACENT_AFTER` | 1 | `브랜드\s+(?P<object>[0-9A-Za-z가-힣_+\-]{1,40})` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:5176 | `_GROUP_AXIS_OBJECT_RE` | 1 | `^\s*[0-9A-Za-z가-힣_+\-]+\s*별\s*$` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:6106 | `_HANGUL_SYLLABLE` | 1 | `[가-힣]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:7384 | `_ZERO_AMOUNT_MARKER` | 1 | `(?<!\d)0\s*원` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8150 | `_BRAND_SCOPE_RE` | 1 | `(?P<val>[가-힣A-Za-z0-9]+)\s*브랜드` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | graph_rag.py:8151 | `_CATEGORY_SCOPE_RE` | 1 | `(?P<val>[가-힣A-Za-z0-9]+)\s*카테고리` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | rag_index.py:26 | `SPECIAL_CHARACTER_PATTERN` | 1 | `[^0-9A-Za-z가-힣ㄱ-ㅎㅏ-ㅣ\s.!?%+\-_,:/]` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | unresolved_triage.py:153 | `_TOKEN_RE` | 1 | `[가-힣]{2,}` | 한글 표면어와 구조가 섞여 있다 — 어휘 부분만 분리 가능한지 개별 판단 |
| domain | calendar_window.py:60 | `_CAL_TOKEN_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | calendar_window.py:86 | `_ENUM_LINK_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | calendar_window.py:118 | `_YEAR_ANCHOR_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | calendar_window.py:120 | `_ADJACENT_YEAR_ANCHOR_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | calendar_window.py:445 | `WORD_DURATION_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | calendar_window.py:526 | `RELATIVE_PAST_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:1383 | `_CATEGORY_COPULA_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:1390 | `_CATEGORY_ADJACENT_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:1887 | `pattern` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:6433 | `_REGION_DENSITY_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:6436 | `_REGION_DENSITY_ALT_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:7221 | `range_p` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:7222 | `op_p` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:7223 | `eq_p` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:7631 | `_METRIC_THRESHOLD_TAIL_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:8057 | `_KOREAN_COUNT_NUMERAL_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:8544 | `_PURCHASE_COUNT_THRESHOLD_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:9831 | `chain` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:9907 | `_SIGNUP_ONLINE_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:9908 | `_SIGNUP_OFFLINE_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:10783 | `_CAMPAIGN_BUY_VERB_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | graph_rag.py:11133 | `_SCHEMA_QUERY_VALUE_RE` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | segment_semantics.py:439 | `pattern` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | sql_guard.py:54 | `_TABLE_ALIAS_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | sql_guard.py:299 | `_AGG_CALL_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| domain | sql_guard.py:303 | `_INVALID_AGG_ARG_PATTERN` |  | `(동적 조립)` | 동적으로 조립된 패턴 — 소스를 직접 읽어야 한다 |
| grammar | sql_guard.py:429 | `_PERF_FUNC_ON_COLUMN_RE` | 13 | `\b(LEN\|SUBSTRING\|LEFT\|RIGHT\|UPPER\|LOWER\|YEAR\|MONTH\|DAY\|DATEPART\|ISNULL\|LTRIM\|RTRIM)\s*\(\s*(\w+\.\w+)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | graph_rag.py:15079 | `_TARGET_MEMBER_PROJECTION_RE` | 7 | `(?i)(?:\b[A-Za-z_][\w$]*\s*\.\s*)?(?:\[\s*MEMBER_NO\s*\]\|'MEMBER_NO'\|\"MEMBER_NO\"\|MEMBER_NO)\s+AS\s+(?:\[\s*CUST_ID\s*\]\|'CUST_ID'\|\"CUST_ID\"\|CUST_ID)(?![\w$])` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | sql_ast.py:24 | `_ALIAS_PATTERN` | 6 | `\b(?:FROM\|JOIN)\s+([A-Za-z_][A-Za-z0-9_\.]*)\s+(?!ON\b\|WHERE\b\|GROUP\b\|ORDER\b\|HAVING\b)([A-Za-z_][A-Za-z0-9_]*)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | sql_guard.py:423 | `_PERF_CAST_JOIN_RE` | 6 | `(?:TRY_CAST\|CAST\|CONVERT)\s*\(\s*(\w+\.\w+)\b[^)]*\)\s*=\|=\s*(?:TRY_CAST\|CAST\|CONVERT)\s*\(\s*(\w+\.\w+)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | aggregation_requirements.py:655 | `_RELATIVE_WINDOW_PLACEHOLDER_RE` | 5 | `\bwindow_(?:start\|end)_\d+(?:d\|w\|m\|y)\b` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | rag_index.py:23 | `HTML_SCRIPT_STYLE_PATTERN` | 2 | `<\s*(script\|style)\b[^>]*>.*?<\s*/\s*\1\s*>` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | aggregation_requirements.py:635 | `_PHYSICAL_FIELD_IN_NOTE_RE` | 1 | `\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | aggregation_requirements.py:636 | `_RELATIVE_PERIOD_RE` | 1 | `^P(?=\d)(?:\d+[YMWD])+$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | build_member_value_index.py:41 | `_CODE_VALUE` | 1 | `^[A-Z0-9_]+\.[^.].*$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | build_rag_knowledge.py:329 | `pattern` | 1 | `--\s*(?P<number>\d+)\.\s*(?P<title>.+?)\n(?P<sql>.*?;)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | db_swap_preflight.py:41 | `_COLUMN_RE` | 1 | `^(?:([A-Za-z]\w*)\.)?([A-Z][A-Z0-9_]{1,})$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | entity_set.py:178 | `_COMPACT_DROP_RE` | 1 | `[\s.,!?·_\-/'\"()]` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | graph_rag.py:6107 | `_ASCII_ALNUM` | 1 | `[0-9A-Za-z]` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | graph_rag.py:6193 | `_PLAIN_NUMERIC_VALUE` | 1 | `^[\d.\-/:%\s]+$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | graph_rag.py:14975 | `prefix` | 1 | `\bnot\s+exists\s*\(` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | graph_rag.py:15147 | `_CONSTANT_PROJECTION_RE` | 1 | `N?'([^']*)'\s+AS\s+([A-Za-z_]\w*)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | logical_expression.py:231 | `_PLACEHOLDER_RE` | 1 | `@([A-Za-z0-9_]+)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | rag_index.py:22 | `HTML_TAG_PATTERN` | 1 | `<[^>]+>` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | rag_index.py:24 | `WHITESPACE_PATTERN` | 1 | `\s+` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | rag_index.py:25 | `SENTENCE_PATTERN` | 1 | `[^.!?。！？]+[.!?。！？]*` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | schema_extract.py:9 | `CREATE_TABLE_RE` | 1 | `CREATE\s+TABLE\s+(?P<table>[\w.]+)\s*\((?P<body>.*?)\);` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | schema_extract.py:13 | `CREATE_INDEX_RE` | 1 | `CREATE\s+(?P<unique>UNIQUE\s+)?INDEX\s+(?P<name>\w+)\s+ON\s+(?P<table>[\w.]+)\s*\((?P<columns>[^)]+)\);` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | schema_extract.py:17 | `CREATE_VIEW_RE` | 1 | `CREATE\s+VIEW\s+(?P<view>[\w.]+)\s+AS\s+(?P<body>.*?);` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | schema_extract.py:21 | `COLUMN_RE` | 1 | `^(?P<name>\w+)\s+(?P<type>[A-Z][A-Z0-9_]*(?:\([^)]*\))?)(?P<constraints>.*)$` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | schema_extract.py:22 | `REFERENCES_RE` | 1 | `REFERENCES\s+(?P<table>[\w.]+)\s*\((?P<column>\w+)\)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | schema_extract.py:23 | `FOREIGN_KEY_RE` | 1 | `FOREIGN\s+KEY\s*\((?P<columns>[^)]+)\)\s*REFERENCES\s+(?P<table>[\w.]+)\s*\((?P<ref_columns>[^)]+)\)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | segment_semantics.py:53 | `_NUMBER_RE` | 1 | `\d[\d,]*` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | sql_guard.py:59 | `_EQUI_JOIN_PATTERN` | 1 | `\b([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)\s*=\s*([A-Za-z_][\w]*)\.([A-Za-z_][\w]*)` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | sql_guard.py:300 | `_BARE_COLUMN_PATTERN` | 1 | `\b[A-Za-z_]\w*\.[A-Za-z_]\w*` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | sql_guard.py:307 | `_NONE_LITERAL_PATTERN` | 1 | `\bNone\b` | 표면어 없이 구조만 — 코드에 남는다 |
| grammar | sql_guard.py:422 | `_PERF_LEADING_WILDCARD_RE` | 1 | `(\w+(?:\.\w+)?)\s+LIKE\s+N?'%` | 표면어 없이 구조만 — 코드에 남는다 |
