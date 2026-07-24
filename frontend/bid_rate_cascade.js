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

  /* ===== 지역별 낙찰가율 기준표 (월 1회 갱신 · 출처: 지지옥션 경매동향) ===== */
  var REGION_RATES = {
    _asof: '2026-05',
    national: { rate: 87.3, asof: '2026-05' },
    sido: {
      '서울': { rate: 100.8, asof: '2026-05' },
      '경기': { rate: 89.0,  asof: '2026-05' },
      '인천': { rate: 79.8,  asof: '2026-05' },
      '경남': { rate: 82.1, asof: '2026-02' }, '경북': { rate: 82.1, asof: '2026-02' },
      '충북': { rate: 86.0, asof: '2026-02' }, '충남': { rate: 84.2, asof: '2026-02' },
      '전남': { rate: 80.2, asof: '2026-02' }, '전북': { rate: 84.5, asof: '2026-02' },
      '강원': { rate: 83.4, asof: '2026-02' }, '제주': { rate: 81.2, asof: '2026-02' },
      '세종': { rate: 88.1, asof: '2026-02' }
    },
    // 서울 자치구 (지지옥션 자치구별 표) — 최신월 나오면 rate/asof 교체
    sigungu: {
      '도봉구': { rate: 92.7,  asof: '2025-12' },
      '노원구': { rate: 90.8,  asof: '2025-12' },
      '양천구': { rate: 122.0, asof: '2025-12' },
      '성동구': { rate: 120.5, asof: '2025-12' },
      '강동구': { rate: 117.3, asof: '2025-12' },
      '동작구': { rate: 105.7, asof: '2025-12' },
      '동대문구': { rate: 104.6, asof: '2025-12' },
      '분당구': { rate: 115.8, asof: '2025-12' }   // 경기 성남
    }
  };

  var CFG = {
    minSameComplex: 3, minSigungu: 5, spread: 5,
    minRate: 30, maxRate: 130, def: { con: 85, mid: 90, agg: 95 }
  };
  var PINK = 'var(--kiwoom-pink-deep, #CC00CC)';

  /* ===== 용도 계수 (빠른버전) — 공표 용도별 평균 낙찰가율 비율 근사 =====
     아파트=기준(1.00). 빌라·오피스텔·단독은 아파트보다 낙찰가율이 낮음(전세사기·개별성·수요층).
     ※ 근사값. 추후 courtauction 매각결과 실측(정밀버전)으로 대체 예정. */
  var USE_FACTOR = { apt: 1.00, rh: 0.82, offi: 0.85, sh: 0.80 };
  function _useFactor(use) {
    var u = (use || '').replace(/\s/g, '');
    if (/오피스텔/.test(u)) return USE_FACTOR.offi;
    if (/다세대|연립|빌라/.test(u)) return USE_FACTOR.rh;
    if (/단독|다가구/.test(u)) return USE_FACTOR.sh;
    return USE_FACTOR.apt;
  }
  function _useLabel(use) {
    var u = (use || '').replace(/\s/g, '');
    if (/오피스텔/.test(u)) return '오피스텔';
    if (/다세대|연립|빌라/.test(u)) return '연립·다세대';
    if (/단독|다가구/.test(u)) return '단독·다가구';
    return '아파트';
  }
  function _useGroup(use) {
    var u = (use || '').replace(/\s/g, '');
    if (/오피스텔/.test(u)) return 'offi';
    if (/다세대|연립|빌라/.test(u)) return 'rh';
    if (/단독|다가구/.test(u)) return 'sh';
    return 'apt';
  }

  /* ===== 실측 낙찰가율 (법원경매 매각결과 축적 DB) =====
     캐스케이드는 동기이므로, 물건 선택 시 prefetch로 캐시에 채운 뒤 읽는다.
     실측이 있으면 근사 용도계수 대신 실측 중앙값·사분위를 사용한다. */
  var _realCache = {};   // key: use_group|sido|sigungu → stat | null
  function _rsKey(g, sido, sgg) { return g + '|' + (sido || '') + '|' + (sgg || ''); }

  // 주소 → {sido, sigungu} 자체 파싱 (외부 파서 필드명에 의존하지 않음)
  function _parseRegion(addr) {
    var a = (addr || '').trim();
    var m = a.match(/^(\S+?(?:특별시|광역시|특별자치시|특별자치도|남도|북도|자치도|도))\s+(\S+?(?:시|군|구))(?:\s|$)/);
    if (m) return { sido: m[1], sigungu: m[2] };
    var m2 = a.match(/^(\S+?(?:특별시|광역시|특별자치시|특별자치도|남도|북도|자치도|도))/);
    return { sido: m2 ? m2[1] : '', sigungu: '' };
  }

  function _realStat(p) {
    var g = _useGroup(p.use);
    var rg = _parseRegion(p.addrLot || p.addrRoad || '');
    var v = _realCache[_rsKey(g, rg.sido, rg.sigungu)];
    return v || null;
  }

  // 물건 선택/저장 시 호출 → 실측 통계 미리 로드 (없으면 조용히 근사계수로 폴백)
  async function prefetchRealRates(p) {
    if (!p || typeof window.BACKEND_URL !== 'string') return;
    // P1 추정 엔진도 함께 미리 로드(독립 실행, 실패해도 실측 경로엔 영향 없음).
    try { prefetchAuctionEstimate(p.id, p); } catch (e) {}
    var g = _useGroup(p.use);
    var rg = _parseRegion(p.addrLot || p.addrRoad || '');
    var key = _rsKey(g, rg.sido, rg.sigungu);
    if (key in _realCache) return;
    try {
      var qs = new URLSearchParams({ use_group: g, sido: rg.sido, sigungu: rg.sigungu, months: '12', min_n: '5' });
      var r = await fetch(window.BACKEND_URL + '/api/auction/rates?' + qs.toString());
      var d = await r.json();
      _realCache[key] = (d && d.available) ? {
        median: d.median_rate, p25: d.p25_rate, p75: d.p75_rate,
        n: d.sample_n, scope: d.scope, asof: d.asof,
        region: d.region, periodLabel: d.period_label, derivation: d.derivation,
        sido: d.sido, sigungu: d.sigungu,
      } : null;
    } catch (e) { _realCache[key] = null; }
  }
  window.prefetchRealRates = prefetchRealRates;

  /* ===== P1: 추정 엔진(/api/auction/estimate) =====
     최근성 가중 + 계층수축 + 유찰보정 결과를 미리 로드해 캐스케이드 최상위로 사용한다.
     실측(stat_real)보다 우선. 없으면 조용히 기존 캐스케이드로 폴백. */
  var _estCache = {};   // key: use_group|sido|sigungu|dong → est | null (유찰보정은 프론트에서 적용)

  // 주소 → {sido, sigungu, dong}. 캐스케이드의 견고한 parseSido/parseSigungu 재사용
  // (약칭 '경기' 등도 처리). dong은 시군구 뒤 첫 토큰(행정동명).
  function _parseRegion3(addr) {
    var a = (addr || '');
    var sido = parseSido(a);       // '경기', '서울' 등 (auction_sales.sido 포맷과 일치 가정)
    var sgg = parseSigungu(a);     // '시흥시', '강남구' 등
    var dong = '';
    if (sgg) {
      var idx = a.indexOf(sgg);
      if (idx >= 0) {
        var rest = a.slice(idx + sgg.length).trim();
        dong = rest ? rest.split(/\s+/)[0] : '';
      }
    }
    return { sido: sido, sigungu: sgg, dong: dong };
  }

  // 본건의 예상 유찰차수: 활성 경매객체의 failedCount(낙찰 전 건 우선)
  function _subjectFailCount(pid) {
    var aucs = (state && state.auctions && state.auctions[pid]) || [];
    if (!aucs.length) return null;
    var active = null;
    for (var i = 0; i < aucs.length; i++) { if (!aucs[i].winningBid) { active = aucs[i]; break; } }
    if (!active) active = aucs[0];
    var fc = active && active.failedCount;
    return (fc == null || isNaN(+fc)) ? null : +fc;
  }

  function _estKey(g, rg) {
    return g + '|' + (rg.sido || '') + '|' + (rg.sigungu || '') + '|' + (rg.dong || '');
  }

  // 지역·용도별 기준 추정(유찰 미보정)을 반환. 유찰보정은 resolveBidRateCascade에서 본건 차수로 적용.
  function _estStat(p) {
    var g = _useGroup(p.use);
    var rg = _parseRegion3(p.addrLot || p.addrRoad || '');
    return _estCache[_estKey(g, rg)] || null;
  }

  async function prefetchAuctionEstimate(pid, p) {
    if (!p || typeof window.BACKEND_URL !== 'string') return;
    var g = _useGroup(p.use);
    var rg = _parseRegion3(p.addrLot || p.addrRoad || '');
    if (!rg.sido && !rg.sigungu) return;   // 지역 미상이면 추정 불가
    var key = _estKey(g, rg);              // fail_count는 키에 넣지 않음(프론트 보정)
    if (key in _estCache) return;
    try {
      var qs = new URLSearchParams({ use_group: g, sido: rg.sido, sigungu: rg.sigungu, dong: rg.dong, months: '36' });
      var r = await fetch(window.BACKEND_URL + '/api/auction/estimate?' + qs.toString());
      var d = await r.json();
      _estCache[key] = (d && d.available) ? d : null;
    } catch (e) { _estCache[key] = null; }
  }
  window.prefetchAuctionEstimate = prefetchAuctionEstimate;

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

    function _statSgg(sido, sgg) {
      var R = window.AUCTION_RATES;
      if (!R || !R.sigungu || !sido || !sgg) return null;
      var m = R.sigungu[sido]; if (!m) return null;
      if (m[sgg] != null) return { rate: m[sgg], asof: R.asof };
      for (var k in m) { if (k.length >= sgg.length && k.slice(-sgg.length) === sgg) return { rate: m[k], asof: R.asof }; }
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

    // 용도 계수 (근사) — 실측/모델 통계가 없을 때만 적용.
    var useFactor = _useFactor(p.use);
    var est = _estStat(p);        // P1 추정 엔진(유찰 미보정 기준값 + 기울기) | null
    var real = _realStat(p);      // 실측 {median, p25, p75, n, ...} | null
    var usedReal = false, usedModel = false, estDelta = 0, estP25 = null, estP75 = null;

    if (est && est.point_base != null) {
      // 최우선: 추정 엔진. 본건 유찰차수로 유찰보정을 프론트에서 즉시 적용.
      tier = 'stat_model';
      var _fc = _subjectFailCount(pid);
      if (_fc != null && est.fail_slope != null && est.fail_sample_mean != null) {
        var _dc = est.delta_clamp || [-25, 8];
        estDelta = est.fail_slope * (_fc - est.fail_sample_mean);
        estDelta = Math.max(_dc[0], Math.min(_dc[1], estDelta));
        estDelta = round1(estDelta);
      }
      center = round1(est.point_base + estDelta);
      estP25 = est.p25_base + estDelta;
      estP75 = est.p75_base + estDelta;
      asof = est.asof || null; isStat = true; sampleN = est.sample_n;
      useFactor = 1; usedModel = true;
    } else if (real && real.median != null) {
      // 차선: 실측 낙찰가율(용도·지역별)
      tier = 'stat_real';
      center = round1(real.median);
      asof = real.asof; isStat = true; sampleN = real.n;
      useFactor = 1; usedReal = true;
    } else {
      center = round1(center * useFactor);
    }

    var sc;
    var clm = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
    if (usedModel && estP25 != null && estP75 != null) {
      sc = { con: clm(estP25), mid: center, agg: clm(estP75) };   // 추정 분포(P25/P75) + 유찰보정
    } else if (usedReal && real.p25 != null && real.p75 != null) {
      sc = { con: clm(real.p25), mid: center, agg: clm(real.p75) };   // 실측 분포로 시나리오
    } else if (tier === 'default') {
      sc = { con: round1(CFG.def.con * useFactor), mid: center, agg: round1(CFG.def.agg * useFactor) };
    } else {
      sc = { con: clm(center - CFG.spread), mid: clm(center), agg: clm(center + CFG.spread) };
    }

    var scope;
    switch (tier) {
      case 'same_complex': scope = '본건 동일단지 낙찰사례'; break;
      case 'sigungu':      scope = (targetSg || '시군구') + ' 낙찰사례'; break;
      case 'stat_model':   scope = _useLabel(p.use) + ' 추정 · '
                                 + (est.derivation || ((est.chosen_level || '지역') + ' n=' + est.sample_n)); break;
      case 'stat_real':    scope = _useLabel(p.use) + ' 실측 낙찰가율 · '
                                 + (real.derivation || ((real.region || '전국') + ' (n=' + real.n + ')')); break;
      case 'stat_sigungu': scope = (targetSg || '시군구') + ' 통계'; break;
      case 'stat_national':scope = '전국 평균'; break;
      default:             scope = '기본값(지역 미확인)';
    }
    if (!usedReal && !usedModel && useFactor !== 1) scope += ' · 용도보정(근사) ' + _useLabel(p.use) + '(×' + useFactor + ')';

    return { tier: tier, center: sc.mid, scenarios: sc, isStat: isStat, asof: asof,
      sampleN: sampleN, scope: scope, targetSigungu: targetSg, targetSido: targetSido,
      sameComplexN: same.length, sigunguN: sg.length,
      useFactor: useFactor, useLabel: _useLabel(p.use), usedReal: usedReal,
      usedModel: usedModel, est: (usedModel ? est : null),
      modelFailDelta: estDelta, subjectFail: _subjectFailCount(pid) };
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
    var factorProdRaw = +(mExt * mInt * mHo * mEtc).toFixed(4);  // 낙찰가율 제외 요인 곱(원값)
    // 종합 요인 상·하한(±50%): 4개 요인(각 0.5~1.5)의 곱은 0.06~5.06배까지 튈 수 있어
    // 기준시세가 비현실적으로 왜곡되는 것을 막기 위해 곱 자체를 0.5~1.5로 제한한다.
    var factorProd = Math.max(0.5, Math.min(1.5, factorProdRaw));
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
      factorProdRaw: factorProdRaw, factorClamped: (factorProd !== factorProdRaw),
      aiRatePct: aiRatePct, mRatePct: mRatePct,
      baseAi: baseAi, baseMgr: baseMgr,
      aiBid: aiBid, mgrBid: mgrBid,
      decision: decision, decisionLabel: (decision === 'mgr' ? '담당자안' : 'AI안'),
      finalBid: finalBid
    };
  }
  window.resolveBidEstimate = resolveBidEstimate;

  var TIER = {
    manual: ['#7c3aed', '✏️ 직접입력'],
    same_complex: ['#0f6e5c', '1단계 · 동일단지'], sigungu: ['#1e2a44', '2단계 · 시군구 사례'],
    stat_model: ['#0b6b57', '추정모델 · 가중·수축·유찰보정'],
    stat_real: ['#2f7d68', '실측 낙찰가율(용도·지역)'],
    stat_sigungu: ['#1e3a5f', '3단계 · 시군구 통계'],
    stat_national: ['#5a6b8c', '4단계 · 전국 통계'], default: ['#a8884a', '디폴트']
  };
  var TIER_FALLBACK = ['#5a6b8c', '기준값'];

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

  /* ===== (2) 04 경공매 캐스케이드 카드 ===== */
  function cardHTML(pid) {
    var be = resolveBidEstimate(pid); if (!be) return '';
    var cas = be.cas, ap = be.ap, area = be.area;
    var unitPrice = be.unitPrice, baseValue = be.baseAi, bidRate = be.rate;
    var badge = TIER[cas.tier] || TIER_FALLBACK;   // 미정의 티어라도 크래시 방지
    var dec = be.decision;
    var factorProd = be.factorProd;

    // 기준시세 출처: 감정가(사건/직접입력) vs 거래사례 평균(감정가 대용) — 라벨을 실제 출처에 맞춘다.
    var baseIsAppraisal = /감정가/.test(ap.source || '') && !/대용/.test(ap.source || '');
    var unitLabel = baseIsAppraisal ? '감정가 단가' : '거래사례 평균단가';
    var unitDesc  = baseIsAppraisal ? '감정가 ÷ 전용면적' : '거래사례 평균매매가 ÷ 전용면적';
    var baseLabel = baseIsAppraisal ? '= 감정가(기준시세)' : '= 기준시세(거래사례 대용)';

    var note;
    if (cas.tier === 'stat_model') {
      var _d = cas.modelFailDelta || 0;
      note = '<div class="text-small text-muted">📈 <strong>' + cas.scope + '</strong>. '
        + '경공매 실적을 최근성 가중·계층 수축으로 추정'
        + (cas.subjectFail != null && _d !== 0 ? ' 후 유찰 ' + cas.subjectFail + '회 기준 ' + (_d > 0 ? '+' : '') + _d + '%p 보정' : '')
        + '했습니다. 보수·적극 시나리오는 표본 분포(P25/P75)입니다.</div>';
    } else if (cas.isStat) {
      note = '<div class="text-small text-muted">📊 낙찰가율은 <strong>' + cas.scope + '</strong> 종합 낙찰가율입니다 (한국부동산원 법원경매통계 ' + cas.asof + ', 용도무관). 아파트는 종합보다 다소 높을 수 있습니다.</div>';
    } else if (cas.tier === 'default') {
      note = '<div class="text-small" style="color:var(--warn);">⚠️ 소재지에서 지역을 못 읽어 기본값(90%)을 적용했습니다. 주소를 확인하세요.</div>';
    } else {
      note = '<div class="text-small text-muted">📍 본건 <strong>' + cas.scope + '</strong> ' + cas.sampleN + '건의 중앙값입니다.</div>';
    }
    // 낙찰가율은 '매각가/감정가' 기준. 감정가가 없어 거래사례를 대용으로 쓰면 단위가 근사이므로 경고.
    if (!baseIsAppraisal) {
      note += '<div class="text-small" style="color:var(--warn);margin-top:4px;">⚠️ 감정가가 없어 <strong>거래사례 평균</strong>을 감정가 대용으로 사용했습니다. 낙찰가율(매각가/감정가)을 시세에 곱한 근사치이므로, 감정가 입력 시 정확도가 올라갑니다.</div>';
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
      + '<span class="badge" style="background:' + badge[0] + ';color:#fff;">' + badge[1] + '</span></div>'
      + '<div class="text-small text-muted" style="margin:-4px 0 14px;">거래사례비교법 — 기준시세 × 가치형성요인 × 낙찰가율. <strong>AI안</strong>(요인 1.00·낙찰가율 캐스케이드)과 <strong>담당자안</strong>(요인·낙찰가율 직접보정)을 산출해 둘 중 하나를 최종 채택합니다.</div>'
      + '<table style="width:100%;border-collapse:collapse;font-size:14px;">'
      + row(unitLabel, won(unitPrice) + '/㎡', unitDesc)
      + row('× 전용면적', (area ? area.toFixed(2) : '-') + '㎡', ap.source || '')
      + row(baseLabel, won(baseValue), '요인·낙찰가율 적용 전', true)
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
      + '<tr style="border-top:1px solid var(--line,#dfe4ee);"><td style="padding:6px 0;font-weight:700;font-size:13px;color:var(--ink-soft);">요인 곱 <span style="font-weight:400;color:var(--ink-muted);font-size:11px;">(낙찰가율 제외' + (be.factorClamped ? ' · 상·하한 0.5~1.5 적용, 원값 ' + be.factorProdRaw.toFixed(2) : '') + ')</span></td><td style="padding:6px 8px;text-align:right;font-weight:700;font-size:13px;">1.00</td><td style="padding:6px 0;text-align:right;font-weight:700;font-size:13px;color:' + PINK + ';">' + factorProd.toFixed(2) + '</td></tr>'
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
      + '</div>';
  }

  function injectBidEst() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    var pid = state.currentPropertyId; if (!pid) return;
    var host = document.getElementById('bidEstHost');
    var empty = '<div class="card"><div class="text-muted">감정가 또는 거래사례가 없어 추정할 수 없습니다. 02 거래사례 / 04 경공매 사례 탭에서 데이터를 채워주세요.</div></div>';
    // 어떤 티어/입력에서도 카드 산출이 예외를 던지면 앱이 아니라 카드만 폴백 표시.
    var html;
    try { html = cardHTML(pid); }
    catch (e) {
      console.warn('[낙찰가 카드] 산출 실패, 폴백 표시:', e);
      html = '<div class="card"><div class="text-muted">추정 낙찰가액 카드를 표시할 수 없습니다(일시 오류). 데이터를 확인하거나 다시 시도하세요.</div></div>';
    }
    if (host) { host.innerHTML = html || empty; return; }
    // host가 아직 없으면(렌더 타이밍) viewContainer 끝에 주입
    Array.prototype.forEach.call(vc.querySelectorAll('[data-cascade="1"]'), function (n) { n.remove(); });
    if (!html) return;
    var tmp = document.createElement('div'); tmp.innerHTML = html;
    if (tmp.firstElementChild) vc.appendChild(tmp.firstElementChild);
  }
  window.injectBidEst = injectBidEst;

  window.setManualBidRate = function (pid, val) {
    if (!pid) return;
    state.scenarios = state.scenarios || {};
    state.scenarios[pid] = state.scenarios[pid] || {};
    var v = parseFloat(val);
    if (val === '' || val == null || isNaN(v)) delete state.scenarios[pid].manualBidRate;
    else state.scenarios[pid].manualBidRate = round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v)));
    if (typeof saveState === 'function') { try { saveState(); } catch (e) {} }
    injectBidEst();
  };

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

    // 동일면적 매매, 해제 제외 → 3→6→12→24개월 윈도 평균. 허용폭은 index.html의 _sameAreaTol과
    // 동일하게 용도별로(아파트·오피스텔 ±1㎡, 빌라·다세대·연립 ±10%·최소 2㎡) 맞춰 빌라 표본 과소를 해소.
    var _u = ((p.use) || '').replace(/\s/g, '');
    var areaTol = /다세대|연립|빌라/.test(_u) ? Math.max(2, area * 0.10) : 1;
    var sameArea = comps.filter(function (x) {
      return x.type === '매매' && Math.abs((x.area || 0) - area) < areaTol && (!x.memo || x.memo.indexOf('해제') < 0);
    });
    var info = windowedAvg(sameArea);

    var grid = vc.querySelector('.grid.grid-4');
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

    var nplCard = null;
    Array.prototype.forEach.call(vc.querySelectorAll('div'), function (d) {
      if (nplCard) return;
      var st = d.getAttribute('style') || '';
      if (/linear-gradient/.test(st) && /NPL 회수 가능 금액 추정/.test(d.textContent)) nplCard = d;
    });
    if (nplCard) {
      var sub = info.total
        ? (info.windowLabel + ' 매매 ' + info.total + '건'
            + (info.trimmed ? ' 중 최고·최저 제외 ' + info.used + '건' : '')
            + ' 평균 · 범위 ' + won(info.min) + ' ~ ' + won(info.max))
        : '동일면적 거래 없음';
      var card = document.createElement('div');
      card.style.cssText = 'background:linear-gradient(135deg, var(--kiwoom-navy-soft) 0%, #d4dcf0 100%);border:1px solid rgba(30,42,68,.18);border-left:4px solid var(--kiwoom-navy);border-radius:8px;padding:22px 26px;margin-bottom:16px;';
      card.innerHTML =
        '<div style="font-size:11px;letter-spacing:.18em;color:var(--kiwoom-navy);text-transform:uppercase;font-weight:700;margin-bottom:8px;">📊 평균 매매가 · AVERAGE SALE PRICE (동일 면적대)</div>'
        + '<div style="font-family:var(--mono);font-size:32px;font-weight:800;color:var(--kiwoom-navy-deep);line-height:1.15;">' + won(info.avg) + '</div>'
        + '<div style="font-size:13px;color:var(--ink-soft,#5b6473);margin-top:6px;font-family:var(--mono);">' + sub + '</div>';
      nplCard.replaceWith(card);
    }
  }

  /* ===== (3) 05 시세추정 최종 추정가 분홍 강조 ===== */
  function patchValuation() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    if (vc.getAttribute('data-val-pink')) return;
    var best = null, bestSize = 0;
    Array.prototype.forEach.call(vc.querySelectorAll('*'), function (e) {
      if (e.children.length) return;
      var t = (e.textContent || '').trim();
      if (!/(억|만)/.test(t)) return;
      var fs = parseFloat(getComputedStyle(e).fontSize) || 0;
      if (fs > bestSize) { bestSize = fs; best = e; }
    });
    if (best && bestSize >= 30) { best.style.color = PINK; best.style.fontWeight = '800'; vc.setAttribute('data-val-pink', '1'); }
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

    var sh = findByText(vc, '.section-h', /분석\s*요약/);
    if (sh) {
      var prose = sh.nextElementSibling;
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
  }

  /* ===== (1) 좌측 메뉴 재정렬: 시세추정 → 3번째 ===== */
  function reorderNav() {
    try {
      if (window.__navReordered) return;
      var labels = ['본건 정보', '거래사례 비교', '매물현황', '경공매 사례', '시세 추정', '권리분석', '수익률', '리포트'];
      function leafFor(lbl) {
        var best = null;
        Array.prototype.forEach.call(document.querySelectorAll('a,button,li,div,span'), function (e) {
          var t = (e.textContent || '').replace(/\s+/g, ' ').trim();
          if (t.indexOf(lbl) >= 0 && t.length <= lbl.length + 7 && e.querySelectorAll('a,button').length === 0) {
            if (!best || e.textContent.length < best.textContent.length) best = e;
          }
        });
        return best;
      }
      var leaves = labels.map(leafFor);
      if (leaves.some(function (x) { return !x; })) return;
      function ancestors(n) { var a = []; while (n) { a.push(n); n = n.parentNode; } return a; }
      var common = ancestors(leaves[0]);
      for (var i = 1; i < leaves.length; i++) {
        var s = ancestors(leaves[i]);
        common = common.filter(function (x) { return s.indexOf(x) >= 0; });
      }
      var box = common[0]; if (!box) return;
      function rowOf(leaf) { var n = leaf; while (n && n.parentNode !== box) n = n.parentNode; return n; }
      var rows = leaves.map(rowOf);
      if (rows.some(function (x) { return !x; })) return;
      box.insertBefore(rows[4], rows[2]);
      var order = [rows[0], rows[1], rows[4], rows[2], rows[3], rows[5], rows[6], rows[7]];
      order.forEach(function (row, idx) { setNum(row, ('0' + (idx + 1)).slice(-2)); });
      window.__navReordered = true;
    } catch (e) {}
  }
  function setNum(row, numStr) {
    var leaf = null;
    Array.prototype.forEach.call(row.querySelectorAll('*'), function (c) {
      if (!leaf && c.children.length === 0 && /^\s*0?\d{1,2}\s*$/.test(c.textContent || '')) leaf = c;
    });
    if (leaf) { leaf.textContent = numStr; return; }
    try {
      var w = document.createTreeWalker(row, NodeFilter.SHOW_TEXT, null);
      var tn;
      while ((tn = w.nextNode())) {
        if (/^\s*0?\d{1,2}(?=\D|$)/.test(tn.nodeValue)) {
          tn.nodeValue = tn.nodeValue.replace(/^(\s*)0?\d{1,2}/, '$1' + numStr); return;
        }
      }
    } catch (e) {}
  }

  /* ===== (1) 좌측 '분석 도구' 헤더 + 'NPL 자산 분석' 메뉴 제거 ===== */
  function removeNplMenu() {
    try {
      if (window.__nplRemoved) return;
      // NPL 자산 분석 항목
      var leaf = null;
      Array.prototype.forEach.call(document.querySelectorAll('a,button,li,div,span'), function (e) {
        if (leaf) return;
        var t = (e.textContent || '').replace(/\s+/g, ' ').trim();
        if (/NPL\s*자산\s*분석/.test(t) && t.length <= 16 && e.querySelectorAll('a,button').length === 0) leaf = e;
      });
      if (leaf) {
        var row = (leaf.closest && leaf.closest('a,[onclick],[data-view],li')) || leaf.parentElement || leaf;
        row.remove();
      }
      // '분석 도구' 헤더(빈 섹션) 제거
      var hdr = null;
      Array.prototype.forEach.call(document.querySelectorAll('div,span,p,h1,h2,h3,h4,h5,li'), function (e) {
        if (hdr) return;
        var t = (e.textContent || '').replace(/\s+/g, ' ').trim();
        if (t === '분석 도구' && e.children.length === 0) hdr = e;
      });
      if (hdr) hdr.remove();
      window.__nplRemoved = true;
    } catch (e) {}
  }

  /* ===== 디스패처 ===== */
  function inject() {
    try {
      if (!state) return;
      var pid = state.currentPropertyId;
      if (autoAlign(pid)) { window.renderView(); return; }
      var v = state.currentView;
      if (v === 'bidest') injectBidEst();
      else if (v === 'comparables') patchComparables();
      else if (v === 'valuation') patchValuation();
      else if (v === 'report') patchReport();
    } catch (e) { console.warn('[보정] 건너뜀:', e); }
  }

  function hook() {
    if (typeof window.renderView !== 'function') { setTimeout(hook, 200); return; }
    if (window.__cascadeHooked) return;
    window.__cascadeHooked = true;
    var orig = window.renderView;
    window.renderView = function () { var r = orig.apply(this, arguments); inject(); return r; };
    reorderNav();
    removeNplMenu();
    inject();
    console.log('[낙찰가율 캐스케이드] v6 · 한국부동산원 시군구 종합 ' + (window.AUCTION_RATES ? window.AUCTION_RATES.asof : '미로드'));
  }

  if (document.readyState !== 'loading') setTimeout(hook, 300);
  else document.addEventListener('DOMContentLoaded', function () { setTimeout(hook, 300); });
})();
