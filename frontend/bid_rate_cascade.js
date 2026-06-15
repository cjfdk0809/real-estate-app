/* =========================================================================
 *  낙찰가율 캐스케이드 연결 모듈  (bid_rate_cascade.js)
 *  -----------------------------------------------------------------------
 *  index.html 본문은 한 줄도 수정하지 않습니다.
 *  맨 아래 </body> 바로 위에 아래 한 줄만 추가하면 작동합니다.
 *
 *      <script src="/bid_rate_cascade.js"></script>
 *
 *  동작: 경공매(04) 화면의 '본건 적용' 카드를 캐스케이드 방식으로 교체.
 *    1단계 동일단지 = 현재 물건에 등록된 낙찰사례 (N≥3)        ← 중앙값
 *    2단계 시군구    = 전체 물건의 낙찰사례 중 같은 시군구 (N≥5) ← 중앙값
 *    3단계 디폴트    = 85 / 90 / 95
 *  채택 단계·표본수·중심값을 카드에 표시하고, [보정] 버튼은 기존
 *  applyCaseRatesToScenario()를 그대로 호출해 권리분석 시나리오에 반영합니다.
 *
 *  ※ 임계치(아래 CFG)는 자유롭게 조정하세요.
 *  ※ 평균이 아닌 '중앙값'을 써서 특이 낙찰 1건에 흔들리지 않게 했습니다.
 * ========================================================================= */
