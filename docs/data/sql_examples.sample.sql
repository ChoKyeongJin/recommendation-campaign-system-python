-- NL2SQL/RAG retrieval examples — 실DB 기준. 아래 승인된 테이블만 사용한다.
-- 라우팅: 제목의 [DB] 태그가 어느 연결에서 실행할지를 뜻한다.
--   - [quadmax_sdz] = MariaDB(quadmax_sdz). MySQL 방언. 타겟리스트 원본.
--   - [CRMDW]       = SQL Server smart_quadmax_mart. 기본 DB의 dbo 이름을 그대로 쓴다. T-SQL 방언.
-- 모든 실DB 는 읽기 전용(SELECT/WITH 만). 각 예시는 세미콜론으로 끝나는 단일 문장이어야 한다.
--
-- 승인된 사용 테이블 목록:
--   [quadmax_sdz] T_TARGETLIST_CUST
--   [CRMDW]       CRM_MB_BASEINFO, CRM_CM_ADDRESS, CRM_CM_OFFSHOP
--   [CRMDW]       Z_CAMPAIGN, Z_CAMP_CELL, Z_CAMP_MBR, MCS_CAMP_MBR_RSPN_FT
--   [CRMDW]       ODS_MALL_OMS_CART, CRM_CM_PRODUCT
--   [CRMDW]       CRM_SL_ORDERHEADERMALL, CRM_SL_ORDERDETAILMALL (몰 주문/구매), CRM_SL_ORDERHEADERALL/DETAILALL (통합)
--
-- 날짜형 다수는 nvarchar(8) 'YYYYMMDD' 문자열이라 사전식 비교, 또는 CONVERT(varchar(8), DATEADD(...), 112) 를 쓴다.
-- 회원키는 CRMDW MEMBER_NO(bigint)로 통일한다: 장바구니 CART_ID→CRM_MB_BASEINFO.MEMBER_ID→MEMBER_NO,
--   주문 CRM_SL_ORDERHEADERMALL.MEMBER_NO(bigint)와 동일 도메인이라 단일 DB(CRMDW) 조인/anti-join 가능.

-- ============================================================
-- 그룹 1. 타겟리스트 (quadmax_sdz / MariaDB)
-- ============================================================

-- 1. [quadmax_sdz] 특정 타겟리스트(TL_ID)에 속한 대상 고객 목록
-- 주: TL_ID 는 'SV%' 패턴의 타겟리스트 식별자. 'SV0001'은 대표값.
SELECT DISTINCT CUST_ID
FROM T_TARGETLIST_CUST
WHERE TL_ID = 'SV0001';

-- 2. [quadmax_sdz] 타겟리스트별 대상 고객 수 집계
SELECT TL_ID, COUNT(DISTINCT CUST_ID) AS cust_cnt
FROM T_TARGETLIST_CUST
GROUP BY TL_ID
ORDER BY cust_cnt DESC;

-- ============================================================
-- 그룹 2. 회원 기본정보 (CRMDW)
-- ============================================================

-- 3. [CRMDW] 휴면 회원 조회
SELECT MEMBER_NO, MEMBER_ID, GENDER_CD, AGE, LAST_LOGIN_DATE
FROM CRM_MB_BASEINFO
WHERE SLEEP_MEMBER_YN = 'Y';

-- 4. [CRMDW] 앱푸시 수신 동의한 활동 회원(블랙리스트 제외)
SELECT MEMBER_NO, GENDER_CD, AGE, SIDO, SIGUNGU
FROM CRM_MB_BASEINFO
WHERE ACTIVITY_MEMBER_YN = 'Y'
  AND APP_PUSH_YN = 'Y'
  AND ISNULL(BLACKLIST_YN, 'N') = 'N';

-- 5. [CRMDW] 마케팅 발송 대상 회원기본정보(정상/수신동의/블랙리스트·쇼핑몰회원 제외) + 발송채널 버킷
-- 주: 회원상태 정상·휴면N·동의Y·블랙리스트N·쇼핑몰회원N은 고정 조건. 이메일 수신동의(EMAIL_YN='Y')는 필수.
--     PCH_CHAN_TYPE는 MEMBER_NO 6번째 자리로 발송채널(01/02/03)을 나누는 파생 컬럼. 성별(GENDER_CD)·연령(AGE) 등은 선택 필터.
SELECT A.MEMBER_NO AS CUST_ID
     , CASE WHEN SUBSTRING(CAST(A.MEMBER_NO AS VARCHAR), 6, 1) IN ('0','1','2') THEN '01'
            WHEN SUBSTRING(CAST(A.MEMBER_NO AS VARCHAR), 6, 1) IN ('3','4','5') THEN '02'
            ELSE '03' END AS PCH_CHAN_TYPE
