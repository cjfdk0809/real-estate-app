/* =========================================================================
 *  낙찰가율 캐스케이드 + 화면 보정 모듈  v3  (bid_rate_cascade.js)
 *  -----------------------------------------------------------------------
 *  index.html 본문 수정 없음. </body> 위(또는 kfi_trade_collector.js 아래)에
 *      <script src="/bid_rate_cascade.js"></script>
 *  한 줄만 있으면 작동. (이미 넣으셨으면 이 파일만 덮어쓰기 업로드하면 끝)
 *
 *  [낙찰가율 단계] 사례 0건이어도 소재지(시도/시군구)만 보고 자동 적용:
 *    1 동일단지(N≥3) → 2 시군구사례(N≥5) → 3 시군구통계 → 4 시도통계 → 5 전국
 *
 *  [화면 보정]
 *   (1) 02 거래사례: 1박스=본건(동·호·면적), 2박스=동일면적 매매,
 *       NPL 회수카드 → 평균 매매가 카드로 교체
 *   (2) 04 경공매: "아파트 평균 낙찰가율" / "본건 예상감정가" / 예상낙찰가 분홍 강조
 *   (3) 리포트 2.거래사례: 매매 거래만 표시
 *   (4) 리포트 3-1: 맨 위 '평균 낙찰가율' 행 추가(보수·중립·적극과 비교)
 *   (5) 리포트 5.분석요약: 평균·적극·중립·보수 예상 낙찰금액 문장 추가
 *
 *  ※ 모든 보정은 구조가 안 맞으면 그냥 원본 유지(앱이 깨지지 않음).
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
      '인천': { rate: 79.8,  asof: '2026-05' }
    },
    sigungu: {
      // 예시(주석 해제 후 최신 수치로 교체):
      // '강남구': { rate: 102.0, asof: '2026-05' },
      // '시흥시': { rate: 84.3,  asof: '2026-05' }
    }
  };

  var CFG = {
    minSameComplex: 3, minSigungu: 5, spread: 5,
    minRate: 30, maxRate: 130, def: { con: 85, mid: 90, agg: 95 }
  };

  /* ===== 공통 유틸 ===== */
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

  /* ===== 캐스케이드 핵심 ===== */
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

    var extSg = (targetSg && REGION_RATES.sigungu[targetSg]) || null;
    var extSido = (targetSido && REGION_RATES.sido[targetSido]) || null;
    var extNat = REGION_RATES.national || null;

    var tier, source, center, asof = null, isStat = false, sampleN = null, basisLabel, basisValue, scope = '';
    if (same.length >= CFG.minSameComplex) {
      tier = 'same_complex'; source = '본건 등록 낙찰사례'; sampleN = same.length; center = median(same);
      basisLabel = '표본 수'; basisValue = sampleN + '건 (중앙값)';
    } else if (sg.length >= CFG.minSigungu) {
      tier = 'sigungu'; source = '시군구 자체사례(' + targetSg + ')'; sampleN = sg.length; center = median(sg);
      basisLabel = '표본 수'; basisValue = sampleN + '건 (중앙값)'; scope = targetSg;
    } else if (extSg) {
      tier = 'stat_sigungu'; source = '시군구 통계(' + targetSg + ')'; center = extSg.rate; asof = extSg.asof; isStat = true;
      basisLabel = '기준'; basisValue = '지지옥션 ' + asof; scope = targetSg;
    } else if (extSido) {
      tier = 'stat_sido'; source = '시도 통계(' + targetSido + ')'; center = extSido.rate; asof = extSido.asof; isStat = true;
      basisLabel = '기준'; basisValue = '지지옥션 ' + asof; scope = targetSido;
    } else if (extNat) {
      tier = 'stat_national'; source = '전국 통계'; center = extNat.rate; asof = extNat.asof; isStat = true;
      basisLabel = '기준'; basisValue = '지지옥션 ' + asof; scope = '전국';
    } else {
      tier = 'default'; source = '전국 디폴트'; center = CFG.def.mid; basisLabel = '표본 수'; basisValue = '-';
    }

    var sc;
    if (tier === 'default') sc = { con: CFG.def.con, mid: CFG.def.mid, agg: CFG.def.agg };
    else { var cl = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
      sc = { con: cl(center - CFG.spread), mid: cl(center), agg: cl(center + CFG.spread) }; }

    return { tier: tier, source: source, center: sc.mid, scenarios: sc, isStat: isStat, asof: asof,
      basisLabel: basisLabel, basisValue: basisValue, sampleN: sampleN, scope: scope,
      sameComplexN: same.length, sigunguN: sg.length, targetSigungu: targetSg, targetSido: targetSido };
  }
  window.resolveBidRateCascade = resolveBidRateCascade;

  var TIER = {
    same_complex: ['#0f6e5c', '1단계 · 동일단지'], sigungu: ['#1e2a44', '2단계 · 시군구 사례'],
    stat_sigungu: ['#1e3a5f', '3단계 · 시군구 통계'], stat_sido: ['#2A4FBE', '4단계 · 시도 통계'],
    stat_national: ['#5a6b8c', '5단계 · 전국 통계'], default: ['#a8884a', '디폴트']
  };
  var PINK = 'var(--kiwoom-pink-deep, #CC00CC)';

  /* ===== (2) 04 경공매 캐스케이드 카드 ===== */
  function cardHTML(pid) {
    var p = ((state && state.properties) || {})[pid]; if (!p) return '';
    var auctions = ((state && state.auctions) || {})[pid] || [];
    var ap = (typeof getActiveAppraisal === 'function') ? getActiveAppraisal(p, auctions) : { value: null, source: '' };
    if (!ap.value) return '';

    var cas = resolveBidRateCascade(pid);
    var bid = Math.round(ap.value * cas.center / 100);
    var badge = TIER[cas.tier];
    var region = cas.scope || cas.targetSigungu || cas.targetSido || '';

    var label, note;
    if (cas.isStat) {
      label = '기준: ' + cas.source + ' · 지역 평균 ' + cas.scenarios.mid + '% (지지옥션 ' + cas.asof + ')';
      note = '<div class="text-small text-muted">📊 소재지 <strong>' + region + '</strong> 지역 평균을 자동 적용했습니다. 실제 낙찰은 단지·물건별 편차가 큽니다. 이 물건에 낙찰사례를 3건 이상 추가하면 더 정확한 1단계로 자동 전환됩니다.</div>';
    } else if (cas.tier === 'default') {
      label = '기준: 전국 디폴트 (지역 통계·사례 모두 없음)';
      note = '<div class="text-small" style="color:var(--warn);">⚠️ 소재지에서 시도/시군구를 못 읽었습니다. 주소를 확인하거나 지역 통계를 채우세요.</div>';
    } else {
      label = '기준: ' + cas.source + ' · 중앙값 ' + cas.scenarios.mid + '% (N=' + cas.sampleN + ')';
      note = '';
    }

    return ''
      + '<div class="card mb-24" data-cascade="1" style="border-left:4px solid var(--accent);">'
      + '<div class="card-title">본건 적용 · 낙찰가율 캐스케이드 '
      + '<span class="badge" style="background:' + badge[0] + ';color:#fff;">' + badge[1] + '</span></div>'
      + '<div class="grid grid-2">'
      + '<div>'
      + '<div class="info-row"><div class="info-label">채택 기준</div><div class="info-value">' + cas.source + '</div></div>'
      + '<div class="info-row"><div class="info-label">아파트 평균 낙찰가율</div><div class="info-value mono text-accent">' + cas.center + '%</div></div>'
      + '<div class="info-row"><div class="info-label">' + cas.basisLabel + '</div><div class="info-value mono">' + cas.basisValue + '</div></div>'
      + '<div class="info-row"><div class="info-label">본건 예상감정가</div><div class="info-value mono">' + won(ap.value) + ' <span class="text-muted text-small">(' + (ap.source || '') + ')</span></div></div>'
      + '<div class="info-row" style="align-items:center;"><div class="info-label">예상 낙찰가</div><div class="info-value mono" style="font-size:26px;font-weight:800;color:' + PINK + ';line-height:1.1;">' + won(bid) + '</div></div>'
      + '</div>'
      + '<div style="display:flex;flex-direction:column;justify-content:center;gap:10px;">'
      + '<button class="btn btn-primary no-print" onclick="applyCaseRatesToScenario(' + cas.center + ')">권리분석 낙찰가율을 이 기준(' + cas.center + '%)으로 보정</button>'
      + '<div class="text-small text-muted">' + label + '<br>중립 ' + cas.scenarios.mid + '% · 보수 ' + cas.scenarios.con + '% · 적극 ' + cas.scenarios.agg + '% 로 반영</div>'
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

  /* ===== (1) 02 거래사례 비교 ===== */
  function patchComparables() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    var pid = state.currentPropertyId; if (!pid) return;
    var p = ((state.properties) || {})[pid]; if (!p) return;
    var comps = ((state.comparables) || {})[pid] || [];
    var area = p.exclusiveArea || 0;
    var sales = comps.filter(function (x) { return x.type === '매매'; });
    var sameArea = sales.filter(function (x) { return Math.abs((x.area || 0) - area) < 1; });
    var prices = sameArea.map(function (x) { return x.price; });
    var avg = prices.length ? Math.round(prices.reduce(function (s, v) { return s + v; }, 0) / prices.length) : 0;
    var mn = prices.length ? Math.min.apply(null, prices) : 0;
    var mx = prices.length ? Math.max.apply(null, prices) : 0;

    // 1·2번 박스 교체
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

    // NPL 회수 카드 → 평균 매매가 카드
    var nplCard = null;
    Array.prototype.forEach.call(vc.querySelectorAll('div'), function (d) {
      if (nplCard) return;
      var st = d.getAttribute('style') || '';
      if (/linear-gradient/.test(st) && /NPL 회수 가능 금액 추정/.test(d.textContent)) nplCard = d;
    });
    if (nplCard) {
      var card = document.createElement('div');
      card.setAttribute('data-avg-card', '1');
      card.style.cssText = 'background:linear-gradient(135deg, var(--kiwoom-navy-soft) 0%, #d4dcf0 100%);border:1px solid rgba(30,42,68,.18);border-left:4px solid var(--kiwoom-navy);border-radius:8px;padding:22px 26px;margin-bottom:16px;';
      card.innerHTML =
        '<div style="font-size:11px;letter-spacing:.18em;color:var(--kiwoom-navy);text-transform:uppercase;font-weight:700;margin-bottom:8px;">📊 평균 매매가 · AVERAGE SALE PRICE (동일 면적대)</div>'
        + '<div style="font-family:var(--mono);font-size:32px;font-weight:800;color:var(--kiwoom-navy-deep);line-height:1.15;">' + won(avg) + '</div>'
        + '<div style="font-size:13px;color:var(--ink-soft,#5b6473);margin-top:6px;font-family:var(--mono);">범위 ' + won(mn) + ' ~ ' + won(mx) + ' · 동일면적 표본 ' + sameArea.length + '건</div>';
      nplCard.replaceWith(card);
    }
  }

  /* ===== (3)(4)(5) 리포트 ===== */
  function patchReport() {
    var vc = document.getElementById('viewContainer'); if (!vc) return;
    var pid = state.currentPropertyId; if (!pid) return;
    var p = ((state.properties) || {})[pid]; if (!p) return;
    var auctions = ((state.auctions) || {})[pid] || [];

    // (3) 2.거래사례 분석 표 — 매매만
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

    var cas = resolveBidRateCascade(pid);
    var center = cas.center;
    var sc = ((state.scenarios) || {})[pid] || {};
    var _con = sc.con != null ? sc.con : 92, _mid = sc.mid != null ? sc.mid : 97, _agg = sc.agg != null ? sc.agg : 100;
    var amt = function (r) { return Math.round(ap.value * r / 100); };

    // (4) 3-1 표 — 평균 낙찰가율 행 맨 위 추가
    var t31 = findTableByHead(vc, /시나리오/);
    if (t31) {
      var tb31 = t31.querySelector('tbody');
      if (tb31 && !(tb31.firstElementChild && /평균/.test(tb31.firstElementChild.textContent))) {
        var tr = document.createElement('tr');
        tr.innerHTML =
          '<td><strong style="color:' + PINK + ';">평균 낙찰가율</strong></td>'
          + '<td class="num">' + center + '%</td>'
          + '<td class="price" style="color:' + PINK + ';font-weight:800;">' + won(amt(center)) + '</td>';
        tb31.insertBefore(tr, tb31.firstChild);
      }
    }

    // (5) 5.분석 요약 — 평균·적극·중립·보수 예상 낙찰금액 문장
    var sh = findByText(vc, '.section-h', /분석\s*요약/);
    if (sh) {
      var prose = sh.nextElementSibling;
      if (prose && !prose.getAttribute('data-bid-summary')) {
        var line = document.createElement('div');
        line.style.cssText = 'margin-bottom:12px;padding:11px 15px;background:var(--kiwoom-pink-soft,#FFE6FF);border-left:3px solid ' + PINK + ';border-radius:0 6px 6px 0;font-weight:600;line-height:1.7;';
        line.innerHTML =
          '평균 낙찰가율(' + center + '%)로 예상하는 본건 낙찰금액은 <strong style="color:' + PINK + ';">' + won(amt(center)) + '</strong>이며, '
          + '적극적 ' + won(amt(_agg)) + ' · 중립적 ' + won(amt(_mid)) + ' · 보수적 ' + won(amt(_con)) + '으로 추정됩니다.';
        prose.insertBefore(line, prose.firstChild);
        prose.setAttribute('data-bid-summary', '1');
      }
    }
  }

  /* ===== 디스패처 ===== */
  function inject() {
    try {
      if (!state) return;
      if (state.currentView === 'auction') injectAuction();
      else if (state.currentView === 'comparables') patchComparables();
      else if (state.currentView === 'report') patchReport();
    } catch (e) { console.warn('[캐스케이드/보정] 건너뜀:', e); }
  }

  function hook() {
    if (typeof window.renderView !== 'function') { setTimeout(hook, 200); return; }
    if (window.__cascadeHooked) return;
    window.__cascadeHooked = true;
    var orig = window.renderView;
    window.renderView = function () { var r = orig.apply(this, arguments); inject(); return r; };
    inject();
    console.log('[낙찰가율 캐스케이드] v3 활성화됨 · 지역 통계 기준월 ' + REGION_RATES._asof);
  }

  if (document.readyState !== 'loading') setTimeout(hook, 300);
  else document.addEventListener('DOMContentLoaded', function () { setTimeout(hook, 300); });
})();
