-- 회원 대상 파라미터 템플릿 계열 NL2SQL/RAG 예시 — 실DB(CRMDW/CRMAN) 기준. SQL Server(T-SQL) 방언.
-- t_xlig_query_prompt / t_xlig_dimension_list 의 파라미터 템플릿(@@..@@, ##..##, $$..$$, [[필수]]/[선택], ::op::)을 구체화한 예시 모음.
-- 대상 테이블: [CRMDW] CRM_MB_BASEINFO(회원기본정보)/CRM_MB_MONTHCRMINFO(회원등급/월CRM), [CRMAN] CRM_MB_BASE_INFO/CRM_MB_CRM_INFO(회원프로파일). 모두 읽기 전용(SELECT/WITH만).
-- 타겟리스트($$타겟리스트$$=@@타겟리스트@@)는 quadmax_sdz.T_TARGETLIST(TL_ID 'SV%')에서 오고, 대상 회원(T_TARGETLIST_CUST)은 MariaDB에만 있어 OPENQUERY(MSSQL_TO_MARIADB74) linked server로 조인한다.
-- 코드값 일부는 실제 조회 대신 대표값(예시)으로 채웠으며 주석/인라인에 '대표값'으로 표기 → CRMDW 연결 복구 시 실제 코드로 치환.
--   회원등급(ZTS_GRADE)=CRM_CM_CODE(EN_MALL/MEM_GRADE_CD), 가치등급(WORTH_GRADE), 상태등급(CAMP/STATE_GRADE) 등.
-- 각 예시는 세미콜론으로 끝나는 단일 문장이어야 한다.

-- 1. [CRMDW] 최신 기준월 회원등급 정보 기준 타겟(가치등급 VIP/VVIP)
-- 주: 기준월(YYYYMM)만 필수. 관점구분(DIV_ID)/회원등급(ZTS_GRADE)/전월회원등급/상태등급/R·F·M등급/등급성장유형(GRADE_GROW_TYPE) IN 필터는 선택.
SELECT A.MEMBER_NO AS CUST_ID
FROM CRM_MB_MONTHCRMINFO A WITH(NOLOCK)
WHERE A.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)
  AND A.WORTH_GRADE IN ('VIP', 'VVIP');

-- 2. [CRMDW] 최신 기준월 최근1년 첫구매 이력 보유 + 특정 회원등급(ZTS_GRADE) 타겟
-- 주: 기준월(YYYYMM)·최근1년첫구매일(RECENT_YEAR_FIRST_BUY_DATE 존재)은 고정, 회원등급(@@회원등급@@ → A.ZTS_GRADE IN)은 필수. @@회원등급A@@도 동일 ZTS_GRADE 대상의 선택 필터.
--     ZTS_GRADE 코드는 CRM_CM_CODE(EN_MALL/MEM_GRADE_CD)에서 옴 — 아래 '1'은 대표값(CRMDW 복구 후 실제 등급코드로 치환).
SELECT A.MEMBER_NO AS CUST_ID
FROM CRM_MB_MONTHCRMINFO A WITH(NOLOCK)
WHERE A.YYYYMM = (SELECT MAX(YYYYMM) FROM CRM_MB_MONTHCRMINFO)
  AND A.RECENT_YEAR_FIRST_BUY_DATE > ''
  AND A.ZTS_GRADE IN ('1');   -- '1'은 대표값(ZTS_GRADE 실제 코드로 치환)

-- 3. [CRMDW] 마케팅 발송 대상 회원기본정보(정상/수신동의/블랙리스트·쇼핑몰회원 제외) + 발송채널 버킷
-- 주: 회원상태 정상·휴면N·제3자동의Y·블랙리스트N·쇼핑몰회원N은 고정 조건. 이메일/마케팅 수신동의(@@이메일수신동의여부@@·@@마케팅수신동의여부@@ → A.EMAIL_YN='Y')는 필수.
--     PCH_CHAN_TYPE는 MEMBER_NO 6번째 자리로 발송채널(01/02/03)을 나누는 파생 컬럼. 성별(GENDER_CD)·연령(AGE)·등급(EMART_GRADE_CD)·가입경로/매장·SNS·주소 등은 선택 필터.
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

