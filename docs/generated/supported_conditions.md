# 지원 타겟 조건 표

> **자동 생성 문서 — 손으로 편집하지 마라.** 실행 자산(targeting_ir·V4 스키마·
> member_target_filters.json·requirement_capabilities.json)에서 파생되며,
> `python tools/generate_supported_conditions.py` 로 재생성한다.
> 최신성은 tests/test_supported_conditions_doc.py 가 CI 에서 강제한다.

## 1. 구조화 슬롯 조건

| 슬롯 | 라벨 | 컨테이너 | LLM 노출 | 조건부 지원 각주 |
|---|---|---|---|---|
| `signup_target` | 가입일 조건 | target_user | O |  |
| `recent_login` | 최근 로그인 기간 조건 | target_user | O |  |
| `inactivity_period` | 미접속 기간 조건 | target_user | O |  |
| `purchase_inactivity` | 미구매 기간 조건 | target_user | O |  |
| `cart_retention` | 장바구니 보관 기간 조건 | target_user | O |  |
| `cart_aggregate` | 장바구니 집계 조건(담은 수량/금액) | target_user | O |  |
| `cart_type` | 장바구니 유형 조건 | target_user | O |  |
| `birthday_target` | 생일 조건 | target_user | O |  |
| `campaign_responses` | 캠페인 반응 조건 | target_user | O |  |
| `campaign_response_frequency` | 캠페인 반응 횟수 조건 | target_user | O |  |
| `campaign_buy_amount` | 캠페인 구매 금액 조건 | target_user | O |  |
| `campaign_buy_count` | 캠페인 구매 건수 조건 | target_user | O |  |
| `cell_rate_target` | 캠페인 셀 반응률 조건 | target_user | O |  |
| `purchase_date` | 구매일 조건 | target_user | O |  |
| `metric_trend` | 기간 대비 지표 증감 조건 | target_user | O | 수치 집계 지표만 지원(날짜·요약 지표의 기간 비교는 미지원 안내) |
| `purchase_object` | 구매 상품 조건 | target_user | O |  |
| `cart_absence` | 장바구니 미보유 조건 | target_user | O |  |
| `aggregate_conditions` | 집계 조건(구매 금액/횟수 임계값) | target_user | O |  |
| `balance_conditions` | 잔액 조건 | target_user | O |  |
| `profile_date_conditions` | 회원 프로필 날짜 조건 | target_user | O |  |
| `region_density_target` | 지역 밀집 랭킹 조건 | plan | X(제외 선언) |  |
| `member_metric_ranking` | 회원 지표 랭킹 조건 | plan | O |  |
| `purchase_count_ranking` | 구매 건수 랭킹 조건 | plan | X(제외 선언) |  |

## 2. 주문 행동(behaviors)

| 행동 | 지원 | 비고 |
|---|---|---|
| `first_purchase` | 지원 | 첫 구매, 최초 구매, 한 번 구매 |
| `repeat_buyer` | 지원 | 재구매, 반복 구매, 두 번 이상 구매 |
| `no_purchase` | 지원 | 미구매, 구매 이력 없음, 한 번도 구매하지 않은 |
| `lapsed_buyer` | **미지원** | 선언만 있고 컴파일 경로가 없다. |

## 3. 조건×수식어(base×qualifier)

| base | qualifier | 지원 | 안내 |
|---|---|---|---|
| 장바구니 조건(`cart_retention`) | brand | 지원 |  |
| 장바구니 조건(`cart_retention`) | product | **미지원** | 현재 장바구니 조건에는 특정 상품 필터를 함께 적용할 수 없습니다. |
| 장바구니 조건(`cart_retention`) | category | **미지원** | 현재 장바구니 조건에는 카테고리 필터를 함께 적용할 수 없습니다. |
| 구매 조건(`purchase`) | brand | 지원 |  |
| 구매 조건(`purchase`) | product | 지원 |  |
| 구매 조건(`purchase`) | category | 지원 |  |
| 쿠폰 사용 조건(`coupon`) | brand | **미지원** | 현재 쿠폰 사용 조건에는 브랜드 필터를 함께 적용할 수 없습니다. |
| 쿠폰 사용 조건(`coupon`) | product | **미지원** | 현재 쿠폰 사용 조건에는 특정 상품 필터를 함께 적용할 수 없습니다. |
| 쿠폰 사용 조건(`coupon`) | category | **미지원** | 현재 쿠폰 사용 조건에는 카테고리 필터를 함께 적용할 수 없습니다. |
| 로그인/접속 조건(`login`) | brand | **미지원** | 현재 로그인/접속 조건에는 브랜드 필터를 함께 적용할 수 없습니다. |
| 로그인/접속 조건(`login`) | product | **미지원** | 현재 로그인/접속 조건에는 특정 상품 필터를 함께 적용할 수 없습니다. |
| 로그인/접속 조건(`login`) | category | **미지원** | 현재 로그인/접속 조건에는 카테고리 필터를 함께 적용할 수 없습니다. |
| 이 조건(`_default`) | brand | **미지원** | 현재 이 조건에는 브랜드 필터를 함께 적용할 수 없습니다. 브랜드는 '구매' 조건으로 지정해 주세요. |
| 이 조건(`_default`) | product | **미지원** | 현재 이 조건에는 특정 상품 필터를 함께 적용할 수 없습니다. |
| 이 조건(`_default`) | category | **미지원** | 현재 이 조건에는 카테고리 필터를 함께 적용할 수 없습니다. |

## 4. 특수 조건부 지원

- **동시구매(condition_evaluation)**: 검증된 구성 서명 `same_product_same_order_quantity_v1` 만 컴파일한다 — 동일 주문 내 동일 상품 수량 집계 외의 조합(주문 횡단·상이 상품 등)은 fail-close 로 명시 차단된다.
- **기간 대비 지표 증감 조건(`metric_trend`)**: 수치 집계 지표만 지원(날짜·요약 지표의 기간 비교는 미지원 안내).
