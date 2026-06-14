/* =========================================================================
 *  낙찰가율 캐스케이드 리졸버  (bid_rate_resolver.js)
 *  -----------------------------------------------------------------------
 *  가장 구체적인 근거(동일단지) → 일반(전국 디폴트) 순으로 내려가며,
 *  표본 임계치를 만족하는 첫 단계의 값을 채택한다.
 *
 *  의존성 없음. <script src="bid_rate_resolver.js"></script> 한 줄로 사용.
 *  window.resolveBidRate(input) 로 호출.
 * ========================================================================= */
(function (global) {
  'use strict';

  // 기본 파라미터 (호출 시 input.config 로 일부만 덮어쓸 수 있음)
  var DEFAULT_CONFIG = {
    windowMonths: 24,     // 낙찰사례 인정 기간(개월)
    minSameComplex: 3,    // 1단계 동일단지 최소 표본
    minDong: 10,          // 2단계 동(洞) 최소 표본
    minSigungu: 20,       // 3단계 시군구(자체) 최소 표본
    spread: 5,            // 보수/낙관 시나리오 ±%p
    minRate: 30,          // 이상치 하한컷(%) — 미만은 사례에서 제외
    maxRate: 130,         // 이상치 상한컷(%) — 초과는 사례에서 제외
    defaultScenarios: { conservative: 85, base: 90, optimistic: 95 }
  };

  // 단계 메타 (UI 라벨/색상 매핑용으로도 활용)
  var TIER_META = {
    same_complex:    { rank: 1, source: '동일단지 실제 낙찰사례' },
    dong:            { rank: 2, source: '동(洞) 평균' },
    sigungu:         { rank: 3, source: '시군구 평균(자체 누적)' },
    sigungu_external:{ rank: 4, source: '시군구 통계(외부)' },
    default:         { rank: 5, source: '전국 디폴트' }
  };

  function median(nums) {
    if (!nums.length) return null;
    var s = nums.slice().sort(function (a, b) { return a - b; });
    var m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }

  function monthsBetween(past, now) {
    return (now.getFullYear() - past.getFullYear()) * 12 +
           (now.getMonth() - past.getMonth());
  }

  function round1(v) { return Math.round(v * 10) / 10; }

  /**
   * @param {Object} input
   *   input.target        {complexId, dong, sigungu}  본건 식별자
   *   input.comps         [{complexId, dong, sigungu, bidRate, saleDate}]  누적 낙찰사례
   *   input.externalStats {"시흥시": 84.3, ...}  시군구 외부통계(선택, 4단계용)
   *   input.asOf          기준일(선택, 기본 오늘)
   *   input.config        파라미터 일부 덮어쓰기(선택)
   * @returns {Object} {tier, source, rank, sampleN, centerRate, scenarios, label, candidates}
   */
  function resolveBidRate(input) {
    input = input || {};
    var cfg = Object.assign({}, DEFAULT_CONFIG, input.config || {});
    var now = input.asOf ? new Date(input.asOf) : new Date();
    var target = input.target || {};
    var ext = input.externalStats || null;

    // 1) 기간·이상치 필터
    var comps = (input.comps || []).filter(function (c) {
      if (typeof c.bidRate !== 'number' || isNaN(c.bidRate)) return false;
      if (c.bidRate < cfg.minRate || c.bidRate > cfg.maxRate) return false;
      if (c.saleDate && monthsBetween(new Date(c.saleDate), now) > cfg.windowMonths) return false;
      return true;
    });

    // 2) 단계별 후보 집합
    var sameComplex = comps.filter(function (c) { return target.complexId && c.complexId === target.complexId; });
    var sameDong    = comps.filter(function (c) { return target.dong && c.dong === target.dong; });
    var sameSigungu = comps.filter(function (c) { return target.sigungu && c.sigungu === target.sigungu; });

    // UI에서 각 단계의 충족 여부를 보여주기 위한 후보 요약
    var candidates = {
      same_complex:     { n: sameComplex.length, need: cfg.minSameComplex, met: sameComplex.length >= cfg.minSameComplex },
      dong:             { n: sameDong.length,    need: cfg.minDong,        met: sameDong.length >= cfg.minDong },
      sigungu:          { n: sameSigungu.length, need: cfg.minSigungu,     met: sameSigungu.length >= cfg.minSigungu },
      sigungu_external: { n: null, need: null,   met: !!(ext && target.sigungu && typeof ext[target.sigungu] === 'number') },
      default:          { n: null, need: null,   met: true }
    };

    // 3) 캐스케이드 — 위에서부터 첫 충족 단계 채택
    var tier, sampleN, center;
    if (candidates.same_complex.met) {
      tier = 'same_complex'; sampleN = sameComplex.length; center = median(sameComplex.map(function (c) { return c.bidRate; }));
    } else if (candidates.dong.met) {
      tier = 'dong'; sampleN = sameDong.length; center = median(sameDong.map(function (c) { return c.bidRate; }));
    } else if (candidates.sigungu.met) {
      tier = 'sigungu'; sampleN = sameSigungu.length; center = median(sameSigungu.map(function (c) { return c.bidRate; }));
    } else if (candidates.sigungu_external.met) {
      tier = 'sigungu_external'; sampleN = null; center = ext[target.sigungu];
    } else {
      tier = 'default'; sampleN = null; center = cfg.defaultScenarios.base;
    }

    // 4) 시나리오 생성 (디폴트 단계만 고정 85/90/95, 그 외엔 중심값 ±spread)
    var scenarios;
    if (tier === 'default') {
      scenarios = Object.assign({}, cfg.defaultScenarios);
    } else {
      var clamp = function (v) { return round1(Math.max(cfg.minRate, Math.min(cfg.maxRate, v))); };
      scenarios = {
        conservative: clamp(center - cfg.spread),
        base:         clamp(center),
        optimistic:   clamp(center + cfg.spread)
      };
    }

    var meta = TIER_META[tier];
    var label = (sampleN != null)
      ? '기준: ' + meta.source + ' (N=' + sampleN + ', 최근 ' + cfg.windowMonths + '개월)'
      : '기준: ' + meta.source;

    return {
      tier: tier,
      source: meta.source,
      rank: meta.rank,
      sampleN: sampleN,
      centerRate: scenarios.base,
      scenarios: scenarios,
      label: label,
      candidates: candidates,
      config: cfg
    };
  }

  /* -------------------------------------------------------------------------
   *  (선택) 차수별 저감 연동 헬퍼
   *  감정가·저감률·예상 낙찰차수를 주면 차수별 최저매각가와
   *  시나리오별 예상낙찰가(감정가 기준)를 함께 돌려준다.
   *  ※ 기존 '차수별 저감 시뮬레이션'이 있으면 scenarios만 넘겨 쓰면 됨.
   * ----------------------------------------------------------------------- */
  function applyToRounds(opts) {
    var appraisal = opts.appraisalPrice;            // 감정가(원)
    var stepRate  = (opts.reductionRate != null ? opts.reductionRate : 0.30); // 차수당 저감률(서울 0.20 / 인천 등 0.30)
    var rounds    = opts.rounds || 5;               // 표시 차수 수
    var scenarios = opts.scenarios;                 // resolveBidRate(...).scenarios

    var table = [];
    for (var n = 1; n <= rounds; n++) {
      table.push({
        round: n,
        minPrice: Math.round(appraisal * Math.pow(1 - stepRate, n - 1)) // n차 최저매각가
      });
    }
    // 낙찰가율은 '감정가 대비' 비율이므로 예상낙찰가는 감정가 × 낙찰가율
    var expected = {
      conservative: Math.round(appraisal * scenarios.conservative / 100),
      base:         Math.round(appraisal * scenarios.base / 100),
      optimistic:   Math.round(appraisal * scenarios.optimistic / 100)
    };
    return { rounds: table, expected: expected };
  }

  global.resolveBidRate = resolveBidRate;
  global.applyBidRateToRounds = applyToRounds;
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = { resolveBidRate: resolveBidRate, applyBidRateToRounds: applyToRounds };
  }
})(typeof window !== 'undefined' ? window : this);