FROM CRM_MB_BASEINFO A WITH(NOLOCK)
WHERE A.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'
  AND A.SLEEP_MEMBER_YN = 'N'
  AND A.AGREE_YN = 'Y'
  AND ISNULL(A.BLACKLIST_YN, 'N') = 'N'
  AND A.SITE_MEMBER_YN = 'N'
  AND A.EMAIL_YN = 'Y'
  AND A.GENDER_CD IN ('F')
  AND A.AGE >= 20;

-- 6. [CRMDW] 회원기본정보 + 주소/가입매장 조인 — 특정 시군구 가입매장 회원(발송채널 버킷)
-- 주: 5번과 동일 고정조건. 주소(B, ZIP_CD=ZIP_CODE)·가입매장(C, REG_OFFSHOP_ID=OFFSHOP_ID) LEFT JOIN + GROUP BY 집계.
--     선택 필터: 가입매장 지역(C.SIGUNGU)·성별 등. N'강남구'는 대표값(가입매장 시군구).
SELECT A.MEMBER_NO AS cust_id
     , MAX(CASE WHEN SUBSTRING(CAST(A.MEMBER_NO AS VARCHAR), 6, 1) IN ('0','1','2') THEN '01'
                WHEN SUBSTRING(CAST(A.MEMBER_NO AS VARCHAR), 6, 1) IN ('3','4','5') THEN '02'
                ELSE '03' END) AS PCH_CHAN_TYPE
FROM CRM_MB_BASEINFO A WITH(NOLOCK)
     LEFT OUTER JOIN CRM_CM_ADDRESS B ON A.ZIP_CD = B.ZIP_CODE
     LEFT OUTER JOIN CRM_CM_OFFSHOP C ON A.REG_OFFSHOP_ID = C.OFFSHOP_ID
WHERE A.MEMBER_STATE_CD = 'MEMBER_STATE_CD.NORMAL'
  AND A.SLEEP_MEMBER_YN = 'N'
  AND A.AGREE_YN = 'Y'
  AND ISNULL(A.BLACKLIST_YN, 'N') = 'N'
  AND A.SITE_MEMBER_YN = 'N'
  AND C.SIGUNGU IN (N'강남구')
  AND A.GENDER_CD IN ('F')
GROUP BY A.MEMBER_NO;

-- ============================================================
-- 그룹 3. 캠페인 (CRMDW)
-- ============================================================

-- 7. [CRMDW] 최근 3개월 실행된 캠페인 목록(취소/중지 제외)
SELECT CAMP_ID, CAMP_EXEC_NO, CAMP_NAME, CAMP_TYPE_CD, CAMP_PURPOSE_CD, CAMP_SDATE, CAMP_EDATE
FROM Z_CAMPAIGN
WHERE ISNULL(CANCEL_YN, 'N') = 'N'
  AND CAMP_SDATE >= CONVERT(varchar(8), DATEADD(MONTH, -3, GETDATE()), 112)
ORDER BY CAMP_SDATE DESC;

-- 8. [CRMDW] 캠페인 실행별 발송/오퍼반응/구매반응 집계(타겟군 기준)
SELECT CAMP_ID, CAMP_EXEC_NO,
       COUNT(*) AS sent_cnt,
       SUM(CASE WHEN CNCT_SCS_YN = 'Y' THEN 1 ELSE 0 END) AS contact_cnt,
       SUM(CASE WHEN OFFR_RSPN_YN = 'Y' THEN 1 ELSE 0 END) AS offer_rspn_cnt,
       SUM(CASE WHEN BUY_RSPN_YN = 'Y' THEN 1 ELSE 0 END) AS buy_rspn_cnt,
       SUM(BUY_AMT) AS buy_amt
FROM MCS_CAMP_MBR_RSPN_FT
WHERE CGRP_TYPE_CD = 'T'
GROUP BY CAMP_ID, CAMP_EXEC_NO
ORDER BY buy_amt DESC;

