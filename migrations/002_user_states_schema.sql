-- =====================================================================
-- 사용자 상태(본건·거래사례·추정가 등) 클라우드 동기화 스키마
-- 적용 위치: Supabase SQL Editor → New query → 전체 복사 → RUN
-- 프로젝트: uzpmaunjjxaysswptvmi
--
-- 목적: 프런트엔드 state(properties/comparables/auctions/rights/pricing/
--       scenarios/opinion)를 user_id 단위로 저장하여, 어느 PC·모바일에서
--       접속해도 같은 데이터(추정가 포함)를 보게 함.
--       /api/state (GET/POST) 엔드포인트가 이 테이블을 upsert/select 함.
--
-- ⚠️ 이 테이블이 없으면 /api/state POST 가 실패하여 클라우드 공유가
--    동작하지 않고 각 브라우저 localStorage 에만 남습니다.
-- =====================================================================

-- ---------------------------------------------------------------------
-- user_states : user_id 당 전체 앱 상태 스냅샷(JSONB) 1행
--   - 기본 공용 모드: user_id = 'kiwoom-team' (전원 공유, 시연용)
--   - 지정 모드: URL ?user=xxx 로 접속 시 해당 user_id 로 분리 저장
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_states (
    user_id     VARCHAR(100) PRIMARY KEY,      -- 'kiwoom-team' 또는 ?user=지정값
    state       JSONB        NOT NULL,          -- 앱 전체 state 스냅샷
    updated_at  TIMESTAMPTZ  DEFAULT NOW()      -- last-write-wins 비교용
);

-- upsert 시 updated_at 을 자동 갱신 (last-write-wins 정확도 향상)
CREATE OR REPLACE FUNCTION set_user_states_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_user_states_updated_at ON user_states;
CREATE TRIGGER trg_user_states_updated_at
    BEFORE INSERT OR UPDATE ON user_states
    FOR EACH ROW EXECUTE FUNCTION set_user_states_updated_at();

-- ---------------------------------------------------------------------
-- RLS 비활성화 (서비스 키로 백엔드에서만 접근)
-- ---------------------------------------------------------------------
ALTER TABLE user_states DISABLE ROW LEVEL SECURITY;

-- ---------------------------------------------------------------------
-- 검증 쿼리 (생성 후 실행해 보세요)
-- ---------------------------------------------------------------------
-- SELECT table_name FROM information_schema.tables
-- WHERE table_schema = 'public' AND table_name = 'user_states';
-- SELECT user_id, updated_at, jsonb_typeof(state) FROM user_states;
