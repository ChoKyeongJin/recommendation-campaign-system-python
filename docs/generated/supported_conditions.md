# 지원 타겟 조건 표

> **자동 생성 문서 — 손으로 편집하지 마라.** LLM은 고정 Event IR 대수인
> `audience_requirement`만 만들며 아래 호환 슬롯을 직접 만들지 않는다.
> 표는 실행 자산(targeting_ir·V4 스키마·폐기 슬롯 목록·
> member_target_filters.json·requirement_capabilities.json)에서 파생되며,
> `python tools/generate_supported_conditions.py` 로 재생성한다.
> 최신성은 tests/test_supported_conditions_doc.py 가 CI 에서 강제한다.

## 1. 호환 실행 슬롯 조건

| 슬롯 | 라벨 | 컨테이너 | 지원 | LLM 직접 노출 | 비고 |
|---|---|---|---|---|---|
| `signup_target` | 가입일 조건 | target_user | 지원 | X |  |
| `recent_login` | 최근 로그인 기간 조건 | target_user | 지원 | X |  |
| `inactivity_period` | 미접속 기간 조건 | target_user | 지원 | X |  |
| `purchase_inactivity` | 미구매 기간 조건 | target_user | 지원 | X |  |
| `purchase_membership` | 구매 이력 존재 조건 | target_user | 지원 | X |  |
| `cart_retention` | 장바구니 보관 기간 조건 | target_user | 지원 | X |  |
| `cart_aggregate` | 장바구니 집계 조건(담은 수량/금액) | target_user | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `cart_type` | 장바구니 유형 조건 | target_user | 지원 | X |  |
| `birthday_target` | 생일 조건 | target_user | 지원 | X |  |
| `campaign_responses` | 캠페인 반응 조건 | target_user | 지원 | X |  |
| `campaign_response_frequency` | 캠페인 반응 횟수 조건 | target_user | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `campaign_buy_amount` | 캠페인 구매 금액 조건 | target_user | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `campaign_buy_count` | 캠페인 구매 건수 조건 | target_user | 지원 | X |  |
| `cell_rate_target` | 캠페인 셀 반응률 조건 | target_user | 지원 | X |  |
| `purchase_date` | 구매일 조건 | target_user | 지원 | X |  |
| `metric_trend` | 기간 대비 지표 증감 조건 | target_user | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `purchase_object` | 구매 상품 조건 | target_user | 지원 | X |  |
| `cart_absence` | 장바구니 미보유 조건 | target_user | 지원 | X |  |
| `aggregate_conditions` | 집계 조건(구매 금액/횟수 임계값) | target_user | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `balance_conditions` | 잔액 조건 | target_user | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `profile_date_conditions` | 회원 프로필 날짜 조건 | target_user | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `region_density_target` | 지역 밀집 랭킹 조건 | plan | 지원 | X(제외 선언) |  |
| `member_metric_ranking` | 회원 지표 랭킹 조건 | plan | **미지원** | X(후보 드롭) | 선언만 있고 생산자(슬롯 컴파일러)가 폐기돼 채우는 경로가 없다. |
| `purchase_count_ranking` | 구매 건수 랭킹 조건 | plan | 지원 | X(제외 선언) |  |

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

- 조건부로 지원되는 슬롯이 현재 없다 — 선언된 각주는 모두 생산자가 폐기된 슬롯의 것이라 §1 에서 미지원으로 내렸다.