-- 9. [CRMDW] 특정 캠페인의 타겟 회원 목록
SELECT CAMP_ID, CAMP_EXEC_NO, CELL_NODE_ID, MBR_NO, CONTAC_SUCC_YN
FROM Z_CAMP_MBR
WHERE CAMP_ID = 'CAMP0001'
  AND CELL_TYPE_CD = 'T';

-- 10. [CRMDW] 캠페인 셀별 ROI 상위(셀 + 캠페인 조인)
SELECT c.CAMP_ID, g.CAMP_NAME, c.CELL_NODE_ID, c.CELL_NAME, c.SBJ_GP_MBRNUM, c.EXP_ROI
FROM Z_CAMP_CELL c
JOIN Z_CAMPAIGN g ON g.CAMP_ID = c.CAMP_ID AND g.CAMP_EXEC_NO = c.CAMP_EXEC_NO
WHERE c.EXP_ROI IS NOT NULL
ORDER BY c.EXP_ROI DESC;

-- 11. [CRMDW] 캠페인에서 구매 반응한 회원 프로파일(반응팩트 + 회원기본정보, 동일 CRMDW 회원키)
-- 주: MBR_NO(varchar) 와 MEMBER_NO(bigint) 는 형이 달라 TRY_CAST 로 맞춘다. 비숫자 값은 매칭에서 제외된다.
SELECT f.CAMP_ID, f.CAMP_EXEC_NO, f.MBR_NO, b.GENDER_CD, b.AGE, b.SIDO, f.BUY_AMT
FROM MCS_CAMP_MBR_RSPN_FT f
JOIN CRM_MB_BASEINFO b ON b.MEMBER_NO = TRY_CAST(f.MBR_NO AS bigint)
WHERE f.CGRP_TYPE_CD = 'T'
  AND f.BUY_RSPN_YN = 'Y';

-- 12. [CRMDW] 특정 캠페인(CAMP0001) 대상군 중 구매반응 발생(구매금액 1만~100만원) 회원
-- 주: 캠페인명(A.CAMP_ID IN)은 필수, C.CELL_TYPE_CD='T'는 대상군만 추출하는 고정 조건. 반응실적(D)은 LEFT JOIN.
--     선택 필터: 접촉성공/구매반응/오퍼반응여부=Y·N, 구매금액/오퍼사용구매금액/오퍼할인금액 BETWEEN.
SELECT C.MBR_NO
FROM Z_CAMPAIGN A WITH(NOLOCK)
     INNER JOIN Z_CAMP_CELL B WITH(NOLOCK)
       ON A.CAMP_ID = B.CAMP_ID AND A.CAMP_EXEC_NO = B.CAMP_EXEC_NO
     INNER JOIN Z_CAMP_MBR C WITH(NOLOCK)
       ON A.CAMP_ID = C.CAMP_ID AND A.CAMP_EXEC_NO = C.CAMP_EXEC_NO AND B.CELL_NODE_ID = C.CELL_NODE_ID
     LEFT OUTER JOIN MCS_CAMP_MBR_RSPN_FT D WITH(NOLOCK)
       ON A.CAMP_ID = D.CAMP_ID AND A.CAMP_EXEC_NO = D.CAMP_EXEC_NO AND B.CELL_NODE_ID = D.CELL_NODE_ID AND C.MBR_NO = D.MBR_NO
WHERE A.CAMP_ID IN ('CAMP0001')
  AND C.CELL_TYPE_CD = 'T'
  AND D.BUY_RSPN_YN = 'Y'
  AND D.BUY_AMT BETWEEN 10000 AND 1000000;

-- 13. [CRMDW] 특정 캠페인 실행회차/반응노드(셀)의 채널·오퍼·구매 반응 고객 추출(대상군)
-- 주: 반응 실행 쿼리의 '특정 셀' 분기. CNCT_SCS_YN(채널)/OFFR_RSPN_YN(오퍼)/BUY_RSPN_YN(구매), 모두 Y·N.
--     각 반응필터가 'ALL'이면 해당 조건은 무시(전체)되고, 값이 지정되면 그 값으로 필터.
SELECT DISTINCT MBR_NO
FROM MCS_CAMP_MBR_RSPN_FT
WHERE CAMP_ID = 'C17003G'
  AND CAMP_EXEC_NO = '1'
  AND CELL_NODE_ID = 'N170216095828506'
  AND CGRP_TYPE_CD = 'T'
  AND CNCT_SCS_YN = 'Y'
  AND OFFR_RSPN_YN = 'Y'
  AND BUY_RSPN_YN = 'Y';

