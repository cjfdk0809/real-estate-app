/* =========================================================================
 *  낙찰가율 캐스케이드 연결 모듈  v2  (bid_rate_cascade.js)
 *  -----------------------------------------------------------------------
 *  index.html 본문 수정 없음. </body> 위(또는 kfi_trade_collector.js 아래)에
 *      <script src="/bid_rate_cascade.js"></script>
 *  한 줄만 있으면 작동. (이미 넣으셨으면 이 파일만 덮어쓰기 업로드하면 끝)
 *
 *  단계(가장 구체적 → 일반, 표본 임계치 충족하는 첫 단계 채택):
 *    1 동일단지   = 현재 물건 등록 낙찰사례 (N≥3)         ← 중앙값
 *    2 시군구사례 = 전체 물건의 같은 시군구 낙찰사례 (N≥5) ← 중앙값
 *    3 시군구통계 = 아래 REGION_RATES.sigungu 표          ← 지역 평균(자동)
 *    4 시도통계   = 아래 REGION_RATES.sido 표             ← 지역 평균(자동)
 *    5 전국통계   = REGION_RATES.national                ← 전국 평균(자동)
 *  → 사례를 한 건도 안 넣어도 3~5단계가 소재지만 보고 자동 적용됩니다.
 * ========================================================================= */
