-- 002_backtest_history.sql
-- 추정 정확도 백테스트 주간 스냅샷 이력 (대시보드 추이 표시용)
-- POST /api/auction/backtest/snapshot 이 run_date 기준 upsert.

CREATE TABLE IF NOT EXISTS backtest_history (
    run_date        date PRIMARY KEY,          -- 스냅샷 실행일 (하루 1건, 재실행 시 덮어씀)
    holdout_start   date,                       -- 검증셋 시작일
    holdout_months  int,
    train_months    int,
    tested_n        int,                        -- 검증에 사용된 매각건 수
    mape            numeric(6,2),               -- 평균 절대 오차율(%)
    bias            numeric(6,2),               -- 예측−실제 편향(%p)
    rmse            numeric(6,2),
    coverage        numeric(5,1),               -- 실제가 P25~P75 구간에 든 비율(%)
    by_use_group    jsonb,                      -- 용도별 지표
    created_at      timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_backtest_history_run_date ON backtest_history (run_date DESC);

-- 서버는 service_role 키로 접근하므로 RLS는 비활성(다른 매핑 테이블과 동일 정책).
ALTER TABLE backtest_history DISABLE ROW LEVEL SECURITY;