-- 14. [CRMDW] 특정 캠페인 전체의 구매 반응 고객 추출(반응노드=ALL 분기, 대상군)
-- 주: 반응 실행 쿼리의 'ALL' 분기 — CELL_NODE_ID/CAMP_EXEC_NO 없이 캠페인 전체에서 추출.
SELECT DISTINCT MBR_NO
FROM MCS_CAMP_MBR_RSPN_FT
WHERE CAMP_ID = 'C17003G'
  AND CGRP_TYPE_CD = 'T'
  AND BUY_RSPN_YN = 'Y';

-- ============================================================
-- 그룹 4. 장바구니 (CRMDW)
-- ============================================================

-- 15. [CRMDW] 현재 판매중인 상품 조회(판매기간 기준)
SELECT PRODUCT_ID, PRODUCT_NAME, BRAND_NAME, CATEGORYL_NAME, CATEGORYM_NAME
FROM CRM_CM_PRODUCT
WHERE SALE_START_DT <= CONVERT(varchar(8), GETDATE(), 112)
  AND (SALE_END_DT IS NULL OR SALE_END_DT >= CONVERT(varchar(8), GETDATE(), 112));

-- 16. [CRMDW] 장바구니에 담겨있는(유지) 인기 상품 TOP 20
SELECT TOP 20 PRODUCT_ID, SUM(QTY) AS total_qty, COUNT(*) AS cart_line_cnt
FROM ODS_MALL_OMS_CART
WHERE KEEP_YN = 'Y'
GROUP BY PRODUCT_ID
ORDER BY total_qty DESC;

-- 17. [CRMDW] 장바구니 상품에 상품 마스터를 조인해 상품명까지 조회
SELECT TOP 50 c.CART_ID, c.PRODUCT_ID, p.PRODUCT_NAME, p.BRAND_NAME, c.QTY, c.TOTAL_SALE_PRICE
FROM ODS_MALL_OMS_CART c
JOIN CRM_CM_PRODUCT p ON p.PRODUCT_ID = c.PRODUCT_ID
WHERE c.KEEP_YN = 'Y'
ORDER BY c.INS_DT DESC;

-- 18. [CRMDW] 최근 30일 장바구니 담은 회원 중 장바구니 합계금액 5만~50만원
-- 주: 회원기본정보(B)에 CART_ID=MEMBER_ID로 조인, 상품마스터(C)는 상품분류 필터용 LEFT JOIN. 모든 WHERE 필터는 선택.
--     선택 필터: 보관시작일(INS_DT)/보관종료일(END_DT) 기간, 장바구니유형(CART_TYPE_CD), 상품분류, HAVING 장바구니금액(SUM(SALE_PRICE)).
SELECT B.MEMBER_NO AS CUST_ID
FROM ODS_MALL_OMS_CART A WITH(NOLOCK)
     INNER JOIN CRM_MB_BASEINFO B WITH(NOLOCK)
       ON A.CART_ID = B.MEMBER_ID
     LEFT OUTER JOIN CRM_CM_PRODUCT C WITH(NOLOCK)
       ON A.PRODUCT_ID = C.PRODUCT_ID
WHERE CONVERT(CHAR(8), A.INS_DT, 112)
      BETWEEN CONVERT(varchar(8), DATEADD(DAY, -30, GETDATE()), 112)
          AND CONVERT(varchar(8), GETDATE(), 112)
GROUP BY B.MEMBER_NO
HAVING SUM(A.SALE_PRICE) BETWEEN 50000 AND 500000;

-- 22. [CRMDW] 장바구니에 특정 상품브랜드(BRAND_ID)를 담은 회원 추출(대상군)
-- 주: 상품브랜드는 '상품브랜드' 디멘션(거래브랜드)으로 브랜드명 -> BRAND_ID 코드로 변환해 필터한다. 예: 포멜카멜리 = 'A'.
--     장바구니(A)에 회원기본정보(B)를 CART_ID=MEMBER_ID로, 상품마스터(C)를 PRODUCT_ID로 조인하고 C.BRAND_ID로 브랜드를 건다.
SELECT DISTINCT B.MEMBER_NO AS CUST_ID
FROM ODS_MALL_OMS_CART A WITH(NOLOCK)
     INNER JOIN CRM_MB_BASEINFO B WITH(NOLOCK)
       ON A.CART_ID = B.MEMBER_ID
     INNER JOIN CRM_CM_PRODUCT C WITH(NOLOCK)
       ON A.PRODUCT_ID = C.PRODUCT_ID