(function () {
  'use strict';

  /* =======================================================================
   *  지역별 낙찰가율 기준표  (월 1회 갱신 권장)
   *  출처: 지지옥션 월간 경매동향보고서 (무료 공식 실시간 API 없음)
   *  갱신법: 매월 초 보고서 수치로 rate / asof만 바꿔 다시 업로드.
   *  ※ rate = 감정가 대비 낙찰가율(%) 평균.
   * ===================================================================== */
  var REGION_RATES = {
    _asof: '2026-05',                                   // 표 기준월
    national: { rate: 87.3, asof: '2026-05' },          // 전국 아파트

    // 시도/광역 단위 (서울/경기/인천은 최신 확인치, 그 외는 전국으로 폴백)
    sido: {
      '서울': { rate: 100.8, asof: '2026-05' },
      '경기': { rate: 89.0,  asof: '2026-05' },
      '인천': { rate: 79.8,  asof: '2026-05' }
      // 필요시 추가: '부산','대구','대전','광주','울산','세종','강원','충북',
      //            '충남','전북','전남','경북','경남','제주'
    },

    // 시군구/자치구 단위 — 정밀도가 필요한 지역만 채워 쓰세요(없으면 시도값 적용)
    // 보고서의 자치구별 표에서 옮겨 적고 기준월(asof)을 꼭 같이 넣으세요.
    sigungu: {
      // 예시(주석 해제 후 최신 수치로 교체):
      // '강남구':  { rate: 102.0, asof: '2026-05' },
      // '양천구':  { rate: 122.0, asof: '2025-12' },
      // '성동구':  { rate: 120.5, asof: '2025-12' },
      // '분당구':  { rate: 115.8, asof: '2025-12' },
      // '시흥시':  { rate: 84.3,  asof: '2026-05' }
    }
  };

  // ---- 조정 가능한 파라미터 ----
  var CFG = {
    minSameComplex: 3,   // 1단계 최소 표본
    minSigungu: 5,       // 2단계 최소 표본
    spread: 5,           // 보수/적극 = 중심값 ∓ 5%p
    minRate: 30, maxRate: 130,
    def: { con: 85, mid: 90, agg: 95 }   // 최후 디폴트(지역통계도 전혀 없을 때)
  };

  // ---- 주소 파서 ----
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
    var head = String(addr).trim().split(/\s+/)[0] || '';   // 주소 첫 토큰(시도)
    var T = [
      ['서울', '서울'], ['부산', '부산'], ['대구', '대구'], ['인천', '인천'], ['광주', '광주'],
      ['대전', '대전'], ['울산', '울산'], ['세종', '세종'], ['경기', '경기'], ['강원', '강원'],
      ['충청북', '충북'], ['충북', '충북'], ['충청남', '충남'], ['충남', '충남'],
      ['전북', '전북'], ['전라북', '전북'], ['전라남', '전남'], ['전남', '전남'],
      ['경상북', '경북'], ['경북', '경북'], ['경상남', '경남'], ['경남', '경남'], ['제주', '제주']
    ];
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

  // ---- 캐스케이드 핵심 ----
  function resolveBidRateCascade(pid) {
    var props = (state && state.properties) || {};
    var aucs = (state && state.auctions) || {};
    var p = props[pid] || {};
    var addr = p.addrLot || p.addrRoad || '';
    var targetSg = parseSigungu(addr);
    var targetSido = parseSido(addr);

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

    var tier, source, center, asof = null, isStat = false, sampleN = null, basisLabel, basisValue;
    if (same.length >= CFG.minSameComplex) {
      tier = 'same_complex'; source = '본건 등록 낙찰사례'; sampleN = same.length; center = median(same);
      basisLabel = '표본 수'; basisValue = sampleN + '건 (중앙값)';
    } else if (sg.length >= CFG.minSigungu) {
      tier = 'sigungu'; source = '시군구 자체사례(' + targetSg + ')'; sampleN = sg.length; center = median(sg);
      basisLabel = '표본 수'; basisValue = sampleN + '건 (중앙값)';
    } else if (extSg) {
      tier = 'stat_sigungu'; source = '시군구 통계(' + targetSg + ')'; center = extSg.rate; asof = extSg.asof; isStat = true;
      basisLabel = '기준'; basisValue = '지지옥션 ' + asof;
    } else if (extSido) {
      tier = 'stat_sido'; source = '시도 통계(' + targetSido + ')'; center = extSido.rate; asof = extSido.asof; isStat = true;
      basisLabel = '기준'; basisValue = '지지옥션 ' + asof;
    } else if (extNat) {
      tier = 'stat_national'; source = '전국 통계'; center = extNat.rate; asof = extNat.asof; isStat = true;
      basisLabel = '기준'; basisValue = '지지옥션 ' + asof;
    } else {
      tier = 'default'; source = '전국 디폴트'; center = CFG.def.mid;
      basisLabel = '표본 수'; basisValue = '-';
    }

    var sc;
    if (tier === 'default') {
      sc = { con: CFG.def.con, mid: CFG.def.mid, agg: CFG.def.agg };
    } else {
      var cl = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
      sc = { con: cl(center - CFG.spread), mid: cl(center), agg: cl(center + CFG.spread) };
    }
    return {
      tier: tier, source: source, center: sc.mid, scenarios: sc, isStat: isStat, asof: asof,
      basisLabel: basisLabel, basisValue: basisValue, sampleN: sampleN,
      sameComplexN: same.length, sigunguN: sg.length, targetSigungu: targetSg, targetSido: targetSido
    };
  }
  window.resolveBidRateCascade = resolveBidRateCascade;

  function won(m) { return (typeof fmt !== 'undefined' && fmt.won) ? fmt.won(m) : (m + '만'); }

  var TIER = {
    same_complex: ['#0f6e5c', '1단계 · 동일단지'],
    sigungu: ['#1e2a44', '2단계 · 시군구 사례'],
    stat_sigungu: ['#1e3a5f', '3단계 · 시군구 통계'],
    stat_sido: ['#2A4FBE', '4단계 · 시도 통계'],
    stat_national: ['#5a6b8c', '5단계 · 전국 통계'],
    default: ['#a8884a', '디폴트']
  };

  function cardHTML(pid) {
    var p = ((state && state.properties) || {})[pid]; if (!p) return '';
    var auctions = ((state && state.auctions) || {})[pid] || [];
    var ap = (typeof getActiveAppraisal === 'function') ? getActiveAppraisal(p, auctions) : { value: null, source: '' };
    if (!ap.value) return '';

    var cas = resolveBidRateCascade(pid);
    var bid = Math.round(ap.value * cas.center / 100);
    var badge = TIER[cas.tier];
    var region = cas.targetSigungu || cas.targetSido || '';

    var label, note;
    if (cas.isStat) {
      label = '기준: ' + cas.source + ' · 지역 평균 ' + cas.scenarios.mid + '% (지지옥션 ' + cas.asof + ')';
      note = '<div class="text-small text-muted">📊 소재지 <strong>' + region + '</strong> 지역 평균을 자동 적용했습니다. 실제 낙찰은 단지·물건별 편차가 큽니다. 이 물건에 낙찰사례를 3건 이상 추가하면 더 정확한 1단계로 자동 전환됩니다.</div>';
    } else if (cas.tier === 'default') {
      label = '기준: 전국 디폴트 (지역 통계·사례 모두 없음)';
      note = '<div class="text-small" style="color:var(--warn);">⚠️ 소재지에서 시도/시군구를 못 읽었습니다. 주소(지번/도로명)를 확인하거나 지역 통계를 채우세요.</div>';
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
      + '<div class="info-row"><div class="info-label">중심 낙찰가율</div><div class="info-value mono text-accent">' + cas.center + '%</div></div>'
      + '<div class="info-row"><div class="info-label">' + cas.basisLabel + '</div><div class="info-value mono">' + cas.basisValue + '</div></div>'
      + '<div class="info-row"><div class="info-label">본건 감정가</div><div class="info-value mono">' + won(ap.value) + ' <span class="text-muted text-small">(' + (ap.source || '') + ')</span></div></div>'
      + '<div class="info-row"><div class="info-label">예상 낙찰가</div><div class="info-value mono text-accent">' + won(bid) + '</div></div>'
      + '</div>'
      + '<div style="display:flex;flex-direction:column;justify-content:center;gap:10px;">'
      + '<button class="btn btn-primary no-print" onclick="applyCaseRatesToScenario(' + cas.center + ')">권리분석 낙찰가율을 이 기준(' + cas.center + '%)으로 보정</button>'
      + '<div class="text-small text-muted">' + label + '<br>중립 ' + cas.scenarios.mid + '% · 보수 ' + cas.scenarios.con + '% · 적극 ' + cas.scenarios.agg + '% 로 반영</div>'
      + note
      + '</div>'
      + '</div>'
      + '</div>';
  }

  function inject() {
    try {
      if (!state || state.currentView !== 'auction') return;
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
      if (grid && grid.parentNode) { grid.parentNode.insertBefore(node, grid.nextSibling); }
      else { vc.insertBefore(node, vc.children[1] || null); }
    } catch (e) { console.warn('[낙찰가율 캐스케이드] 주입 건너뜀:', e); }
  }

  function hook() {
    if (typeof window.renderView !== 'function') { setTimeout(hook, 200); return; }
    if (window.__cascadeHooked) return;
    window.__cascadeHooked = true;
    var orig = window.renderView;
    window.renderView = function () { var r = orig.apply(this, arguments); inject(); return r; };
    inject();
    console.log('[낙찰가율 캐스케이드] v2 활성화됨 · 지역 통계 기준월 ' + REGION_RATES._asof);
  }

  if (document.readyState !== 'loading') setTimeout(hook, 300);
  else document.addEventListener('DOMContentLoaded', function () { setTimeout(hook, 300); });
})();
