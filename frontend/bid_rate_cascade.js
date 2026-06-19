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

    // ★ 최우선: 본건별 낙찰가율 직접입력 (인포케어 아파트 낙찰율 등)
    var _sc0 = (state && state.scenarios && state.scenarios[pid]) || {};
    if (_sc0.manualBidRate != null && _sc0.manualBidRate !== '' && !isNaN(parseFloat(_sc0.manualBidRate))) {
      var _mr = round1(parseFloat(_sc0.manualBidRate));
      var _clm = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
      return { tier: 'manual', center: _mr, scenarios: { con: _clm(_mr - CFG.spread), mid: _mr, agg: _clm(_mr + CFG.spread) },
        isStat: false, asof: null, sampleN: 0, scope: '본건 직접입력', targetSigungu: targetSg, targetSido: targetSido,
        sameComplexN: 0, sigunguN: 0 };
    }

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
    var extSido = _statSido(targetSido);
    var extNat = _statSido('전국');

    var tier, center, asof = null, isStat = false, sampleN = null;
    if (same.length >= CFG.minSameComplex) { tier = 'same_complex'; sampleN = same.length; center = median(same); }
    else if (sg.length >= CFG.minSigungu) { tier = 'sigungu'; sampleN = sg.length; center = median(sg); }
    else if (extSg) { tier = 'stat_sigungu'; center = extSg.rate; asof = extSg.asof; isStat = true; }
    else if (extSido) { tier = 'stat_sido'; center = extSido.rate; asof = extSido.asof; isStat = true; }
    else if (extNat) { tier = 'stat_national'; center = extNat.rate; asof = extNat.asof; isStat = true; }
    else { tier = 'default'; center = CFG.def.mid; }

    var sc;
    if (tier === 'default') sc = { con: CFG.def.con, mid: CFG.def.mid, agg: CFG.def.agg };
    else { var cl = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
      sc = { con: cl(center - CFG.spread), mid: cl(center), agg: cl(center + CFG.spread) }; }

    var scope;
    switch (tier) {
      case 'same_complex': scope = '본건 동일단지 낙찰사례'; break;
      case 'sigungu':      scope = (targetSg || '시군구') + ' 낙찰사례'; break;
      case 'stat_sigungu': scope = (targetSg || '시군구') + ' 종합'; break;
      case 'stat_sido':    scope = (targetSido || '시도') + ' 전체 평균'; break;
      case 'stat_national':scope = '전국 평균'; break;
      default:             scope = '기본값(지역 미확인)';
    }

    return { tier: tier, center: sc.mid, scenarios: sc, isStat: isStat, asof: asof,
      sampleN: sampleN, scope: scope, targetSigungu: targetSg, targetSido: targetSido,
      sameComplexN: same.length, sigunguN: sg.length };
  }
  window.resolveBidRateCascade = resolveBidRateCascade;

  var TIER = {
    manual: ['#7c3aed', '✏️ 직접입력'],
    same_complex: ['#0f6e5c', '1단계 · 동일단지'], sigungu: ['#1e2a44', '2단계 · 시군구 사례'],
    stat_sigungu: ['#1e3a5f', '3단계 · 시군구 통계'], stat_sido: ['#2A4FBE', '4단계 · 시도 통계'],
    stat_national: ['#5a6b8c', '5단계 · 전국 통계'], default: ['#a8884a', '디폴트']
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

  /* ===== (2) 04 경공매 캐스케이드 카드 ===== */
  function cardHTML(pid) {
    var p = ((state && state.properties) || {})[pid]; if (!p) return '';
    var auctions = ((state && state.auctions) || {})[pid] || [];
    var ap = (typeof getActiveAppraisal === 'function') ? getActiveAppraisal(p, auctions) : { value: null, source: '' };
    if (!ap.value) return '';

    var cas = resolveBidRateCascade(pid);
    var bid = Math.round(ap.value * cas.center / 100);
    var badge = TIER[cas.tier];
    var _scV = ((state && state.scenarios) || {})[pid] || {};
    var manualVal = (_scV.manualBidRate != null && _scV.manualBidRate !== '') ? _scV.manualBidRate : null;

    var note;
    if (cas.tier === 'manual') {
      note = '<div class="text-small text-muted">✏️ 본건에 <strong>직접 입력</strong>한 낙찰가율입니다. 칸을 비우면 시군구 통계로 자동 복귀합니다.</div>';
    } else if (cas.isStat) {
      note = '<div class="text-small text-muted">📊 위 낙찰가율은 <strong>' + cas.scope + '</strong> 낙찰가율입니다 (한국부동산원 법원경매통계 ' + cas.asof + ', 용도무관 종합). '
        + '아파트 등 특정 용도는 다소 높을 수 있어, 필요 시 본건별 직접입력으로 보정하세요.</div>';
    } else if (cas.tier === 'default') {
      note = '<div class="text-small" style="color:var(--warn);">⚠️ 소재지에서 지역을 못 읽어 기본값(90%)을 적용했습니다. 주소를 확인하세요.</div>';
    } else {
      note = '<div class="text-small text-muted">📍 본건 <strong>' + cas.scope + '</strong> ' + cas.sampleN + '건의 중앙값입니다.</div>';
    }

    return ''
      + '<div class="card mb-24" data-cascade="1" style="border-left:4px solid var(--accent);">'
      + '<div class="card-title">본건 적용 · 낙찰가율 캐스케이드 '
      + '<span class="badge" style="background:' + badge[0] + ';color:#fff;">' + badge[1] + '</span></div>'
      + '<div class="grid grid-2">'
      + '<div>'
      + '<div class="info-row"><div class="info-label">적용 지역</div><div class="info-value"><strong>' + cas.scope + '</strong>' + (cas.asof ? ' <span class="text-muted text-small">(한국부동산원 ' + cas.asof + ')</span>' : '') + '</div></div>'
      + '<div class="info-row"><div class="info-label">적용 낙찰가율</div><div class="info-value mono text-accent">' + cas.center + '%</div></div>'
      + '<div class="info-row no-print"><div class="info-label">낙찰가율 직접입력</div><div class="info-value">'
      + '<input type="number" step="0.1" min="0" max="200" value="' + (manualVal != null ? manualVal : '') + '" placeholder="예: 96.5"'
      + ' style="width:84px;padding:4px 8px;border:1px solid var(--line,#d0d5dd);border-radius:6px;text-align:right;font-family:inherit;font-size:14px;"'
      + ' onchange="setManualBidRate(\'' + pid + '\', this.value)"> %'
      + (manualVal != null ? ' <button class="no-print" style="margin-left:8px;border:none;background:none;color:var(--ink-muted,#888);font-size:12px;cursor:pointer;text-decoration:underline;" onclick="setManualBidRate(\'' + pid + '\',\'\')">자동으로</button>' : '')
      + '</div></div>'
      + '<div class="info-row"><div class="info-label">본건 예상감정가</div><div class="info-value mono">' + won(ap.value) + ' <span class="text-muted text-small">(' + (ap.source || '') + ')</span></div></div>'
      + '<div class="info-row" style="align-items:center;"><div class="info-label">예상 낙찰가</div><div class="info-value mono" style="font-size:26px;font-weight:800;color:' + PINK + ';line-height:1.1;">' + won(bid) + '</div></div>'
      + '</div>'
      + '<div style="display:flex;flex-direction:column;justify-content:center;gap:10px;">'
      + '<div class="text-small text-muted" style="padding:6px 0;">✅ 이 적용 낙찰가율은 <strong>05 권리분석</strong> · <strong>리포트</strong>에 자동 반영됩니다.<br>중립 ' + cas.scenarios.mid + '% · 보수 ' + cas.scenarios.con + '%(−5) · 적극 ' + cas.scenarios.agg + '%(+5)</div>'
      + note
      + '</div>'
      + '</div>'
      + '</div>';
  }

  function injectAuction() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    var pid = state.currentPropertyId; if (!pid) return;
    Array.prototype.forEach.call(vc.querySelectorAll('[data-cascade="1"]'), function (n) { n.remove(); });
    var html = cardHTML(pid); if (!html) return;
    var tmp = document.createElement('div'); tmp.innerHTML = html;
    var node = tmp.firstElementChild;
    var orig = null;
    Array.prototype.forEach.call(vc.querySelectorAll('.card'), function (c) {
      if (!orig && /본건 적용 · 사례 기반/.test(c.textContent)) orig = c;
    });
    if (orig) { orig.replaceWith(node); return; }
    var grid = vc.querySelector('.grid.grid-4');
    if (grid && grid.parentNode) grid.parentNode.insertBefore(node, grid.nextSibling);
    else vc.insertBefore(node, vc.children[1] || null);
  }

  window.setManualBidRate = function (pid, val) {
    if (!pid) return;
    state.scenarios = state.scenarios || {};
    state.scenarios[pid] = state.scenarios[pid] || {};
    var v = parseFloat(val);
    if (val === '' || val == null || isNaN(v)) delete state.scenarios[pid].manualBidRate;
    else state.scenarios[pid].manualBidRate = round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v)));
    if (typeof saveState === 'function') { try { saveState(); } catch (e) {} }
    injectAuction();
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
        line.innerHTML = '평균 낙찰가율(' + center + '%, ' + cas.scope + ')로 예상하는 본건 낙찰금액은 <strong style="color:' + PINK + ';">' + won(amt(center)) + '</strong>이며, '
          + '적극적 ' + won(amt(_agg)) + ' · 중립적 ' + won(amt(_mid)) + ' · 보수적 ' + won(amt(_con)) + '으로 추정됩니다.';
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
      if (v === 'auction') injectAuction();
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