-- 4. [CRMDW] 회원기본정보 + 주소/가입매장 조인 변형 — 특정 시군구 가입매장 회원(발송채널 버킷)
-- 주: 3번과 동일 고정조건(정상/휴면N/동의Y/블랙N/쇼핑몰N). 이 변형은 주소(B, ZIP_CD=ZIP_CODE)·가입매장(C, REG_OFFSHOP_ID=OFFSHOP_ID) LEFT JOIN + GROUP BY로 집계, PCH_CHAN_TYPE는 MAX(CASE ...)로 감쌈. 필수 필터 없음(전부 선택).
--     선택 필터: 수신동의(EMAIL_YN/SMS_YN)/등급/가입경로·주거래지점/거래브랜드/가입매장(C.TRANSFER_OFFSHOP_ID)·지역(C.SIGUNGU)/SNS/성별·연령/기념일/예치금·당근잔액/주소(A.ZIP_CD, B.ZIP_CODE)/로그인/자녀·혼인·임직원 등. 여기선 가입매장 지역(C.SIGUNGU)+성별로 구체화(강남구는 대표값).
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
  AND C.SIGUNGU IN (N'강남구')   -- N'강남구'는 대표값(가입매장 시군구)
  AND A.GENDER_CD IN ('F');

-- 5. [CRMAN] 회원 프로파일 분석 — 최신 기준월 최근1개월 가입 회원의 속성/등급/구매성향 코드명 조회
-- 주: 회원기본정보(A) + 월CRM정보(B, YYYYMM=최신월·DIV_ID='ALL') INNER JOIN, 주소(C, DIV_ID='NEW')·매장(D) LEFT JOIN. 코드/등급명은 CRM_CM_CODE·CRM_CM_STANDARD 서브쿼리로 디코딩.
--     선택 필터: 가입일(REG_DATE BETWEEN @@가입일@@), 가입브랜드(D.BRAND_CD IN @@가입브랜드_SSG@@), 가입매장(A.REG_SHOP_CD IN @@매장_C@@), 사용자정의연령대(Commasplit @@사용자정의연령대@@), 타겟리스트($$타겟리스트$$). 여기선 가입일(최근 1개월)로 구체화, 사용자정의연령대·타겟리스트 분기는 생략(→ 6번 참고).
SELECT A.MBR_NO
     , ISNULL(A.S_JOIN_BRAND, N'미정의') AS REG_BRAND_NM
     , ISNULL(A.S_JOIN_SHOP, N'미정의') AS REG_SHOP_NM
     , ISNULL(SUBSTRING(A.REG_DATE, 1, 4) + N'년', N'미정의') AS REG_YYYY
     , ISNULL(SUBSTRING(A.REG_DATE, 5, 2) + N'월', N'미정의') AS REG_MM
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'GENDER_CD' AND CD = A.SEX_CD), N'미정의') AS SEX_NM
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'AGE_DIV2_CD' AND CD = A.AGE_DIV2_CD), N'미정의') AS AGE_DIV2_NM
     , ISNULL(C.SIDO, N'미정의') AS SIDO
     , ISNULL(C.SIGUNGU, N'미정의') AS SIGUNGU
     , ISNULL((SELECT GRADE_NAME FROM Customer_Analytics.dbo.CRM_CM_STANDARD WITH(NOLOCK) WHERE DIV_ID = 'ALL' AND GRADE_TYPE = 'WORTH' AND GRADE_CODE = B.PREV_GRD_CD), N'미정의') AS PREV_GRD_NM
     , ISNULL(B.S_MBR_GRADE, N'미정의') AS GRD_NM
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'GRADE_GROW_TYPE_CD' AND CD = B.GRD_GROW_TYPE_CD), N'미정의') AS GRD_GROW_TYPE_NM
     , ISNULL(B.S_MBR_STATUS, N'미정의') AS STAT_GRD_NM
     , ISNULL((SELECT GRADE_NAME FROM Customer_Analytics.dbo.CRM_CM_STANDARD WITH(NOLOCK) WHERE DIV_ID = 'ALL' AND GRADE_TYPE = 'R_GRD' AND GRADE_CODE = B.R_GRD_CD), N'미정의') AS R_GRD_NM
     , ISNULL((SELECT GRADE_NAME FROM Customer_Analytics.dbo.CRM_CM_STANDARD WITH(NOLOCK) WHERE DIV_ID = 'ALL' AND GRADE_TYPE = 'F_GRD' AND GRADE_CODE = B.F_GRD_CD), N'미정의') AS F_GRD_NM
     , ISNULL((SELECT GRADE_NAME FROM Customer_Analytics.dbo.CRM_CM_STANDARD WITH(NOLOCK) WHERE DIV_ID = 'ALL' AND GRADE_TYPE = 'M_GRD' AND GRADE_CODE = B.M_GRD_CD), N'미정의') AS M_GRD_NM
     , ISNULL(B.S_BUY_CHNL_COMB, N'미정의') AS BUY_CHANNEL_TYPE_NM
     , ISNULL(B.S_MAIN_BUY_CHNL_TYPE, N'미정의') AS MAIN_BUY_CHANNEL_NM
     , ISNULL(B.S_MAIN_BUY_BRAND, N'미정의') AS MAIN_BUY_BRAND_NM
     , ISNULL(B.S_MAIN_BUY_CATE, N'미정의') AS MAIN_BUY_SUB_CATEGORY_NM
     , ISNULL(B.S_MAIN_BUY_SHOP, N'미정의') AS MAIN_BUY_SHOP_NM
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'DAYS_WEEK' AND CD = B.MAIN_BUY_DAYS_WEEK_CD), N'미정의') AS MAIN_BUY_DAYS_WEEK_NM
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'YN' AND CD = A.EMPLOYEE_YN), N'미정의') AS EMPLOYEE_YN
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'YN' AND CD = A.AGREE_YN), N'미정의') AS AGREE_YN
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'YN' AND CD = A.BLACKLIST_YN), N'미정의') AS BLACKLIST_YN
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'CHANNEL_AGREE_YN' AND CD = A.EMAIL_YN), N'미정의') AS EMAIL_YN
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'CHANNEL_AGREE_YN' AND CD = A.SMS_YN), N'미정의') AS SMS_YN
     , ISNULL((SELECT CD_NAME FROM Customer_Analytics.dbo.CRM_CM_CODE WITH(NOLOCK) WHERE CD_GROUP_CD = 'CHANNEL_AGREE_YN' AND CD = A.APP_PUSH_YN), N'미정의') AS APP_PUSH_YN
     , 1 AS MBR_CNT