WHERE A.KEEP_YN = 'Y'
  AND C.BRAND_ID IN ('A');

-- ============================================================
-- 그룹 6. 장바구니 이탈 / 미결제 (재구매·구매전환 유도) - CRMDW
-- ============================================================

-- 23. [CRMDW] 장바구니 보관 중이나 최근 90일 몰 주문이 없는 회원(장바구니 이탈=담고 결제 안 함, 구매전환/재구매 유도 대상)
-- 주: 장바구니(A, KEEP_YN='Y')에 회원기본정보(B)를 CART_ID=MEMBER_ID로 조인해 MEMBER_NO를 얻고,
--     몰 주문헤더 CRM_SL_ORDERHEADERMALL 에 같은 MEMBER_NO 주문이 없으면(NOT EXISTS) 미결제 이탈로 본다. 모두 CRMDW라 단일 쿼리 anti-join.
--     기간(-90일)은 선택. 몰 구매여부는 CRM_SL_ORDERHEADERMALL(적재됨) 기준이며 ODS_MALL_OMS_ORDER(0행)는 쓰지 않는다.
SELECT DISTINCT B.MEMBER_NO AS CUST_ID
FROM ODS_MALL_OMS_CART A WITH(NOLOCK)
     INNER JOIN CRM_MB_BASEINFO B WITH(NOLOCK)
       ON A.CART_ID = B.MEMBER_ID
WHERE A.KEEP_YN = 'Y'
  AND NOT EXISTS (
        SELECT 1
        FROM CRM_SL_ORDERHEADERMALL O WITH(NOLOCK)
        WHERE O.MEMBER_NO = B.MEMBER_NO
          AND O.ORDER_DATE >= CONVERT(varchar(8), DATEADD(DAY, -90, GETDATE()), 112)
      );

-- 24. [CRMDW] 장바구니 보관 중이나 몰 주문 이력이 전혀 없는 회원(순수 미구매자)
-- 주: 예시 23에서 기간 조건을 뺀 형태. 몰에서 한 번도 결제한 적 없는 장바구니 보관 회원. 주문 데이터가 과거까지만 있을 때 안전한 정의.
SELECT DISTINCT B.MEMBER_NO AS CUST_ID
FROM ODS_MALL_OMS_CART A WITH(NOLOCK)
     INNER JOIN CRM_MB_BASEINFO B WITH(NOLOCK)
       ON A.CART_ID = B.MEMBER_ID
WHERE A.KEEP_YN = 'Y'
  AND NOT EXISTS (
        SELECT 1
        FROM CRM_SL_ORDERHEADERMALL O WITH(NOLOCK)
        WHERE O.MEMBER_NO = B.MEMBER_NO
      );

-- 25. [CRMDW] 특정 상품을 장바구니에 담았으나 아직 그 상품을 사지 않은 회원(상품 단위 재구매·구매 유도)
-- 주: 장바구니 라인(A)의 PRODUCT_ID 기준으로, 같은 회원이 같은 상품을 몰에서 산 적 없으면(CRM_SL_ORDERDETAILMALL NOT EXISTS) 대상.
--     대상 상품은 CRM_CM_PRODUCT.PRODUCT_ID 로 지정(브랜드로 걸려면 CRM_CM_BRAND.BRAND_ID 사용). 아래는 특정 PRODUCT_ID 예시.
SELECT DISTINCT B.MEMBER_NO AS CUST_ID
FROM ODS_MALL_OMS_CART A WITH(NOLOCK)
     INNER JOIN CRM_MB_BASEINFO B WITH(NOLOCK)
       ON A.CART_ID = B.MEMBER_ID
WHERE A.KEEP_YN = 'Y'
  AND A.PRODUCT_ID = 'P0001'
  AND NOT EXISTS (
        SELECT 1
        FROM CRM_SL_ORDERDETAILMALL D WITH(NOLOCK)
        WHERE D.MEMBER_NO = B.MEMBER_NO
          AND D.PRODUCT_ID = A.PRODUCT_ID
      );
