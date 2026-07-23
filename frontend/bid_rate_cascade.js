/* =========================================================================
 *  낙찰가율 캐스케이드 + 화면 보정 모듈  v5  (bid_rate_cascade.js)
 *  -----------------------------------------------------------------------
 *  index.html 본문 수정 없음. </body> 위(또는 kfi_trade_collector.js 아래)에
 *      <script src="/bid_rate_cascade.js"></script>  한 줄. (덮어쓰기 업로드)
 *
 *  단계: 1 동일단지(N≥3) → 2 시군구사례(N≥5) → 3 시군구통계 → 4 시도통계 → 5 전국
 *  v5: (1) 좌측 '분석 도구' 헤더+NPL메뉴 제거, (3) 서울 자치구 표 추가
 *      → 자치구(도봉구 등)가 있으면 '서울 전체'보다 먼저 적용됨.
 * ========================================================================= */
(function () {
  'use strict';

  /* 낙찰가율 통계표는 auction_rates.js 의 window.AUCTION_RATES(한국부동산원)를 사용한다.
     (과거 지지옥션 기준 REGION_RATES 하드코딩은 실제로 참조되지 않아 제거함 — 유지보수 혼선 방지) */

  var CFG = {
    minSameComplex: 3, minSigungu: 5, minDong: 5, simMinNeff: 3, spread: 5,
    minRate: 30, maxRate: 130,
    statMin: 45, statMax: 115,   // 통계표(용도무관 종합) 셀의 신뢰 가능 범위 — 벗어나면 이상치로 보고 상위 통계로 폴백
    asofWarnMonths: 3,           // 적용 낙찰가율 기준월이 이 개월수 이상 지나면 신선도 경고 표시
    def: { con: 85, mid: 90, agg: 95 }
  };

  /* 신뢰도 등급: 유효표본수(N_eff)로 상/중/하. 표본 얇으면 '수기 검토 권장' 경고. */
  function _confGrade(nEff) {
    if (nEff == null || isNaN(nEff)) return null;
    if (nEff >= 8) return { grade: '상', color: '#0f766e', label: '표본 충분' };
    if (nEff >= 4) return { grade: '중', color: '#a8884a', label: '표본 보통' };
    return { grade: '하', color: '#b45309', label: '표본 부족 · 수기 검토 권장' };
  }
  var PINK = 'var(--kiwoom-pink-deep, #CC00CC)';

  /* ===== 용도 계수 — 전국 용도별 낙찰가율(INFOCARE 통계) ÷ 아파트(98.1%) 실측 비율 =====
     아파트=기준(1.00). 다세대 78.6%→0.80 · 연립 67.9%(rh는 다세대 위주라 0.80) ·
     오피스텔(주거) 76.3%→0.78 · 단독·다가구 ~73%→0.74. (지역 실측이 있으면 이 계수 미적용) */
  var USE_FACTOR = { apt: 1.00, rh: 0.80, offi: 0.78, sh: 0.74 };
  function _useFactor(use) {
    var u = (use || '').replace(/\s/g, '');
    if (/오피스텔/.test(u)) return USE_FACTOR.offi;
    if (/다세대|연립|빌라|도시형생활|도생/.test(u)) return USE_FACTOR.rh;
    if (/단독|다가구/.test(u)) return USE_FACTOR.sh;
    return USE_FACTOR.apt;
  }
  function _useLabel(use) {
    var u = (use || '').replace(/\s/g, '');
    if (/오피스텔/.test(u)) return '오피스텔';
    if (/다세대|연립|빌라|도시형생활|도생/.test(u)) return '연립·다세대';
    if (/단독|다가구/.test(u)) return '단독·다가구';
    return '아파트';
  }
  function _useGroup(use) {
    var u = (use || '').replace(/\s/g, '');
    if (/오피스텔/.test(u)) return 'offi';
    if (/다세대|연립|빌라|도시형생활|도생/.test(u)) return 'rh';
    if (/단독|다가구/.test(u)) return 'sh';
    return 'apt';
  }

  /* ===== 실측 낙찰가율 (법원경매 매각결과 축적 DB) =====
     캐스케이드는 동기이므로, 물건 선택 시 prefetch로 캐시에 채운 뒤 읽는다.
     실측이 있으면 근사 용도계수 대신 실측 중앙값·사분위를 사용한다. */
  var _realCache = {};   // key: use_group|sido|sigungu|dong → stat | null
  function _rsKey(g, sido, sgg, dong) { return g + '|' + (sido || '') + '|' + (sgg || '') + '|' + (dong || ''); }

  // 주소 → {sido, sigungu, dong} 자체 파싱 (외부 파서 필드명에 의존하지 않음)
  function _parseRegion(addr) {
    var a = (addr || '').trim();
    var m3 = a.match(/^(\S+?(?:특별시|광역시|특별자치시|특별자치도|남도|북도|자치도|도))\s+(\S+?(?:시|군|구))\s+(\S+?(?:동|읍|면|리|가|로))(?=\s|\d|$)/);
    if (m3) return { sido: m3[1], sigungu: m3[2], dong: m3[3] };
    var m = a.match(/^(\S+?(?:특별시|광역시|특별자치시|특별자치도|남도|북도|자치도|도))\s+(\S+?(?:시|군|구))(?:\s|$)/);
    if (m) return { sido: m[1], sigungu: m[2], dong: '' };
    var m2 = a.match(/^(\S+?(?:특별시|광역시|특별자치시|특별자치도|남도|북도|자치도|도))/);
    return { sido: m2 ? m2[1] : '', sigungu: '', dong: '' };
  }

  // 대상 물건의 '현재 유찰횟수' — 아직 낙찰 안 된 활성 경매의 failedCount.
  // (활성 건이 없으면 첫 사건 기준) 값이 없으면 null → 유찰보정 미적용.
  function _activeFailCount(list) {
    var arr = list || [];
    var act = null;
    for (var i = 0; i < arr.length; i++) { if (arr[i] && !arr[i].winningBid) { act = arr[i]; break; } }
    if (!act && arr.length) act = arr[0];
    if (!act) return null;
    var f = parseInt(act.failedCount, 10);
    return (isNaN(f) || f < 0) ? null : f;
  }

  function _realStat(p) {
    var g = _useGroup(p.use || p.usage);
    var rg = _parseRegion(p.addrLot || p.addrRoad || '');
    var v = _realCache[_rsKey(g, rg.sido, rg.sigungu, rg.dong)];
    return v || null;
  }

  // 물건 선택/저장 시 호출 → 실측 통계 미리 로드 (없으면 조용히 근사계수로 폴백)
  async function prefetchRealRates(p) {
    if (!p || typeof window.BACKEND_URL !== 'string') return;
    var g = _useGroup(p.use || p.usage);
    var rg = _parseRegion(p.addrLot || p.addrRoad || '');
    var key = _rsKey(g, rg.sido, rg.sigungu, rg.dong);
    if (key in _realCache) return;
    try {
      var _area = (p.exclusiveArea || p.supplyArea || '') + '';
      var qs = new URLSearchParams({ use_group: g, sido: rg.sido, sigungu: rg.sigungu, dong: rg.dong || '', area: _area, months: '12', min_n: '5' });
      var r = await fetch(window.BACKEND_URL + '/api/auction/rates?' + qs.toString());
      var d = await r.json();
      _realCache[key] = (d && d.available) ? {
        median: d.median_rate, p25: d.p25_rate, p75: d.p75_rate,
        n: d.sample_n, scope: d.scope, asof: d.asof,
        region: d.region, periodLabel: d.period_label, derivation: d.derivation,
        sido: d.sido, sigungu: d.sigungu,
        failSlope: d.fail_slope, failRef: d.fail_ref, failLevels: d.fail_levels,
        dongName: d.dong, dongRate: d.dong_rate, dongP25: d.dong_p25, dongP75: d.dong_p75,
        dongN: d.dong_n, dongRefFail: d.dong_ref_fail,
        simRate: d.sim_rate, simP25: d.sim_p25, simP75: d.sim_p75,
        simN: d.sim_n, simNeff: d.sim_neff, simRefFail: d.sim_ref_fail,
      } : null;
    } catch (e) { _realCache[key] = null; }
  }
  window.prefetchRealRates = prefetchRealRates;

  /* ===== 유틸 ===== */
  function parseSigungu(addr) {
    if (!addr) return '';
    var toks = String(addr).match(/[가-힣]+(?:특별시|광역시|특별자치시|특별자치도|시|군|구)/g) || [];
    var gu = toks.filter(function (t) { return t.slice(-1) === '구'; }).pop();
    if (gu) return gu;
    var si = toks.filter(function (t) { var c = t.slice(-1); return c === '시' || c === '군'; }).pop();
    return si || '';
  }
  function parseSido(addr) {
    if (!addr) return '';
    var head = String(addr).trim().split(/\s+/)[0] || '';
    var T = [['서울','서울'],['부산','부산'],['대구','대구'],['인천','인천'],['광주','광주'],
      ['대전','대전'],['울산','울산'],['세종','세종'],['경기','경기'],['강원','강원'],
      ['충청북','충북'],['충북','충북'],['충청남','충남'],['충남','충남'],
      ['전북','전북'],['전라북','전북'],['전라남','전남'],['전남','전남'],
      ['경상북','경북'],['경북','경북'],['경상남','경남'],['경남','경남'],['제주','제주']];
    for (var i = 0; i < T.length; i++) { if (head.indexOf(T[i][0]) === 0) return T[i][1]; }
    return '';
  }
  function median(a) {
    if (!a.length) return null;
    var s = a.slice().sort(function (x, y) { return x - y; });
    var m = Math.floor(s.length / 2);
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  }
  function rateOf(x) { return (x && x.winningBid && x.appraisal) ? (x.winningBid / x.appraisal * 100) : null; }
  function ok(r) { return typeof r === 'number' && !isNaN(r) && r >= CFG.minRate && r <= CFG.maxRate; }
  function round1(v) { return Math.round(v * 10) / 10; }
  // 기준월(YYYY-MM 또는 YYYY-MM-DD) → 현재로부터 경과 개월수. 파싱 실패 시 null.
  function asofMonths(asof) {
    var m = String(asof || '').match(/(\d{4})-(\d{2})/);
    if (!m) return null;
    var now = new Date();
    return (now.getFullYear() - parseInt(m[1], 10)) * 12 + (now.getMonth() + 1 - parseInt(m[2], 10));
  }
  function trimMean(a) {
    if (!a || !a.length) return 0;
    if (a.length >= 5) { var s = a.slice().sort(function (x, y) { return x - y; }); var m = s.slice(1, -1); return m.reduce(function (p, q) { return p + q; }, 0) / m.length; }
    return a.reduce(function (p, q) { return p + q; }, 0) / a.length;
  }
  // 🆕 동일면적 윈도 평균: 3→6→12→24개월 확장, 5건 이상 모이는 첫 구간 채택 + 최고·최저 트림
  function windowedAvg(rows) {
    var arr = (rows || []).filter(function (x) { return x && x.price && x.date; });
    if (!arr.length) return { avg: 0, windowLabel: '-', total: 0, used: 0, trimmed: false, min: 0, max: 0 };
    var now = new Date();
    function cut(m) { var d = new Date(now); d.setMonth(d.getMonth() - m); return d; }
    var picked = [], usedWindow = 0, wins = [3, 6, 12, 24];
    for (var i = 0; i < wins.length; i++) {
      var inWin = arr.filter(function (x) { return new Date(x.date) >= cut(wins[i]); });
      picked = inWin; usedWindow = wins[i];
      if (inWin.length >= 5) break;
    }
    if (!picked.length) { picked = arr.slice(); usedWindow = 0; }
    var prices = picked.map(function (x) { return x.price; }).sort(function (a, b) { return a - b; });
    var used = prices.slice(), trimmed = false;
    if (prices.length >= 5) { used = prices.slice(1, -1); trimmed = true; }
    var avg = used.length ? Math.round(used.reduce(function (s, v) { return s + v; }, 0) / used.length) : 0;
    return {
      avg: avg, windowLabel: usedWindow ? ('최근 ' + usedWindow + '개월') : '전체 기간',
      total: picked.length, used: used.length, trimmed: trimmed,
      min: prices.length ? prices[0] : 0, max: prices.length ? prices[prices.length - 1] : 0
    };
  }
  function won(m) { return (typeof fmt !== 'undefined' && fmt.won) ? fmt.won(m) : ((m || 0) + '만'); }
  function findByText(root, sel, re) {
    var f = null;
    Array.prototype.forEach.call(root.querySelectorAll(sel), function (e) { if (!f && re.test(e.textContent)) f = e; });
    return f;
  }
  function findTableByHead(root, re) {
    var f = null;
    Array.prototype.forEach.call(root.querySelectorAll('table.tbl'), function (t) {
      var h = t.querySelector('thead'); if (!f && h && re.test(h.textContent)) f = t;
    });
    return f;
  }

  /* ===== 캐스케이드 ===== */
  function resolveBidRateCascade(pid) {
    var props = (state && state.properties) || {}, aucs = (state && state.auctions) || {};
    var p = props[pid] || {};
    var addr = p.addrLot || p.addrRoad || '';
    var targetSg = parseSigungu(addr), targetSido = parseSido(addr);

    var same = (aucs[pid] || []).map(rateOf).filter(ok);
    var sg = [];
    Object.keys(aucs).forEach(function (k) {
      (aucs[k] || []).forEach(function (x) {
        var r = rateOf(x); if (!ok(r)) return;
        var s = parseSigungu(x.address || '') || (k === pid ? targetSg : '');
        if (targetSg && s === targetSg) sg.push(r);
      });
    });

    // 주거용 종합 낙찰가율로 비현실적인 통계 셀(소표본·특수매각 오염: 예 옹진군 304%, 상주 122%)은
    // 신뢰 불가로 보고 null 반환 → 캐스케이드가 상위(전국) 통계로 자동 폴백한다.
    function _plausible(v) { return typeof v === 'number' && v >= CFG.statMin && v <= CFG.statMax; }
    function _statSgg(sido, sgg) {
      var R = window.AUCTION_RATES;
      if (!R || !R.sigungu || !sido || !sgg) return null;
      var m = R.sigungu[sido]; if (!m) return null;
      if (m[sgg] != null) return _plausible(m[sgg]) ? { rate: m[sgg], asof: R.asof } : null;
      for (var k in m) { if (k.length >= sgg.length && k.slice(-sgg.length) === sgg) return _plausible(m[k]) ? { rate: m[k], asof: R.asof } : null; }
      return null;
    }
    function _statSido(sido) {
      var R = window.AUCTION_RATES;
      if (!R || !R.sido_avg || !sido) return null;
      var v = R.sido_avg[sido];
      return (v != null && v !== '-') ? { rate: parseFloat(v), asof: R.asof } : null;
    }
    var extSg = _statSgg(targetSido, targetSg);
    var extNat = _statSido('전국');

    var tier, center, asof = null, isStat = false, sampleN = null;
    if (same.length >= CFG.minSameComplex) { tier = 'same_complex'; sampleN = same.length; center = median(same); }
    else if (sg.length >= CFG.minSigungu) { tier = 'sigungu'; sampleN = sg.length; center = median(sg); }
    else if (extSg) { tier = 'stat_sigungu'; center = extSg.rate; asof = extSg.asof; isStat = true; }
    else if (extNat) { tier = 'stat_national'; center = extNat.rate; asof = extNat.asof; isStat = true; }
    else { tier = 'default'; center = CFG.def.mid; }

    // 용도 계수 (근사) — 실측 통계가 없을 때만 적용. 실측이 있으면 그 값을 그대로 쓴다.
    var useFactor = _useFactor(p.use || p.usage);
    var real = _realStat(p);   // {median, p25, p75, n, ..., dongRate, dongN, dongRefFail} | null
    var usedReal = false;
    var failDelta = 0, failAdj = null;   // 유찰횟수 보정(실측 단계 전용)
    var _p25 = null, _p75 = null;        // 채택 분포의 사분위(시나리오용)

    // 채택 우선순위: 유사도 가중(면적×지역) → 동 단위(≥5) → 시도 실측 집계.
    // 유사도 가중은 같은 동 사례를 최대가중으로 포함하면서 면적 근접까지 반영하므로
    // 표본만 충분하면(N_eff≥3) 가장 관련성이 높다. 연립·다세대·나홀로아파트에 특히 유효.
    var useSim = !!(real && real.simRate != null && real.simNeff != null && real.simNeff >= CFG.simMinNeff);
    var useDong = !useSim && !!(real && real.dongRate != null && real.dongN >= CFG.minDong);
    var effN = null;
    if (real && (real.median != null || useDong || useSim)) {
      var baseRate, baseRef;
      if (useSim) {
        tier = 'sim';
        baseRate = real.simRate; _p25 = real.simP25; _p75 = real.simP75;
        baseRef = (real.simRefFail != null) ? real.simRefFail : real.failRef;
        sampleN = real.simN; effN = real.simNeff;
      } else if (useDong) {
        tier = 'stat_dong';
        baseRate = real.dongRate; _p25 = real.dongP25; _p75 = real.dongP75;
        baseRef = (real.dongRefFail != null) ? real.dongRefFail : real.failRef;
        sampleN = real.dongN; effN = real.dongN;
      } else {
        tier = 'stat_real';
        baseRate = real.median; _p25 = real.p25; _p75 = real.p75;
        baseRef = real.failRef;
        sampleN = real.n; effN = real.n;
      }
      center = round1(baseRate);
      asof = real.asof; isStat = true; useFactor = 1; usedReal = true;

      // 유찰횟수 보정: 대상이 표본 평균보다 더/덜 유찰됐으면 가감. 채택 분포의 기준유찰(baseRef)
      // 대비로 센터링해 이중조정을 피한다. 기울기(넓은 표본)·기준점 없으면 미보정.
      if (real.failSlope != null && baseRef != null) {
        var _tf = _activeFailCount(aucs[pid]);
        if (_tf != null) {
          var _d = real.failSlope * (_tf - baseRef);
          _d = Math.max(-25, Math.min(15, _d));
          failDelta = _d;
          center = round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, center + _d)));
          failAdj = { targetFail: _tf, refFail: round1(baseRef), slope: real.failSlope,
                      delta: round1(center - round1(baseRate)) };
        }
      }
    } else if (tier === 'default') {
      // 지역 신호가 전혀 없을 때만 근사 용도계수 적용(아파트 기준 90% × 용도계수).
      center = round1(center * useFactor);
    } else {
      // 통계표(용도무관 종합)·관측 사례(동일단지·시군구)는 이미 용도가 섞여 있으므로
      // 아파트 기준으로 만든 근사 용도계수를 다시 곱하면 이중 할인이 된다 → 미적용.
      useFactor = 1;
      center = round1(center);
    }

    var sc;
    if (usedReal && _p25 != null && _p75 != null) {
      var cl0 = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
      // 시나리오(보수/적극)도 채택 분위수에 유찰보정폭을 함께 반영해 분포째 이동
      sc = { con: cl0(_p25 + failDelta), mid: center, agg: cl0(_p75 + failDelta) };
    } else if (tier === 'default') {
      sc = { con: round1(CFG.def.con * useFactor), mid: center, agg: round1(CFG.def.agg * useFactor) };
    } else { var cl = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
      sc = { con: cl(center - CFG.spread), mid: cl(center), agg: cl(center + CFG.spread) }; }

    var scope;
    switch (tier) {
      case 'same_complex': scope = '본건 동일단지 낙찰사례'; break;
      case 'sigungu':      scope = (targetSg || '시군구') + ' 낙찰사례'; break;
      case 'sim':          scope = _useLabel(p.use || p.usage) + ' 유사도 가중 낙찰가율 · '
                                 + (targetSg || real.sigungu || real.region || '') + ' 등 (면적·지역 근접, 유효표본 ' + round1(real.simNeff) + ')'; break;
      case 'stat_dong':    scope = _useLabel(p.use || p.usage) + ' 실측 낙찰가율 · '
                                 + (real.dongName || '동') + ' (동 단위, n=' + real.dongN + ')'; break;
      case 'stat_real':    scope = _useLabel(p.use || p.usage) + ' 실측 낙찰가율 · '
                                 + (real.derivation || ((real.region || '전국') + ' (n=' + real.n + ')')); break;
      case 'stat_sigungu': scope = (targetSg || '시군구') + ' 통계'; break;
      case 'stat_national':scope = '전국 평균'; break;
      default:             scope = '기본값(지역 미확인)';
    }
    if (!usedReal && useFactor !== 1) scope += ' · 용도보정(근사) ' + _useLabel(p.use || p.usage) + '(×' + useFactor + ')';

    if (effN == null) effN = sampleN;   // 동일단지·시군구 사례는 사례수로 신뢰도 산정
    var conf = _confGrade(effN);

    return { tier: tier, center: sc.mid, scenarios: sc, isStat: isStat, asof: asof,
      sampleN: sampleN, scope: scope, targetSigungu: targetSg, targetSido: targetSido,
      sameComplexN: same.length, sigunguN: sg.length,
      useFactor: useFactor, useLabel: _useLabel(p.use || p.usage), usedReal: usedReal,
      failAdj: failAdj, effN: effN, conf: conf,
      rangeLo: sc.con, rangeHi: sc.agg };
  }
  window.resolveBidRateCascade = resolveBidRateCascade;

  /* ===== 낙찰가 산정: AI안(요인1.00) vs 담당자안(요인보정) → 최종 채택 =====
     04 카드·07 담당자의견·리포트가 모두 이 한 곳의 숫자를 사용한다. */
  function clampFactor(v) { v = parseFloat(v); if (isNaN(v)) return 1.00; return Math.max(0.5, Math.min(1.5, Math.round(v * 100) / 100)); }
  function clampRate(v) { v = parseFloat(v); if (isNaN(v)) return null; return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); }
  function resolveBidEstimate(pid) {
    var props = (state && state.properties) || {}, aucs = (state && state.auctions) || {};
    var p = props[pid]; if (!p) return null;
    var auctions = aucs[pid] || [];
    var ap = (typeof getActiveAppraisal === 'function') ? getActiveAppraisal(p, auctions) : { value: null, source: '' };
    if (!ap || !ap.value) return null;
    var area = p.exclusiveArea || 0;
    var cas = resolveBidRateCascade(pid);
    var sc = (state.scenarios && state.scenarios[pid]) || {};
    // 가치형성요인(0.50~1.50)
    var mExt = clampFactor(sc.mgrFactorExt != null ? sc.mgrFactorExt : 1.00);
    var mInt = clampFactor(sc.mgrFactorInt != null ? sc.mgrFactorInt : 1.00);
    var mHo  = clampFactor(sc.mgrFactorHo  != null ? sc.mgrFactorHo  : 1.00);
    var mEtc = clampFactor(sc.mgrFactorEtc != null ? sc.mgrFactorEtc : 1.00);
    var factorProd = +(mExt * mInt * mHo * mEtc).toFixed(4);  // 낙찰가율 제외 요인 곱
    // 낙찰가율(%) — AI는 캐스케이드값, 담당자는 직접 조정
    var aiRatePct = cas.center;
    var mRatePct = clampRate(sc.mgrBidRate); if (mRatePct == null) mRatePct = aiRatePct;
    var aiRate = aiRatePct / 100, mRate = mRatePct / 100;
    var baseAi = ap.value;                              // 기준시세 (요인 1.00)
    var baseMgr = Math.round(ap.value * factorProd);    // 담당자 보정 기준시세 (요인 적용)
    var aiBid = Math.round(baseAi * aiRate);            // 기준시세 × 낙찰가율(AI)
    var mgrBid = Math.round(baseMgr * mRate);           // 보정시세 × 낙찰가율(담당자)
    var decision = (sc.bidDecision === 'mgr') ? 'mgr' : 'ai';
    var finalBid = decision === 'mgr' ? mgrBid : aiBid;
    return {
      ap: ap, area: area, cas: cas, rate: aiRate,
      unitPrice: area ? Math.round(ap.value / area) : 0,
      mExt: mExt, mInt: mInt, mHo: mHo, mEtc: mEtc, factorProd: factorProd,
      aiRatePct: aiRatePct, mRatePct: mRatePct,
      baseAi: baseAi, baseMgr: baseMgr,
      aiBid: aiBid, mgrBid: mgrBid,
      decision: decision, decisionLabel: (decision === 'mgr' ? '담당자안' : 'AI안'),
      finalBid: finalBid
    };
  }
  window.resolveBidEstimate = resolveBidEstimate;

  var TIER = {
    same_complex: ['#0f6e5c', '1단계 · 동일단지'], sigungu: ['#1e2a44', '2단계 · 시군구 사례'],
    sim: ['#0e7490', '유사도 가중 실측'],
    stat_dong: ['#0b7a53', '동 단위 실측'],
    stat_real: ['#0f766e', '실측 낙찰가율'],
    stat_sigungu: ['#1e3a5f', '3단계 · 시군구 통계'],
    stat_national: ['#5a6b8c', '4단계 · 전국 통계'], default: ['#a8884a', '디폴트']
  };

  /* ===== 시나리오 자동정렬: 중립=평균, 보수=평균-5, 적극=평균+5 ===== */
  window.__alignedPids = window.__alignedPids || {};
  function autoAlign(pid) {
    if (!pid || window.__alignedPids[pid]) return false;
    if (!state.properties || !state.properties[pid]) return false;
    var cas = resolveBidRateCascade(pid);
    var c = cas.center, lo = round1(c - CFG.spread), hi = round1(c + CFG.spread);
    state.scenarios = state.scenarios || {};
    var sc = state.scenarios[pid];
    var isDefault = !sc || (sc.con == null && sc.mid == null && sc.agg == null)
      || (sc.con === 85 && sc.mid === 90 && sc.agg === 95);
    window.__alignedPids[pid] = true;
    if (isDefault) {
      var next = { con: lo, mid: c, agg: hi };
      if (sc) { for (var k in sc) { if (k !== 'con' && k !== 'mid' && k !== 'agg') next[k] = sc[k]; } }
      state.scenarios[pid] = next;
      return true;
    }
    return false;
  }

  /* ===== 기준시세 산출근거 — 거래사례 × 공시가격 개별보정 (연립·다세대 핵심) ===== */
  function _compAdjHTML(pid) {
    var ca = (window._compAdj || {})[pid];
    if (!ca) return '';
    if (!ca.available) {
      return '<div class="text-small text-muted" style="margin-top:10px;padding:9px 11px;background:#f8fafc;border:1px solid var(--line,#e2e8f0);border-radius:6px;">'
        + 'ℹ️ 거래사례×공시 개별보정 미적용 — ' + (ca.reason || '조건 부족') + '. (감정가·거래사례로 추정합니다)</div>';
    }
    var rows = (ca.comps || []).map(function (c) {
      var used = c.used;
      return '<tr style="' + (used ? '' : 'opacity:.45;') + '">'
        + '<td>' + (c.name || '-') + ' <span class="text-muted">' + (c.floor ? c.floor + '층' : '') + '</span></td>'
        + '<td class="num">' + (c.date || '-') + '</td>'
        + '<td class="num">' + (c.area != null ? c.area + '㎡' : '-') + '</td>'
        + '<td class="num">' + (c.price ? won(c.price) : '-') + '</td>'
        + '<td class="num">' + (c.gongsi != null ? won(Math.round(c.gongsi / 10000)) : '-') + '</td>'
        + '<td class="num">' + (c.ratio != null ? '×' + c.ratio : '-') + '</td>'
        + '<td class="num">' + (c.adjusted != null ? '<b>' + won(c.adjusted) + '</b>' : '<span class="text-muted">' + (c.reason || '제외') + '</span>') + '</td>'
        + '</tr>';
    }).join('');
    return '<div style="margin-top:14px;padding:12px 14px;border:1px solid var(--line,#e2e8f0);border-radius:8px;background:#f8fafc;">'
      + '<div style="font-weight:700;font-size:13px;margin-bottom:6px;">📐 기준시세 산출근거 · 거래사례 × 공시가격 개별보정</div>'
      + '<div class="text-small" style="margin-bottom:9px;">본건 공시가격 <b>' + won(ca.origin_gongsi_manwon) + '</b> × 종합배율 <b>×' + ca.ratio + '</b>'
      + ' <span class="text-muted">(실거래÷공시, ' + ca.n_used + '/' + ca.n_total + '건 가중종합·유효표본 ' + (ca.neff != null ? ca.neff : '-') + ')</span>'
      + ' = 추정시세 <b style="color:' + PINK + '">' + won(ca.estimate_manwon) + '</b>'
      + ' <span class="text-muted">(범위 ' + won(ca.estimate_lo_manwon) + ' ~ ' + won(ca.estimate_hi_manwon) + ')</span></div>'
      + '<div style="overflow-x:auto;"><table class="tbl" style="font-size:12px;min-width:560px;"><thead><tr>'
      + '<th>거래사례</th><th>거래일</th><th class="text-right">면적</th><th class="text-right">실거래가</th><th class="text-right">사례공시</th><th class="text-right">배율</th><th class="text-right">보정후시세</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
      + '<div class="text-small text-muted" style="margin-top:6px;">배율 = 실거래가 ÷ 사례 공시가격. 본건공시 × 배율 = 보정후 시세. '
      + '층·호·면적 등 개별성은 <b>본건 공시가격</b>에 이미 반영됩니다. 사례엔 호 정보가 없어 공시는 지번·면적 근사입니다(참고).</div>'
      + '</div>';
  }

  /* ===== (2) 04 경공매 캐스케이드 카드 ===== */
  function cardHTML(pid) {
    var be = resolveBidEstimate(pid); if (!be) return '';
    var cas = be.cas, ap = be.ap, area = be.area;
    var unitPrice = be.unitPrice, baseValue = be.baseAi, bidRate = be.rate;
    var badge = TIER[cas.tier] || TIER.default;
    var dec = be.decision;
    var factorProd = be.factorProd;

    var note;
    if (cas.tier === 'sim') {
      note = '<div class="text-small text-muted">🎯 낙찰가율은 <strong>' + cas.scope + '</strong>입니다. 본건과 <strong>면적이 비슷하고 가까운 지역</strong>(같은 동 > 같은 구 > 같은 시)의 실측 낙찰사례에 <strong>가중치</strong>를 줘 산출했습니다 — 연립·다세대·나홀로아파트처럼 사례가 얇고 개별성이 큰 물건에 맞춘 방식입니다.</div>';
    } else if (cas.tier === 'stat_dong') {
      note = '<div class="text-small text-muted">📍 낙찰가율은 <strong>' + cas.scope + '</strong> ' + cas.sampleN + '건의 중앙값입니다 (본건과 같은 동·용도의 실측 낙찰사례 우선 적용).</div>';
    } else if (cas.isStat) {
      note = '<div class="text-small text-muted">📊 낙찰가율은 <strong>' + cas.scope + '</strong> 종합 낙찰가율입니다 (한국부동산원 법원경매통계 ' + cas.asof + ', 용도무관). 아파트는 종합보다 다소 높을 수 있습니다.</div>';
    } else if (cas.tier === 'default') {
      note = '<div class="text-small" style="color:var(--warn);">⚠️ 소재지에서 지역을 못 읽어 기본값(90%)을 적용했습니다. 주소를 확인하세요.</div>';
    } else {
      note = '<div class="text-small text-muted">📍 본건 <strong>' + cas.scope + '</strong> ' + cas.sampleN + '건의 중앙값입니다.</div>';
    }
    // 유찰횟수 보정 근거 표기 (실측 단계에서 보정이 적용됐을 때만)
    if (cas.failAdj && cas.failAdj.delta !== 0) {
      var _fa = cas.failAdj;
      note += '<div class="text-small text-muted">↳ 유찰 ' + _fa.targetFail + '회 반영: '
        + (_fa.delta >= 0 ? '+' : '') + _fa.delta + '%p (표본 평균 ' + _fa.refFail + '회 대비, 유찰 1회당 '
        + (_fa.slope >= 0 ? '+' : '') + _fa.slope + '%p)</div>';
    }
    // 신뢰도 등급 + 추정 범위 (표본이 얇을 때 과신을 막기 위해 항상 범위로 함께 제시)
    if (cas.conf && cas.rangeLo != null && cas.rangeHi != null) {
      var _loAmt = Math.round(baseValue * cas.rangeLo / 100);
      var _hiAmt = Math.round(baseValue * cas.rangeHi / 100);
      note += '<div class="text-small" style="margin-top:7px;padding-top:7px;border-top:1px dashed var(--line,#dfe4ee);">'
        + '<span style="display:inline-block;padding:1px 9px;border-radius:10px;font-weight:700;color:#fff;background:' + cas.conf.color + ';">신뢰도 ' + cas.conf.grade + '</span> '
        + '<span class="text-muted">' + cas.conf.label + (cas.effN != null ? ' · 유효표본 ' + round1(cas.effN) + '건' : '') + '</span>'
        + '<div class="text-muted" style="margin-top:4px;">추정 낙찰가율 범위 <strong>' + round1(cas.rangeLo) + '~' + round1(cas.rangeHi) + '%</strong> → 추정낙찰가 <strong>' + won(_loAmt) + ' ~ ' + won(_hiAmt) + '</strong> <span style="opacity:.8;">(25~75분위)</span></div>'
        + '</div>';
    }
    // 낙찰가율 기준월 신선도 — 기준월이 오래되면 과거 통계 사용 가능성을 경고(수기 검토 유도)
    if (cas.asof) {
      var _am = asofMonths(cas.asof);
      if (_am != null && _am >= CFG.asofWarnMonths) {
        note += '<div class="text-small" style="margin-top:7px;color:var(--warn,#b45309);font-weight:600;">'
          + '⚠️ 적용 낙찰가율 기준월 <strong>' + cas.asof + '</strong> · 약 ' + _am + '개월 경과. '
          + '최신 경매통계로 갱신되었는지 확인하세요(수기 검토 권장).</div>';
      }
    }

    var row = function (label, val, desc, strong) {
      return '<tr>'
        + '<td style="padding:7px 0;color:var(--ink-soft);' + (strong ? 'font-weight:700;' : '') + '">' + label + '</td>'
        + '<td style="padding:7px 0;text-align:right;font-variant-numeric:tabular-nums;font-weight:' + (strong ? '700' : '600') + ';white-space:nowrap;">' + val + '</td>'
        + '<td style="padding:7px 0 7px 14px;color:var(--ink-muted);font-size:12px;">' + (desc || '') + '</td>'
        + '</tr>';
    };

    var facInput = function (which, val) {
      return '<input type="number" min="0.5" max="1.5" step="0.05" value="' + val.toFixed(2) + '" '
        + 'onchange="window.updateBidFactor(\'' + which + '\', this.value)" '
        + 'style="width:64px;padding:4px 6px;text-align:right;font-variant-numeric:tabular-nums;border:1px solid var(--line-strong,#c2cad9);border-radius:6px;font-size:13px;font-weight:700;color:var(--ink);">';
    };
    var facRow = function (label, desc, mgrVal, which) {
      return '<tr>'
        + '<td style="padding:6px 0;color:var(--ink-soft);font-size:13px;">' + label + '<div style="color:var(--ink-muted);font-size:11px;">' + desc + '</div></td>'
        + '<td style="padding:6px 8px;text-align:right;color:var(--ink-muted);font-variant-numeric:tabular-nums;font-size:13px;">1.00</td>'
        + '<td style="padding:6px 0 6px 8px;text-align:right;">' + facInput(which, mgrVal) + '</td>'
        + '</tr>';
    };
    var rateRow = function (label, desc, aiPct, mgrPct) {
      return '<tr>'
        + '<td style="padding:6px 0;color:var(--ink-soft);font-size:13px;font-weight:600;">' + label + '<div style="color:var(--ink-muted);font-size:11px;font-weight:400;">' + desc + '</div></td>'
        + '<td style="padding:6px 8px;text-align:right;color:var(--ink-muted);font-variant-numeric:tabular-nums;font-size:13px;">' + aiPct + '%</td>'
        + '<td style="padding:6px 0 6px 8px;text-align:right;white-space:nowrap;">'
        + '<input type="number" min="' + CFG.minRate + '" max="' + CFG.maxRate + '" step="0.1" value="' + (Math.round(mgrPct * 10) / 10) + '" '
        + 'onchange="window.updateBidFactor(\'rate\', this.value)" '
        + 'style="width:60px;padding:4px 6px;text-align:right;font-variant-numeric:tabular-nums;border:1px solid var(--line-strong,#c2cad9);border-radius:6px;font-size:13px;font-weight:700;color:var(--ink);">'
        + ' <span style="font-size:12px;color:var(--ink-muted);">%</span></td>'
        + '</tr>';
    };
    var priceBox = function (key, title, amount, sub, active) {
      return '<label style="flex:1;min-width:158px;cursor:pointer;display:block;border:2px solid ' + (active ? PINK : 'var(--line,#dfe4ee)') + ';background:' + (active ? 'var(--kiwoom-pink-soft,#FFE6FF)' : 'transparent') + ';border-radius:10px;padding:12px 14px;">'
        + '<div style="display:flex;align-items:center;gap:8px;">'
        + '<input type="radio" name="bidDecision_' + pid + '" ' + (active ? 'checked' : '') + ' onchange="window.setBidDecision(\'' + pid + '\',\'' + key + '\')" style="accent-color:' + PINK + ';width:16px;height:16px;">'
        + '<span style="font-weight:700;color:var(--ink);font-size:13px;">' + title + '</span></div>'
        + '<div class="mono" style="font-size:22px;font-weight:800;color:' + (active ? PINK : 'var(--ink-soft)') + ';margin-top:6px;line-height:1.1;">' + won(amount) + '</div>'
        + '<div style="color:var(--ink-muted);font-size:11px;margin-top:3px;">' + sub + '</div>'
        + '</label>';
    };

    return ''
      + '<div class="card mb-24" data-cascade="1" style="border-left:4px solid var(--accent);">'
      + '<div class="card-title">추정 낙찰가액 '
      + '<span class="badge" style="background:' + badge[0] + ';color:#fff;">' + badge[1] + '</span>'
      + (cas.conf ? ' <span class="badge" style="background:' + cas.conf.color + ';color:#fff;">신뢰도 ' + cas.conf.grade + '</span>' : '')
      + '</div>'
      + '<div class="text-small text-muted" style="margin:-4px 0 14px;">거래사례비교법 — 기준시세 × 가치형성요인 × 낙찰가율. <strong>AI안</strong>(요인 1.00·낙찰가율 캐스케이드)과 <strong>담당자안</strong>(요인·낙찰가율 직접보정)을 산출해 둘 중 하나를 최종 채택합니다.</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
      + row('거래사례 평균단가', won(unitPrice) + '/㎡', '거래사례 평균매매가 ÷ 전용면적')
      + row('× 전용면적', (area ? area.toFixed(2) : '-') + '㎡', ap.source || '')
      + row('= 기준시세', won(baseValue), '요인·낙찰가율 적용 전', true)
      + '</table>'
      + '<div style="margin-top:16px;font-weight:700;color:var(--ink);font-size:13px;">보정 요인 <span class="text-muted" style="font-weight:400;font-size:11px;">· 담당자안: 요인 0.50~1.50, 낙찰가율 ' + CFG.minRate + '~' + CFG.maxRate + '% 직접 입력</span></div>'
      + '<table style="width:100%;border-collapse:collapse;margin-top:6px;">'
      + '<thead><tr><th style="text-align:left;font-size:11px;color:var(--ink-muted);font-weight:600;padding-bottom:6px;">항목</th><th style="text-align:center;font-size:15px;color:var(--ink);font-weight:800;padding-bottom:6px;">AI안</th><th style="text-align:center;font-size:15px;color:' + PINK + ';font-weight:800;padding-bottom:6px;">담당자안</th></tr></thead>'
      + '<tbody>'
      + facRow('단지외부요인', '교통·입지·학군·환경', be.mExt, 'ext')
      + facRow('단지내부요인', '브랜드·세대수·구조·노후도', be.mInt, 'int')
      + facRow('호별요인', '층·향·위치별 효용', be.mHo, 'ho')
      + rateRow('낙찰가율', cas.scope + (cas.asof ? ' · 한국부동산원 ' + cas.asof : ''), be.aiRatePct, be.mRatePct)
      + facRow('기타요인', '명도난이도·시장상황·급매 등 개별조정', be.mEtc, 'etc')
      + '<tr style="border-top:1px solid var(--line,#dfe4ee);"><td style="padding:6px 0;font-weight:700;font-size:13px;color:var(--ink-soft);">요인 곱 <span style="font-weight:400;color:var(--ink-muted);font-size:11px;">(낙찰가율 제외)</span></td><td style="padding:6px 8px;text-align:right;font-weight:700;font-size:13px;">1.00</td><td style="padding:6px 0;text-align:right;font-weight:700;font-size:13px;color:' + PINK + ';">' + factorProd.toFixed(2) + '</td></tr>'
      + '</tbody></table>'
      + '<div style="display:flex;gap:10px;flex-wrap:wrap;margin-top:16px;">'
      + priceBox('ai', 'AI안 채택', be.aiBid, '요인 1.00 · 낙찰가율 ' + be.aiRatePct + '%', dec === 'ai')
      + priceBox('mgr', '담당자안 채택', be.mgrBid, '요인 ' + factorProd.toFixed(2) + ' · 낙찰가율 ' + be.mRatePct + '%', dec === 'mgr')
      + '</div>'
      + '<div style="display:flex;justify-content:space-between;align-items:center;margin-top:16px;padding-top:14px;border-top:2px solid var(--line-strong,#c2cad9);">'
      + '<div style="font-weight:800;color:var(--ink);font-size:15px;">= 최종 낙찰예상가 <span style="font-size:12px;font-weight:600;color:' + PINK + ';">(' + be.decisionLabel + ' 채택)</span></div>'
      + '<div class="mono" style="font-size:26px;font-weight:800;color:' + PINK + ';line-height:1.1;">' + won(be.finalBid) + '</div>'
      + '</div>'
      + '<div class="text-small text-muted" style="margin-top:12px;">✅ 최종 채택값은 <strong>08 담당자 의견</strong> · <strong>리포트(3-2 낙찰가 산정 결정·분석요약)</strong>에 자동 반영됩니다.</div>'
      + note
      + _compAdjHTML(pid)
      + '</div>';
  }

  function injectBidEst() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    var pid = state.currentPropertyId; if (!pid) return;
    var host = document.getElementById('bidEstHost');
    var html = cardHTML(pid);
    var empty = '<div class="card"><div class="text-muted">감정가 또는 거래사례가 없어 추정할 수 없습니다. 02 거래사례 / 04 경공매 사례 탭에서 데이터를 채워주세요.</div></div>';
    if (host) { host.innerHTML = html || empty; return; }
    // host가 아직 없으면(렌더 타이밍) viewContainer 끝에 주입
    Array.prototype.forEach.call(vc.querySelectorAll('[data-cascade="1"]'), function (n) { n.remove(); });
    if (!html) return;
    var tmp = document.createElement('div'); tmp.innerHTML = html;
    vc.appendChild(tmp.firstElementChild);
  }
  window.injectBidEst = injectBidEst;

  /* 담당자 보정: 가치형성요인(0.50~1.50) / 낙찰가율(%) → 담당자안 재산출 */
  window.updateBidFactor = function (which, val) {
    var pid = state && state.currentPropertyId; if (!pid) return;
    state.scenarios = state.scenarios || {};
    state.scenarios[pid] = state.scenarios[pid] || {};
    if (which === 'rate') {
      var r = clampRate(val);
      if (r == null) delete state.scenarios[pid].mgrBidRate;
      else state.scenarios[pid].mgrBidRate = r;
    } else {
      var key = which === 'ext' ? 'mgrFactorExt' : which === 'int' ? 'mgrFactorInt' : which === 'ho' ? 'mgrFactorHo' : which === 'etc' ? 'mgrFactorEtc' : null;
      if (!key) return;
      state.scenarios[pid][key] = clampFactor(val);
    }
    if (typeof saveState === 'function') { try { saveState(); } catch (e) {} }
    injectBidEst();
  };

  /* AI안 / 담당자안 최종 채택 */
  window.setBidDecision = function (pid, val) {
    if (!pid) return;
    state.scenarios = state.scenarios || {};
    state.scenarios[pid] = state.scenarios[pid] || {};
    state.scenarios[pid].bidDecision = (val === 'mgr') ? 'mgr' : 'ai';
    if (typeof saveState === 'function') { try { saveState(); } catch (e) {} }
    injectBidEst();
  };

  /* ===== 02 거래사례 비교 보정 ===== */
  function patchComparables() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    var pid = state.currentPropertyId; if (!pid) return;
    var p = ((state.properties) || {})[pid]; if (!p) return;
    var allComps = ((state.comparables) || {})[pid] || [];
    var area = p.exclusiveArea || 0;

    // index.html과 동일한 단지 필터 적용
    var danjiList = [];
    allComps.forEach(function (x) { if (x.name && danjiList.indexOf(x.name) < 0) danjiList.push(x.name); });
    var curDanji = window._compsDanjiFilter || (danjiList.indexOf(p.name) >= 0 ? p.name : '전체');
    if (curDanji !== '전체' && danjiList.indexOf(curDanji) < 0) curDanji = '전체';
    var comps = (curDanji === '전체') ? allComps : allComps.filter(function (x) { return x.name === curDanji; });

    // 동일면적(전용 ±1㎡) 매매, 해제 제외 → 3→6→12→24개월 윈도 평균
    var sameArea = comps.filter(function (x) {
      return x.type === '매매' && Math.abs((x.area || 0) - area) < 1 && (!x.memo || x.memo.indexOf('해제') < 0);
    });
    var info = windowedAvg(sameArea);

    var grid = document.getElementById('cmpStatGrid') || vc.querySelector('.grid.grid-4');
    if (grid && grid.children.length >= 2 && !grid.getAttribute('data-boxes-patched')) {
      var b1 = grid.children[0], b2 = grid.children[1];
      var pyong = (typeof fmt !== 'undefined' && fmt.pyong) ? fmt.pyong(area) : '';
      b1.innerHTML = '<div class="stat-label">본건 (동·호)</div>'
        + '<div class="stat-value" style="font-size:20px;">' + ((p.building || '-') + ' ' + (p.unit || '')) + '</div>'
        + '<div class="stat-trend">전용 ' + (area || '-') + '㎡ ' + (pyong ? '· ' + pyong : '') + '</div>';
      b2.innerHTML = '<div class="stat-label">동일면적 매매</div>'
        + '<div class="stat-value">' + sameArea.length + '<span class="stat-unit">건</span></div>'
        + '<div class="stat-trend">전용 ' + (area || '-') + '㎡ ±1㎡</div>';
      grid.setAttribute('data-boxes-patched', '1');
    }

    // 안정적 id 훅 우선 → 못 찾으면(구버전 캐시) 그라디언트+문구 폴백
    var nplCard = document.getElementById('nplRecoveryCard');
    if (!nplCard) {
      Array.prototype.forEach.call(vc.querySelectorAll('div'), function (d) {
        if (nplCard) return;
        var st = d.getAttribute('style') || '';
        if (/linear-gradient/.test(st) && /NPL 회수 가능 금액 추정/.test(d.textContent)) nplCard = d;
      });
    }
    if (nplCard) {
      var sub = info.total
        ? (info.windowLabel + ' 매매 ' + info.total + '건'
            + (info.trimmed ? ' 중 최고·최저 제외 ' + info.used + '건' : '')
            + ' 평균 · 범위 ' + won(info.min) + ' ~ ' + won(info.max))
        : '동일면적 거래 없음';
      var card = document.createElement('div');
      card.style.cssText = 'background:linear-gradient(135deg, var(--kiwoom-navy-soft) 0%, #d4dcf0 100%);border:1px solid rgba(30,42,68,.18);border-left:4px solid var(--kiwoom-navy);border-radius:8px;padding:22px 26px;margin-bottom:16px;';
      var ppa = (info.avg && area) ? Math.round(info.avg / area) : null;   // 전용면적당(㎡당) 단가(만원)
      card.innerHTML =
        '<div style="font-size:11px;letter-spacing:.18em;color:var(--kiwoom-navy);text-transform:uppercase;font-weight:700;margin-bottom:8px;">📊 평균 매매가 · AVERAGE SALE PRICE (동일 면적대)</div>'
        + '<div style="font-family:var(--mono);font-size:32px;font-weight:800;color:var(--kiwoom-navy-deep);line-height:1.15;">' + won(info.avg)
        + (ppa ? ' <span style="font-size:15px;font-weight:700;color:var(--ink-soft,#5b6473);">· ㎡당 ' + won(ppa) + '</span>' : '') + '</div>'
        + '<div style="font-size:13px;color:var(--ink-soft,#5b6473);margin-top:6px;font-family:var(--mono);">' + sub + '</div>';
      nplCard.replaceWith(card);
    }
  }

  /* ===== 리포트 보정 ===== */
  function patchReport() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    var pid = state.currentPropertyId; if (!pid) return;
    var p = ((state.properties) || {})[pid]; if (!p) return;
    var auctions = ((state.auctions) || {})[pid] || [];

    var t2 = findTableByHead(vc, /거래일/);
    if (t2 && !t2.getAttribute('data-only-sale')) {
      var tb = t2.querySelector('tbody');
      if (tb) {
        Array.prototype.slice.call(tb.querySelectorAll('tr')).forEach(function (tr) {
          var c = tr.querySelectorAll('td');
          if (c.length >= 2 && c[1].textContent.trim() !== '매매') tr.remove();
        });
        t2.setAttribute('data-only-sale', '1');
      }
    }

    var ap = (typeof getActiveAppraisal === 'function') ? getActiveAppraisal(p, auctions) : { value: null };
    if (!ap || !ap.value) return;
    var cas = resolveBidRateCascade(pid), center = cas.center;
    var sc = ((state.scenarios) || {})[pid] || {};
    var _con = sc.con != null ? sc.con : 92, _mid = sc.mid != null ? sc.mid : 97, _agg = sc.agg != null ? sc.agg : 100;
    var amt = function (r) { return Math.round(ap.value * r / 100); };

    // 안정적 id 훅 우선 → 못 찾으면(구버전 캐시) '분석 요약' 헤더 다음 요소로 폴백
    var prose = document.getElementById('reportSummaryProse');
    if (!prose) { var _sh = findByText(vc, '.section-h', /분석\s*요약/); prose = _sh ? _sh.nextElementSibling : null; }
    if (prose && !prose.getAttribute('data-bid-summary')) {
      var line = document.createElement('div');
      line.style.cssText = 'margin-bottom:12px;padding:11px 15px;background:var(--kiwoom-pink-soft,#FFE6FF);border-left:3px solid ' + PINK + ';border-radius:0 6px 6px 0;font-weight:600;line-height:1.7;';
      var be = (typeof resolveBidEstimate === 'function') ? resolveBidEstimate(pid) : null;
      if (be) {
        line.innerHTML = '본건 최종 낙찰예상가는 <strong style="color:' + PINK + ';">' + won(be.finalBid) + '</strong> (' + be.decisionLabel + ' 채택)입니다. '
          + '<span style="font-weight:500;color:var(--ink-soft);">AI안 ' + won(be.aiBid) + ' · 담당자안 ' + won(be.mgrBid) + ' · 적용 낙찰가율 ' + center + '%(' + cas.scope + ').</span>';
      } else {
        line.innerHTML = '평균 낙찰가율(' + center + '%, ' + cas.scope + ')로 예상하는 본건 낙찰금액은 <strong style="color:' + PINK + ';">' + won(amt(center)) + '</strong>이며, '
          + '적극적 ' + won(amt(_agg)) + ' · 중립적 ' + won(amt(_mid)) + ' · 보수적 ' + won(amt(_con)) + '으로 추정됩니다.';
      }
      prose.insertBefore(line, prose.firstChild);
      prose.setAttribute('data-bid-summary', '1');
    }
  }

  /* 좌측 메뉴 재정렬(reorderNav)·NPL 메뉴 제거(removeNplMenu)는 제거함.
     index.html의 nav가 이미 data-view 속성과 번호(01~09)로 정리돼 있고 'NPL 자산 분석' 메뉴도
     없어, 두 함수는 라벨 문자열이 안 맞아 항상 no-op이던 죽은 코드였다. (텍스트 스크래핑 취약성 제거) */

  /* ===== 디스패처 ===== */
  function inject() {
    try {
      if (!state) return;
      var pid = state.currentPropertyId;
      if (autoAlign(pid)) { window.renderView(); return; }
      var v = state.currentView;
      if (v === 'bidest') injectBidEst();
      else if (v === 'comparables') patchComparables();
      else if (v === 'report') patchReport();
    } catch (e) { console.warn('[보정] 건너뜀:', e); }
  }

  function hook() {
    if (typeof window.renderView !== 'function') { setTimeout(hook, 200); return; }
    if (window.__cascadeHooked) return;
    window.__cascadeHooked = true;
    var orig = window.renderView;
    window.renderView = function () { var r = orig.apply(this, arguments); inject(); return r; };
    inject();
    console.log('[낙찰가율 캐스케이드] v7 · id 훅 기반 · 한국부동산원 시군구 종합 ' + (window.AUCTION_RATES ? window.AUCTION_RATES.asof : '미로드'));
  }

  if (document.readyState !== 'loading') setTimeout(hook, 300);
  else document.addEventListener('DOMContentLoaded', function () { setTimeout(hook, 300); });
})();