FROM Customer_Analytics.dbo.CRM_MB_BASE_INFO A WITH(NOLOCK)
     INNER JOIN Customer_Analytics.dbo.CRM_MB_CRM_INFO B WITH(NOLOCK)
       ON B.YYYYMM = (SELECT MAX(YYYYMM) FROM Customer_Analytics.dbo.CRM_MB_CRM_INFO)
      AND B.DIV_ID = 'ALL'
      AND A.MBR_NO = B.MBR_NO
     LEFT OUTER JOIN Customer_Analytics.dbo.CRM_CM_ADDRESS C WITH(NOLOCK)
       ON C.DIV_ID = 'NEW'
      AND A.ZIP_CD = C.ZIP_CD
     LEFT OUTER JOIN Customer_Analytics.dbo.CRM_CM_SHOP_INFO D WITH(NOLOCK)
       ON A.REG_SHOP_CD = D.SHOP_CD
WHERE A.REG_DATE BETWEEN CONVERT(varchar(8), DATEADD(MONTH, -1, GETDATE()), 112)
                     AND CONVERT(varchar(8), GETDATE(), 112);

-- 6. [CRMAN→MariaDB linked server] 특정 타겟리스트 소속 회원의 프로파일 조인 (OPENQUERY 브릿지)
-- 주: 타겟리스트 회원 목록(T_TARGETLIST_CUST)은 MariaDB에만 있어 OPENQUERY(MSSQL_TO_MARIADB74)로 조회 후 CRMAN 회원정보와 조인. 'SV0001'은 대표값(quadmax_sdz.T_TARGETLIST.TL_ID 'SV%').
SELECT A.MBR_NO
     , ISNULL(B.S_MBR_GRADE, N'미정의') AS GRD_NM
     , ISNULL(C.SIDO, N'미정의') AS SIDO
     , ISNULL(C.SIGUNGU, N'미정의') AS SIGUNGU
FROM Customer_Analytics.dbo.CRM_MB_BASE_INFO A WITH(NOLOCK)
     INNER JOIN Customer_Analytics.dbo.CRM_MB_CRM_INFO B WITH(NOLOCK)
       ON B.YYYYMM = (SELECT MAX(YYYYMM) FROM Customer_Analytics.dbo.CRM_MB_CRM_INFO)
      AND B.DIV_ID = 'ALL'
      AND A.MBR_NO = B.MBR_NO
     LEFT OUTER JOIN Customer_Analytics.dbo.CRM_CM_ADDRESS C WITH(NOLOCK)
       ON C.DIV_ID = 'NEW'
      AND A.ZIP_CD = C.ZIP_CD
     INNER JOIN OPENQUERY(MSSQL_TO_MARIADB74,
                'SELECT CUST_ID FROM T_TARGETLIST_CUST WHERE T_ID = ''SV0001'' GROUP BY CUST_ID') Z
       ON A.MBR_NO = Z.CUST_ID;
