-- =====================================================================
-- Day 8: 단지 교차검증 캐시 스키마
-- 적용 위치: Supabase SQL Editor → New query → 전체 복사 → RUN
-- 프로젝트: uzpmaunjjxaysswptvmi
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. complex_mapping : 단지 정식 식별자 캐시
--    한 번 교차검증된 단지의 mgmBldrgstPk, 신지번, 도로명주소를 영구 저장
--    K-apt 코드를 PK로 사용하여 다음 조회 시 즉시 hit
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS complex_mapping (
    kapt_code           VARCHAR(20)  PRIMARY KEY,
    mgm_bldrgst_pk      VARCHAR(50)  NOT NULL,        -- 4719025331-3-08340000
    complex_name        VARCHAR(200) NOT NULL,        -- 구미푸르지오센트럴파크
    road_addr           VARCHAR(300),                 -- 경상북도 구미시 고아읍 신원대로 7-60
    jibun_addr          VARCHAR(300),                 -- 경상북도 구미시 고아읍 원호리 834 (신지번)
    sigungu_cd          VARCHAR(5)   NOT NULL,        -- 47190
    bjdong_cd           VARCHAR(5)   NOT NULL,        -- 25331
    plat_gb_cd          CHAR(1),                      -- 0: 일반대지, 1: 산
    bun                 VARCHAR(4),                   -- 0834
    ji                  VARCHAR(4),                   -- 0000
    household_count     INTEGER,                      -- 819
    use_approval_date   DATE,                         -- 2024-04-26
    total_floors        INTEGER,                      -- 23
    verified_score      INTEGER,                      -- 0~100 매칭 신뢰도
    verified_at         TIMESTAMPTZ  DEFAULT NOW(),
    updated_at          TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_complex_mapping_bjdong
    ON complex_mapping(sigungu_cd, bjdong_cd);

CREATE INDEX IF NOT EXISTS idx_complex_mapping_name
    ON complex_mapping(complex_name);

CREATE INDEX IF NOT EXISTS idx_complex_mapping_pk
    ON complex_mapping(mgm_bldrgst_pk);


-- ---------------------------------------------------------------------
-- 2. dong_mapping : 단지 내 동(棟) 단위 식별자 캐시
--    "103동" 같은 단지 내 동의 mgmBldrgstPk를 별도로 저장하여
--    이후 표제부/전유공용면적 조회를 동 PK 기반으로 정확하게 수행
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dong_mapping (
    kapt_code           VARCHAR(20)  NOT NULL,
    dong_name           VARCHAR(20)  NOT NULL,        -- 103 또는 103동
    dong_mgm_pk         VARCHAR(50)  NOT NULL,        -- 동별 mgmBldrgstPk
    total_floors        INTEGER,                      -- 23
    household_per_dong  INTEGER,                      -- 44
    verified_at         TIMESTAMPTZ  DEFAULT NOW(),
    PRIMARY KEY (kapt_code, dong_name),
    FOREIGN KEY (kapt_code) REFERENCES complex_mapping(kapt_code) ON DELETE CASCADE
);


-- ---------------------------------------------------------------------
-- 3. complex_match_log : 매칭 시도 로그 (실패 분석 및 신뢰도 튜닝용)
--    매칭이 실패한 경우 candidates_count, error_message를 보면
--    원인이 "후보 자체가 없다" 인지 "단지명 매칭 실패" 인지 즉시 판별 가능
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS complex_match_log (
    id                  SERIAL       PRIMARY KEY,
    kapt_code           VARCHAR(20),
    attempt_at          TIMESTAMPTZ  DEFAULT NOW(),
    success             BOOLEAN,
    match_score         INTEGER,
    candidates_count    INTEGER,
    score_breakdown     JSONB,
    error_message       TEXT
);

CREATE INDEX IF NOT EXISTS idx_match_log_kapt
    ON complex_match_log(kapt_code, attempt_at DESC);

CREATE INDEX IF NOT EXISTS idx_match_log_failure
    ON complex_match_log(success, attempt_at DESC) WHERE success = FALSE;


-- ---------------------------------------------------------------------
-- 4. RLS 비활성화 (서비스 키로 백엔드에서만 접근)
-- ---------------------------------------------------------------------
ALTER TABLE complex_mapping DISABLE ROW LEVEL SECURITY;
ALTER TABLE dong_mapping DISABLE ROW LEVEL SECURITY;
ALTER TABLE complex_match_log DISABLE ROW LEVEL SECURITY;


-- ---------------------------------------------------------------------
-- 검증 쿼리 (생성 후 실행해 보세요)
-- ---------------------------------------------------------------------
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public'
--   AND table_name IN ('complex_mapping', 'dong_mapping', 'complex_match_log');