(function () {
  'use strict';

  // ---- 조정 가능한 파라미터 ----
  var CFG = {
    minSameComplex: 3,   // 1단계(동일단지) 최소 표본
    minSigungu: 5,       // 2단계(시군구) 최소 표본
    spread: 5,           // 보수/적극 = 중심값 ∓ 5%p
    minRate: 30,         // 이상치 하한컷(%)
    maxRate: 130,        // 이상치 상한컷(%)
    def: { con: 85, mid: 90, agg: 95 }  // 3단계 디폴트
  };

  function parseSigungu(addr) {
    if (!addr) return '';
    var toks = String(addr).match(/[가-힣]+(?:특별시|광역시|특별자치시|특별자치도|시|군|구)/g) || [];
    var gu = toks.filter(function (t) { return t.slice(-1) === '구'; }).pop();
    if (gu) return gu;                                   // 자치구 우선
    var si = toks.filter(function (t) { var c = t.slice(-1); return c === '시' || c === '군'; }).pop();
    return si || '';
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

  // ---- 캐스케이드 핵심 (state는 index.html의 전역 상태) ----
  function resolveBidRateCascade(pid) {
    var props = (state && state.properties) || {};
    var aucs = (state && state.auctions) || {};
    var p = props[pid] || {};
    var targetSg = parseSigungu(p.addrLot || p.addrRoad || '');

    // 1단계: 현재 물건에 등록된 낙찰 사례
    var same = (aucs[pid] || []).map(rateOf).filter(ok);

    // 2단계: 전체 물건의 낙찰 사례 중 같은 시군구
    var sg = [];
    Object.keys(aucs).forEach(function (k) {
      (aucs[k] || []).forEach(function (x) {
        var r = rateOf(x); if (!ok(r)) return;
        var s = parseSigungu(x.address || '') || (k === pid ? targetSg : '');
        if (targetSg && s === targetSg) sg.push(r);
      });
    });

    var tier, source, n, center;
    if (same.length >= CFG.minSameComplex) {
      tier = 'same_complex'; source = '본건 등록 낙찰사례'; n = same.length; center = median(same);
    } else if (sg.length >= CFG.minSigungu) {
      tier = 'sigungu'; source = '시군구 사례(' + targetSg + ')'; n = sg.length; center = median(sg);
    } else {
      tier = 'default'; source = '전국 디폴트'; n = same.length; center = CFG.def.mid;
    }

    var sc;
    if (tier === 'default') {
      sc = { con: CFG.def.con, mid: CFG.def.mid, agg: CFG.def.agg };
    } else {
      var cl = function (v) { return round1(Math.max(CFG.minRate, Math.min(CFG.maxRate, v))); };
      sc = { con: cl(center - CFG.spread), mid: cl(center), agg: cl(center + CFG.spread) };
    }
    return {
      tier: tier, source: source, sampleN: (tier === 'default' ? null : n),
      center: sc.mid, scenarios: sc,
      sameComplexN: same.length, sigunguN: sg.length, targetSigungu: targetSg
    };
  }
  window.resolveBidRateCascade = resolveBidRateCascade;  // 콘솔 디버깅용 노출

  function won(m) { return (typeof fmt !== 'undefined' && fmt.won) ? fmt.won(m) : (m + '만'); }

  // ---- 카드 HTML (기존 클래스 그대로 사용해 디자인 일치) ----
  function cardHTML(pid) {
    var p = ((state && state.properties) || {})[pid]; if (!p) return '';
    var auctions = ((state && state.auctions) || {})[pid] || [];
    var ap = (typeof getActiveAppraisal === 'function') ? getActiveAppraisal(p, auctions) : { value: null, source: '' };
    if (!ap.value) return '';   // 감정가 없으면 카드 미표시 (기존 동작과 동일)

    var cas = resolveBidRateCascade(pid);
    var bid = Math.round(ap.value * cas.center / 100);
    var badge = {
      same_complex: ['#0f6e5c', '1단계 · 동일단지'],
      sigungu: ['#1e2a44', '2단계 · 시군구'],
      default: ['#a8884a', '3단계 · 디폴트']
    }[cas.tier];
    var label = (cas.tier === 'default')
      ? '기준: 전국 디폴트 (동일단지 ' + cas.sameComplexN + '건 — 임계 ' + CFG.minSameComplex + '건 미달)'
      : '기준: ' + cas.source + ' · 중앙값 ' + cas.scenarios.mid + '% (N=' + cas.sampleN + ')';

    return ''
      + '<div class="card mb-24" data-cascade="1" style="border-left:4px solid var(--accent);">'
      + '<div class="card-title">본건 적용 · 낙찰가율 캐스케이드 '
      + '<span class="badge" style="background:' + badge[0] + ';color:#fff;">' + badge[1] + '</span></div>'
      + '<div class="grid grid-2">'
      + '<div>'
      + '<div class="info-row"><div class="info-label">채택 기준</div><div class="info-value">' + cas.source + '</div></div>'
      + '<div class="info-row"><div class="info-label">중심값(중앙값)</div><div class="info-value mono text-accent">' + cas.center + '%</div></div>'
      + '<div class="info-row"><div class="info-label">표본 수</div><div class="info-value mono">' + (cas.sampleN != null ? cas.sampleN + '건' : '-') + '</div></div>'
      + '<div class="info-row"><div class="info-label">본건 감정가</div><div class="info-value mono">' + won(ap.value) + ' <span class="text-muted text-small">(' + (ap.source || '') + ')</span></div></div>'
      + '<div class="info-row"><div class="info-label">예상 낙찰가</div><div class="info-value mono text-accent">' + won(bid) + '</div></div>'
      + '</div>'
      + '<div style="display:flex;flex-direction:column;justify-content:center;gap:10px;">'
      + '<button class="btn btn-primary no-print" onclick="applyCaseRatesToScenario(' + cas.center + ')">권리분석 낙찰가율을 이 기준(' + cas.center + '%)으로 보정</button>'
      + '<div class="text-small text-muted">' + label + '<br>중립 ' + cas.scenarios.mid + '% · 보수 ' + cas.scenarios.con + '% · 적극 ' + cas.scenarios.agg + '% 로 반영</div>'
      + (cas.tier === 'default'
        ? '<div class="text-small" style="color:var(--warn);">⚠️ 등록 사례 부족 — 동일단지 ' + cas.sameComplexN + '건 / 시군구 ' + cas.sigunguN + '건. 사례를 더 추가하면 자동으로 상위 단계로 전환됩니다.</div>'
        : '')
      + '</div>'
      + '</div>'
      + '</div>';
  }

  // ---- 경공매 화면에 카드 주입(교체) ----
  function inject() {
    try {
      if (!state || state.currentView !== 'auction') return;
      var vc = document.getElementById('viewContainer'); if (!vc) return;
      var pid = state.currentPropertyId; if (!pid) return;

      // 중복 방지: 이전 주입분 제거
      Array.prototype.forEach.call(vc.querySelectorAll('[data-cascade="1"]'), function (n) { n.remove(); });

      var html = cardHTML(pid); if (!html) return;
      var tmp = document.createElement('div'); tmp.innerHTML = html;
      var node = tmp.firstElementChild;

      // 기존 '본건 적용 · 사례 기반' 카드가 있으면 그 자리에 교체
      var orig = null;
      Array.prototype.forEach.call(vc.querySelectorAll('.card'), function (c) {
        if (!orig && /본건 적용 · 사례 기반/.test(c.textContent)) orig = c;
      });
      if (orig) { orig.replaceWith(node); return; }

      // 없으면(디폴트 단계 등) 상단 통계 그리드 바로 뒤에 삽입
      var grid = vc.querySelector('.grid.grid-4');
      if (grid && grid.parentNode) { grid.parentNode.insertBefore(node, grid.nextSibling); }
      else { vc.insertBefore(node, vc.children[1] || null); }
    } catch (e) {
      console.warn('[낙찰가율 캐스케이드] 주입 건너뜀:', e);
    }
  }

  // ---- renderView를 감싸 매 렌더 후 주입 (원본 로직은 그대로 실행) ----
  function hook() {
    if (typeof window.renderView !== 'function') { setTimeout(hook, 200); return; }
    if (window.__cascadeHooked) return;
    window.__cascadeHooked = true;
    var orig = window.renderView;
    window.renderView = function () { var r = orig.apply(this, arguments); inject(); return r; };
    inject();  // 현재 화면 즉시 반영
    console.log('[낙찰가율 캐스케이드] 활성화됨');
  }

  if (document.readyState !== 'loading') setTimeout(hook, 300);
  else document.addEventListener('DOMContentLoaded', function () { setTimeout(hook, 300); });
})();
